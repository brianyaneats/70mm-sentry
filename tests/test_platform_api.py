"""Unit tests for platformd.api -- the client-facing HTTP API.

Real HTTP against an ephemeral-port ThreadingHTTPServer (make_server()'s
default port=0), a fresh temp sqlite file per test, and fake channel
senders that capture what would have been sent instead of making any
network call. No live network calls anywhere in this file.
"""

import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _install_fake_webhook_module_if_absent():
    """platformd.webhook is owned by a separate, concurrently-landing
    change. platformd.api imports it lazily, only on an actual
    channel="webhook" request (see api's module docstring), so this file
    can run whether or not the real module has landed yet. If it's
    already importable, this is a no-op -- nothing here overrides it.
    """
    try:
        import platformd.webhook  # noqa: F401
        return
    except ImportError:
        pass

    fake = types.ModuleType("platformd.webhook")
    fake.WEBHOOK_ADDRESS_PATTERN = re.compile(r"^https://\S+$")

    def is_safe_url(url):
        if not url.startswith("https://"):
            return False, "webhook address must use https"
        if "127.0.0.1" in url or "localhost" in url or "169.254." in url:
            return False, "webhook address may not target a local or link-local host"
        return True, None

    def generate_secret():
        return secrets.token_hex(16)

    class WebhookDeliveryError(Exception):
        def __init__(self, status):
            self.status = status
            super().__init__("webhook delivery failed with status %d" % status)

    class WebhookGone(WebhookDeliveryError):
        def __init__(self):
            super().__init__(410)

    fake.is_safe_url = is_safe_url
    fake.generate_secret = generate_secret
    fake.WebhookGone = WebhookGone
    fake.WebhookDeliveryError = WebhookDeliveryError
    sys.modules["platformd.webhook"] = fake


_install_fake_webhook_module_if_absent()

from platformd import api, core, db, log  # noqa: E402


class FakeChannel:
    """Captures every send() instead of delivering anything."""

    def __init__(self):
        self.sent = []  # list of (address, message, context)

    def send(self, address, message, context=None):
        self.sent.append((address, message, context))
        return True

    def last_code(self):
        message = self.sent[-1][1]
        return message.strip().split()[-1]


def _make_channels():
    return {
        "telegram": FakeChannel(),
        "discord": FakeChannel(),
        "ntfy": FakeChannel(),
        "webhook": FakeChannel(),
    }


def http_request(base_url, method, path, body=None, extra_headers=None):
    """POST/GET helper. body may be a dict (JSON-encoded) or raw bytes
    (to exercise malformed/oversized payloads). Returns
    (status, parsed_json_or_None, raw_text, headers).
    """
    data = None
    headers = {}
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(base_url + path, data=data, headers=headers,
                                  method=method)
    try:
        resp = urllib.request.urlopen(req)
        status = resp.status
        payload = resp.read()
        resp_headers = dict(resp.headers)
    except urllib.error.HTTPError as exc:
        status = exc.code
        payload = exc.read()
        resp_headers = dict(exc.headers)

    text = payload.decode("utf-8")
    try:
        parsed = json.loads(text) if text else None
    except ValueError:
        parsed = None
    return status, parsed, text, resp_headers


def _start_server(db_path, channels, **kwargs):
    httpd = api.make_server(db_path, channels, **kwargs)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    return httpd, thread, "http://%s:%d" % (host, port)


class PlatformAPITestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dropwatch-test-")
        self.db_path = os.path.join(self.tmpdir, "test.sqlite3")
        self.channels = _make_channels()
        self.httpd, self.thread, self.base_url = _start_server(
            self.db_path, self.channels)

        conn = db.connect(self.db_path)
        core.register_drop(conn, "green-drop", "Green Drop",
                            description="A healthy, official-source drop.",
                            risk_tier="green", now=time.time())
        core.register_drop(conn, "yellow-drop", "Yellow Drop [DEMO]",
                            description="An unofficial-source drop.",
                            risk_tier="yellow", now=time.time())
        conn.close()

        log.clear()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def _subscribe(self, drop_id="green-drop", channel="telegram",
                    address="@watcher_one"):
        return http_request(self.base_url, "POST", "/api/subscribe", {
            "drop_id": drop_id, "channel": channel, "address": address,
        })

    # ---------------------------------------------------------- happy path

    def test_subscribe_verify_unsubscribe_happy_path(self):
        status, body, text, _ = self._subscribe()
        self.assertEqual(status, 200)
        self.assertIn("subscription_id", body)
        sub_id = body["subscription_id"]

        code = self.channels["telegram"].last_code()
        self.assertRegex(code, r"^\d{6}$")
        # The verify code must never appear in the subscribe response.
        self.assertNotIn(code, text)

        status, vbody, _, _ = http_request(self.base_url, "POST", "/api/verify", {
            "subscription_id": sub_id, "code": code,
        })
        self.assertEqual(status, 200)
        self.assertIn("unsubscribe_token", vbody)
        token = vbody["unsubscribe_token"]

        conn = db.connect(self.db_path)
        self.assertEqual(core.get_subscription(conn, sub_id)["status"], "active")
        conn.close()

        status, ubody, _, _ = http_request(self.base_url, "POST", "/api/unsubscribe", {
            "token": token,
        })
        self.assertEqual(status, 200)

        conn = db.connect(self.db_path)
        self.assertEqual(core.get_subscription(conn, sub_id)["status"], "disabled")
        conn.close()

    def test_wrong_code_five_times_locks(self):
        status, body, _, _ = self._subscribe(address="@lockout_watcher")
        sub_id = body["subscription_id"]

        for _ in range(4):
            status, vbody, _, _ = http_request(self.base_url, "POST", "/api/verify", {
                "subscription_id": sub_id, "code": "000000",
            })
            self.assertEqual(status, 403)

        # 5th wrong attempt disables the row (core.verify's own lockout).
        status, vbody, _, _ = http_request(self.base_url, "POST", "/api/verify", {
            "subscription_id": sub_id, "code": "000000",
        })
        self.assertEqual(status, 403)

        conn = db.connect(self.db_path)
        self.assertEqual(core.get_subscription(conn, sub_id)["status"], "disabled")
        conn.close()

        entries = log.recent(tag=log.TAG_RATELIMIT)
        self.assertTrue(any(str(sub_id) in e.line for e in entries))

    def test_malformed_json_400(self):
        status, body, _, _ = http_request(self.base_url, "POST", "/api/subscribe",
                                           b"{not valid json")
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_oversized_body_413(self):
        big = b'{"drop_id": "' + b"x" * (17 * 1024) + b'"}'
        status, body, _, _ = http_request(self.base_url, "POST", "/api/subscribe", big)
        self.assertEqual(status, 413)
        self.assertIn("error", body)

    def test_unknown_drop_404(self):
        status, body, _, _ = self._subscribe(drop_id="does-not-exist")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_get_index_returns_html_with_h1(self):
        status, _, text, headers = http_request(self.base_url, "GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn("Know the moment your campsite opens.", text)

    def test_get_index_lists_webhook_channel_option(self):
        status, _, text, _ = http_request(self.base_url, "GET", "/")
        self.assertEqual(status, 200)
        self.assertIn('value="webhook"', text)
        self.assertIn("your-endpoint.example/dropwatch", text)
        self.assertIn("Alerts arrive as signed JSON", text)

    def test_verify_past_ttl_returns_403_with_expired_message(self):
        status, body, _, _ = self._subscribe(address="@ttl_watcher")
        self.assertEqual(status, 200)
        sub_id = body["subscription_id"]
        code = self.channels["telegram"].last_code()

        conn = db.connect(self.db_path)
        conn.execute(
            "UPDATE subscriptions SET created_at = created_at - ? WHERE id = ?",
            (core.VERIFY_CODE_TTL_SECONDS, sub_id))
        conn.close()

        status, vbody, _, _ = http_request(self.base_url, "POST", "/api/verify", {
            "subscription_id": sub_id, "code": code,
        })
        self.assertEqual(status, 403)
        self.assertEqual(vbody["error"], "code expired — subscribe again")

    def test_verify_within_ttl_still_succeeds_via_api(self):
        status, body, _, _ = self._subscribe(address="@fresh_watcher")
        sub_id = body["subscription_id"]
        code = self.channels["telegram"].last_code()

        status, vbody, _, _ = http_request(self.base_url, "POST", "/api/verify", {
            "subscription_id": sub_id, "code": code,
        })
        self.assertEqual(status, 200)
        self.assertIn("unsubscribe_token", vbody)

    def test_expired_subscription_is_replaceable_via_api(self):
        status, body, _, _ = self._subscribe(address="@replace_watcher")
        first_id = body["subscription_id"]

        conn = db.connect(self.db_path)
        conn.execute(
            "UPDATE subscriptions SET created_at = created_at - ? WHERE id = ?",
            (core.VERIFY_CODE_TTL_SECONDS, first_id))
        conn.close()

        status, body2, _, _ = self._subscribe(address="@replace_watcher")
        self.assertEqual(status, 200)
        second_id = body2["subscription_id"]
        self.assertNotEqual(second_id, first_id)

        conn = db.connect(self.db_path)
        self.assertEqual(core.get_subscription(conn, first_id)["status"], "disabled")
        conn.close()

    def test_get_drops_shows_amber_for_yellow_demo_drop(self):
        status, body, _, _ = http_request(self.base_url, "GET", "/api/drops")
        self.assertEqual(status, 200)
        by_id = {d["id"]: d for d in body["drops"]}

        self.assertEqual(by_id["yellow-drop"]["health"]["state"], "amber")
        self.assertIn("unofficial data source",
                      by_id["yellow-drop"]["health"]["detail"])
        self.assertEqual(by_id["green-drop"]["health"]["state"], "green")
        self.assertIsNone(by_id["green-drop"]["health"]["detail"])


class PlatformAPIRateLimitTestCase(unittest.TestCase):
    """Isolated in its own server/tempdir so its POST burst can't be
    diluted (or accidentally tripped) by another test's requests.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dropwatch-ratelimit-")
        self.db_path = os.path.join(self.tmpdir, "rl.sqlite3")
        self.channels = _make_channels()
        self.httpd, self.thread, self.base_url = _start_server(
            self.db_path, self.channels)

        conn = db.connect(self.db_path)
        core.register_drop(conn, "rl-drop", "RL Drop", risk_tier="green",
                            now=time.time())
        conn.close()
        log.clear()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def test_rate_limit_trips_on_eleventh_post(self):
        statuses = []
        for i in range(11):
            status, _, _, _ = http_request(self.base_url, "POST", "/api/subscribe", {
                "drop_id": "rl-drop", "channel": "telegram",
                "address": "@ratelimit_addr_%02d" % i,
            })
            statuses.append(status)

        self.assertTrue(all(s != 429 for s in statuses[:10]), statuses)
        self.assertEqual(statuses[10], 429)

        entries = log.recent(tag=log.TAG_RATELIMIT)
        self.assertTrue(len(entries) >= 1)
        for e in entries:
            self.assertNotIn("ratelimit_addr", e.line)
            self.assertNotIn("@", e.line)


class AddressNormalizationTestCase(PlatformAPITestCase):
    """Regression, found live 2026-08-04: a real user typed a bare
    Telegram handle ('easy_score') and bounced off the @-only pattern.
    Bare handles are canonicalized server-side, not rejected."""

    def test_bare_telegram_handle_is_accepted_and_canonicalized(self):
        status, body, _, _ = self._subscribe(address="easy_score")
        self.assertEqual(status, 200)

        conn = db.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT address FROM subscriptions WHERE id=?",
                (body["subscription_id"],)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["address"], "@easy_score")

        # The code went to the canonical address through the fake channel.
        sent = self.channels["telegram"].sent
        self.assertEqual(sent[-1][0], "@easy_score")

    def test_bare_and_prefixed_handle_dedupe_to_one_subscription(self):
        # Activate a subscription under the canonical form, then subscribe
        # again via HTTP with the bare form: normalization must land on the
        # same row (active-dedupe), not mint a second subscription.
        conn = db.connect(self.db_path)
        try:
            row = core.subscribe(
                conn, "green-drop", "telegram", "@easy_score", "free",
                time.time())
            active_id = row["id"]
            core.verify(conn, active_id, row["verify_code"], time.time())
        finally:
            conn.close()
        status, body, _, _ = self._subscribe(address="easy_score")
        self.assertEqual(status, 200)
        self.assertEqual(body["subscription_id"], active_id)

    def test_too_short_bare_handle_still_rejected(self):
        status, body, _, _ = self._subscribe(address="abcd")
        self.assertEqual(status, 400)


class DemoFlagTestCase(unittest.TestCase):
    def test_drops_payload_carries_demo_flag_only_when_demo(self):
        for demo, expected in ((True, True), (False, False)):
            tmpdir = tempfile.mkdtemp(prefix="dropwatch-demo-flag-")
            db_path = os.path.join(tmpdir, "t.sqlite3")
            httpd, thread, base = _start_server(
                db_path, _make_channels(), demo=demo)
            try:
                status, body, _, _ = http_request(base, "GET", "/api/drops")
                self.assertEqual(status, 200)
                self.assertEqual(body.get("demo"), expected)
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)


class WebhookChannelTestCase(PlatformAPITestCase):
    """channel="webhook" end to end: subscribe, address safety, secret
    handling, and the signed verify-code delivery."""

    def test_webhook_subscribe_verify_unsubscribe_happy_path(self):
        status, body, text, _ = self._subscribe(
            channel="webhook", address="https://good.example.com/hook")
        self.assertEqual(status, 200)
        sub_id = body["subscription_id"]

        sent = self.channels["webhook"].sent
        self.assertEqual(len(sent), 1)
        address, message, context = sent[-1]
        self.assertEqual(address, "https://good.example.com/hook")
        self.assertRegex(message, r"\d{6}")
        code = re.search(r"\d{6}", message).group(0)

        conn = db.connect(self.db_path)
        secret = conn.execute(
            "SELECT webhook_secret FROM subscriptions WHERE id=?",
            (sub_id,)).fetchone()["webhook_secret"]
        conn.close()
        self.assertIsNotNone(secret)
        self.assertEqual(context["webhook_secret"], secret)
        # the secret must never appear in the subscribe response itself
        self.assertNotIn(secret, text)

        status, vbody, vtext, _ = http_request(self.base_url, "POST", "/api/verify", {
            "subscription_id": sub_id, "code": code,
        })
        self.assertEqual(status, 200)
        self.assertIn("unsubscribe_token", vbody)
        self.assertNotIn(secret, vtext)
        token = vbody["unsubscribe_token"]

        status, _, _, _ = http_request(self.base_url, "POST", "/api/unsubscribe", {
            "token": token,
        })
        self.assertEqual(status, 200)
        conn = db.connect(self.db_path)
        self.assertEqual(core.get_subscription(conn, sub_id)["status"], "disabled")
        conn.close()

    def test_webhook_rejects_unsafe_url_without_echoing_it(self):
        status, body, _, _ = self._subscribe(
            channel="webhook", address="https://169.254.169.254/latest/meta-data")
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        self.assertNotIn("169.254.169.254", body["error"])

    def test_webhook_rejects_non_https_url(self):
        status, body, _, _ = self._subscribe(
            channel="webhook", address="http://good.example.com/hook")
        self.assertEqual(status, 400)

    def test_webhook_secret_differs_per_subscription(self):
        status1, body1, _, _ = self._subscribe(
            channel="webhook", address="https://one.example.com/hook")
        status2, body2, _, _ = self._subscribe(
            channel="webhook", address="https://two.example.com/hook")
        self.assertEqual(status1, 200)
        self.assertEqual(status2, 200)

        conn = db.connect(self.db_path)
        try:
            s1 = conn.execute("SELECT webhook_secret FROM subscriptions WHERE id=?",
                              (body1["subscription_id"],)).fetchone()["webhook_secret"]
            s2 = conn.execute("SELECT webhook_secret FROM subscriptions WHERE id=?",
                              (body2["subscription_id"],)).fetchone()["webhook_secret"]
        finally:
            conn.close()
        self.assertNotEqual(s1, s2)


class ProxyAwareRateLimitTestCase(unittest.TestCase):
    """make_server(trust_proxy=...) -- X-Forwarded-For is ignored by
    default (the rate-limit key is always the real socket peer) and only
    consulted when the deployment explicitly opts in."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dropwatch-xff-")
        log.clear()

    def _server(self, trust_proxy):
        db_path = os.path.join(self.tmpdir, "xff-%s.sqlite3" % trust_proxy)
        httpd, thread, base = _start_server(db_path, _make_channels(),
                                             trust_proxy=trust_proxy)
        conn = db.connect(db_path)
        core.register_drop(conn, "xff-drop", "XFF Drop", risk_tier="green",
                            now=time.time())
        conn.close()
        return httpd, thread, base

    def _post_with_xff(self, base, xff_value, i):
        status, _, _, _ = http_request(
            base, "POST", "/api/subscribe",
            {"drop_id": "xff-drop", "channel": "telegram",
             "address": "@xff_addr_%02d" % i},
            extra_headers={"X-Forwarded-For": xff_value})
        return status

    def test_xff_ignored_by_default(self):
        httpd, thread, base = self._server(trust_proxy=False)
        try:
            # 10 posts, each claiming a distinct X-Forwarded-For -- ignored
            # by default, so they all share ONE bucket keyed on the real
            # loopback client address.
            statuses = [self._post_with_xff(base, "10.0.0.%d" % i, i) for i in range(10)]
            self.assertTrue(all(s != 429 for s in statuses), statuses)

            # the 11th request, still a brand-new XFF value, still trips
            # the SAME (real-ip) bucket -- proof XFF was never consulted.
            eleventh = self._post_with_xff(base, "10.0.0.99", 10)
            self.assertEqual(eleventh, 429)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_xff_honored_with_trust_proxy(self):
        httpd, thread, base = self._server(trust_proxy=True)
        try:
            statuses = [self._post_with_xff(base, "10.0.0.1", i) for i in range(10)]
            self.assertTrue(all(s != 429 for s in statuses), statuses)

            eleventh = self._post_with_xff(base, "10.0.0.1", 10)
            self.assertEqual(eleventh, 429,
                              "the 11th request sharing the same XFF hop must trip")

            different = self._post_with_xff(base, "10.0.0.2", 11)
            self.assertNotEqual(
                different, 429,
                "a distinct XFF hop must get its own, still-fresh bucket")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


