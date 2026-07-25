"""Unit tests for the sentry watchdog. All I/O is mocked or temp-filed."""

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sentry  # noqa: E402
import watchdog  # noqa: E402

NOW = dt.datetime(2026, 7, 24, 21, 0, 0)


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self._paths = (sentry.STATE_PATH, watchdog.WD_STATE,
                       watchdog.LOG_PATH, watchdog.LAUNCHD_LOG)
        self.tmp = tempfile.mkdtemp()
        sentry.STATE_PATH = os.path.join(self.tmp, "state.json")
        watchdog.WD_STATE = os.path.join(self.tmp, "wd.json")
        watchdog.LOG_PATH = os.path.join(self.tmp, "wd.log")
        watchdog.LAUNCHD_LOG = os.path.join(self.tmp, "launchd.log")
        sentry.CFG = dict(sentry.CFG, watchdog_stale_seconds=600,
                          watchdog_renotify_hours=6)

    def tearDown(self):
        (sentry.STATE_PATH, watchdog.WD_STATE, watchdog.LOG_PATH,
         watchdog.LAUNCHD_LOG) = self._paths

    def write_state(self, last_poll):
        with open(sentry.STATE_PATH, "w") as f:
            json.dump({"lastPollAt": last_poll}, f)

    def run_check(self, push_ok=True):
        with mock.patch.object(sentry, "push", return_value=push_ok) as p:
            rc = watchdog.check(NOW)
        return rc, p


class TestLiveness(WatchdogTest):
    def test_fresh_poll_is_silent(self):
        self.write_state((NOW - dt.timedelta(seconds=45)).isoformat())
        rc, p = self.run_check()
        self.assertEqual(rc, 0)
        p.assert_not_called()

    def test_stale_poll_alerts_urgently(self):
        self.write_state((NOW - dt.timedelta(minutes=25)).isoformat())
        rc, p = self.run_check()
        self.assertEqual(rc, 1)
        p.assert_called_once()
        self.assertEqual(p.call_args.args[0], "SENTRY DOWN")
        self.assertIn("25 min", p.call_args.args[1])
        self.assertEqual(p.call_args.kwargs["priority"], "urgent")

    def test_missing_state_file_counts_as_down(self):
        rc, p = self.run_check()
        self.assertEqual(rc, 1)
        p.assert_called_once()

    def test_alert_fires_once_per_outage(self):
        self.write_state((NOW - dt.timedelta(minutes=25)).isoformat())
        self.run_check()
        rc, p = self.run_check()
        p.assert_not_called()

    def test_renotify_after_configured_gap(self):
        self.write_state((NOW - dt.timedelta(minutes=25)).isoformat())
        self.run_check()
        with mock.patch.object(sentry, "push", return_value=True) as p:
            watchdog.check(NOW + dt.timedelta(hours=7))
        p.assert_called_once()

    def test_failed_push_retries_next_pass(self):
        self.write_state((NOW - dt.timedelta(minutes=25)).isoformat())
        self.run_check(push_ok=False)
        rc, p = self.run_check()
        p.assert_called_once()  # not deduped, since nothing was delivered

    def go_down_then_up(self, down_min=25, up_at=None):
        self.write_state((NOW - dt.timedelta(minutes=down_min)).isoformat())
        self.run_check()
        up_at = up_at or NOW
        # the watcher polled just before we looked, so it reads as alive
        self.write_state((up_at - dt.timedelta(seconds=20)).isoformat())
        with mock.patch.object(sentry, "push", return_value=True) as p:
            rc = watchdog.check(up_at)
        return rc, p

    def test_recovery_alert_is_audible_not_silent(self):
        """A silent all-clear defeats the point — it must ring like the
        outage did."""
        rc, p = self.go_down_then_up()
        self.assertEqual(rc, 0)
        p.assert_called_once()
        self.assertEqual(p.call_args.args[0], "SENTRY BACK UP")
        self.assertEqual(p.call_args.kwargs["priority"], "good")
        self.assertNotEqual(p.call_args.kwargs["priority"], "low")

    def test_recovery_reports_how_long_it_was_down(self):
        rc, p = self.go_down_then_up(up_at=NOW + dt.timedelta(minutes=18))
        self.assertIn("18 min down", p.call_args.args[1])

    def test_no_all_clear_for_an_outage_you_were_never_told_about(self):
        self.write_state((NOW - dt.timedelta(minutes=25)).isoformat())
        self.run_check(push_ok=False)          # DOWN alert never delivered
        self.write_state((NOW - dt.timedelta(seconds=20)).isoformat())
        rc, p = self.run_check()
        p.assert_not_called()

    def test_failed_all_clear_retries_next_pass(self):
        self.write_state((NOW - dt.timedelta(minutes=25)).isoformat())
        self.run_check()
        self.write_state((NOW - dt.timedelta(seconds=20)).isoformat())
        self.run_check(push_ok=False)          # all-clear lost
        rc, p = self.run_check()               # still owed one
        p.assert_called_once()
        self.assertEqual(p.call_args.args[0], "SENTRY BACK UP")

    def test_recovery_fires_once_not_every_pass(self):
        self.go_down_then_up()
        rc, p = self.run_check()
        p.assert_not_called()


