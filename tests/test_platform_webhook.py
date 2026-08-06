"""Unit tests for platformd.webhook -- the signed-WEBHOOK channel.

http_post is always a fake here; no real sockets, no urllib. Every test
either drives WebhookChannel with an injected callable or exercises the
pure functions (is_safe_url, sign, build_event_payload) directly.
"""

import hashlib
import hmac
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from platformd import webhook  # noqa: E402


SECRET = "s3cr3t-do-not-log-me"


class FakePost:
    """Records every call and returns a scripted status (or raises)."""

    def __init__(self, status=200, raises=None):
        self.status = status
        self.raises = raises
        self.calls = []

    def __call__(self, url, body, headers):
        self.calls.append((url, body, headers))
        if self.raises is not None:
            raise self.raises
        return self.status


def make_context(**overrides):
    ctx = {
        "webhook_secret": SECRET,
        "drop_id": "drop-1",
        "event_id": "evt-1",
        "kind": "new_showtime",
        "unit_id": "unit-1",
        "detected_at": 1_700_000_000.0,
    }
    ctx.update(overrides)
    return ctx


# ---------- signing ----------

class SignTests(unittest.TestCase):
    def test_round_trips_with_hmac_compare_digest(self):
        body = b'{"hello":"world"}'
        digest = webhook.sign(body, SECRET)
        expected = hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
        self.assertTrue(hmac.compare_digest(digest, expected))

    def test_wrong_secret_does_not_verify(self):
        body = b'{"hello":"world"}'
        digest = webhook.sign(body, SECRET)
        other = hmac.new(b"different-secret", body, hashlib.sha256).hexdigest()
        self.assertFalse(hmac.compare_digest(digest, other))

    def test_accepts_bytes_secret_too(self):
        body = b"payload"
        as_str = webhook.sign(body, SECRET)
        as_bytes = webhook.sign(body, SECRET.encode("utf-8"))
        self.assertEqual(as_str, as_bytes)


class GenerateSecretTests(unittest.TestCase):
    def test_is_hex_and_reasonably_long(self):
        secret = webhook.generate_secret()
        self.assertEqual(len(secret), 64)  # token_hex(32) -> 64 hex chars
        int(secret, 16)  # raises if not valid hex

    def test_two_calls_differ(self):
        self.assertNotEqual(webhook.generate_secret(), webhook.generate_secret())


# ---------- payload schema ----------

class BuildEventPayloadTests(unittest.TestCase):
    def test_exact_key_set(self):
        row = {
            "drop_id": "drop-1", "event_id": "evt-1", "kind": "restock",
            "unit_id": "unit-1", "detected_at": 123.0, "message": "seat opened",
        }
        payload = webhook.build_event_payload(row)
        self.assertEqual(set(payload.keys()), webhook.EVENT_PAYLOAD_KEYS)
        self.assertEqual(
            set(payload.keys()),
            {"schema", "drop_id", "event_id", "kind", "unit_id", "detected_at", "message"},
        )

    def test_schema_value_is_stable(self):
        payload = webhook.build_event_payload({"event_id": "e"})
        self.assertEqual(payload["schema"], "dropwatch.event.v1")

    def test_falls_back_from_detected_at_to_recorded_at(self):
        row = {"event_id": "e", "recorded_at": 999.0}
        payload = webhook.build_event_payload(row)
        self.assertEqual(payload["detected_at"], 999.0)

    def test_detected_at_wins_over_recorded_at_when_both_present(self):
        row = {"event_id": "e", "detected_at": 111.0, "recorded_at": 999.0}
        payload = webhook.build_event_payload(row)
        self.assertEqual(payload["detected_at"], 111.0)

    def test_missing_fields_become_none_not_keyerror(self):
        payload = webhook.build_event_payload({})
        self.assertIsNone(payload["drop_id"])
        self.assertIsNone(payload["event_id"])
        self.assertIsNone(payload["detected_at"])

    def test_message_defaults_to_empty_string(self):
        payload = webhook.build_event_payload({"event_id": "e"})
        self.assertEqual(payload["message"], "")

    def test_is_json_serializable(self):
        row = make_context()
        json.dumps(webhook.build_event_payload(row))  # must not raise


