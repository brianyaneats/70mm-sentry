"""Unit tests for platformd.runner — the dispatch-loop process.

sqlite file-backed dbs (a fresh tempfile per test, never ':memory:' --
that's what the module is meant to be pointed at), no real sleeps: the
loop-forever path is exercised by monkeypatching time.sleep to raise the
module's own _Stop signal-handler exception on its first call, exactly
what the real SIGINT handler would do, without needing actual OS signal
delivery or a wall-clock wait.
"""

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from platformd import core, db, log, runner  # noqa: E402

DROP = "drop-1"
# Real wall-clock time, not a fixed historical epoch: runner.main() calls
# outbox.dispatch() with now=time.time() internally (no override), so an
# outbox row's created_at must be "now" too or it reads as already past
# outbox.TTL_SECONDS old and gets exhausted instead of merely failed.
T0 = time.time()


class AlwaysOk:
    def __init__(self):
        self.calls = []

    def send(self, address, message, context=None):
        self.calls.append((address, message))
        return True


def _make_temp_db_with_one_due_row():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.unlink(path)  # db.connect() creates it fresh
    conn = db.connect(path)
    core.register_drop(conn, DROP, "Drop One", now=T0)
    row = core.subscribe(conn, DROP, "telegram", "a", "paid", T0)
    verified = core.verify(conn, row["id"], row["verify_code"], T0)
    core.record_event(conn, DROP, {
        "event_id": "evt-1", "kind": "restock", "unit_id": "unit-1",
        "state": "available",
    }, now=T0)
    conn.close()
    return path, verified["id"]


class RunOnceTests(unittest.TestCase):
    def setUp(self):
        log.clear()
        self.path, self.sub_id = _make_temp_db_with_one_due_row()
        self.addCleanup(lambda: [
            os.path.exists(self.path + sfx) and os.unlink(self.path + sfx)
            for sfx in ("", "-wal", "-shm")])

    def test_run_once_dispatches_due_row_and_logs_heartbeat(self):
        conn = db.connect(self.path)
        try:
            channel = AlwaysOk()
            before, after = runner.run_once(conn, {"telegram": channel}, now=T0)
            self.assertEqual(before, 1)
            self.assertEqual(after, 0)
            self.assertEqual(len(channel.calls), 1)

            row = conn.execute(
                "SELECT status FROM outbox WHERE subscription_id=?",
                (self.sub_id,)).fetchone()
            self.assertEqual(row["status"], "delivered")
        finally:
            conn.close()

        ticks = [e for e in log.recent(tag=log.TAG_RUNNER)
                 if "tick complete" in e.msg]
        self.assertEqual(len(ticks), 1)
        self.assertEqual(ticks[0].fields["pending_before"], 1)
        self.assertEqual(ticks[0].fields["pending_after"], 0)


class MainOnceTests(unittest.TestCase):
    def setUp(self):
        log.clear()
        self.path, self.sub_id = _make_temp_db_with_one_due_row()
        self.addCleanup(lambda: [
            os.path.exists(self.path + sfx) and os.unlink(self.path + sfx)
            for sfx in ("", "-wal", "-shm")])

    def test_once_flag_runs_a_single_tick_and_returns(self):
        # No real channels wired -- send() will KeyError against the empty
        # dict, which is a normal per-row delivery failure (per
        # outbox.dispatch's per-row isolation), not a crash.
        runner.main(["--db", self.path, "--once"])

        conn = db.connect(self.path)
        try:
            row = conn.execute(
                "SELECT status FROM outbox WHERE subscription_id=?",
                (self.sub_id,)).fetchone()
            self.assertEqual(row["status"], "failed")
        finally:
            conn.close()

        startup = [e for e in log.recent(tag=log.TAG_RUNNER) if "starting" in e.msg]
        ticks = [e for e in log.recent(tag=log.TAG_RUNNER) if "tick complete" in e.msg]
        shutdown = [e for e in log.recent(tag=log.TAG_RUNNER) if "shutting down" in e.msg]
        self.assertEqual(len(startup), 1)
        self.assertEqual(len(ticks), 1)
        self.assertEqual(len(shutdown), 1)

    def test_demo_channels_flag_delivers_via_print_channel(self):
        runner.main(["--db", self.path, "--once", "--demo-channels"])

        conn = db.connect(self.path)
        try:
            row = conn.execute(
                "SELECT status FROM outbox WHERE subscription_id=?",
                (self.sub_id,)).fetchone()
            self.assertEqual(row["status"], "delivered")
        finally:
            conn.close()


class SigintShutdownTests(unittest.TestCase):
    def setUp(self):
        log.clear()
        self.path, self.sub_id = _make_temp_db_with_one_due_row()
        self.addCleanup(lambda: [
            os.path.exists(self.path + sfx) and os.unlink(self.path + sfx)
            for sfx in ("", "-wal", "-shm")])

    def test_loop_stops_cleanly_and_logs_shutdown_on_stop_signal(self):
        # Simulate what the real SIGINT handler does (raise runner._Stop)
        # on the very first sleep -- deterministic, no real signals, no
        # real waiting.
        with mock.patch.object(runner.time, "sleep", side_effect=runner._Stop()):
            runner.main(["--db", self.path, "--interval", "0.01",
                         "--demo-channels"])

        ticks = [e for e in log.recent(tag=log.TAG_RUNNER) if "tick complete" in e.msg]
        shutdown = [e for e in log.recent(tag=log.TAG_RUNNER) if "shutting down" in e.msg]
        # exactly one tick ran before the (simulated) SIGINT arrived
        self.assertEqual(len(ticks), 1)
        self.assertEqual(len(shutdown), 1)

        # and dispatch actually ran during that one tick
        conn = db.connect(self.path)
        try:
            row = conn.execute(
                "SELECT status FROM outbox WHERE subscription_id=?",
                (self.sub_id,)).fetchone()
            self.assertEqual(row["status"], "delivered")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
