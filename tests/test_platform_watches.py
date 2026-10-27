"""Campground watches + paid entitlements: dated fan-out, the per-
destination code cooldown, active_watches(), Stripe signature/event
handling, the HTTP surface for all of it, and budget-aware polling.

No network: Stripe events are signed locally with a test secret."""

import datetime as dt
import hashlib
import hmac
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_platform_api import _make_channels, _start_server, http_request  # noqa: E402

from adapters.base import SourceAdapter  # noqa: E402
from platformd import billing, core, db, log, outbox as outbox_mod, poller  # noqa: E402

T0 = 1_700_000_000.0
COOL = core.RESEND_COOLDOWN_SECONDS
WHSEC = "whsec_test_dropwatch"


def active(conn, drop, address, now, date_from=None, date_to=None,
           channel="ntfy"):
    row = core.subscribe(conn, drop, channel, address, "free", now,
                         date_from=date_from, date_to=date_to)
    return core.verify(conn, row["id"], row["verify_code"], now)


def fire(conn, drop, date, now, unit="u1"):
    return core.record_event(conn, drop, {
        "event_id": "%s:%s@%d" % (unit, date, now), "kind": "opened",
        "unit_id": "%s:%s" % (unit, date), "state": "available",
        "payload": {"label": "Site", "date": date}}, now=now)


def outbox(conn):
    return conn.execute(
        "SELECT s.address, o.not_before - e.recorded_at AS delay "
        "FROM outbox o JOIN subscriptions s ON s.id = o.subscription_id "
        "JOIN events e ON e.id = o.event_row_id ORDER BY o.id").fetchall()


class DatedFanoutTest(unittest.TestCase):
    def setUp(self):
        log.clear()
        self.conn = db.connect(":memory:")
        core.register_drop(self.conn, "rg-1", "Camp", risk_tier="green", now=T0)

    def test_event_reaches_only_watches_covering_its_date(self):
        active(self.conn, "rg-1", "in-range", T0, "2026-11-10", "2026-11-12")
        active(self.conn, "rg-1", "out-of-range", T0, "2026-12-01", "2026-12-03")
        active(self.conn, "rg-1", "undated", T0)
        fire(self.conn, "rg-1", "2026-11-12", T0 + 5)
        self.assertEqual(sorted(r["address"] for r in outbox(self.conn)),
                         ["in-range", "undated"])

    def test_overlapping_watches_on_one_destination_alert_once(self):
        active(self.conn, "rg-1", "me", T0, "2026-11-10", "2026-11-12")
        active(self.conn, "rg-1", "me", T0 + COOL, "2026-11-11", "2026-11-20")
        fire(self.conn, "rg-1", "2026-11-11", T0 + 2 * COOL)
        self.assertEqual(len(outbox(self.conn)), 1)

    def test_entitlement_makes_alert_instant(self):
        sub = active(self.conn, "rg-1", "payer", T0)
        active(self.conn, "rg-1", "free-rider", T0)
        core.grant_entitlement(self.conn, sub["id"], "cus_1", "sub_1", T0)
        fire(self.conn, "rg-1", "2026-11-11", T0 + 5)
        delays = {r["address"]: r["delay"] for r in outbox(self.conn)}
        self.assertEqual(delays, {"payer": 0, "free-rider": core.TIER_DELAYS["free"]})

    def test_cancelled_entitlement_reverts_to_free_delay(self):
        sub = active(self.conn, "rg-1", "payer", T0)
        core.grant_entitlement(self.conn, sub["id"], "cus_1", "sub_1", T0)
        core.set_entitlement_by_stripe_subscription(self.conn, "sub_1", False, T0)
        fire(self.conn, "rg-1", "2026-11-11", T0 + 5)
        self.assertEqual(outbox(self.conn)[0]["delay"], core.TIER_DELAYS["free"])

    def test_yellow_drop_never_gets_paid_speed(self):
        core.register_drop(self.conn, "gray", "Gray", risk_tier="yellow", now=T0)
        sub = active(self.conn, "gray", "payer", T0)
        core.grant_entitlement(self.conn, sub["id"], "cus_1", "sub_1", T0)
        fire(self.conn, "gray", "2026-11-11", T0 + 5)
        self.assertEqual(outbox(self.conn)[0]["delay"], core.TIER_DELAYS["free"])


