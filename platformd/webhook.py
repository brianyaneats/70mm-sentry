"""platformd.webhook — the signed-WEBHOOK channel: "automate it yourself".

We POST a signed, structured JSON event to a URL the subscriber controls.
Their own automation (a Lambda, a Zapier hook, a home server) verifies the
signature and acts with ITS OWN credentials on ITS OWN infrastructure. We
never see, hold, or need the subscriber's downstream secrets — only the
per-subscription webhook_secret used to sign outbound requests, so the
receiver can prove the POST really came from us.

This module owns three concerns:

  1. Request-shape address safety (is_safe_url) — reject the cheap,
     unambiguous SSRF shapes at subscribe time: non-https, embedded
     userinfo, and literal-IP hosts in private/reserved/loopback/
     link-local/metadata ranges. This is deliberately NOT a full SSRF
     guard: a hostname like "internal.corp.example" that *resolves* to
     169.254.169.254 sails through here, because resolving it would mean
     this module doing DNS, which is out of scope for a pure request-shape
     gate. Resolution-time SSRF (DNS rebinding, a hostname that resolves
     somewhere private) is a production egress-layer concern — the
     outbound HTTP client must be pinned or proxied through an egress
     guard that resolves-then-checks-then-connects atomically. Treat this
     function as the cheap first gate, not the whole defense.

  2. The public event payload contract (build_event_payload) — a
     versioned, stable JSON schema subscribers' automation can parse
     without guessing.

  3. WebhookChannel — signs and POSTs the payload via an INJECTED
     http_post callable. This module never imports urllib or opens a
     socket itself; the kernel/tests own the transport so sends stay
     mockable and the whole suite stays network-free.

stdlib only.
"""

import hashlib
import hmac
import ipaddress
import json
import re
import secrets
from urllib.parse import urlparse

# ---------- address validation ----------

# https:// only, and capped at 512 chars total (scheme included) so a
# pathological address can't bloat storage or log lines. This is the
# shape check used at subscribe time; is_safe_url() below does the
# deeper (still request-shape-only) safety check.
WEBHOOK_ADDRESS_PATTERN = re.compile(r"^https://\S{1,504}$")


def _hostname_from_url(url):
    """Return the URL's hostname, stripped of IPv6 brackets, or None."""
    parsed = urlparse(url)
    return parsed, parsed.hostname


def is_safe_url(url):
    """Request-shape SSRF gate for a candidate webhook address.

    Returns (True, None) if the URL passes every check, else
    (False, reason) with a short, non-PII reason string (never echoes the
    URL itself back — callers that log the reason must not separately
    tack on the raw address).

    Checks, in order:
      - matches WEBHOOK_ADDRESS_PATTERN (https://, <=512 chars total)
      - no userinfo (`user@host` / `user:pass@host` forms)
      - if the hostname is a literal IP (v4 or v6), it must not be
        private / loopback / link-local / reserved / unspecified /
        multicast (this catches 127.0.0.1, 10.x, 169.254.169.254,
        [::1], 0.0.0.0, etc. -- anything ipaddress recognizes as
        non-public)

    DNS resolution is deliberately NOT performed: a hostname is only
    checked when it is ALREADY a literal IP. See the module docstring
    for why that's a real gap and where it must be closed in production.
    """
    if not isinstance(url, str) or not WEBHOOK_ADDRESS_PATTERN.match(url):
        return False, "must be an https:// URL of at most 512 characters"

    parsed = urlparse(url)

    if parsed.scheme != "https":
        return False, "scheme must be https"

    if parsed.username is not None or parsed.password is not None:
        return False, "userinfo (user@host) is not allowed in webhook addresses"

    hostname = parsed.hostname
    if not hostname:
        return False, "URL has no hostname"

    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        # Not a literal IP -- it's a DNS name. Resolution-time SSRF is
        # out of scope here; see module docstring.
        return True, None

    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_unspecified or ip.is_multicast):
        return False, "literal IP host is in a private/reserved/loopback/link-local range"

    return True, None


# ---------- signing ----------

def sign(payload_bytes, secret):
    """Return the hex HMAC-SHA256 of payload_bytes under secret.

    secret must be str or bytes; str is encoded utf-8 first. Verification
    on the receiving end is exactly hmac.new(secret, body, sha256).hexdigest()
    compared with hmac.compare_digest -- documented in the payload's
    X-Dropwatch-Signature header format ("sha256=<hex>").
    """
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    return hmac.new(secret, payload_bytes, hashlib.sha256).hexdigest()


def generate_secret():
    """Generate a new per-subscription webhook signing secret."""
    return secrets.token_hex(32)


# ---------- event payload contract ----------

