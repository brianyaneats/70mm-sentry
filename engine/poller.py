"""engine.poller -- the detection loop: adapters in, record_event out.

One watch = one drop's poll work, built from live subscriptions
(core.active_watches) or a static catalog (serve.load_watches):

    {"drop_id", "adapter", "target": {...}, "dates": ["YYYY-MM-DD", ...],
     "interval_seconds": 60}

poll_watch() runs one pass over a watch's dates. Every network touch
goes through governor.acquire() first, so pacing/budget/breaker hold no
matter how many watches share a source.

Detection is a transition rule against the ledger, not "is it available
now": an alert fires when a unit becomes available that was not
available on the previous poll. The first pass over each (drop, date)
only seeds the ledger -- otherwise every site already open at boot, or
on a date newly rolling into the window, would page every subscriber.

Adapters may define fetch_key(target, date); dates sharing a key share
one fetch per pass (recreation.gov returns a whole month per request).
"""

import random
import time

from sources.base import STATE_AVAILABLE
from engine import core, governor, log as _log

DEFAULT_INTERVAL_SECONDS = 60
JITTER_FRACTION = 0.1


class UpstreamRefused(Exception):
    """The transport got a 403/429/5xx. Raised by the production http_get
    so the poller can tag it [REFUSED] instead of [PIPE]."""

    def __init__(self, status):
        super().__init__("upstream refused: HTTP %s" % status)
        self.status = status


def _ledger_states(conn, drop_id):
    rows = conn.execute(
        "SELECT unit_id, last_state FROM ledger WHERE drop_id = ?",
        (drop_id,)).fetchall()
    return {r["unit_id"]: r["last_state"] for r in rows}


def _seed_ledger(conn, drop_id, obs, now):
    conn.execute(
        "INSERT INTO ledger (drop_id, unit_id, last_state, ever_available, "
        "updated_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(drop_id, unit_id) DO UPDATE SET "
        "  last_state = excluded.last_state, "
        "  ever_available = MAX(ledger.ever_available, excluded.ever_available), "
        "  updated_at = excluded.updated_at",
        (drop_id, obs["unit_id"], obs["state"],
         1 if obs["state"] == STATE_AVAILABLE else 0, now))


def detect(conn, drop_id, date, observations, now):
    """Apply the transition rule; returns the number of events recorded."""
    known = _ledger_states(conn, drop_id)
    baseline = conn.execute(
        "SELECT 1 FROM poll_baseline WHERE drop_id = ? AND date = ?",
        (drop_id, date)).fetchone() is not None
    fired = 0
    for obs in observations:
        prev = known.get(obs["unit_id"])
        opened = (obs["state"] == STATE_AVAILABLE and prev != STATE_AVAILABLE
                  and (prev is not None or baseline))
        if not opened:
            if prev != obs["state"]:
                _seed_ledger(conn, drop_id, obs, now)
            continue
        core.record_event(conn, drop_id, {
            "event_id": "%s@%d" % (obs["unit_id"], int(now)),
            "kind": "opened" if prev is not None else "new_listing",
            "unit_id": obs["unit_id"],
            "state": obs["state"],
            "payload": {"label": obs["label"], "date": obs["date"],
                        "url": obs["meta"].get("url")},
        }, now=now)
        fired += 1
    if not baseline:
        conn.execute("INSERT INTO poll_baseline (drop_id, date) VALUES (?, ?)",
                     (drop_id, date))
    return fired


def _acquire(conn, adapter, clock, sleep, stop=None):
    """Wait out spacing denials (seconds, by design); give up on budget
    or breaker denials, which last hours, and on shutdown."""
    while True:
        if stop is not None and stop.is_set():
            return False, "stopped"
        ok, reason, wait = governor.acquire(
            conn, adapter.name, clock(), adapter.min_spacing_seconds,
            adapter.daily_request_budget)
        if ok or reason not in ("spacing", "contention"):
            return ok, reason
        sleep(wait)


