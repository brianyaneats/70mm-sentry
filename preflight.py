#!/usr/bin/env python3
"""Cutover gate for 70mm Sentry — run this ON the box before trusting it.

Answers the two questions that decide whether a VPS deployment is viable,
and exits non-zero if either answer is no:

1. Does Fandango's NAPI answer THIS IP?  Fandango's robots.txt disallows
   /napi/ and the endpoint refuses datacenter addresses far more readily
   than residential ones. Samples several dates inside the known run window
   and requires HTTP 200, a non-empty movie list, and at least one 70mm hit
   somewhere — a refusal (403/429/503) is named explicitly.
2. Does the configured cadence fit the daily request budget?  Budget
   exhaustion makes fetch_day return None, which trips the breaker and
   produces a "Sentry degraded" alert indistinguishable from an IP block.
   Projects a full day's spend from the live config and fails if it exceeds
   daily_request_budget, so that ambiguity can't happen at 2am.

Read-only: no pushes, no state.json writes. Reuses the sentry's own
fetch_day/odyssey_70mm so it exercises the exact request path (UA, Referer,
headers) the watcher will use.
"""

import datetime as dt
import sys
import time

import sentry

SAMPLE_DATES = 4


def sample_dates():
    """Dates spread over tomorrow .. today+reconcile_days — inside the known
    run window, where the reconcile canary already proves showtimes exist,
    so an empty answer is a real failure and not 'no shows that day'."""
    rd = sentry.CFG["reconcile_days"]
    offsets = sorted({max(1, rd * i // (SAMPLE_DATES - 1))
                      for i in range(SAMPLE_DATES)})
    today = dt.date.today()
    return [(today + dt.timedelta(days=o)).isoformat() for o in offsets]


def check_reachability(st):
    """Fetch each sampled date per venue; return (ok, total_70mm_hits)."""
    ok = True
    hits_total = 0
    for venue in sentry.CFG["venues"]:
        for i, date in enumerate(sample_dates()):
            if i:
                time.sleep(sentry.CFG["sweep_spacing_seconds"])
            vm = sentry.fetch_day(st, venue, date)
            lf = st.get("lastFetch") or {}
            if vm is None:
                err = lf.get("error", "unknown")
                if any(code in err for code in ("403", "429", "503")):
                    print(f"  {venue} {date}: REFUSED — this IP is blocked "
                          f"({err[:120]})")
                else:
                    print(f"  {venue} {date}: FETCH FAILED ({err[:120]})")
                ok = False
                continue
            if not lf.get("movies"):
                print(f"  {venue} {date}: BLIND — HTTP {lf.get('status')}, "
                      f"{lf.get('bytes')}B, 0 movies")
                ok = False
                continue
            hits_total += lf.get("hits", 0)
            print(f"  {venue} {date}: HTTP {lf.get('status')} "
                  f"{lf.get('bytes')}B, {lf.get('movies')} movies, "
                  f"{lf.get('hits')} 70mm hits")
    return ok, hits_total


def cycles_per_day():
    """Cycles the sentry will actually run on its busiest weekday, honouring
    burst windows and quiet hours via the sentry's own poll_interval."""
    worst = 0
    today = dt.date.today()
    for d in range(7):
        day = today + dt.timedelta(days=1 + d)
        cycles = sum(3600 / sentry.poll_interval(
            dt.datetime(day.year, day.month, day.day, hour, 0, 0))
            for hour in range(24))
        worst = max(worst, cycles)
    return worst


def project_budget():
    """Projected requests/day from the live config; (total, breakdown)."""
    cfg = sentry.CFG
    nv = len(cfg["venues"])
    cycles = cycles_per_day()
    probes = cycles * nv
    target_polls = min(cycles, 86400 / cfg.get("target_every_seconds", 120))
    targets = target_polls * len(cfg.get("target_dates", [])) * nv
    today = dt.date.today()
    tomorrow = (today + dt.timedelta(days=1)).isoformat()
    restock = 0
    for d in cfg.get("restock_watch_dates", []):
        if d < today.isoformat():
            continue
        interval = (cfg.get("restock_burst_seconds", 60) if d <= tomorrow
                    else cfg.get("restock_every_seconds", 300))
        restock += min(cycles, 86400 / interval) * nv
    reconcile = (cfg["reconcile_days"] + 1) * nv
    total = int(probes + targets + restock + reconcile)
    breakdown = (f"  probes    {int(probes):>6}  ({int(cycles)} cycles × "
                 f"{nv} venue{'s' if nv > 1 else ''})\n"
                 f"  targets   {int(targets):>6}  "
                 f"({len(cfg.get('target_dates', []))} dates)\n"
                 f"  restock   {int(restock):>6}\n"
                 f"  reconcile {reconcile:>6}")
    return total, breakdown


def run():
    # belt and braces: this run must be side-effect free
    sentry.CFG["capture_last_response"] = False
    sentry.CFG["blind_streak_alert"] = 10 ** 9  # no pushes from a preflight
    st = sentry.default_state()  # throwaway — never written to disk

    failures = []

    print(f"Preflight — {len(sentry.CFG['venues'])} venue(s), "
          f"sampling {SAMPLE_DATES} dates:")
    reachable, hits = check_reachability(st)
    if not reachable:
        failures.append("endpoint refused or unreadable from this IP")
    elif not hits:
        failures.append("0 70mm hits across every sampled date — the run "
                        "should be visible in this window; parsing or the "
                        "feed shape is broken")

    total, breakdown = project_budget()
    cap = sentry.CFG["daily_request_budget"]
    print(f"Projected requests/day (busiest weekday):\n{breakdown}\n"
          f"  total     {total:>6}  vs budget {cap}")
    if total > cap:
        failures.append(
            f"projected {total} requests/day exceeds daily_request_budget "
            f"{cap} — exhaustion trips the breaker and masquerades as an IP "
            f"block; trim target_dates or relax the cadence")

    if failures:
        for f in failures:
            print(f"PREFLIGHT FAIL: {f}")
        return 1
    print("PREFLIGHT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(run())
