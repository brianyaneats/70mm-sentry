"""engine.governor — per-source politeness: spacing floor, daily
budget, and a breaker whose curve mirrors sentry.py's tuned one (see
sentry.py's breaker_fail(): first trip is short and escalates fast,
because a couple of read timeouts is far more often a network blip than
a block).

Two log tags exist only for this module's denials, registered in
engine.log alongside the adapter-fetch vocabulary:
    [BUDGET]   daily request budget exhausted, request denied
    [BREAKER]  breaker open (or just opened), request denied
Note for whoever owns the docs/standard: these two are engine
additions on top of the four sources/base.py tags and should be folded
into the shared tag-standard doc.
"""

import datetime as dt

from engine import log as _log

BREAKER_FAIL_THRESHOLD = 3
BREAKER_BASE_BACKOFF_SECONDS = 60
BREAKER_CAP_SECONDS = 4 * 3600


def _day_str(now):
    return dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y-%m-%d")


def _get_or_create_row(conn, source):
    row = conn.execute(
        "SELECT * FROM governor WHERE source = ?", (source,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO governor "
            "(source, day, requests_used, breaker_fails, breaker_open_until, "
            " last_request_at) VALUES (?, NULL, 0, 0, NULL, NULL)",
            (source,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM governor WHERE source = ?", (source,)
        ).fetchone()
    return row


def acquire(conn, source, now, min_spacing_seconds, daily_budget):
    """(allowed: bool, reason: str, wait_seconds: float).

    The grant is a SINGLE conditional UPDATE: breaker, budget, and
    spacing are all checked inside the same statement that spends the
    request, so two workers on separate connections can never both read
    "1999 used" and both spend the 2000th slot. SQLite serializes
    writers; the statement is the critical section. (The old
    select-check-update shape over-granted by ~50% under an 8-thread
    contention bench -- lost updates, not a perf quirk.)

    Checks report in priority order breaker > budget > spacing -- the
    denial reason is computed from a re-read AFTER a failed grant, so it
    is best-effort diagnostics; the denial itself is authoritative.
    """
    conn.execute(
        "INSERT OR IGNORE INTO governor "
        "(source, day, requests_used, breaker_fails, breaker_open_until, "
        " last_request_at) VALUES (?, ?, 0, 0, NULL, NULL)",
        (source, _day_str(now)),
    )
    today = _day_str(now)
    conn.execute(
        "UPDATE governor SET day = ?, requests_used = 0 "
        "WHERE source = ? AND (day IS NULL OR day != ?)",
        (today, source, today),
    )

    cur = conn.execute(
        "UPDATE governor SET requests_used = requests_used + 1, "
        "       last_request_at = ? "
        "WHERE source = ? "
        "  AND (breaker_open_until IS NULL OR breaker_open_until <= ?) "
        "  AND requests_used < ? "
        "  AND (last_request_at IS NULL OR ? - last_request_at >= ?)",
        (now, source, now, daily_budget, now, min_spacing_seconds),
    )
    conn.commit()
    if cur.rowcount == 1:
        return True, "ok", 0.0

    row = conn.execute(
        "SELECT * FROM governor WHERE source = ?", (source,)
    ).fetchone()

    if row["breaker_open_until"] is not None and now < row["breaker_open_until"]:
        wait = row["breaker_open_until"] - now
        _log.log(_log.TAG_BREAKER, "breaker open, request denied",
                  source=source, wait_seconds=round(wait, 3))
        return False, "breaker-open", wait

    if row["requests_used"] >= daily_budget:
        tomorrow = dt.datetime.fromtimestamp(now, dt.timezone.utc).date() + \
            dt.timedelta(days=1)
        reset_at = dt.datetime.combine(
            tomorrow, dt.time.min, tzinfo=dt.timezone.utc
        ).timestamp()
        wait = reset_at - now
        _log.log(_log.TAG_BUDGET, "daily budget exhausted, request denied",
                  source=source, requests_used=row["requests_used"],
                  daily_budget=daily_budget)
        return False, "budget-exhausted", wait

    if row["last_request_at"] is not None:
        elapsed = now - row["last_request_at"]
        if elapsed < min_spacing_seconds:
            return False, "spacing", min_spacing_seconds - elapsed

    # The guarded UPDATE lost to a concurrent writer but the re-read no
    # longer shows why (state moved on between the two statements).
    # Denied is denied; the caller retries after the spacing floor.
    return False, "contention", min_spacing_seconds


def report_failure(conn, source, now):
    """Record one upstream failure; opens the breaker at the 3rd
    consecutive failure, doubling on every failure after that, capped at
    4 hours -- mirrors sentry.py's breaker_fail() curve exactly."""
    _get_or_create_row(conn, source)
    # Increment atomically, then read the new count back -- two workers
    # reporting failures concurrently must not lose either increment
    # (the same lost-update shape acquire() was fixed for).
    conn.execute(
        "UPDATE governor SET breaker_fails = breaker_fails + 1 "
        "WHERE source = ?",
        (source,),
    )
    conn.commit()
    fails = conn.execute(
        "SELECT breaker_fails FROM governor WHERE source = ?", (source,)
    ).fetchone()["breaker_fails"]

    if fails >= BREAKER_FAIL_THRESHOLD:
        backoff = min(
            BREAKER_BASE_BACKOFF_SECONDS * (2 ** max(0, fails - BREAKER_FAIL_THRESHOLD)),
            BREAKER_CAP_SECONDS,
        )
        _log.log(_log.TAG_BREAKER, "breaker opened",
                  source=source, failures=fails, backoff_seconds=backoff)
        conn.execute(
            "UPDATE governor SET breaker_open_until = ? WHERE source = ?",
            (now + backoff, source),
        )
        conn.commit()


def report_success(conn, source, now=None):
    """Reset the breaker on a healthy request/cycle."""
    row = _get_or_create_row(conn, source)
    if row["breaker_fails"] or row["breaker_open_until"] is not None:
        _log.log(_log.TAG_BREAKER, "breaker closed (recovered)", source=source)
    conn.execute(
        "UPDATE governor SET breaker_fails = 0, breaker_open_until = NULL "
        "WHERE source = ?",
        (source,),
    )
    conn.commit()
