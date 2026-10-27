"""Unit tests for engine.governor — spacing floor, daily budget, and
the upstream-fetch breaker curve (mirrors sentry.py's tuned one).

sqlite3 ':memory:' only. No network, no sleeps; every clock value is
passed explicitly as `now=`.
"""

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from engine import db, governor, log  # noqa: E402

SOURCE = "fandango"
T0 = 1_700_000_000.0  # an arbitrary UTC instant, not day-boundary-adjacent


class GovernorTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        log.clear()

    def test_first_request_always_allowed(self):
        allowed, reason, wait = governor.acquire(
            self.conn, SOURCE, T0, min_spacing_seconds=10, daily_budget=100)
        self.assertTrue(allowed)
        self.assertEqual(reason, "ok")
        self.assertEqual(wait, 0.0)

    def test_spacing_floor_denies_then_allows(self):
        governor.acquire(self.conn, SOURCE, T0, min_spacing_seconds=10, daily_budget=100)

        allowed, reason, wait = governor.acquire(
            self.conn, SOURCE, T0 + 5, min_spacing_seconds=10, daily_budget=100)
        self.assertFalse(allowed)
        self.assertEqual(reason, "spacing")
        self.assertAlmostEqual(wait, 5.0, places=3)

        allowed, reason, wait = governor.acquire(
            self.conn, SOURCE, T0 + 11, min_spacing_seconds=10, daily_budget=100)
        self.assertTrue(allowed)

    def test_daily_budget_denies_at_ceiling_and_logs_budget_tag(self):
        for _ in range(3):
            allowed, reason, _ = governor.acquire(
                self.conn, SOURCE, T0, min_spacing_seconds=0, daily_budget=3)
            self.assertTrue(allowed)

        self.assertEqual(log.recent(tag=log.TAG_BUDGET), [])

        allowed, reason, wait = governor.acquire(
            self.conn, SOURCE, T0, min_spacing_seconds=0, daily_budget=3)
        self.assertFalse(allowed)
        self.assertEqual(reason, "budget-exhausted")
        self.assertGreater(wait, 0)

        budget_logs = log.recent(tag=log.TAG_BUDGET)
        self.assertEqual(len(budget_logs), 1)
        self.assertIn(SOURCE, budget_logs[0].line)

    def test_daily_budget_resets_on_new_utc_day(self):
        for _ in range(2):
            governor.acquire(self.conn, SOURCE, T0, min_spacing_seconds=0, daily_budget=2)
        allowed, _, _ = governor.acquire(
            self.conn, SOURCE, T0, min_spacing_seconds=0, daily_budget=2)
        self.assertFalse(allowed)

        next_day = dt.datetime.fromtimestamp(T0, dt.timezone.utc).date() + dt.timedelta(days=1)
        next_day_ts = dt.datetime.combine(
            next_day, dt.time(hour=1), tzinfo=dt.timezone.utc).timestamp()

        allowed, reason, _ = governor.acquire(
            self.conn, SOURCE, next_day_ts, min_spacing_seconds=0, daily_budget=2)
        self.assertTrue(allowed)
        self.assertEqual(reason, "ok")

    def test_breaker_first_trip_is_short(self):
        governor.report_failure(self.conn, SOURCE, T0)
        governor.report_failure(self.conn, SOURCE, T0)
        governor.report_failure(self.conn, SOURCE, T0)  # 3rd failure trips it

        allowed, reason, wait = governor.acquire(
            self.conn, SOURCE, T0, min_spacing_seconds=0, daily_budget=1000)
        self.assertFalse(allowed)
        self.assertEqual(reason, "breaker-open")
        # the owner's tuned curve requires the FIRST trip to be short --
        # a couple of blips shouldn't cost half an hour of blindness.
        self.assertLess(wait, 90)
        self.assertAlmostEqual(wait, 60.0, places=3)

        breaker_logs = log.recent(tag=log.TAG_BREAKER)
        self.assertGreaterEqual(len(breaker_logs), 1)

    def test_breaker_doubles_on_further_failures_and_caps(self):
        for _ in range(3):
            governor.report_failure(self.conn, SOURCE, T0)
        row = self.conn.execute(
            "SELECT breaker_open_until FROM governor WHERE source=?", (SOURCE,)
        ).fetchone()
        self.assertAlmostEqual(row["breaker_open_until"] - T0, 60.0, places=3)

        governor.report_failure(self.conn, SOURCE, T0)  # 4th
        row = self.conn.execute(
            "SELECT breaker_open_until FROM governor WHERE source=?", (SOURCE,)
        ).fetchone()
        self.assertAlmostEqual(row["breaker_open_until"] - T0, 120.0, places=3)

        governor.report_failure(self.conn, SOURCE, T0)  # 5th
        row = self.conn.execute(
            "SELECT breaker_open_until FROM governor WHERE source=?", (SOURCE,)
        ).fetchone()
        self.assertAlmostEqual(row["breaker_open_until"] - T0, 240.0, places=3)

        # keep failing until the doubling would blow past the 4h cap
        for _ in range(15):
            governor.report_failure(self.conn, SOURCE, T0)
        row = self.conn.execute(
            "SELECT breaker_open_until FROM governor WHERE source=?", (SOURCE,)
        ).fetchone()
        self.assertAlmostEqual(row["breaker_open_until"] - T0,
                                governor.BREAKER_CAP_SECONDS, places=3)

    def test_breaker_closes_on_success_and_logs(self):
        for _ in range(3):
            governor.report_failure(self.conn, SOURCE, T0)

        governor.report_success(self.conn, SOURCE, T0 + 1)

        allowed, reason, _ = governor.acquire(
            self.conn, SOURCE, T0 + 1, min_spacing_seconds=0, daily_budget=1000)
        self.assertTrue(allowed)
        self.assertEqual(reason, "ok")

        row = self.conn.execute(
            "SELECT breaker_fails, breaker_open_until FROM governor WHERE source=?",
            (SOURCE,),
        ).fetchone()
        self.assertEqual(row["breaker_fails"], 0)
        self.assertIsNone(row["breaker_open_until"])

        close_logs = [e for e in log.recent(tag=log.TAG_BREAKER)
                      if "closed" in e.msg]
        self.assertEqual(len(close_logs), 1)

    def test_unknown_tag_raises(self):
        with self.assertRaises(ValueError):
            log.log("NOT-A-REAL-TAG", "should not be allowed")