class TestReadOnlyCheck(WatchdogTest):
    """--check backs the container HEALTHCHECK, which runs on its own
    schedule alongside the entrypoint's watchdog loop — it must observe
    without pushing or racing the loop on watchdog_state.json."""

    def test_fresh_poll_reads_alive(self):
        self.write_state((NOW - dt.timedelta(seconds=45)).isoformat())
        down, age = watchdog.is_stale(NOW)
        self.assertFalse(down)
        self.assertAlmostEqual(age, 45, delta=1)

    def test_stale_poll_reads_down(self):
        self.write_state((NOW - dt.timedelta(minutes=25)).isoformat())
        down, _ = watchdog.is_stale(NOW)
        self.assertTrue(down)

    def test_missing_state_reads_down(self):
        down, age = watchdog.is_stale(NOW)
        self.assertTrue(down)
        self.assertIsNone(age)

    def test_probe_neither_pushes_nor_writes_watchdog_state(self):
        self.write_state((NOW - dt.timedelta(minutes=25)).isoformat())
        with mock.patch.object(sentry, "push") as p:
            watchdog.is_stale(NOW)
        p.assert_not_called()
        self.assertFalse(os.path.exists(watchdog.WD_STATE))

    def test_full_check_still_alerts_on_the_same_verdict(self):
        """is_stale() and check() must agree — the refactor may not drift."""
        self.write_state((NOW - dt.timedelta(minutes=25)).isoformat())
        self.assertTrue(watchdog.is_stale(NOW)[0])
        rc, p = self.run_check()
        self.assertEqual(rc, 1)
        p.assert_called_once()


class TestErrorCapture(WatchdogTest):
    def test_alert_carries_recent_launchd_errors(self):
        with open(watchdog.LAUNCHD_LOG, "w") as f:
            f.write("quiet AABQI: nothing\n")
            f.write("You have not agreed to the Xcode license agreements.\n")
        self.write_state((NOW - dt.timedelta(minutes=25)).isoformat())
        rc, p = self.run_check()
        self.assertIn("Xcode license", p.call_args.args[1])

    def test_duplicate_errors_collapse(self):
        with open(watchdog.LAUNCHD_LOG, "w") as f:
            f.write("Traceback (most recent call last)\n" * 30)
        self.write_state((NOW - dt.timedelta(minutes=25)).isoformat())
        rc, p = self.run_check()
        self.assertEqual(p.call_args.args[1].count("Traceback"), 1)

    def test_missing_launchd_log_does_not_break_alert(self):
        self.write_state((NOW - dt.timedelta(minutes=25)).isoformat())
        rc, p = self.run_check()
        p.assert_called_once()


if __name__ == "__main__":
    unittest.main()
