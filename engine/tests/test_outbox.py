"""Unit tests for engine.outbox — dispatch, backoff, TTL exhaustion,
provider breaker, mid-flight STOP honoring, and the time-based
[ZERO-DELIVERED] alarm.

sqlite3 ':memory:' only. No network, no sleeps; every clock value is
passed explicitly as `now=`.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from engine import core, db, log, outbox  # noqa: E402

DROP = "drop-1"
T0 = 1_700_000_000.0


def active_sub(conn, drop_id, channel, address, tier, now):
    """Test helper: subscribe + verify in one step, returning the
    subscription id of the resulting active row."""
    row = core.subscribe(conn, drop_id, channel, address, tier, now)
    verified = core.verify(conn, row["id"], row["verify_code"], now)
    assert verified["status"] == "active"
    return verified["id"]


class AlwaysOk:
    def __init__(self):
        self.calls = []

    def send(self, address, message):
        self.calls.append((address, message))
        return True


class AlwaysFail:
    def __init__(self, exc_factory=lambda: RuntimeError("down")):
        self.exc_factory = exc_factory
        self.calls = 0

    def send(self, address, message):
        self.calls += 1
        raise self.exc_factory()


class Toggle:
    """Fails while mode == 'fail', succeeds while mode == 'ok'."""

    def __init__(self):
        self.mode = "fail"
        self.calls = 0

    def send(self, address, message):
        self.calls += 1
        if self.mode == "fail":
            raise RuntimeError("down")
        return True


def outbox_row(conn, subscription_id):
    """Earliest outbox row for a subscription (by insertion order) --
    deterministic even when a subscription accumulates rows across
    multiple events in the same test."""
    return conn.execute(
        "SELECT * FROM outbox WHERE subscription_id=? ORDER BY id ASC LIMIT 1",
        (subscription_id,)
    ).fetchone()


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        core.register_drop(self.conn, DROP, "Drop One", now=T0)
        log.clear()
        outbox.reset_provider_breakers()

    def test_per_subscriber_isolation(self):
        ok_sub = active_sub(self.conn, DROP, "telegram", "a", "paid", T0)
        bad_sub = active_sub(self.conn, DROP, "discord", "b", "paid", T0)

        core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "new_showtime", "unit_id": "unit-1",
            "state": "available",
        }, now=T0)

        ok_channel = AlwaysOk()
        bad_channel = AlwaysFail(lambda: BrokenPipeError("dead socket"))
        outbox.dispatch(self.conn, {"telegram": ok_channel, "discord": bad_channel}, now=T0)

        ok_row = outbox_row(self.conn, ok_sub)
        bad_row = outbox_row(self.conn, bad_sub)
        self.assertEqual(ok_row["status"], "delivered")
        self.assertEqual(bad_row["status"], "failed")
        self.assertEqual(bad_row["attempts"], 1)
        # the raising channel must not have prevented the healthy one from
        # being attempted in the same pass
        self.assertEqual(len(ok_channel.calls), 1)

        pipe_entries = log.recent(tag=log.TAG_PIPE)
        self.assertEqual(len(pipe_entries), 1)
        self.assertIn("discord", pipe_entries[0].line)

        delivery_entries = log.recent(tag=log.TAG_DELIVERY)
        self.assertGreaterEqual(len(delivery_entries), 1)

    def test_backoff_schedule_doubles_and_caps(self):
        sub = active_sub(self.conn, DROP, "telegram", "a", "paid", T0)
        core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "new_showtime", "unit_id": "unit-1",
            "state": "available",
        }, now=T0)

        bad_channel = AlwaysFail()
        now = T0
        # attempts 1..8: backoff = min(30 * 2**attempts, cap) -- doubles each
        # time, then pins at BACKOFF_CAP_SECONDS once the doubling exceeds it.
        for attempt in range(1, 9):
            expected = min(30 * (2 ** attempt), outbox.BACKOFF_CAP_SECONDS)
            outbox.dispatch(self.conn, {"telegram": bad_channel}, now=now)
            row = outbox_row(self.conn, sub)
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["attempts"], attempt)
            self.assertAlmostEqual(row["next_attempt_at"] - now, expected, places=3)
            now = row["next_attempt_at"]  # jump straight to when it's next due

        # by attempt 8, backoff (30*2^8=7680) must already be pinned at the cap
        self.assertEqual(min(30 * (2 ** 8), outbox.BACKOFF_CAP_SECONDS),
                          outbox.BACKOFF_CAP_SECONDS)

    def test_exhaustion_past_ttl(self):
        sub = active_sub(self.conn, DROP, "telegram", "a", "paid", T0)
        core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "new_showtime", "unit_id": "unit-1",
            "state": "available",
        }, now=T0)

        bad_channel = AlwaysFail()
        outbox.dispatch(self.conn, {"telegram": bad_channel},
                         now=T0 + outbox.TTL_SECONDS + 1)

        row = outbox_row(self.conn, sub)
        self.assertEqual(row["status"], "exhausted")

    def test_provider_breaker_opens_after_five_distinct_subscription_failures(self):
        subs = []
        for i in range(6):
            subs.append(active_sub(
                self.conn, DROP, "telegram", "addr-%d" % i, "paid", T0))

        core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "new_showtime", "unit_id": "unit-1",
            "state": "available",
        }, now=T0)

        bad_channel = AlwaysFail()
        outbox.dispatch(self.conn, {"telegram": bad_channel}, now=T0)

        # only the first 5 rows should have actually been sent to the
        # channel -- the 6th arrives after the breaker has tripped and is
        # skipped without calling send() at all
        self.assertEqual(bad_channel.calls, 5)

        statuses = [outbox_row(self.conn, s)["status"] for s in subs]
        self.assertEqual(statuses.count("failed"), 5)
        self.assertEqual(statuses.count("pending"), 1)

        breaker_open_logs = [e for e in log.recent(tag=log.TAG_DELIVERY)
                              if "breaker opened" in e.msg]
        self.assertEqual(len(breaker_open_logs), 1)

    def test_provider_breaker_skips_once_per_pass_not_per_row(self):
        subs = []
        for i in range(7):
            subs.append(active_sub(
                self.conn, DROP, "telegram", "addr-%d" % i, "paid", T0))

        core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "new_showtime", "unit_id": "unit-1",
            "state": "available",
        }, now=T0)

        bad_channel = AlwaysFail()
        outbox.dispatch(self.conn, {"telegram": bad_channel}, now=T0)

        skip_logs = [e for e in log.recent(tag=log.TAG_DELIVERY)
                     if "skipping channel" in e.msg]
        self.assertEqual(len(skip_logs), 1)
        # rows 6 and 7 are both left pending, skipped without a send() call
        self.assertEqual(bad_channel.calls, 5)

    def test_provider_breaker_closes_and_allows_retry_after_window(self):
        subs = []
        for i in range(5):
            subs.append(active_sub(
                self.conn, DROP, "telegram", "addr-%d" % i, "paid", T0))

        core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "new_showtime", "unit_id": "unit-1",
            "state": "available",
        }, now=T0)

        toggle = Toggle()
        outbox.dispatch(self.conn, {"telegram": toggle}, now=T0)
        for s in subs:
            self.assertEqual(outbox_row(self.conn, s)["status"], "failed")

        # still inside the 60s open window -- a new event's rows for the
        # same provider must be skipped, not attempted
        core.record_event(self.conn, DROP, {
            "event_id": "evt-2", "kind": "restock", "unit_id": "unit-1",
            "state": "available",
        }, now=T0 + 30)
        calls_before = toggle.calls
        outbox.dispatch(self.conn, {"telegram": toggle}, now=T0 + 30)
        self.assertEqual(toggle.calls, calls_before, "breaker should still be open")

        # past the 60s window, and the provider has recovered
        toggle.mode = "ok"
        outbox.dispatch(self.conn, {"telegram": toggle}, now=T0 + 61)
        statuses = [outbox_row(self.conn, s)["status"] for s in subs]
        self.assertTrue(all(s == "delivered" for s in statuses))


class StopMidFlightTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        core.register_drop(self.conn, DROP, "Drop One", now=T0)
        log.clear()
        outbox.reset_provider_breakers()

    def test_unsubscribe_before_due_cancels_the_queued_row_never_sent(self):
        row = core.subscribe(self.conn, DROP, "telegram", "free-addr", "free", T0)
        verified = core.verify(self.conn, row["id"], row["verify_code"], T0)
        sub_id = verified["id"]

        core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "new_showtime", "unit_id": "unit-1",
            "state": "available",
        }, now=T0)
        # the free tier's 60s embargo means this row isn't due yet
        queued = outbox_row(self.conn, sub_id)
        self.assertEqual(queued["status"], "pending")

        core.unsubscribe(self.conn, verified["unsub_token"], T0 + 1)

        channel = AlwaysOk()
        outbox.dispatch(self.conn, {"telegram": channel}, now=T0 + 61)

        row_after = outbox_row(self.conn, sub_id)
        self.assertEqual(row_after["status"], "cancelled")
        self.assertEqual(channel.calls, [], "a cancelled row must never be sent")

        cancelled_logs = [e for e in log.recent(tag=log.TAG_DELIVERY)
                          if e.fields.get("cancelled") == 1]
        self.assertEqual(len(cancelled_logs), 1)
        self.assertEqual(cancelled_logs[0].fields["subscription_id"], sub_id)

    def test_active_subscriber_in_same_dispatch_is_unaffected_by_a_cancellation(self):
        row_a = core.subscribe(self.conn, DROP, "telegram", "a", "paid", T0)
        verified_a = core.verify(self.conn, row_a["id"], row_a["verify_code"], T0)
        row_b = core.subscribe(self.conn, DROP, "telegram", "b", "paid", T0)
        verified_b = core.verify(self.conn, row_b["id"], row_b["verify_code"], T0)

        core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "new_showtime", "unit_id": "unit-1",
            "state": "available",
        }, now=T0)

        core.unsubscribe(self.conn, verified_b["unsub_token"], T0)

        channel = AlwaysOk()
        outbox.dispatch(self.conn, {"telegram": channel}, now=T0)

        self.assertEqual(outbox_row(self.conn, verified_a["id"])["status"], "delivered")
        self.assertEqual(outbox_row(self.conn, verified_b["id"])["status"], "cancelled")


class ZeroDeliveredTimeBasedTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        core.register_drop(self.conn, DROP, "Drop One", now=T0)
        log.clear()
        outbox.reset_provider_breakers()

    def _fire_event(self, event_id="evt-1", now=T0):
        core.record_event(self.conn, DROP, {
            "event_id": event_id, "kind": "new_showtime", "unit_id": "unit-1",
            "state": "available",
        }, now=now)

    def test_no_fire_before_deadline_even_with_all_rows_failed(self):
        active_sub(self.conn, DROP, "telegram", "a", "paid", T0)
        active_sub(self.conn, DROP, "discord", "b", "free", T0)
        self._fire_event()

        bad_tg, bad_dc = AlwaysFail(), AlwaysFail()
        # right up to (but not reaching) the deadline
        outbox.dispatch(self.conn, {"telegram": bad_tg, "discord": bad_dc}, now=T0)
        outbox.dispatch(self.conn, {"telegram": bad_tg, "discord": bad_dc},
                         now=T0 + outbox.ZERO_DELIVERED_DEADLINE_SECONDS - 1)

        self.assertEqual(log.recent(tag=log.TAG_ZERO_DELIVERED), [])

    def test_fires_exactly_at_deadline(self):
        active_sub(self.conn, DROP, "telegram", "a", "paid", T0)
        active_sub(self.conn, DROP, "discord", "b", "free", T0)
        self._fire_event()

        bad_tg, bad_dc = AlwaysFail(), AlwaysFail()
        outbox.dispatch(self.conn, {"telegram": bad_tg, "discord": bad_dc}, now=T0)
        outbox.dispatch(self.conn, {"telegram": bad_tg, "discord": bad_dc},
                         now=T0 + outbox.ZERO_DELIVERED_DEADLINE_SECONDS)

        entries = log.recent(tag=log.TAG_ZERO_DELIVERED)
        self.assertEqual(len(entries), 1)
        self.assertIn("evt-1", entries[0].line)
        self.assertIn(DROP, entries[0].line)

    def test_fires_even_when_the_deadline_pass_touches_no_due_rows(self):
        # backoff can push next_attempt_at well past the deadline; the
        # alarm must still fire on time-based grounds alone, not "some
        # row happened to be attempted this pass".
        sub_id = active_sub(self.conn, DROP, "telegram", "a", "paid", T0)
        self._fire_event()

        bad_tg = AlwaysFail()
        outbox.dispatch(self.conn, {"telegram": bad_tg}, now=T0)          # attempt 1, backoff 60s -> due at T0+60
        outbox.dispatch(self.conn, {"telegram": bad_tg}, now=T0 + 60)     # attempt 2, backoff 120s -> due at T0+180
        outbox.dispatch(self.conn, {"telegram": bad_tg}, now=T0 + 180)    # attempt 3, backoff 240s -> due at T0+420

        row = outbox_row(self.conn, sub_id)
        self.assertGreater(row["next_attempt_at"], T0 + outbox.ZERO_DELIVERED_DEADLINE_SECONDS)

        # dispatch at exactly the deadline: this row isn't due for retry
        # (next_attempt_at is T0+420 > T0+300) so the send loop touches
        # nothing, yet the alarm must still fire on time alone.
        outbox.dispatch(self.conn, {"telegram": bad_tg},
                         now=T0 + outbox.ZERO_DELIVERED_DEADLINE_SECONDS)
        self.assertEqual(len(log.recent(tag=log.TAG_ZERO_DELIVERED)), 1)

    def test_no_refire_on_later_passes(self):
        active_sub(self.conn, DROP, "telegram", "a", "paid", T0)
        self._fire_event()

        bad_tg = AlwaysFail()
        outbox.dispatch(self.conn, {"telegram": bad_tg}, now=T0)
        outbox.dispatch(self.conn, {"telegram": bad_tg},
                         now=T0 + outbox.ZERO_DELIVERED_DEADLINE_SECONDS)
        outbox.dispatch(self.conn, {"telegram": bad_tg},
                         now=T0 + outbox.ZERO_DELIVERED_DEADLINE_SECONDS + 500)

        self.assertEqual(len(log.recent(tag=log.TAG_ZERO_DELIVERED)), 1)

    def test_recovery_after_alarm_logs_exactly_one_delivery_line(self):
        active_sub(self.conn, DROP, "telegram", "a", "paid", T0)
        self._fire_event()

        toggle = Toggle()
        outbox.dispatch(self.conn, {"telegram": toggle}, now=T0)
        outbox.dispatch(self.conn, {"telegram": toggle},
                         now=T0 + outbox.ZERO_DELIVERED_DEADLINE_SECONDS)
        self.assertEqual(len(log.recent(tag=log.TAG_ZERO_DELIVERED)), 1)

        toggle.mode = "ok"
        far = T0 + outbox.ZERO_DELIVERED_DEADLINE_SECONDS + 3600
        outbox.dispatch(self.conn, {"telegram": toggle}, now=far)

        recovered = [e for e in log.recent(tag=log.TAG_DELIVERY)
                     if e.fields.get("recovered_after_zero_delivered") == 1]
        self.assertEqual(len(recovered), 1)

        # and it must never fire again, nor log a second recovery line,
        # on subsequent passes
        outbox.dispatch(self.conn, {"telegram": toggle}, now=far + 1000)
        self.assertEqual(len(log.recent(tag=log.TAG_ZERO_DELIVERED)), 1)
        recovered_after = [e for e in log.recent(tag=log.TAG_DELIVERY)
                            if e.fields.get("recovered_after_zero_delivered") == 1]
        self.assertEqual(len(recovered_after), 1)

    def test_cancelled_only_event_never_fires(self):
        row = core.subscribe(self.conn, DROP, "telegram", "a", "paid", T0)
        verified = core.verify(self.conn, row["id"], row["verify_code"], T0)
        self._fire_event()

        core.unsubscribe(self.conn, verified["unsub_token"], T0)

        channel = AlwaysOk()
        far = T0 + outbox.ZERO_DELIVERED_DEADLINE_SECONDS + 1000
        outbox.dispatch(self.conn, {"telegram": channel}, now=far)

        self.assertEqual(outbox_row(self.conn, verified["id"])["status"], "cancelled")
        self.assertEqual(log.recent(tag=log.TAG_ZERO_DELIVERED), [])

    def test_no_subscribers_never_fires(self):
        # no subscriptions at all -- record_event() creates zero outbox
        # rows, so there's nothing to declare "zero delivered" against.
        self._fire_event()
        far = T0 + outbox.ZERO_DELIVERED_DEADLINE_SECONDS + 10
        outbox.dispatch(self.conn, {}, now=far)
        self.assertEqual(log.recent(tag=log.TAG_ZERO_DELIVERED), [])


class PIINeverLoggedTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        core.register_drop(self.conn, DROP, "Drop One", now=T0)
        log.clear()
        outbox.reset_provider_breakers()

    def test_address_never_appears_in_any_log_line(self):
        secret_address = "brian-super-secret-address@example.com"
        row = core.subscribe(self.conn, DROP, "telegram", secret_address, "paid", T0)
        verified = core.verify(self.conn, row["id"], row["verify_code"], T0)

        core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "new_showtime", "unit_id": "unit-1",
            "state": "available",
        }, now=T0)

        bad_channel = AlwaysFail()
        outbox.dispatch(self.conn, {"telegram": bad_channel}, now=T0)

        for entry in log.recent():
            self.assertNotIn(secret_address, entry.line)
            self.assertNotIn(row["verify_code"], entry.line)
            self.assertNotIn(verified["unsub_token"], entry.line)


class ClaimTests(unittest.TestCase):
    """Regression for the concurrent-dispatch double-send hazard (audit
    P0-3): a row must be atomically claimed before its channel is
    touched, so overlapping dispatch passes — the multi-worker analog of
    sentry.py's flock — can never both send the same alert."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        core.register_drop(self.conn, DROP, "Drop One", now=T0)
        log.clear()
        outbox.reset_provider_breakers()

    def _one_due_row(self):
        sub = active_sub(self.conn, DROP, "telegram", "a", "paid", T0)
        core.record_event(
            self.conn, DROP,
            {"event_id": "evt-1", "kind": "restock", "unit_id": "u1"},
            now=T0)
        return outbox_row(self.conn, sub)

    def test_claim_is_exclusive(self):
        row = self._one_due_row()
        first = self.conn.execute(
            "UPDATE outbox SET status='in_flight', claimed_at=? "
            "WHERE id=? AND (status IN ('pending','failed') "
            "OR (status='in_flight' AND claimed_at <= ?))",
            (T0, row["id"], T0 - outbox.CLAIM_LEASE_SECONDS))
        second = self.conn.execute(
            "UPDATE outbox SET status='in_flight', claimed_at=? "
            "WHERE id=? AND (status IN ('pending','failed') "
            "OR (status='in_flight' AND claimed_at <= ?))",
            (T0, row["id"], T0 - outbox.CLAIM_LEASE_SECONDS))
        self.assertEqual(first.rowcount, 1)
        self.assertEqual(second.rowcount, 0)

    def test_concurrent_dispatch_sends_each_row_exactly_once(self):
        import tempfile
        import threading

        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        os.unlink(path)
        self.addCleanup(lambda: [
            os.path.exists(path + sfx) and os.unlink(path + sfx)
            for sfx in ("", "-wal", "-shm")])

        conn = db.connect(path)
        core.register_drop(conn, DROP, "Drop One", now=T0)
        for i in range(50):
            active_sub(conn, DROP, "telegram", "addr-%d" % i, "paid", T0)
        core.record_event(
            conn, DROP,
            {"event_id": "evt-1", "kind": "restock", "unit_id": "u1"},
            now=T0)
        conn.close()

        send_counts = {}
        count_lock = threading.Lock()

        class CountingChannel:
            def send(self, address, message):
                with count_lock:
                    send_counts[address] = send_counts.get(address, 0) + 1
                return True

        errors = []
        barrier = threading.Barrier(4)

        def worker():
            wconn = db.connect(path)
            try:
                barrier.wait()
                outbox.dispatch(wconn, {"telegram": CountingChannel()},
                                now=T0 + 1)
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                wconn.close()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(send_counts), 50)
        self.assertTrue(all(c == 1 for c in send_counts.values()),
                        "double-sent rows: %r" % {
                            a: c for a, c in send_counts.items() if c != 1})

        vconn = db.connect(path)
        try:
            delivered = vconn.execute(
                "SELECT COUNT(*) AS n FROM outbox WHERE status='delivered'"
            ).fetchone()["n"]
        finally:
            vconn.close()
        self.assertEqual(delivered, 50)

    def test_stale_in_flight_row_is_reclaimed_after_lease(self):
        row = self._one_due_row()
        stale_claim = T0 - outbox.CLAIM_LEASE_SECONDS - 1
        self.conn.execute(
            "UPDATE outbox SET status='in_flight', claimed_at=? WHERE id=?",
            (stale_claim, row["id"]))
        channel = AlwaysOk()
        outbox.dispatch(self.conn, {"telegram": channel}, now=T0 + 1)
        self.assertEqual(len(channel.calls), 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM outbox WHERE id=?", (row["id"],)
            ).fetchone()["status"],
            "delivered")

    def test_fresh_in_flight_row_is_not_stolen(self):
        row = self._one_due_row()
        self.conn.execute(
            "UPDATE outbox SET status='in_flight', claimed_at=? WHERE id=?",
            (T0 + 0.5, row["id"]))
        channel = AlwaysOk()
        outbox.dispatch(self.conn, {"telegram": channel}, now=T0 + 1)
        self.assertEqual(channel.calls, [])