class AlertCopyTest(unittest.TestCase):
    def test_alert_carries_night_booking_link_and_unsubscribe(self):
        conn = db.connect(":memory:")
        core.register_drop(conn, "rg-1", "Upper Pines", risk_tier="green", now=T0)
        sub = active(conn, "rg-1", "me", T0)
        core.grant_entitlement(conn, sub["id"], "cus", "sub", T0)
        core.record_event(conn, "rg-1", {
            "event_id": "e1", "kind": "opened", "unit_id": "1:2:2026-11-14",
            "state": "available", "payload": {
                "label": "012 (STANDARD)", "date": "2026-11-14",
                "url": "https://www.recreation.gov/camping/campsites/2"}}, now=T0)
        chan = _make_channels()
        outbox_mod.dispatch(conn, chan, now=T0, public_url="https://dw.example/")
        self.assertEqual(chan["ntfy"].sent[0][1].split("\n"), [
            "Upper Pines: site opened — 012 (STANDARD)",
            "Night of 2026-11-14",
            "Book: https://www.recreation.gov/camping/campsites/2",
            "Stop these alerts: https://dw.example/unsubscribe?token=%s"
            % sub["unsub_token"]])


class CooldownTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        for i in range(3):
            core.register_drop(self.conn, "rg-%d" % i, "C", risk_tier="green", now=T0)

    def test_cycling_campgrounds_cannot_spam_one_destination(self):
        core.subscribe(self.conn, "rg-0", "ntfy", "victim", "free", T0)
        with self.assertRaises(core.ResendCooldown):
            core.subscribe(self.conn, "rg-1", "ntfy", "victim", "free", T0 + 1)
        with self.assertRaises(core.ResendCooldown):
            core.subscribe(self.conn, "rg-0", "ntfy", "victim", "free", T0 + 2,
                           date_from="2026-11-01", date_to="2026-11-02")

    def test_same_watch_inside_cooldown_returns_the_live_row(self):
        a = core.subscribe(self.conn, "rg-0", "ntfy", "me", "free", T0)
        b = core.subscribe(self.conn, "rg-0", "ntfy", "me", "free", T0 + 1)
        self.assertEqual(a["id"], b["id"])

    def test_cooldown_expires(self):
        core.subscribe(self.conn, "rg-0", "ntfy", "me", "free", T0)
        row = core.subscribe(self.conn, "rg-1", "ntfy", "me", "free", T0 + COOL)
        self.assertEqual(row["status"], "pending_verify")


class ActiveWatchesTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        core.register_drop(self.conn, "rg-1", "C", risk_tier="green", now=T0,
                           adapter="recreation_gov", target={"campground_id": "1"})
        core.register_drop(self.conn, "plain", "P", risk_tier="green", now=T0)
        self.today = dt.date(2026, 11, 10)

    def test_union_of_ranges_clamped_to_today(self):
        active(self.conn, "rg-1", "a", T0, "2026-11-08", "2026-11-11")
        active(self.conn, "rg-1", "b", T0 + COOL, "2026-11-11", "2026-11-12")
        [w] = core.active_watches(self.conn, self.today)
        self.assertEqual(w["dates"], ["2026-11-10", "2026-11-11", "2026-11-12"])
        self.assertEqual(w["target"], {"campground_id": "1"})

    def test_nobody_watching_means_no_requests(self):
        core.subscribe(self.conn, "rg-1", "ntfy", "pending", "free", T0,
                       date_from="2026-11-10", date_to="2026-11-11")  # unverified
        active(self.conn, "rg-1", "past", T0 + COOL, "2026-11-01", "2026-11-02")
        active(self.conn, "plain", "x", T0 + 2 * COOL, "2026-11-10", "2026-11-11")
        self.assertEqual(core.active_watches(self.conn, self.today), [])


def sign(body, secret=WHSEC, t=None):
    t = int(time.time()) if t is None else t
    mac = hmac.new(secret.encode(), b"%d." % t + body, hashlib.sha256).hexdigest()
    return "t=%d,v1=%s" % (t, mac)


class SignatureTest(unittest.TestCase):
    def test_valid_signature_passes(self):
        body = b'{"type":"x"}'
        self.assertTrue(billing.verify_signature(body, sign(body, t=T0), WHSEC, T0))

    def test_tampered_wrong_secret_stale_and_missing_all_fail(self):
        body = b'{"type":"x"}'
        self.assertFalse(billing.verify_signature(body + b" ", sign(body, t=T0), WHSEC, T0))
        self.assertFalse(billing.verify_signature(body, sign(body, "whsec_other", T0), WHSEC, T0))
        self.assertFalse(billing.verify_signature(body, sign(body, t=T0), WHSEC, T0 + 301))
        self.assertFalse(billing.verify_signature(body, None, WHSEC, T0))
        self.assertFalse(billing.verify_signature(body, "t=abc,v1=00", WHSEC, T0))
        self.assertFalse(billing.verify_signature(body, sign(body, t=T0), None, T0))


class HandleEventTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        core.register_drop(self.conn, "rg-1", "C", risk_tier="green", now=T0)
        self.sub = active(self.conn, "rg-1", "me", T0)

    def event(self, kind, **obj):
        return {"type": kind, "data": {"object": obj}}

    def test_checkout_then_cancel(self):
        out = billing.handle_event(self.conn, self.event(
            "checkout.session.completed", client_reference_id="w%d" % self.sub["id"],
            customer="cus_1", subscription="sub_1"), T0)
        self.assertEqual(out, "granted")
        self.assertTrue(core.is_entitled(self.conn, "ntfy", "me"))
        billing.handle_event(self.conn, self.event(
            "customer.subscription.deleted", id="sub_1", status="canceled"), T0)
        self.assertFalse(core.is_entitled(self.conn, "ntfy", "me"))

    def test_past_due_keeps_access_unpaid_drops_it(self):
        billing.handle_event(self.conn, self.event(
            "checkout.session.completed", client_reference_id="w%d" % self.sub["id"],
            customer="cus_1", subscription="sub_1"), T0)
        for status, live in (("past_due", True), ("unpaid", False), ("active", True)):
            billing.handle_event(self.conn, self.event(
                "customer.subscription.updated", id="sub_1", status=status), T0)
            self.assertEqual(core.is_entitled(self.conn, "ntfy", "me"), live, status)

    def test_bogus_references_grant_nothing(self):
        for ref in (None, "123", "w99999", "w1;drop"):
            billing.handle_event(self.conn, self.event(
                "checkout.session.completed", client_reference_id=ref,
                customer="cus_1", subscription="sub_1"), T0)
        n = self.conn.execute("SELECT COUNT(*) FROM entitlements").fetchone()[0]
        self.assertEqual(n, 0)


CAMPGROUNDS = {
    "232447": {"id": "232447", "name": "Upper Pines Campground",
               "parent": "Yosemite National Park", "state": "California"},
    "232450": {"id": "232450", "name": "Lower Pines Campground",
               "parent": "Yosemite National Park", "state": "California"},
}


