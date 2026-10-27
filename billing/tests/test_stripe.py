"""billing.stripe on its own: signature verification and event handling
against an in-memory entitlement store. No engine, no network. The
engine-backed path is covered in app/tests/test_watches.py."""

import hashlib
import hmac
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from billing import stripe  # noqa: E402

SECRET = "whsec_test"
T0 = 1_700_000_000


def sign(payload, secret=SECRET, t=T0):
    mac = hmac.new(secret.encode(), b"%d." % t + payload, hashlib.sha256)
    return "t=%d,v1=%s" % (t, mac.hexdigest())


class FakeStore:
    """The two-method interface billing writes through."""

    def __init__(self, known_ids):
        self.known = set(known_ids)
        self.paid = {}  # stripe subscription id -> active?

    def grant(self, subscription_id, stripe_customer, stripe_subscription, now):
        if subscription_id not in self.known:
            return None
        self.paid[stripe_subscription] = True
        return ("ntfy", "topic")

    def set_by_stripe_subscription(self, stripe_subscription, active, now):
        if stripe_subscription not in self.paid:
            return 0
        self.paid[stripe_subscription] = active
        return 1


def event(kind, **obj):
    return {"type": kind, "data": {"object": obj}}


class VerifySignatureTest(unittest.TestCase):
    def test_valid_and_tampered(self):
        body = json.dumps({"id": "evt_1"}).encode()
        self.assertTrue(stripe.verify_signature(body, sign(body), SECRET, T0))
        self.assertFalse(stripe.verify_signature(body + b" ", sign(body), SECRET, T0))
        self.assertFalse(stripe.verify_signature(body, sign(body, "whsec_x"), SECRET, T0))

    def test_any_of_several_v1_signatures_may_match(self):
        # Stripe sends two v1 values while a secret is being rolled
        body = b"{}"
        good = sign(body).split(",")[1]
        header = "t=%d,v1=%s,%s" % (T0, "00" * 32, good)
        self.assertTrue(stripe.verify_signature(body, header, SECRET, T0))

    def test_replay_window(self):
        body = b"{}"
        self.assertTrue(stripe.verify_signature(body, sign(body), SECRET, T0 + 300))
        self.assertFalse(stripe.verify_signature(body, sign(body), SECRET, T0 + 301))
        self.assertFalse(stripe.verify_signature(body, sign(body), SECRET, T0 - 301))


class HandleEventTest(unittest.TestCase):
    def test_checkout_grants_and_cancel_revokes(self):
        store = FakeStore({7})
        out = stripe.handle_event(store, event(
            "checkout.session.completed", client_reference_id="w7",
            customer="cus_1", subscription="sub_1"), T0)
        self.assertEqual(out, "granted")
        self.assertEqual(store.paid, {"sub_1": True})
        out = stripe.handle_event(store, event(
            "customer.subscription.deleted", id="sub_1", status="canceled"), T0)
        self.assertEqual(out, "inactive x1")
        self.assertEqual(store.paid, {"sub_1": False})

    def test_unknown_watch_and_one_time_payments_grant_nothing(self):
        store = FakeStore({7})
        self.assertEqual(stripe.handle_event(store, event(
            "checkout.session.completed", client_reference_id="w8",
            subscription="sub_1"), T0), "ignored: unknown watch")
        self.assertTrue(stripe.handle_event(store, event(
            "checkout.session.completed", client_reference_id="w7"),
            T0).startswith("ignored"))
        self.assertEqual(store.paid, {})

    def test_unrelated_events_are_acknowledged(self):
        self.assertEqual(stripe.handle_event(FakeStore(()), event("invoice.paid"), T0),
                         "ignored: invoice.paid")

    def test_checkout_url_carries_the_watch_reference(self):
        self.assertEqual(stripe.checkout_url("https://buy.stripe.com/x", 12),
                         "https://buy.stripe.com/x?client_reference_id=w12")
        self.assertEqual(stripe.checkout_url("https://buy.stripe.com/x?a=1", 12),
                         "https://buy.stripe.com/x?a=1&client_reference_id=w12")


if __name__ == "__main__":
    unittest.main()
