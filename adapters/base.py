"""Source adapter contract — the seam the platform generalization hinges on.

An adapter owns exactly one upstream's mechanics: how to fetch, how to
parse, and how to tell a healthy payload from a blind one. It owns nothing
else. Pacing, budgets, breakers, cadence, detection state, dedup ledgers,
and delivery are kernel services — an adapter must never sleep, retry,
or open its own side-channel connections.

Detection (new-listing / status-flip / horizon-extend) is kernel-owned and
operates on the normalized Observation stream, so the interesting bugs
(placeholder resolution, first-sale-vs-restock) get fixed once, not once
per source.

Failure logging is part of the contract: every fetch/parse failure path
must raise (never swallow) so the kernel can log it with the adapter name,
errno, and exception class. BrokenPipeError / ConnectionResetError /
socket.timeout on a kept-alive connection are expected events, not
anomalies — the kernel logs them distinctly (see kernel log tags below)
and they must stay visible in the log stream.

Kernel log tags (grep-able, stable):
    [PIPE]    broken pipe / reset / dead kept-alive socket on fetch
    [BLIND]   HTTP OK but payload empty or shape-mismatched
    [REFUSED] 403/429/5xx from upstream
    [PARSE]   payload fetched but parse() raised

stdlib only. Nothing in this package may import third-party code.
"""

# ---------- normalized observation ----------

# The full vocabulary of unit states, across every source. A source that
# cannot distinguish some of these maps what it has onto the nearest one
# and documents the mapping in its module docstring.
STATE_AVAILABLE = "available"        # purchasable / bookable right now
STATE_SOLDOUT = "soldout"            # listed, not purchasable
STATE_RESTRICTED = "restricted"      # listed, gated (site-specific meaning)
STATE_GONE = "gone"                  # was listed, no longer appears
STATES = (STATE_AVAILABLE, STATE_SOLDOUT, STATE_RESTRICTED, STATE_GONE)


def observation(unit_id, date, state, label="", meta=None):
    """One normalized sighting of one inventory unit at one moment.

    unit_id: stable identity for dedup across polls — must not change
             between observations of the same unit (e.g. showtimeHashCode,
             campsite_id + date). The kernel's ledgers key on it.
    date:    "YYYY-MM-DD" the unit belongs to (the subscribable axis).
    state:   one of STATES.
    label:   human-facing line for alert copy ("7:00 PM", "Site A12").
    meta:    source-specific extras the kernel passes through to copy
             templates untouched.
    """
    if state not in STATES:
        raise ValueError("unknown state %r for unit %r" % (state, unit_id))
    return {
        "unit_id": str(unit_id),
        "date": date,
        "state": state,
        "label": label,
        "meta": meta or {},
    }


# ---------- the adapter contract ----------

class SourceAdapter:
    """Subclass per upstream. Implement the four methods; nothing else.

    Politeness attributes are the adapter's declaration of what its
    upstream tolerates — the kernel enforces them at its choke point;
    the adapter itself must never sleep or pace.
    """

    # Stable registry key, e.g. "fandango", "recreation_gov".
    name = None

    # Politeness declaration, enforced by the kernel:
    min_spacing_seconds = 1.0      # floor between any two requests
    daily_request_budget = 25000   # hard daily ceiling for this source

    # Legal/risk tier, enforced by the kernel (a YELLOW source is never
    # sold on a prepaid plan; a RED adapter refuses to load at all):
    # "green"  — sanctioned API, contractually authorized
    # "yellow" — public endpoint, tolerated-gray; personal/free scale only
    # "red"    — requires circumvention; must not be built
    risk_tier = None

    def fetch(self, target, date, http_get):
        """Fetch one (target, date)'s raw payload.

        target:   adapter-specific dict from the DropSpec (venue id,
                  campground id, ...). Opaque to the kernel.
        http_get: kernel-provided callable http_get(url, headers) -> bytes.
                  The ONLY way an adapter may touch the network — it runs
                  inside the kernel's pacer, budget, breaker, and failure
                  logging. Raises on any transport failure; do not catch.
        Returns the raw payload (bytes/str/parsed json — parse()'s input).
        """
        raise NotImplementedError

    def parse(self, raw, target, date):
        """Turn a raw payload into a list of observation() dicts.

        Must raise on a shape it does not recognize — a silent [] return
        on a changed feed shape is exactly the mis-parse failure mode the
        kernel's [PARSE]/[BLIND] alarms exist to catch, and swallowing it
        here would blind the alarm.
        """
        raise NotImplementedError

    def health_check(self, raw):
        """(ok: bool, reason: str) — is this payload plausibly healthy?

        Called by the kernel on every fetch for blind-streak accounting,
        and by the daily reconcile for the mis-parse alarm. "ok" means
        'this looks like the feed I know', not 'it has hits'.
        """
        raise NotImplementedError

    def cadence_hints(self):
        """Optional dict of source-specific cadence knowledge, e.g.
        {"burst_windows": [...], "overnight_ok": True}. The kernel treats
        these as hints, never as permission to exceed the politeness
        declaration above."""
        return {}
