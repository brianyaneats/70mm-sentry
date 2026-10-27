"""engine.outbox x notify: dispatch against the real notify exception
classes (WebhookGone, PermanentDeliveryError, RateLimitedError) and the
webhook context= seam. Lives in app/ because the app is the only layer
that wires the two together; the engine side alone is covered in
engine/tests/test_outbox.py."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from engine import core, db, log, outbox  # noqa: E402
from engine.tests.test_outbox import (  # noqa: E402
    DROP, T0, AlwaysFail, CapturingChannel, GoneOnceChannel, active_sub,
    outbox_row)
from notify import channels, webhook  # noqa: E402


class WebhookChannelDispatchTests(unittest.TestCase):
    """channel="webhook" rows get a context= kwarg -- see outbox's
    module docstring."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        core.register_drop(self.conn, DROP, "Drop One", now=T0)
        log.clear()
        outbox.reset_provider_breakers()

    def _webhook_sub(self, address, secret):
        row = core.subscribe(self.conn, DROP, "webhook", address, "paid", T0,
                              webhook_secret=secret)
        verified = core.verify(self.conn, row["id"], row["verify_code"], T0)
        return verified["id"]

    def test_webhook_send_receives_context_with_secret_and_event_fields(self):
        sub_id = self._webhook_sub("https://good.example.com/hook", "topsecret123")
        core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "new_showtime", "unit_id": "unit-1",
            "state": "available", "payload": {"label": "7:00 PM"},
        }, now=T0)

        channel = CapturingChannel()
        outbox.dispatch(self.conn, {"webhook": channel}, now=T0)

        self.assertEqual(outbox_row(self.conn, sub_id)["status"], "delivered")
        self.assertEqual(len(channel.calls), 1)
        address, message, context = channel.calls[0]
        self.assertEqual(address, "https://good.example.com/hook")
        self.assertEqual(message, "Drop One: new_showtime — 7:00 PM")
        self.assertEqual(context["webhook_secret"], "topsecret123")
        self.assertEqual(context["event_id"], "evt-1")
        self.assertEqual(context["kind"], "new_showtime")
        self.assertEqual(context["drop_id"], DROP)
        self.assertEqual(context["unit_id"], "unit-1")

    def test_webhook_secret_never_appears_in_any_log_line(self):
        sub_id = self._webhook_sub("https://flaky.example.com/hook", "s3cr3t-value")
        core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "restock", "unit_id": "unit-1",
        }, now=T0)

        outbox.dispatch(self.conn, {"webhook": GoneOnceChannel(RuntimeError("timeout"))},
                         now=T0)

        for entry in log.recent():
            self.assertNotIn("s3cr3t-value", entry.line)

    def test_webhook_gone_maps_to_exhausted_with_gone_log_and_no_retry(self):
        sub_id = self._webhook_sub("https://gone.example.com/hook", "s3cr3t")
        core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "new_showtime", "unit_id": "unit-1",
        }, now=T0)

        channel = GoneOnceChannel(webhook.WebhookGone())
        outbox.dispatch(self.conn, {"webhook": channel}, now=T0)

        row = outbox_row(self.conn, sub_id)
        self.assertEqual(row["status"], "exhausted")
        self.assertEqual(len(channel.calls), 1)

        gone_logs = [e for e in log.recent(tag=log.TAG_DELIVERY)
                     if e.fields.get("gone") == 1]
        self.assertEqual(len(gone_logs), 1)
        self.assertEqual(gone_logs[0].fields["subscription_id"], sub_id)
        self.assertEqual(gone_logs[0].fields["channel"], "webhook")

        # no retry storm: a much later dispatch pass must not call send()
        # again -- the row is terminal, not merely backed off.
        outbox.dispatch(self.conn, {"webhook": channel}, now=T0 + 100000)
        self.assertEqual(len(channel.calls), 1)

    def test_webhook_gone_does_not_trip_the_provider_breaker(self):
        subs = [self._webhook_sub("https://gone-%d.example.com/hook" % i, "s")
                for i in range(6)]
        core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "restock", "unit_id": "unit-1",
        }, now=T0)

        channel = GoneOnceChannel(webhook.WebhookGone())
        outbox.dispatch(self.conn, {"webhook": channel}, now=T0)

        # all 6 rows attempted (unlike a real provider failure, which
        # would trip the breaker after 5 and skip the 6th) -- a deleted
        # endpoint says nothing about the provider being down.
        self.assertEqual(len(channel.calls), 6)
        statuses = [outbox_row(self.conn, s)["status"] for s in subs]
        self.assertEqual(statuses.count("exhausted"), 6)

    def test_webhook_delivery_error_uses_standard_backoff_not_exhausted(self):
        sub_id = self._webhook_sub("https://flaky.example.com/hook", "s3cr3t")
        core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "restock", "unit_id": "unit-1",
        }, now=T0)

        channel = GoneOnceChannel(webhook.WebhookDeliveryError(503))
        outbox.dispatch(self.conn, {"webhook": channel}, now=T0)

        row = outbox_row(self.conn, sub_id)
        self.assertEqual(row["status"], "failed")
        self.assertGreater(row["next_attempt_at"], T0)