class GovernorConcurrencyTests(unittest.TestCase):
    """Regression: acquire() must never over-grant under multi-connection
    contention. The 2026-08-04 contention bench caught the original
    select-check-update shape over-granting by ~50% (3176 grants against
    a budget of 2000) — the grant is now a single conditional UPDATE and
    this pins it. File-backed db on purpose: ':memory:' can't be shared
    across connections, and the race only exists across connections."""

    def test_concurrent_acquire_never_exceeds_budget(self):
        import tempfile
        import threading

        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        os.unlink(path)
        self.addCleanup(lambda: [
            os.path.exists(path + sfx) and os.unlink(path + sfx)
            for sfx in ("", "-wal", "-shm")])

        db.connect(path).close()  # apply schema once
        budget = 300
        n_threads, per_thread = 8, 100
        allowed_counts = [0] * n_threads
        errors = []
        barrier = threading.Barrier(n_threads)

        def worker(idx):
            conn = db.connect(path)
            try:
                barrier.wait()
                for _ in range(per_thread):
                    ok, _r, _w = governor.acquire(
                        conn, SOURCE, T0, min_spacing_seconds=0,
                        daily_budget=budget)
                    if ok:
                        allowed_counts[idx] += 1
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        total_allowed = sum(allowed_counts)
        self.assertEqual(total_allowed, budget)

        conn = db.connect(path)
        try:
            used = conn.execute(
                "SELECT requests_used FROM governor WHERE source=?",
                (SOURCE,)).fetchone()["requests_used"]
        finally:
            conn.close()
        self.assertEqual(used, budget)  # no lost updates either


if __name__ == "__main__":
    unittest.main()