def poll_watch(conn, watch, adapter, http_get, clock=time.time,
               sleep=time.sleep, stop=None):
    """One pass over a watch. Returns {"fetched", "events", "denied"}.
    A budget/breaker denial ends the pass; the next one retries. A set
    `stop` event ends it before the next fetch (denied="stopped")."""
    drop_id, target = watch["drop_id"], watch["target"]
    key_of = getattr(adapter, "fetch_key", lambda t, d: d)
    raw_by_key = {}
    stats = {"fetched": 0, "events": 0, "denied": None}

    for date in watch["dates"]:
        key = key_of(target, date)
        if key not in raw_by_key:
            ok, reason = _acquire(conn, adapter, clock, sleep, stop)
            now = clock()
            if not ok:
                stats["denied"] = reason
                break
            started = time.monotonic()
            try:
                raw = adapter.fetch(target, date, http_get)
            except UpstreamRefused as exc:
                _log.log(_log.TAG_REFUSED, "upstream refused",
                         source=adapter.name, drop_id=drop_id,
                         status=exc.status)
                governor.report_failure(conn, adapter.name, now)
                break
            except OSError as exc:
                _log.log(_log.TAG_PIPE, "fetch failed", source=adapter.name,
                         drop_id=drop_id, exc_class=type(exc).__name__)
                governor.report_failure(conn, adapter.name, now)
                break
            stats["fetched"] += 1
            healthy, why = adapter.health_check(raw)
            _log.log(_log.TAG_POLL, "fetched", source=adapter.name,
                     drop_id=drop_id, key=key, healthy=healthy,
                     ms=round((time.monotonic() - started) * 1000))
            if not healthy:
                _log.log(_log.TAG_BLIND, why, source=adapter.name,
                         drop_id=drop_id)
                governor.report_failure(conn, adapter.name, now)
                raw_by_key[key] = None
                continue
            governor.report_success(conn, adapter.name, now)
            raw_by_key[key] = raw

        raw = raw_by_key[key]
        if raw is None:
            continue
        try:
            observations = adapter.parse(raw, target, date)
        except Exception as exc:  # noqa: BLE001 -- contract: parse raises on drift
            _log.log(_log.TAG_PARSE, "parse failed", source=adapter.name,
                     drop_id=drop_id, exc_class=type(exc).__name__)
            continue
        n = detect(conn, drop_id, date, observations, clock())
        if n:
            _log.log(_log.TAG_POLL, "detected", drop_id=drop_id, date=date,
                     events=n)
        stats["events"] += n
    return stats


def fetches_per_pass(watch, adapter):
    key_of = getattr(adapter, "fetch_key", lambda t, d: d)
    return len({key_of(watch["target"], d) for d in watch["dates"]})


_last_stretch = {}  # source -> last logged stretch, so [POLL] logs changes only


def budget_intervals(watches, adapters, budget_fraction=0.8):
    """{drop_id: seconds between passes} so every source stays inside
    budget_fraction of its daily request budget all day.

    Each watch keeps its own interval_seconds when the source can afford
    it; otherwise every watch on that source slows by the same factor.
    Running out at noon and going blind until midnight is worse than
    polling everyone a little slower -- this is the scaling knob, and
    [POLL] logs it whenever it binds."""
    per_source = {}
    for w in watches:
        a = adapters[w["adapter"]]
        interval = w.get("interval_seconds", DEFAULT_INTERVAL_SECONDS)
        per_source.setdefault(a.name, []).append(
            (w, interval, fetches_per_pass(w, a)))
    out = {}
    for name, rows in per_source.items():
        budget = adapters[name].daily_request_budget * budget_fraction
        demand = sum(86400.0 / interval * n for _, interval, n in rows)
        stretch = max(1.0, demand / budget)
        if round(stretch, 1) != _last_stretch.get(name, 1.0):
            _last_stretch[name] = round(stretch, 1)
            _log.log(_log.TAG_POLL, "budget-bound: intervals stretched",
                     source=name, watches=len(rows),
                     stretch=round(stretch, 2))
        for w, interval, _ in rows:
            out[w["drop_id"]] = interval * stretch
    return out


def run_forever(conn, watch_source, adapters, http_get, stop=None,
                clock=time.time, sleep=time.sleep, idle_seconds=5.0):
    """Poll whichever watch is most overdue, forever.

    watch_source() is re-read every iteration (it is a cheap query), so
    a newly verified subscription starts polling within idle_seconds and
    a cancelled one stops costing requests. A new watch is due at once.
    Jitter keeps watches on one source from synchronizing into bursts.
    `stop` is a threading.Event (or None for forever); pass
    sleep=stop.wait too so spacing waits inside a pass end on shutdown.""" 
    due = {}
    while not (stop and stop.is_set()):
        watches = {w["drop_id"]: w for w in watch_source()}
        for drop_id in set(due) - set(watches):
            del due[drop_id]
        now = clock()
        for drop_id in watches:
            due.setdefault(drop_id, now)
        ready = [d for d in due if due[d] <= now]
        if not ready:
            wait = min([due[d] - now for d in due] + [idle_seconds])
            if stop:
                stop.wait(wait)
            else:
                sleep(wait)
            continue
        drop_id = min(ready, key=due.get)
        watch = watches[drop_id]
        try:
            poll_watch(conn, watch, adapters[watch["adapter"]], http_get,
                       clock=clock, sleep=sleep, stop=stop)
        except Exception as exc:  # noqa: BLE001 -- one bad watch must not
            # stop detection for every other drop
            _log.log(_log.TAG_POLL, "pass crashed", drop_id=drop_id,
                     exc_class=type(exc).__name__)
        interval = budget_intervals(list(watches.values()), adapters)[drop_id]
        due[drop_id] = clock() + interval * (
            1 + random.uniform(-JITTER_FRACTION, JITTER_FRACTION))
