"""Fandango NAPI adapter — the one upstream sentry.py has ever polled.

Owns exactly what the SourceAdapter contract (sources/base.py) says an
adapter owns: URL construction (theaterMovieShowtimes NAPI), the venue
Referer header Fandango requires, payload parsing into normalized
observation() dicts, 70mm/title matching, and the blind-payload health
heuristic. Everything else — pacing, budget, breaker, dedup ledgers,
alert copy, delivery — stays in sentry.py (the kernel) untouched.

target dict shape (opaque to the kernel, built by sentry.py from
CFG["venues"][venue] and passed in): {"venue": <id>, "referer": <url>,
optionally "movie_match"/"movie_exclude"/"format_match" for parse()}.

State mapping (Fandango showtime.type -> contract state):
    available    -> STATE_AVAILABLE
    soldout      -> STATE_SOLDOUT
    restricted   -> STATE_RESTRICTED
    pastshowtime, or expired=True -> DROPPED, not emitted as an
        observation at all. This preserves sentry.py's pre-existing
        odyssey_70mm() behavior exactly: it has always excluded past
        showtimes from its result rather than reporting them as "gone",
        and a returns-observations rewrite that started reporting them as
        STATE_GONE would be a real (if small) behavior change for every
        caller downstream of odyssey_70mm.
    anything else (a Fandango type this adapter has never seen) ->
        STATE_GONE, conservatively, rather than silently dropped — an
        unrecognized type is exactly the upstream-shape-drift case the
        kernel's [PARSE]/[BLIND] alarms exist to catch, and a silent drop
        here would blind them the same way a silent [] return would.

stdlib only. Does not import sentry — no circular import — and never
reads config.json; venue/movie-match config always arrives via `target`
at call time.
"""

import json

from .base import (SourceAdapter, observation, STATE_AVAILABLE,
                    STATE_SOLDOUT, STATE_RESTRICTED, STATE_GONE)

NAPI = "https://www.fandango.com/napi/theaterMovieShowtimes/{venue}?date={date}"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

STATE_MAP = {
    "available": STATE_AVAILABLE,
    "soldout": STATE_SOLDOUT,
    "restricted": STATE_RESTRICTED,
}


class FandangoFeedShapeError(ValueError):
    """Raised when a payload is neither a recognized viewModel envelope
    nor the benign 'no listings for this expired date' shape. Kept as a
    distinct type (rather than a bare ValueError) so the kernel can tag
    it [PARSE] in the log without resorting to string-sniffing."""


def build_url(target, date):
    return NAPI.format(venue=target["venue"], date=date)


def build_headers(target):
    return {"User-Agent": UA,
            "Referer": target["referer"],
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9"}


def extract_viewmodel(payload):
    """Pull the viewModel out of a decoded NAPI payload.

    A date whose showtimes have all passed answers {"date": null} with no
    "viewModel" key at all — that is a valid "nothing listed", not a
    failure, and sentry.py has always treated ANY payload missing
    "viewModel" the same way (regardless of whether "date" is present),
    so this preserves that exactly rather than tightening the check.
    Only a payload that is not even a JSON object is treated as a real
    shape failure.
    """
    if not isinstance(payload, dict):
        raise FandangoFeedShapeError(
            f"fandango: payload is not an object ({type(payload).__name__})")
    if "viewModel" not in payload:
        return None
    return payload["viewModel"]


def match_showtimes(vm, movie_match, movie_exclude, format_match):
    """Core 70mm/title matching, in sentry.py's legacy hit-dict shape:
    {"time", "type", "formats", "url"}. Kept separate from parse() so
    sentry.py's own odyssey_70mm() can delegate to exactly this function
    and stay byte-for-byte identical to its pre-extraction behavior."""
    hits = []
    movie_match = (movie_match or "").lower()
    for m in (vm or {}).get("movies", []) or []:
        title = (m.get("title") or "").lower()
        if movie_match not in title:
            continue
        # "odyssey" alone would also match a "2001: A Space Odyssey" 70mm
        # revival — excluded titles guard against that false match
        if any(x in title for x in movie_exclude or []):
            continue
        for v in m.get("variants", []):
            for g in v.get("amenityGroups", []):
                for s in g.get("showtimes", []):
                    fmts = [f.get("filterName", "")
                            for f in s.get("filmFormat", [])]
                    if not any(format_match in x.upper() for x in fmts):
                        continue
                    if s.get("type") == "pastshowtime" or s.get("expired"):
                        continue
                    hits.append({"time": s.get("date"),
                                 "type": s.get("type"), "formats": fmts,
                                 "url": s.get("ticketingJumpPageURL")})
    return hits


class FandangoAdapter(SourceAdapter):
    name = "fandango"
    risk_tier = "yellow"          # public endpoint, tolerated-gray
    min_spacing_seconds = 1.0     # Fandango's own partner API: 1 call/sec
    daily_request_budget = 25000  # matches config.example.json default

    def fetch(self, target, date, http_get):
        url = build_url(target, date)
        headers = build_headers(target)
        return http_get(url, headers)

    def parse(self, raw, target, date):
        payload = json.loads(raw) if isinstance(raw, (bytes, str)) else raw
        vm = extract_viewmodel(payload)
        if vm is None:
            return []  # no listings for this (expired) date
        if "movies" not in vm:
            raise FandangoFeedShapeError(
                "fandango: viewModel missing 'movies' — feed shape may "
                "have changed")
        venue = target.get("venue", "")
        movie_match = target.get("movie_match", "")
        movie_exclude = target.get("movie_exclude", [])
        format_match = target.get("format_match", "")
        obs = []
        for h in match_showtimes(vm, movie_match, movie_exclude,
                                  format_match):
            state = STATE_MAP.get(h["type"], STATE_GONE)
            unit_id = f"{venue}|{date}|{h['time']}"
            obs.append(observation(unit_id, date, state,
                                    label=h["time"] or "",
                                    meta={"formats": h["formats"],
                                          "url": h["url"],
                                          "raw_type": h["type"]}))
        return obs

    def health_check(self, raw):
        """(ok, reason) — mirrors the blind-payload heuristic sentry.py's
        fetch_day has always used: a theater page listing zero movies is
        not a normal response (endpoint changed, soft-failing, or
        politely rejecting us), regardless of HTTP status or byte count."""
        try:
            payload = (json.loads(raw) if isinstance(raw, (bytes, str))
                       else raw)
        except Exception as e:
            return False, f"payload not valid JSON: {e}"
        try:
            vm = extract_viewmodel(payload)
        except FandangoFeedShapeError as e:
            return False, str(e)
        if vm is None:
            return True, "no listings for this date (expired) — not blind"
        nbytes = len(raw) if isinstance(raw, (bytes, str)) else None
        movies = vm.get("movies") or []
        if not movies:
            size = f", {nbytes}B" if nbytes is not None else ""
            return False, f"0 movies listed{size} — blind or shape changed"
        return True, f"{len(movies)} movies listed"