# ---------- is_safe_url ----------

class IsSafeUrlTests(unittest.TestCase):
    def test_https_ok(self):
        ok, reason = webhook.is_safe_url("https://example.com/hooks/abc")
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_http_rejected(self):
        ok, reason = webhook.is_safe_url("http://example.com/hook")
        self.assertFalse(ok)
        self.assertTrue(reason)

    def test_userinfo_rejected(self):
        ok, reason = webhook.is_safe_url("https://user@example.com/hook")
        self.assertFalse(ok)
        self.assertTrue(reason)

    def test_userinfo_with_password_rejected(self):
        ok, reason = webhook.is_safe_url("https://user:pass@example.com/hook")
        self.assertFalse(ok)
        self.assertTrue(reason)

    def test_loopback_v4_rejected(self):
        ok, reason = webhook.is_safe_url("https://127.0.0.1/hook")
        self.assertFalse(ok)

    def test_rfc1918_rejected(self):
        ok, reason = webhook.is_safe_url("https://10.1.2.3/hook")
        self.assertFalse(ok)

    def test_metadata_ip_rejected(self):
        ok, reason = webhook.is_safe_url("https://169.254.169.254/latest/meta-data")
        self.assertFalse(ok)

    def test_loopback_v6_rejected(self):
        ok, reason = webhook.is_safe_url("https://[::1]/hook")
        self.assertFalse(ok)

    def test_unspecified_v4_rejected(self):
        ok, reason = webhook.is_safe_url("https://0.0.0.0/hook")
        self.assertFalse(ok)

    def test_public_ip_literal_is_allowed(self):
        # A literal public IP is a legitimate (if unusual) https webhook
        # target -- only private/reserved/loopback/link-local ranges are
        # rejected by this request-shape gate.
        ok, reason = webhook.is_safe_url("https://8.8.8.8/hook")
        self.assertTrue(ok)

    def test_too_long_rejected(self):
        url = "https://example.com/" + ("a" * 600)
        ok, reason = webhook.is_safe_url(url)
        self.assertFalse(ok)

    def test_non_string_rejected(self):
        ok, reason = webhook.is_safe_url(None)
        self.assertFalse(ok)

    def test_reason_never_echoes_the_raw_url(self):
        url = "https://127.0.0.1/super-secret-path?token=abc123"
        ok, reason = webhook.is_safe_url(url)
        self.assertFalse(ok)
        self.assertNotIn("127.0.0.1", reason)
        self.assertNotIn("super-secret-path", reason)
        self.assertNotIn("abc123", reason)


# ---------- WebhookChannel.send ----------