# PUBLIC CONTRACT: subscribers' automation parses this shape. Changing an
# existing key's meaning or removing a key is a breaking change and needs
# a new "schema" value (dropwatch.event.v2, ...); additive-only changes
# may keep v1 but should still be treated with care since parsers may do
# exact-key-set validation on their end, same as our own tests do on ours.
EVENT_SCHEMA_V1 = "dropwatch.event.v1"


def build_event_payload(row_dict):
    """Build the public webhook event payload from an event-ish dict.

    row_dict is read via .get(), so it may be an events-table row, a
    plain dict, or anything dict-like; only keys are read, never written.
    Recognized input keys:
        drop_id, event_id, kind, unit_id  -- as recorded by platformd.core
        detected_at                       -- preferred timestamp key
        recorded_at                       -- fallback if detected_at absent
                                              (this is the events-table
                                              column name)
        message                           -- human-facing summary line

    Returns a dict with EXACTLY this key set (this is the stable, public
    schema -- do not add or remove keys without bumping "schema"):
        schema, drop_id, event_id, kind, unit_id, detected_at, message
    """
    detected_at = row_dict.get("detected_at")
    if detected_at is None:
        detected_at = row_dict.get("recorded_at")

    return {
        "schema": EVENT_SCHEMA_V1,
        "drop_id": row_dict.get("drop_id"),
        "event_id": row_dict.get("event_id"),
        "kind": row_dict.get("kind"),
        "unit_id": row_dict.get("unit_id"),
        "detected_at": detected_at,
        "message": row_dict.get("message", ""),
    }


# Keys build_event_payload() always returns -- used by tests (and safe
# for callers) to assert schema stability without hardcoding the set
# twice.
EVENT_PAYLOAD_KEYS = frozenset(
    {"schema", "drop_id", "event_id", "kind", "unit_id", "detected_at", "message"}
)


# ---------- delivery errors ----------

class WebhookDeliveryError(Exception):
    """Non-2xx response from a webhook endpoint (other than 410 Gone).

    Carries the HTTP status so the dispatcher can decide retry policy.
    Never includes the address or the signing secret in its message --
    only the status code -- so it stays safe to log verbatim under the
    [DELIVERY] tag (PII rule: no addresses/tokens in log lines).
    """

    def __init__(self, status):
        self.status = status
        super().__init__("webhook delivery failed with status %d" % status)


class WebhookGone(WebhookDeliveryError):
    """410 Gone from a webhook endpoint -- the receiver deleted it.

    Distinct from WebhookDeliveryError so the dispatcher can treat it as
    terminal (never retry-storm a dead endpoint) instead of backing off
    and trying again like a transient failure.
    """

    def __init__(self):
        super().__init__(410)


# ---------- channel ----------

class WebhookChannel:
    """Signed-webhook delivery channel.

    http_post(url, body_bytes, headers) -> status_int is INJECTED by the
    caller (kernel in production, a fake in tests). This module never
    imports urllib or touches a socket -- transport is entirely the
    caller's responsibility, which is what keeps every test here free of
    real network I/O.
    """

    def __init__(self, http_post):
        self.http_post = http_post

    def send(self, address, message, context=None):
        """Sign and POST one event to address. Returns True or raises.

        context, when given, carries the event's fields (drop_id,
        event_id, kind, unit_id, detected_at/recorded_at) plus the
        mandatory "webhook_secret" used to sign this delivery. When
        context has no event_id (including context=None), the payload
        falls back to a minimal {schema, message} body -- e.g. for a
        one-off verification ping that has a secret but no event to
        describe yet.

        Never swallows: a 2xx status returns True; a 410 raises
        WebhookGone; any other non-2xx raises WebhookDeliveryError;
        BrokenPipeError (or anything else) from http_post propagates
        untouched -- the dispatcher owns [PIPE] logging, this module
        must not catch-and-hide it.
        """
        context = context if context is not None else {}
        try:
            secret = context["webhook_secret"]
        except KeyError:
            raise ValueError(
                "context must include 'webhook_secret' to sign a webhook delivery"
            )

        if context.get("event_id") is not None:
            row = dict(context)
            row.setdefault("message", message)
            payload = build_event_payload(row)
        else:
            payload = {"schema": EVENT_SCHEMA_V1, "message": message}

        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        signature = sign(body, secret)
        headers = {
            "Content-Type": "application/json",
            "X-Dropwatch-Signature": "sha256=%s" % signature,
            "X-Dropwatch-Event": payload.get("event_id") or "",
        }

        status = self.http_post(address, body, headers)

        if 200 <= status < 300:
            return True
        if status == 410:
            raise WebhookGone()
        raise WebhookDeliveryError(status)
