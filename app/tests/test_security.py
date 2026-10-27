"""Security regressions from the pre-hosting audit (2026-10-27). Each test
names the attack it pins. No real network: servers bind 127.0.0.1 on an
ephemeral port; the DNS test uses "localhost", which never leaves the box."""

import os
import re
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.tests.test_api import _make_channels, _start_server, http_request  # noqa: E402

from app import api  # noqa: E402
from engine import core, db, log  # noqa: E402
from notify import channels, webhook  # noqa: E402


class _ServerCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dropwatch-sec-")
        log.clear()
        self.servers = []

    def tearDown(self):
        for httpd, thread in self.servers:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def start(self, **kwargs):
        db_path = os.path.join(self.tmpdir, "sec-%d.sqlite3" % len(self.servers))
        chans = _make_channels()
        httpd, thread, base = _start_server(db_path, chans, **kwargs)
        self.servers.append((httpd, thread))
        conn = db.connect(db_path)
        core.register_drop(conn, "sec-drop", "Sec Drop", risk_tier="green",
                           now=time.time())
        conn.close()
        return base, chans


class RateLimitKeyTest(_ServerCase):
    def _sub(self, base, i, headers):
        return http_request(base, "POST", "/api/subscribe",
                            {"drop_id": "sec-drop", "channel": "ntfy",
                             "address": "sec-topic-%02d" % i},
                            extra_headers=headers)[0]

    def test_spoofed_leading_xff_hop_does_not_mint_fresh_buckets(self):
        # An append-style proxy (Fly, nginx) turns a client-sent
        # "X-Forwarded-For: <spoof>" into "<spoof>, <real ip>".
        base, _ = self.start(trust_proxy=True)
        codes = [self._sub(base, i, {"X-Forwarded-For": "6.6.6.%d, 203.0.113.9" % i})
                 for i in range(11)]
        self.assertEqual(codes[-1], 429, codes)

    def test_proxy_owned_header_wins_over_xff(self):
        base, _ = self.start(trust_proxy=True, client_ip_header="Fly-Client-IP")
        codes = [self._sub(base, i, {"Fly-Client-IP": "203.0.113.9",
                                     "X-Forwarded-For": "6.6.6.%d" % i})
                 for i in range(11)]
        self.assertEqual(codes[-1], 429, codes)


class VerifyCodeSpamTest(_ServerCase):
    def test_repeat_subscribe_sends_one_code_per_destination(self):
        base, chans = self.start()
        body = {"drop_id": "sec-drop", "channel": "ntfy", "address": "victim-topic"}
        ids = {http_request(base, "POST", "/api/subscribe", body)[1]["subscription_id"]
               for _ in range(5)}
        self.assertEqual(len(chans["ntfy"].sent), 1,
                         "repeat subscribes must not re-send to the destination")
        self.assertEqual(len(ids), 1)


class SecurityHeadersTest(_ServerCase):
    def test_html_has_nonce_csp_matching_its_only_script(self):
        base, _ = self.start()
        for path in ("/", "/how", "/unsubscribe?token=abc"):
            status, _, raw, headers = http_request(base, "GET", path)
            self.assertEqual(status, 200, path)
            csp = headers.get("Content-Security-Policy", "")
            self.assertIn("frame-ancestors 'none'", csp, path)
            self.assertNotIn("unsafe-inline", csp.split("script-src")[1].split(";")[0])
            m = re.search(r"'nonce-([^']+)'", csp)
            for tag in re.findall(r"<script[^>]*>", raw):
                self.assertIn('nonce="%s"' % m.group(1), tag, path)
            self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
            self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")

    def test_nonce_is_fresh_per_response(self):
        base, _ = self.start()
        a = http_request(base, "GET", "/")[3]["Content-Security-Policy"]
        b = http_request(base, "GET", "/")[3]["Content-Security-Policy"]
        self.assertNotEqual(a, b)

    def test_json_responses_get_locked_down_csp(self):
        base, _ = self.start()
        headers = http_request(base, "GET", "/api/drops")[3]
        self.assertEqual(headers.get("Content-Security-Policy"),
                         "default-src 'none'; frame-ancestors 'none'")

    def test_server_header_hides_python_version(self):
        base, _ = self.start()
        self.assertNotIn("Python", http_request(base, "GET", "/")[3].get("Server", ""))


class UnsubscribeTokenInjectionTest(_ServerCase):
    def test_token_cannot_open_an_html_comment_or_close_the_script(self):
        base, _ = self.start()
        raw = http_request(base, "GET",
                           "/unsubscribe?token=%3C!--%3Cscript%3E%3C/script%3E")[2]
        script = raw.split("<script", 1)[1].split("</script>", 1)[0]
        self.assertNotIn("<!--", script)
        self.assertNotIn("<script", script)


class AddressAnchorTest(unittest.TestCase):
    def test_trailing_newline_is_rejected_everywhere(self):
        self.assertIsNone(api._ADDRESS_PATTERNS["ntfy"].match("topic\n"))
        self.assertIsNone(channels.NTFY_TOPIC_PATTERN.match("topic\n"))
        self.assertIsNone(channels.DISCORD_WEBHOOK_URL_PATTERN.match(
            "https://discord.com/api/webhooks/1/abc\n"))
        self.assertIsNone(webhook.WEBHOOK_ADDRESS_PATTERN.match("https://x.example/h\n"))


class SsrfTest(unittest.TestCase):
    def test_non_global_literal_ranges_are_refused(self):
        for url in ("https://100.64.0.1/h",          # CGNAT: not private, not global
                    "https://[::ffff:10.0.0.1]/h",   # IPv4-mapped private
                    "https://[fdaa::3]/h",           # Fly 6PN private network
                    "https://169.254.169.254/h"):
            ok, _ = webhook.is_safe_url(url)
            self.assertFalse(ok, url)


if __name__ == "__main__":
    unittest.main()
