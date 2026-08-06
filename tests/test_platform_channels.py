"""Unit tests for platformd.channels -- the real Discord and ntfy
delivery channels.

http_post is always a fake here; no real sockets, no urllib. Every test
drives DiscordWebhookChannel/NtfyChannel with an injected callable that
returns (status, response_body), the contract documented in the module
docstring (a deliberate deviation from platformd.webhook's bare-int
return -- see there for why).
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from platformd import channels  # noqa: E402


DISCORD_URL = "https://discord.com/api/webhooks/123456789012345678/some-token_here.123"


class FakePost:
    """Records every call and returns a scripted (status, body) (or
    raises)."""

    def __init__(self, status=200, body=b"", raises=None):
        self.status = status
        self.body = body
        self.raises = raises
        self.calls = []

    def __call__(self, url, body, headers):
        self.calls.append((url, body, headers))
        if self.raises is not None:
            raise self.raises
        return self.status, self.body


# ---------- DiscordWebhookChannel ----------

class DiscordChannelSendTests(unittest.TestCase):
    def test_2xx_returns_true(self):
        post = FakePost(status=200)
        chan = channels.DiscordWebhookChannel(post)
        self.assertTrue(chan.send(DISCORD_URL, "seat opened") is True)
        self.assertEqual(len(post.calls), 1)

    def test_204_also_returns_true(self):
        post = FakePost(status=204)
        chan = channels.DiscordWebhookChannel(post)
        self.assertTrue(chan.send(DISCORD_URL, "seat opened") is True)

    def test_content_truncated_at_2000_chars(self):
        post = FakePost(status=200)
        chan = channels.DiscordWebhookChannel(post)
        long_message = "x" * 3000
        chan.send(DISCORD_URL, long_message)
        _, body, _ = post.calls[0]
        payload = json.loads(body)
        self.assertEqual(len(payload["content"]), 2000)
        self.assertEqual(payload["content"], "x" * 2000)

    def test_short_message_not_padded_or_altered(self):
        post = FakePost(status=200)
        chan = channels.DiscordWebhookChannel(post)
        chan.send(DISCORD_URL, "short message")
        _, body, _ = post.calls[0]
        payload = json.loads(body)
        self.assertEqual(payload["content"], "short message")

    def test_body_is_json_with_content_type_header(self):
        post = FakePost(status=200)
        chan = channels.DiscordWebhookChannel(post)
        chan.send(DISCORD_URL, "m")
        url, body, headers = post.calls[0]
        self.assertEqual(url, DISCORD_URL)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(json.loads(body), {"content": "m"})

    def test_404_raises_permanent_delivery_error(self):
        post = FakePost(status=404)
        chan = channels.DiscordWebhookChannel(post)
        with self.assertRaises(channels.PermanentDeliveryError) as ctx:
            chan.send(DISCORD_URL, "m")
        self.assertEqual(ctx.exception.status, 404)

    def test_410_raises_permanent_delivery_error(self):
        post = FakePost(status=410)
        chan = channels.DiscordWebhookChannel(post)
        with self.assertRaises(channels.PermanentDeliveryError) as ctx:
            chan.send(DISCORD_URL, "m")
        self.assertEqual(ctx.exception.status, 410)

    def test_429_raises_rate_limited_with_retry_after(self):
        body = json.dumps({"retry_after": 4.25, "global": False}).encode("utf-8")
        post = FakePost(status=429, body=body)
        chan = channels.DiscordWebhookChannel(post)
        with self.assertRaises(channels.RateLimitedError) as ctx:
            chan.send(DISCORD_URL, "m")
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(ctx.exception.retry_after, 4.25)

    def test_429_without_parseable_body_has_none_retry_after(self):
        post = FakePost(status=429, body=b"not json")
        chan = channels.DiscordWebhookChannel(post)
        with self.assertRaises(channels.RateLimitedError) as ctx:
            chan.send(DISCORD_URL, "m")
        self.assertIsNone(ctx.exception.retry_after)

    def test_429_with_empty_body_has_none_retry_after(self):
        post = FakePost(status=429, body=b"")
        chan = channels.DiscordWebhookChannel(post)
        with self.assertRaises(channels.RateLimitedError) as ctx:
            chan.send(DISCORD_URL, "m")
        self.assertIsNone(ctx.exception.retry_after)

    def test_500_raises_delivery_error_with_status(self):
        post = FakePost(status=500)
        chan = channels.DiscordWebhookChannel(post)
        with self.assertRaises(channels.DeliveryError) as ctx:
            chan.send(DISCORD_URL, "m")
        self.assertEqual(ctx.exception.status, 500)
        self.assertNotIsInstance(ctx.exception, channels.PermanentDeliveryError)
        self.assertNotIsInstance(ctx.exception, channels.RateLimitedError)

    def test_403_raises_plain_delivery_error(self):
        post = FakePost(status=403)
        chan = channels.DiscordWebhookChannel(post)
        with self.assertRaises(channels.DeliveryError) as ctx:
            chan.send(DISCORD_URL, "m")
        self.assertEqual(ctx.exception.status, 403)

    def test_bad_url_shape_raises_value_error_without_calling_transport(self):
        post = FakePost(status=200)
        chan = channels.DiscordWebhookChannel(post)
        with self.assertRaises(ValueError):
            chan.send("https://example.com/not-a-discord-webhook", "m")
        self.assertEqual(len(post.calls), 0)

    def test_http_url_rejected(self):
        post = FakePost(status=200)
        chan = channels.DiscordWebhookChannel(post)
        bad = DISCORD_URL.replace("https://", "http://")
        with self.assertRaises(ValueError):
            chan.send(bad, "m")

    def test_non_string_address_rejected(self):
        post = FakePost(status=200)
        chan = channels.DiscordWebhookChannel(post)
        with self.assertRaises(ValueError):
            chan.send(None, "m")

    def test_broken_pipe_propagates_untouched(self):
        post = FakePost(raises=BrokenPipeError("pipe gone"))
        chan = channels.DiscordWebhookChannel(post)
        with self.assertRaises(BrokenPipeError):
            chan.send(DISCORD_URL, "m")

    def test_connection_reset_propagates_untouched(self):
        post = FakePost(raises=ConnectionResetError("reset"))
        chan = channels.DiscordWebhookChannel(post)
        with self.assertRaises(ConnectionResetError):
            chan.send(DISCORD_URL, "m")

    def test_discordapp_legacy_host_accepted(self):
        post = FakePost(status=200)
        chan = channels.DiscordWebhookChannel(post)
        legacy = "https://discordapp.com/api/webhooks/123456789012345678/tok-en"
        self.assertTrue(chan.send(legacy, "m") is True)


# ---------- NtfyChannel ----------

class NtfyChannelSendTests(unittest.TestCase):
    def test_2xx_returns_true(self):
        post = FakePost(status=200)
        chan = channels.NtfyChannel(post)
        self.assertTrue(chan.send("my-topic", "seat opened") is True)

    def test_url_composition_default_base(self):
        post = FakePost(status=200)
        chan = channels.NtfyChannel(post)
        chan.send("my-topic", "m")
        url, body, _ = post.calls[0]
        self.assertEqual(url, "https://ntfy.sh/my-topic")
        self.assertEqual(body, b"m")

    def test_url_composition_custom_base_url_trailing_slash_stripped(self):
        post = FakePost(status=200)
        chan = channels.NtfyChannel(post, base_url="https://ntfy.example.com/")
        chan.send("topic-2", "m")
        url, _, _ = post.calls[0]
        self.assertEqual(url, "https://ntfy.example.com/topic-2")

    def test_default_headers_title_and_priority(self):
        post = FakePost(status=200)
        chan = channels.NtfyChannel(post)
        chan.send("my-topic", "m")
        _, _, headers = post.calls[0]
        self.assertEqual(headers["Title"], "Dropwatch")
        self.assertEqual(headers["Priority"], "default")

    def test_priority_mapped_from_context(self):
        post = FakePost(status=200)
        chan = channels.NtfyChannel(post)
        for priority in ("min", "low", "default", "high", "urgent"):
            post.calls.clear()
            chan.send("my-topic", "m", context={"priority": priority})
            _, _, headers = post.calls[0]
            self.assertEqual(headers["Priority"], priority)

    def test_invalid_priority_rejected(self):
        post = FakePost(status=200)
        chan = channels.NtfyChannel(post)
        with self.assertRaises(ValueError):
            chan.send("my-topic", "m", context={"priority": "critical"})
        self.assertEqual(len(post.calls), 0)

    def test_none_context_defaults_priority(self):
        post = FakePost(status=200)
        chan = channels.NtfyChannel(post)
        chan.send("my-topic", "m", context=None)
        _, _, headers = post.calls[0]
        self.assertEqual(headers["Priority"], "default")

    def test_topic_validation_rejects_bad_shape(self):
        post = FakePost(status=200)
        chan = channels.NtfyChannel(post)
        for bad_topic in ("", "has space", "slash/in/topic", "x" * 65, "emoji😀"):
            with self.assertRaises(ValueError):
                chan.send(bad_topic, "m")
        self.assertEqual(len(post.calls), 0)

    def test_topic_validation_accepts_max_length(self):
        post = FakePost(status=200)
        chan = channels.NtfyChannel(post)
        topic = "a" * 64
        self.assertTrue(chan.send(topic, "m") is True)

    def test_non_string_topic_rejected(self):
        post = FakePost(status=200)
        chan = channels.NtfyChannel(post)
        with self.assertRaises(ValueError):
            chan.send(None, "m")

    def test_429_raises_rate_limited_with_retry_after(self):
        body = json.dumps({"retry_after": 12}).encode("utf-8")
        post = FakePost(status=429, body=body)
        chan = channels.NtfyChannel(post)
        with self.assertRaises(channels.RateLimitedError) as ctx:
            chan.send("my-topic", "m")
        self.assertEqual(ctx.exception.retry_after, 12)

    def test_500_raises_delivery_error(self):
        post = FakePost(status=500)
        chan = channels.NtfyChannel(post)
        with self.assertRaises(channels.DeliveryError) as ctx:
            chan.send("my-topic", "m")
        self.assertEqual(ctx.exception.status, 500)
        self.assertNotIsInstance(ctx.exception, channels.PermanentDeliveryError)

    def test_broken_pipe_propagates_untouched(self):
        post = FakePost(raises=BrokenPipeError("pipe gone"))
        chan = channels.NtfyChannel(post)
        with self.assertRaises(BrokenPipeError):
            chan.send("my-topic", "m")


# ---------- exception hierarchy / retry_after parsing ----------

class ExceptionShapeTests(unittest.TestCase):
    def test_permanent_delivery_error_is_a_delivery_error(self):
        self.assertTrue(issubclass(channels.PermanentDeliveryError, channels.DeliveryError))

    def test_rate_limited_error_is_a_delivery_error(self):
        self.assertTrue(issubclass(channels.RateLimitedError, channels.DeliveryError))

    def test_delivery_error_carries_arbitrary_status(self):
        exc = channels.DeliveryError(503)
        self.assertEqual(exc.status, 503)

    def test_rate_limited_error_defaults_retry_after_to_none(self):
        exc = channels.RateLimitedError(429)
        self.assertIsNone(exc.retry_after)


class ParseRetryAfterTests(unittest.TestCase):
    def test_valid_numeric_field(self):
        self.assertEqual(channels._parse_retry_after(b'{"retry_after": 3}'), 3)

    def test_float_field(self):
        self.assertEqual(channels._parse_retry_after(b'{"retry_after": 1.5}'), 1.5)

    def test_none_body(self):
        self.assertIsNone(channels._parse_retry_after(None))

    def test_empty_body(self):
        self.assertIsNone(channels._parse_retry_after(b""))

    def test_malformed_json(self):
        self.assertIsNone(channels._parse_retry_after(b"{not json"))

    def test_json_array_not_object(self):
        self.assertIsNone(channels._parse_retry_after(b"[1, 2, 3]"))

    def test_missing_key(self):
        self.assertIsNone(channels._parse_retry_after(b"{}"))

    def test_non_numeric_value(self):
        self.assertIsNone(channels._parse_retry_after(b'{"retry_after": "soon"}'))

    def test_boolean_value_rejected(self):
        # bool is an int subclass in Python -- must not sneak through.
        self.assertIsNone(channels._parse_retry_after(b'{"retry_after": true}'))


# ---------- make_default_channels ----------

class MakeDefaultChannelsTests(unittest.TestCase):
    def test_returns_discord_and_ntfy_only(self):
        result = channels.make_default_channels(FakePost(status=200))
        self.assertEqual(set(result.keys()), {"discord", "ntfy"})

    def test_telegram_deliberately_absent(self):
        result = channels.make_default_channels(FakePost(status=200))
        self.assertNotIn("telegram", result)

    def test_returns_working_channel_instances(self):
        post = FakePost(status=200)
        result = channels.make_default_channels(post)
        self.assertIsInstance(result["discord"], channels.DiscordWebhookChannel)
        self.assertIsInstance(result["ntfy"], channels.NtfyChannel)
        self.assertTrue(result["discord"].send(DISCORD_URL, "m") is True)
        self.assertTrue(result["ntfy"].send("topic", "m") is True)


if __name__ == "__main__":
    unittest.main()