class ApiTest(unittest.TestCase):
    def setUp(self):
        log.clear()
        tmp = tempfile.mkdtemp(prefix="dropwatch-watch-")
        self.db_path = os.path.join(tmp, "w.sqlite3")
        self.chans = _make_channels()
        self.httpd, self.thread, self.base = _start_server(
            self.db_path, self.chans, campgrounds=CAMPGROUNDS,
            stripe_webhook_secret=WHSEC,
            payment_link="https://buy.stripe.com/test_abc", price_label="$5/mo")
        d = dt.date.today()
        self.dates = ((d + dt.timedelta(days=7)).isoformat(),
                      (d + dt.timedelta(days=9)).isoformat())

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def req(self, method, path, body=None, headers=None):
        return http_request(self.base, method, path, body, headers)

    def watch(self, address="my-topic", **over):
        body = {"campground_id": "232447", "date_from": self.dates[0],
                "date_to": self.dates[1], "channel": "ntfy", "address": address}
        body.update(over)
        return self.req("POST", "/api/subscribe", body)

    def verified_watch(self):
        sid = self.watch()[1]["subscription_id"]
        code = self.chans["ntfy"].last_code()
        token = self.req("POST", "/api/verify", {"subscription_id": sid,
                                                 "code": code})[1]["unsubscribe_token"]
        return sid, token

    def test_search_by_name_park_and_pasted_url(self):
        names = lambda q: [c["name"] for c in self.req(  # noqa: E731
            "GET", "/api/campgrounds?q=" + q)[1]["campgrounds"]]
        self.assertEqual(names("upper"), ["Upper Pines Campground"])
        self.assertEqual(len(names("yosemite")), 2)
        self.assertEqual(names("https://www.recreation.gov/camping/campgrounds/232450"),
                         ["Lower Pines Campground"])
        self.assertEqual(names("u"), [])

    def test_watch_registers_a_pollable_drop(self):
        status, body, _, _ = self.watch()
        self.assertEqual(status, 200, body)
        conn = db.connect(self.db_path)
        drop = core.get_drop(conn, "rg-232447")
        self.assertEqual((drop["adapter"], drop["title"]),
                         ("recreation_gov", "Upper Pines Campground"))
        conn.close()

    def test_bad_watches_are_rejected(self):
        self.assertEqual(self.watch(campground_id="1")[0], 404)
        self.assertEqual(self.watch(date_from=self.dates[1], date_to=self.dates[0])[0], 400)
        self.assertEqual(self.watch(date_from="2020-01-01")[0], 400)
        far = (dt.date.today() + dt.timedelta(days=60)).isoformat()
        self.assertEqual(self.watch(date_to=far)[0], 400)
        self.assertEqual(self.watch(date_from="nope")[0], 400)

    def test_second_campground_inside_cooldown_is_429(self):
        self.watch()
        status, body, _, _ = self.watch(campground_id="232450")
        self.assertEqual(status, 429)
        self.assertGreater(body["retry_after"], 0)
        self.assertEqual(len(self.chans["ntfy"].sent), 1)

    def test_checkout_needs_the_verify_token(self):
        sid, token = self.verified_watch()
        self.assertEqual(self.req("POST", "/api/checkout",
                                  {"subscription_id": sid, "token": "x"})[0], 403)
        status, body, _, _ = self.req("POST", "/api/checkout",
                                      {"subscription_id": sid, "token": token})
        self.assertEqual(status, 200)
        self.assertEqual(body["url"], "https://buy.stripe.com/test_abc"
                                      "?client_reference_id=w%d" % sid)

    def test_signed_webhook_grants_unsigned_does_not(self):
        sid, _ = self.verified_watch()
        body = json.dumps({"type": "checkout.session.completed", "data": {"object": {
            "client_reference_id": "w%d" % sid, "customer": "cus_1",
            "subscription": "sub_1"}}}).encode()
        self.assertEqual(self.req("POST", "/api/stripe/webhook", body,
                                  {"Stripe-Signature": sign(body, "whsec_forged")})[0], 400)
        conn = db.connect(self.db_path)
        self.assertFalse(core.is_entitled(conn, "ntfy", "my-topic"))
        self.assertEqual(self.req("POST", "/api/stripe/webhook", body,
                                  {"Stripe-Signature": sign(body)})[0], 200)
        self.assertTrue(core.is_entitled(conn, "ntfy", "my-topic"))
        conn.close()

    def test_drops_reports_billing_config(self):
        billing_cfg = self.req("GET", "/api/drops")[1]["billing"]
        self.assertEqual(billing_cfg["enabled"], True)
        self.assertEqual(billing_cfg["price_label"], "$5/mo")

    def test_legal_pages_serve(self):
        for path in ("/terms", "/privacy"):
            self.assertEqual(self.req("GET", path)[0], 200, path)


class _Adapter(SourceAdapter):
    name = "src"
    min_spacing_seconds = 0
    daily_request_budget = 1000

    def fetch_key(self, target, date):
        return date[:7]


class BudgetTest(unittest.TestCase):
    def watch(self, drop, months, interval=60):
        dates = ["2026-%02d-01" % (11 + m) if m < 2 else "2027-01-01"
                 for m in range(months)]
        return {"drop_id": drop, "adapter": "src", "target": {},
                "dates": dates, "interval_seconds": interval}

    def test_affordable_watches_keep_their_interval(self):
        out = poller.budget_intervals([self.watch("a", 1, interval=600)],
                                      {"src": _Adapter()})
        self.assertEqual(out["a"], 600)

    def test_over_budget_stretches_everyone_to_fit(self):
        watches = [self.watch("a", 2), self.watch("b", 1)]
        out = poller.budget_intervals(watches, {"src": _Adapter()})
        per_day = sum(86400 / out[w["drop_id"]] * poller.fetches_per_pass(w, _Adapter())
                      for w in watches)
        self.assertAlmostEqual(per_day, 800, places=3)  # 80% of 1000


class RunForeverTest(unittest.TestCase):
    def test_picks_up_new_watches_and_drops_cancelled_ones(self):
        polled = []
        calls = {"n": 0}

        def source():
            calls["n"] += 1
            if calls["n"] == 1:
                return []
            if calls["n"] <= 3:
                return [{"drop_id": "a", "adapter": "src", "target": {},
                         "dates": ["2026-11-01"]}]
            stop.set()
            return []

        orig = poller.poll_watch
        poller.poll_watch = lambda conn, w, a, g, **kw: polled.append(w["drop_id"])
        stop = threading.Event()
        try:
            clock = [T0]
            stop_wait = stop.wait
            stop.wait = lambda s: clock.__setitem__(0, clock[0] + s)
            poller.run_forever(None, source, {"src": _Adapter()}, None,
                               stop=stop, clock=lambda: clock[0])
            stop.wait = stop_wait
        finally:
            poller.poll_watch = orig
        self.assertEqual(polled, ["a"])


if __name__ == "__main__":
    unittest.main()