class WebhookChannelSendTests(unittest.TestCase):
    def test_2xx_returns_true(self):
        post = FakePost(status=200)
        chan = webhook.WebhookChannel(post)
        result = chan.send("https://example.com/hook", "seat opened", make_context())
        self.assertTrue(result is True)
        self.assertEqual(len(post.calls), 1)

    def test_204_also_returns_true(self):
        post = FakePost(status=204)
        chan = webhook.WebhookChannel(post)
        self.assertTrue(chan.send("https://example.com/hook", "m", make_context()) is True)

    def test_410_raises_webhook_gone(self):
        post = FakePost(status=410)
        chan = webhook.WebhookChannel(post)
        with self.assertRaises(webhook.WebhookGone):
            chan.send("https://example.com/hook", "m", make_context())

    def test_500_raises_webhook_delivery_error_with_status(self):
        post = FakePost(status=500)
        chan = webhook.WebhookChannel(post)
        with self.assertRaises(webhook.WebhookDeliveryError) as ctx:
            chan.send("https://example.com/hook", "m", make_context())
        self.assertEqual(ctx.exception.status, 500)

    def test_403_also_raises_webhook_delivery_error(self):
        post = FakePost(status=403)
        chan = webhook.WebhookChannel(post)
        with self.assertRaises(webhook.WebhookDeliveryError):
            chan.send("https://example.com/hook", "m", make_context())

    def test_broken_pipe_propagates_untouched(self):
        post = FakePost(raises=BrokenPipeError("pipe gone"))
        chan = webhook.WebhookChannel(post)
        with self.assertRaises(BrokenPipeError):
            chan.send("https://example.com/hook", "m", make_context())

    def test_missing_secret_raises_without_calling_transport(self):
        post = FakePost(status=200)
        chan = webhook.WebhookChannel(post)
        with self.assertRaises(ValueError):
            chan.send("https://example.com/hook", "m", context={"event_id": "e"})
        self.assertEqual(len(post.calls), 0)

    def test_none_context_raises_value_error(self):
        post = FakePost(status=200)
        chan = webhook.WebhookChannel(post)
        with self.assertRaises(ValueError):
            chan.send("https://example.com/hook", "m", context=None)

    def test_context_without_event_id_falls_back_to_minimal_payload(self):
        post = FakePost(status=200)
        chan = webhook.WebhookChannel(post)
        chan.send("https://example.com/hook", "verification ping",
                   context={"webhook_secret": SECRET})
        _, body, _ = post.calls[0]
        payload = json.loads(body)
        self.assertEqual(payload, {"schema": "dropwatch.event.v1",
                                    "message": "verification ping"})

    def test_signature_header_verifies_round_trip(self):
        post = FakePost(status=200)
        chan = webhook.WebhookChannel(post)
        chan.send("https://example.com/hook", "seat opened", make_context())
        _, body, headers = post.calls[0]
        sig_header = headers["X-Dropwatch-Signature"]
        self.assertTrue(sig_header.startswith("sha256="))
        digest = sig_header.split("=", 1)[1]
        expected = hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
        self.assertTrue(hmac.compare_digest(digest, expected))

    def test_headers_include_event_id_and_content_type(self):
        post = FakePost(status=200)
        chan = webhook.WebhookChannel(post)
        chan.send("https://example.com/hook", "m", make_context(event_id="evt-42"))
        _, _, headers = post.calls[0]
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["X-Dropwatch-Event"], "evt-42")

    def test_body_matches_build_event_payload(self):
        post = FakePost(status=200)
        chan = webhook.WebhookChannel(post)
        ctx = make_context()
        chan.send("https://example.com/hook", "seat opened", ctx)
        _, body, _ = post.calls[0]
        payload = json.loads(body)
        expected = webhook.build_event_payload(dict(ctx, message="seat opened"))
        self.assertEqual(payload, expected)

    def test_secret_never_appears_in_exception_text(self):
        for status in (410, 500, 403):
            post = FakePost(status=status)
            chan = webhook.WebhookChannel(post)
            try:
                chan.send("https://example.com/hook", "m", make_context())
            except webhook.WebhookDeliveryError as exc:
                self.assertNotIn(SECRET, str(exc))
            else:
                self.fail("expected WebhookDeliveryError for status %r" % status)

        # And the missing-secret ValueError itself must not echo the
        # (absent) secret either.
        chan = webhook.WebhookChannel(FakePost(status=200))
        try:
            chan.send("https://example.com/hook", "m", context={"event_id": "e"})
        except ValueError as exc:
            self.assertNotIn(SECRET, str(exc))


# ---------- WebhookGone / WebhookDeliveryError shape ----------

class ExceptionShapeTests(unittest.TestCase):
    def test_webhook_gone_is_a_webhook_delivery_error(self):
        self.assertTrue(issubclass(webhook.WebhookGone, webhook.WebhookDeliveryError))

    def test_webhook_gone_status_is_410(self):
        exc = webhook.WebhookGone()
        self.assertEqual(exc.status, 410)

    def test_delivery_error_carries_arbitrary_status(self):
        exc = webhook.WebhookDeliveryError(503)
        self.assertEqual(exc.status, 503)


if __name__ == "__main__":
    unittest.main()