class UnsubscribeLandingPageTestCase(PlatformAPITestCase):
    """GET /unsubscribe?token=... -- a safe-method confirm page; the GET
    itself must never disable anything."""

    def _verified_token(self, address="@unsub_landing"):
        status, body, _, _ = self._subscribe(address=address)
        sub_id = body["subscription_id"]
        code = self.channels["telegram"].last_code()
        status, vbody, _, _ = http_request(self.base_url, "POST", "/api/verify", {
            "subscription_id": sub_id, "code": code,
        })
        return sub_id, vbody["unsubscribe_token"]

    def test_get_renders_confirm_page_and_changes_nothing(self):
        sub_id, token = self._verified_token()

        status, _, text, headers = http_request(
            self.base_url, "GET", "/unsubscribe?token=%s" % token)
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn("Unsubscribe", text)

        conn = db.connect(self.db_path)
        self.assertEqual(core.get_subscription(conn, sub_id)["status"], "active")
        conn.close()

    def test_page_button_then_post_actually_unsubscribes(self):
        sub_id, token = self._verified_token(address="@unsub_landing_2")

        status, _, _, _ = http_request(
            self.base_url, "GET", "/unsubscribe?token=%s" % token)
        self.assertEqual(status, 200)

        # the page itself only renders; the actual disable still requires
        # the POST its button fires -- simulate that click here.
        status, ubody, _, _ = http_request(self.base_url, "POST", "/api/unsubscribe", {
            "token": token,
        })
        self.assertEqual(status, 200)

        conn = db.connect(self.db_path)
        self.assertEqual(core.get_subscription(conn, sub_id)["status"], "disabled")
        conn.close()

    def test_token_is_not_html_injectable(self):
        evil = "<script>alert(1)</script>"
        status, _, text, _ = http_request(
            self.base_url, "GET",
            "/unsubscribe?token=" + urllib.parse.quote(evil))
        self.assertEqual(status, 200)
        self.assertNotIn("<script>alert(1)</script>", text)

    def test_missing_token_renders_page_without_crashing(self):
        status, _, text, _ = http_request(self.base_url, "GET", "/unsubscribe")
        self.assertEqual(status, 200)
        self.assertIn("Unsubscribe", text)


