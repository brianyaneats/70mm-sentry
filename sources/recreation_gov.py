"""recreation.gov campsite-availability adapter.

Two distinct upstreams are involved here, and they are NOT the same tier of
sanctioned-ness — this module docstring says so plainly so nobody assumes
otherwise from the `risk_tier` alone:

  * RIDB (ridb.recreation.gov) is the documented, sanctioned API for
    facility/campsite METADATA (names, amenities, geo). It is a real API
    with a real key and real docs. This adapter does not call it.

  * The availability endpoint this adapter actually calls —
    recreation.gov/api/camps/availability/campground/{id}/month
    — is public US-government open data (no auth, no key, served directly
    by the recreation.gov booking frontend). It is NOT formally documented
    or contractually offered as an API; it is the same endpoint the
    recreation.gov website's own calendar widget calls. It has been widely
    used by third-party camping tools for years without enforcement action,
    which is why it is tiered "green" here rather than "yellow" — but that
    is an operational judgment call, not a legal guarantee. The owner runs
    a live ToS/preflight check against recreation.gov's current Terms of
    Use before this adapter is ever pointed at production traffic; nothing
    about this file constitutes that check.

State mapping (recreation.gov raw availability string -> normalized STATE):
    "Available"        -> STATE_AVAILABLE
    "Reserved"          -> STATE_SOLDOUT
    "Not Available"     -> STATE_RESTRICTED  (raw value kept in meta["raw_state"])
    "Not Reservable"    -> STATE_RESTRICTED  (raw value kept in meta["raw_state"])
    "Closed"            -> STATE_RESTRICTED  (raw value kept in meta["raw_state"])
    "NYR"               -> STATE_RESTRICTED  (raw value kept in meta["raw_state"], "Not Yet Reservable")
    (anything else)     -> STATE_RESTRICTED  (unknown raw value kept in meta["raw_state"])

A site with no entry at all for the requested date in its availabilities
dict yields no observation for that site (not an error — recreation.gov
simply omits dates outside a site's active calendar window sometimes).

An unrecognized top-level payload shape (missing "campsites" key, or a
"campsites" value that isn't a dict) raises ValueError rather than
returning an empty list, per the adapter contract in sources/base.py.
"""

from urllib.parse import quote

from sources.base import (
    STATE_AVAILABLE,
    STATE_RESTRICTED,
    STATE_SOLDOUT,
    SourceAdapter,
    observation,
)

_STATE_MAP = {
    "Available": STATE_AVAILABLE,
    "Reserved": STATE_SOLDOUT,
    "Not Available": STATE_RESTRICTED,
    "Not Reservable": STATE_RESTRICTED,
    "Closed": STATE_RESTRICTED,
    "NYR": STATE_RESTRICTED,
}

_MONTH_URL_TEMPLATE = (
    "https://www.recreation.gov/api/camps/availability/campground/{campground_id}/month"
    "?start_date={start_date}"
)

_SITE_URL_TEMPLATE = "https://www.recreation.gov/camping/campsites/{site_id}"

_ACCEPT_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
}


def _month_start_iso(date):
    """"YYYY-MM-DD" -> URL-encoded "YYYY-MM-01T00:00:00.000Z"."""
    year, month, _day = date.split("-")
    raw = "%s-%s-01T00:00:00.000Z" % (year, month)
    return quote(raw, safe="")


def _day_key_for(date):
    """"YYYY-MM-DD" -> the exact key recreation.gov uses inside a site's
    `availabilities` dict for that day: "YYYY-MM-DDT00:00:00Z"."""
    return "%sT00:00:00Z" % date


class RecreationGovAdapter(SourceAdapter):
    name = "recreation_gov"
    risk_tier = "green"

    # Conservative until measured against real traffic — recreation.gov's
    # availability endpoint has no published rate-limit contract.
    min_spacing_seconds = 2.0
    daily_request_budget = 5000

    def fetch(self, target, date, http_get):
        campground_id = target["campground_id"]
        start_date = _month_start_iso(date)
        url = _MONTH_URL_TEMPLATE.format(
            campground_id=campground_id, start_date=start_date
        )
        return http_get(url, _ACCEPT_HEADERS)

    def fetch_key(self, target, date):
        """One request returns the whole month, so dates in the same
        month share a fetch (see engine.poller)."""
        return date[:7]

    def parse(self, raw, target, date):
        if not isinstance(raw, dict) or "campsites" not in raw:
            raise ValueError(
                "recreation_gov: unrecognized payload shape (no 'campsites' key): %r"
                % (raw if not isinstance(raw, dict) else sorted(raw.keys()),)
            )

        campsites = raw["campsites"]
        if not isinstance(campsites, dict):
            raise ValueError(
                "recreation_gov: 'campsites' is not a mapping: %r" % (type(campsites),)
            )

        campground_id = target["campground_id"]
        day_key = _day_key_for(date)
        observations = []

        for site_id, site in campsites.items():
            if not isinstance(site, dict):
                raise ValueError(
                    "recreation_gov: campsite %r entry is not a mapping: %r"
                    % (site_id, type(site))
                )

            availabilities = site.get("availabilities")
            if not isinstance(availabilities, dict):
                raise ValueError(
                    "recreation_gov: campsite %r has no usable 'availabilities' map"
                    % (site_id,)
                )

            if day_key not in availabilities:
                # Date simply isn't in this site's active calendar window —
                # not an error, just nothing to report for this site/date.
                continue

            raw_state = availabilities[day_key]
            state = _STATE_MAP.get(raw_state, STATE_RESTRICTED)

            site_name = site.get("site", str(site_id))
            campsite_type = site.get("campsite_type", "")
            label = "%s (%s)" % (site_name, campsite_type) if campsite_type else site_name

            unit_id = "%s:%s:%s" % (campground_id, site_id, date)

            meta = {"raw_state": raw_state, "campsite_type": campsite_type,
                    "url": _SITE_URL_TEMPLATE.format(site_id=site_id)}

            observations.append(observation(unit_id, date, state, label=label, meta=meta))

        return observations

    def health_check(self, raw):
        if not isinstance(raw, dict):
            return False, "payload is not a dict (%r)" % (type(raw),)

        if "campsites" not in raw:
            return False, "payload missing 'campsites' key"

        campsites = raw["campsites"]
        if not isinstance(campsites, dict):
            return False, "'campsites' is not a mapping (%r)" % (type(campsites),)

        if not campsites:
            # An empty campsites map isn't necessarily broken (a campground
            # with zero sites returned for the month is plausible upstream
            # behavior), but it can't be verified as healthy either.
            return False, "'campsites' map is empty"

        for site_id, site in campsites.items():
            if not isinstance(site, dict):
                return False, "campsite %r entry is not a mapping" % (site_id,)
            availabilities = site.get("availabilities")
            if not isinstance(availabilities, dict):
                return False, "campsite %r has no 'availabilities' mapping" % (site_id,)
            if not availabilities:
                return False, "campsite %r has an empty 'availabilities' mapping" % (site_id,)

        return True, "ok"

    def cadence_hints(self):
        return {
            "cancellations_cluster_before": ["weekends", "holidays"],
            "note": (
                "Refund/cancellation churn on recreation.gov campsites "
                "clusters in the days immediately before weekends and "
                "federal holidays, mirroring the same last-minute-drop "
                "pattern seen on ticketed venues."
            ),
        }
