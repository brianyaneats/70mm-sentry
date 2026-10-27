"""platformd.billing -- Stripe, without the Stripe SDK or a single
outbound API call.

The whole integration is two no-code Stripe objects plus one webhook:

    Payment Link   the checkout page. We append ?client_reference_id=w<id>
                   (the subscription the buyer was looking at) and Stripe
                   echoes it back on checkout.session.completed.
    Customer portal  Stripe-hosted cancel/update-card page; we only link it.
    Webhook        POST /api/stripe/webhook, signed with the endpoint's
                   whsec_ secret. The ONLY writer of the entitlements table.

Trust comes from the signature, never from the request: an unsigned or
stale body changes nothing. Handlers are idempotent upserts because
Stripe redelivers on any non-2xx and does not promise ordering.
"""

import hashlib
import hmac
import re
from urllib.parse import urlencode

from platformd import core

SIGNATURE_TOLERANCE_SECONDS = 300
_REF_PATTERN = re.compile(r"w(\d{1,12})\Z")

# Stripe subscription statuses that keep instant alerts on. past_due is
# Stripe's retry window -- cutting a customer off mid-retry for a card
# that will likely succeed tomorrow is worse than a few free days.
_LIVE_STATUSES = {"active", "trialing", "past_due"}


def checkout_url(payment_link, subscription_id):
    sep = "&" if "?" in payment_link else "?"
    return payment_link + sep + urlencode(
        {"client_reference_id": "w%d" % subscription_id})


def verify_signature(payload, header, secret, now,
                     tolerance=SIGNATURE_TOLERANCE_SECONDS):
    """Stripe-Signature: "t=<unix>,v1=<hex hmac>[,v1=...]". The signed
    string is "<t>.<raw body>"; the timestamp bound stops a captured
    request from being replayed later."""
    if not header or not secret:
        return False
    parts = {}
    for item in header.split(","):
        k, _, v = item.partition("=")
        parts.setdefault(k.strip(), []).append(v.strip())
    try:
        t = int(parts["t"][0])
    except (KeyError, ValueError):
        return False
    if abs(now - t) > tolerance:
        return False
    expected = hmac.new(secret.encode(), b"%d." % t + payload,
                        hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", []))


def handle_event(conn, event, now):
    """Apply one verified Stripe event. Returns a short outcome string
    for the log; unknown event types are acknowledged and ignored."""
    kind = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}

    if kind == "checkout.session.completed":
        m = _REF_PATTERN.match(obj.get("client_reference_id") or "")
        if not m or not obj.get("subscription"):
            return "ignored: no watch reference or not a subscription"
        granted = core.grant_entitlement(conn, int(m.group(1)),
                                         obj.get("customer"),
                                         obj["subscription"], now)
        return "granted" if granted else "ignored: unknown watch"

    if kind in ("customer.subscription.updated",
                "customer.subscription.deleted"):
        live = (kind == "customer.subscription.updated"
                and obj.get("status") in _LIVE_STATUSES)
        n = core.set_entitlement_by_stripe_subscription(
            conn, obj.get("id"), live, now)
        return "%s x%d" % ("active" if live else "inactive", n)

    return "ignored: %s" % kind