class AlertCopyFormatTests(unittest.TestCase):
    """Pins the human alert-copy format: "<drop title>: <kind> — <unit
    label>"."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        core.register_drop(self.conn, DROP, "Drop One", now=T0)
        log.clear()
        outbox.reset_provider_breakers()

    def test_alert_copy_format_pinned(self):
        active_sub(self.conn, DROP, "telegram", "a", "paid", T0)
        core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "new_showtime", "unit_id": "unit-1",
            "state": "available", "payload": {"label": "7:00 PM"},
        }, now=T0)

        channel = AlwaysOk()
        outbox.dispatch(self.conn, {"telegram": channel}, now=T0)

        self.assertEqual(channel.calls[0][1], "Drop One: new_showtime — 7:00 PM")

    def test_alert_copy_falls_back_to_unit_id_without_a_label(self):
        active_sub(self.conn, DROP, "telegram", "a", "paid", T0)
        core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "restock", "unit_id": "unit-9",
        }, now=T0)

        channel = AlwaysOk()
        outbox.dispatch(self.conn, {"telegram": channel}, now=T0)

        self.assertEqual(channel.calls[0][1], "Drop One: restock — unit-9")


class CapturingChannel:
    """Records every send() call including its context= kwarg."""

    def __init__(self):
        self.calls = []  # list of (address, message, context)

    def send(self, address, message, context=None):
        self.calls.append((address, message, context))
        return True


class GoneOnceChannel:
    """Always raises the given exception (never succeeds)."""

    def __init__(self, exc):
        self.exc = exc
        self.calls = []

    def send(self, address, message, context=None):
        self.calls.append((address, message, context))
        raise self.exc


class PermanentFlagTests(unittest.TestCase):
    """The engine's side of the "gone for good" contract: any exception
    whose class sets permanent = True, with no notify/ import."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        core.register_drop(self.conn, DROP, "Drop One", now=T0)
        log.clear()
        outbox.reset_provider_breakers()

    def test_permanent_flag_exhausts_and_disables(self):
        class Gone(Exception):
            permanent = True

        sub_id = active_sub(self.conn, DROP, "discord", "u1", "paid", T0)
        core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "restock", "unit_id": "unit-1"}, now=T0)
        outbox.dispatch(self.conn, {"discord": GoneOnceChannel(Gone())}, now=T0)
        self.assertEqual(outbox_row(self.conn, sub_id)["status"], "exhausted")
        self.assertEqual(core.get_subscription(self.conn, sub_id)["status"],
                         "disabled")

    def test_truthy_but_not_true_flag_is_an_ordinary_failure(self):
        class Flaky(Exception):
            permanent = "yes"

        sub_id = active_sub(self.conn, DROP, "discord", "u1", "paid", T0)
        core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "restock", "unit_id": "unit-1"}, now=T0)
        outbox.dispatch(self.conn, {"discord": GoneOnceChannel(Flaky())}, now=T0)
        self.assertEqual(outbox_row(self.conn, sub_id)["status"], "failed")


if __name__ == "__main__":
    unittest.main()