class ChannelsPermanentErrorDispatchTests(unittest.TestCase):
    """notify.channels.PermanentDeliveryError (Discord's 404/410, ntfy's
    equivalent should it ever grow one) is treated exactly like
    webhook.WebhookGone by dispatch() -- see outbox's module docstring.
    """

    def setUp(self):
        self.conn = db.connect(":memory:")
        core.register_drop(self.conn, DROP, "Drop One", now=T0)
        log.clear()
        outbox.reset_provider_breakers()

    def test_permanent_error_exhausts_disables_subscription_and_logs_gone(self):
        sub_id = active_sub(self.conn, DROP, "discord", "https://discord.com/x", "paid", T0)
        core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "new_showtime", "unit_id": "unit-1",
        }, now=T0)

        chan = GoneOnceChannel(channels.PermanentDeliveryError(410))
        outbox.dispatch(self.conn, {"discord": chan}, now=T0)

        row = outbox_row(self.conn, sub_id)
        self.assertEqual(row["status"], "exhausted")

        sub_row = self.conn.execute(
            "SELECT status FROM subscriptions WHERE id=?", (sub_id,)
        ).fetchone()
        self.assertEqual(sub_row["status"], "disabled")

        gone_logs = [e for e in log.recent(tag=log.TAG_DELIVERY)
                     if e.fields.get("gone") == 1]
        self.assertEqual(len(gone_logs), 1)
        self.assertEqual(gone_logs[0].fields["subscription_id"], sub_id)
        self.assertEqual(gone_logs[0].fields["channel"], "discord")

        # no retry storm: a much later pass never calls send() again.
        outbox.dispatch(self.conn, {"discord": chan}, now=T0 + 100000)
        self.assertEqual(len(chan.calls), 1)

    def test_permanent_error_does_not_trip_provider_breaker(self):
        """A second subscriber on the same channel must still be attempted
        in the same pass -- the breaker only trips on real provider
        failures, and a deleted destination isn't one."""
        gone_sub = active_sub(self.conn, DROP, "discord", "https://discord.com/gone", "paid", T0)
        ok_sub = active_sub(self.conn, DROP, "discord", "https://discord.com/ok", "paid", T0)
        core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "restock", "unit_id": "unit-1",
        }, now=T0)

        class MixedChannel:
            def __init__(self):
                self.calls = []

            def send(self, address, message, context=None):
                self.calls.append(address)
                if address == "https://discord.com/gone":
                    raise channels.PermanentDeliveryError(404)
                return True

        chan = MixedChannel()
        outbox.dispatch(self.conn, {"discord": chan}, now=T0)

        self.assertEqual(len(chan.calls), 2)  # both rows attempted
        self.assertEqual(outbox_row(self.conn, gone_sub)["status"], "exhausted")
        self.assertEqual(outbox_row(self.conn, ok_sub)["status"], "delivered")

    def test_permanent_error_address_never_appears_in_any_log_line(self):
        secret_address = "https://discord.com/api/webhooks/super-secret-path"
        sub_id = active_sub(self.conn, DROP, "discord", secret_address, "paid", T0)
        core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "restock", "unit_id": "unit-1",
        }, now=T0)

        outbox.dispatch(
            self.conn, {"discord": GoneOnceChannel(channels.PermanentDeliveryError(404))},
            now=T0)

        for entry in log.recent():
            self.assertNotIn(secret_address, entry.line)
            self.assertNotIn("super-secret-path", entry.line)

    def test_rate_limited_error_is_transient_backoff_not_exhausted(self):
        sub_id = active_sub(self.conn, DROP, "discord", "https://discord.com/flaky", "paid", T0)
        core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "restock", "unit_id": "unit-1",
        }, now=T0)

        chan = GoneOnceChannel(channels.RateLimitedError(429, retry_after=30))
        outbox.dispatch(self.conn, {"discord": chan}, now=T0)

        row = outbox_row(self.conn, sub_id)
        self.assertEqual(row["status"], "failed")
        self.assertGreater(row["next_attempt_at"], T0)

        sub_row = self.conn.execute(
            "SELECT status FROM subscriptions WHERE id=?", (sub_id,)
        ).fetchone()
        self.assertEqual(sub_row["status"], "active")  # never disabled -- transient

        # still retryable later: a due pass calls send() again.
        outbox.dispatch(self.conn, {"discord": chan}, now=row["next_attempt_at"] + 1)
        self.assertEqual(len(chan.calls), 2)


class RetryAfterBackoffTests(unittest.TestCase):
    """A 429 that carries retry_after is the provider naming the moment it
    will accept traffic again. Ignoring it in favour of our own doubling
    curve is how one rate-limit becomes a rolling one -- so an explicit
    hint wins, clamped to the same cap that bounds the default curve."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        core.register_drop(self.conn, DROP, "Drop One", now=T0)
        log.clear()
        outbox.reset_provider_breakers()

    def _row_after_failure(self, exc):
        sub_id = active_sub(self.conn, DROP, "ntfy", "topic-a", "paid", T0)
        core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "restock", "unit_id": "u1",
        }, now=T0)
        outbox.dispatch(self.conn, {"ntfy": AlwaysFail(lambda: exc)}, now=T0)
        return outbox_row(self.conn, sub_id)

    def test_retry_after_hint_sets_next_attempt(self):
        row = self._row_after_failure(
            channels.RateLimitedError(429, retry_after=17))
        self.assertEqual(row["status"], "failed")
        self.assertAlmostEqual(row["next_attempt_at"], T0 + 17, places=3)

    def test_absent_hint_falls_back_to_exponential_backoff(self):
        row = self._row_after_failure(channels.RateLimitedError(429))
        expected = outbox.BACKOFF_BASE_SECONDS * 2  # first attempt
        self.assertAlmostEqual(row["next_attempt_at"], T0 + expected, places=3)

    def test_absurd_hint_is_clamped_to_the_cap(self):
        row = self._row_after_failure(
            channels.RateLimitedError(429, retry_after=999999))
        self.assertAlmostEqual(
            row["next_attempt_at"], T0 + outbox.BACKOFF_CAP_SECONDS, places=3)


if __name__ == "__main__":
    unittest.main()