class TrustPageTestCase(PlatformAPITestCase):
    """The public 'how it works' page existed as a file for a full wave
    while GET /how returned 404 -- a page nobody can reach is not a
    page. Pinned so the route can't quietly disappear again."""

    def test_how_route_serves_the_trust_page(self):
        status, _, text, headers = http_request(self.base_url, "GET", "/how")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        # Load-bearing promises the page must actually carry.
        self.assertIn("head start", text)          # scarcity honesty
        self.assertIn("Dropwatch", text)


class TierIsNeverClientSuppliedTestCase(PlatformAPITestCase):
    """The free/paid notify-delay split IS the monetization mechanism, so
    a client-declared tier was a free upgrade for anyone who could POST
    (red-team audit 2026-08-04). The field is accepted and ignored."""

    def test_client_claiming_paid_still_lands_on_free(self):
        status, body, _, _ = http_request(
            self.base_url, "POST", "/api/subscribe",
            {"drop_id": "green-drop", "channel": "telegram",
             "address": "@tier_claimer", "tier": "paid"})
        self.assertEqual(status, 200)

        conn = db.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT tier FROM subscriptions WHERE id=?",
                (body["subscription_id"],)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["tier"], "free")

    def test_yellow_drop_subscription_still_works_on_free(self):
        status, body, _, _ = self._subscribe(
            drop_id="yellow-drop", address="@yellow_watcher")
        self.assertEqual(status, 200)
        self.assertIn("subscription_id", body)


if __name__ == "__main__":
    unittest.main()
