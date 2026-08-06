"""platformd.channels -- the real Discord and ntfy delivery channels.

Everything in the tree before this module was a fake: FakeChannel in
tests, _PrintChannel in --demo. Nothing had ever attempted a real POST to
Discord or ntfy -- the same works-in-mock-fails-in-reality gap that broke
the Telegram code-over-DM flow (a bot cannot DM a username it hasn't
matched first; see the "telegram absent" note on make_default_channels()
below). This module closes that gap for the two channels whose delivery
model needs nothing more than "sign/shape a request, POST it, read the
status" -- exactly platformd.webhook's model, which this module mirrors:

  - Every channel wraps an INJECTED http_post callable. Neither class
    here imports urllib or opens a socket; the kernel/tests own the
    transport, same reason platformd.webhook does it that way -- it's
    what keeps this module's entire test suite free of real network I/O.

  - ONE DELIBERATE DEVIATION from platformd.webhook.WebhookChannel's
    transport contract: that module's http_post returns a bare status
    int, because WebhookChannel never needs the response body. This
    module's http_post returns (status, response_body) instead, because
    Discord's 429 response carries a JSON body with the provider's own
    "retry_after" hint, and RateLimitedError below needs to read it.
    ntfy's http_post is called with the exact same shape for symmetry,
    even though its response bodies aren't parsed today. Signature, in
    full: http_post(url, body_bytes, headers) -> (status_int,
    response_body_bytes_or_None).

  - Transport exceptions from http_post (BrokenPipeError,
    ConnectionResetError, socket errors, anything else) are never caught
    here -- they propagate straight through send() untouched, exactly
    like WebhookChannel, so platformd.outbox's [PIPE] logging is the one
    place that owns them.

  - Neither channel logs anything itself. Logging is platformd.outbox's
    job (via platformd.log's [DELIVERY]/[PIPE] tags), same division of
    labor as platformd.webhook. PII rule enforced here by omission: these
    classes never construct a log line, so they can't be the place an
    address/topic/webhook URL leaks into one.

Exception hierarchy (importable by platformd.outbox, which treats these
as first-class dispatch outcomes alongside platformd.webhook.WebhookGone):

    DeliveryError(status)
        -- generic non-2xx the provider returned that isn't one of the
           special cases below (Discord/ntfy 5xx, Discord 403, ...).
    PermanentDeliveryError(DeliveryError)
        -- "this destination is gone for good; never retry." Raised by
           DiscordWebhookChannel on 404/410 (Discord's documented
           behavior for a webhook the receiver deleted). A shared,
           channel-agnostic marker: platformd.outbox treats an instance
           of this exactly like a platformd.webhook.WebhookGone --
           terminal status, [DELIVERY] gone=1, subscription disabled,
           and (unlike a real provider failure) NOT counted against the
           provider breaker, because one subscriber's deleted endpoint
           says nothing about Discord/ntfy being down.
    RateLimitedError(DeliveryError)
        -- 429. Transient -- the provider is asking for backoff, not
           reporting a dead destination -- so platformd.outbox handles
           it through the ordinary backoff/TTL retry path, same as any
           other DeliveryError. Carries retry_after (seconds, as the
           provider's own JSON reported it) when present, else None.

make_default_channels(http_post) is the module-level convenience the API
and the dispatch runner should use to build the real channel map; see its
own docstring for the telegram-absence note.

stdlib only.
"""

import json
import re

# ---------- delivery errors ----------


class DeliveryError(Exception):
    """Non-2xx response from a channel provider (other than the special
    cases below). Carries only the HTTP status in its message -- never
    an address, topic, or URL -- so it stays safe to log verbatim under
    the [DELIVERY] tag (PII rule: no addresses/topics/URLs in log lines).
    """

    def __init__(self, status):
        self.status = status
        super().__init__("delivery failed with status %d" % status)


class PermanentDeliveryError(DeliveryError):
    """The destination is gone for good -- never retry.

    See the module docstring's exception-hierarchy section for how
    platformd.outbox treats this (terminal, breaker-exempt, disables the
    owning subscription).
    """


class RateLimitedError(DeliveryError):
    """429 from a provider -- transient, not permanent.

    retry_after is the provider's own hint in seconds (float or int),
    parsed from its JSON body when present; None when the body was
    absent, unparseable, or simply didn't include the field. A missing
    or unparseable retry_after is never an error in itself -- callers
    that want to honor it treat None as "no hint given," not "malformed
    response."
    """

    def __init__(self, status, retry_after=None):
        self.retry_after = retry_after
        super().__init__(status)


def _parse_retry_after(response_body):
    """Best-effort extraction of a 429 response's "retry_after" field.

    Returns None on anything short of a clean numeric field in a JSON
    object body -- absent body, unparseable JSON, non-dict JSON, missing
    key, or a non-numeric value all fall through to None rather than
    raising. The exact backoff hint is a nice-to-have; a malformed one
    must never turn a rate-limit response into a crash.
    """
    if not response_body:
        return None
    try:
        data = json.loads(response_body)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("retry_after")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


# ---------- Discord ----------

# Discord's documented webhook URL shape -- discord.com or the legacy
# discordapp.com host, a numeric webhook id, then an opaque token.
# Mirrors platformd.api's own _ADDRESS_PATTERNS["discord"] shape check
# (that module owns the subscribe-time gate; this is the send-time one --
# duplicated rather than shared to keep this module import-independent of
# platformd.api).
DISCORD_WEBHOOK_URL_PATTERN = re.compile(
    r"^https://discord(?:app)?\.com/api/webhooks/\d+/[\w.-]+$"
)

# Discord hard-truncates (400s) any content field over this length -- we
# truncate first so a long alert message degrades gracefully instead of
# failing delivery outright.
DISCORD_MAX_CONTENT_LEN = 2000


class DiscordWebhookChannel:
    """Discord incoming-webhook delivery channel.

    http_post(url, body_bytes, headers) -> (status_int, response_body) is
    INJECTED by the caller (kernel in production, a fake in tests) -- see
    the module docstring for the exact contract and why it differs from
    platformd.webhook.WebhookChannel's plain-int return.
    """

    def __init__(self, http_post):
        self.http_post = http_post

    def send(self, address, message, context=None):
        """POST {"content": message[:2000]} to the Discord webhook URL
        in `address`. Returns True or raises.

        context is accepted (for call-shape parity with every other
        channel's send()) but unused -- Discord's incoming-webhook API
        takes no per-delivery secret or routing hint beyond the URL
        itself.

        Never swallows: a 2xx status returns True; 404/410 (Discord's
        documented response for a deleted webhook) raises
        PermanentDeliveryError; 429 raises RateLimitedError carrying
        retry_after when the body supplied one; any other non-2xx raises
        DeliveryError. BrokenPipeError (or anything else) from
        http_post propagates untouched -- the dispatcher owns [PIPE]
        logging, this module must not catch-and-hide it.
        """
        if not isinstance(address, str) or not DISCORD_WEBHOOK_URL_PATTERN.match(address):
            raise ValueError("address is not a Discord webhook URL")

        body = json.dumps(
            {"content": message[:DISCORD_MAX_CONTENT_LEN]}
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}

        status, response_body = self.http_post(address, body, headers)

        if 200 <= status < 300:
            return True
        if status in (404, 410):
            raise PermanentDeliveryError(status)
        if status == 429:
            raise RateLimitedError(status, retry_after=_parse_retry_after(response_body))
        raise DeliveryError(status)


# ---------- ntfy ----------

NTFY_TOPIC_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# ntfy's documented priority levels, low to high. Anything else is a
# caller bug, not a value ntfy will just clamp -- reject it up front.
NTFY_PRIORITIES = frozenset({"min", "low", "default", "high", "urgent"})

NTFY_DEFAULT_BASE_URL = "https://ntfy.sh"


class NtfyChannel:
    """ntfy topic-push delivery channel.

    http_post(url, body_bytes, headers) -> (status_int, response_body) is
    INJECTED by the caller, same contract as DiscordWebhookChannel -- see
    the module docstring.
    """

    def __init__(self, http_post, base_url=NTFY_DEFAULT_BASE_URL):
        self.http_post = http_post
        self.base_url = base_url.rstrip("/")

    def send(self, address, message, context=None):
        """POST message as the body to base_url/<topic>, where `address`
        IS the topic (ntfy has no separate handle/URL split). Returns
        True or raises.

        context, when given, may carry "priority" -- one of ntfy's five
        documented levels (min/low/default/high/urgent); anything else
        raises ValueError. Absent or None context defaults to "default".

        Never swallows: a 2xx status returns True; 429 raises
        RateLimitedError carrying retry_after when the body supplied
        one; any other non-2xx raises DeliveryError. BrokenPipeError (or
        anything else) from http_post propagates untouched -- the
        dispatcher owns [PIPE] logging, this module must not
        catch-and-hide it.
        """
        topic = address
        if not isinstance(topic, str) or not NTFY_TOPIC_PATTERN.match(topic):
            raise ValueError("ntfy topic must match ^[A-Za-z0-9_-]{1,64}$")

        context = context if context is not None else {}
        priority = context.get("priority", "default")
        if priority not in NTFY_PRIORITIES:
            raise ValueError(
                "priority must be one of: %s" % ", ".join(sorted(NTFY_PRIORITIES))
            )

        url = "%s/%s" % (self.base_url, topic)
        body = message.encode("utf-8")
        headers = {"Title": "Dropwatch", "Priority": priority}

        status, response_body = self.http_post(url, body, headers)

        if 200 <= status < 300:
            return True
        if status == 429:
            raise RateLimitedError(status, retry_after=_parse_retry_after(response_body))
        raise DeliveryError(status)


# ---------- convenience ----------


def make_default_channels(http_post):
    """Build the real {"discord": ..., "ntfy": ...} channel map.

    Telegram is DELIBERATELY ABSENT here: a Telegram bot cannot DM an
    arbitrary @username -- it can only message a chat that has already
    messaged (or /start'ed) it first. Delivering to a subscriber's
    Telegram handle needs a bot-handshake flow (bind the subscriber's
    /start to their subscription_id) that does not exist yet; wiring a
    fake "telegram" sender in here today would repeat exactly the
    works-in-mock-fails-in-reality mistake this module exists to fix.
    Add "telegram" to this dict only once that handshake is real.

    http_post is the caller's real transport (an actual https POST,
    injected -- never opened by this module or anything it returns; see
    the module docstring for the exact (status, body) contract). Callers
    that want print-only fakes instead (--demo, tests) build their own
    channel objects and must not call this function.
    """
    return {
        "discord": DiscordWebhookChannel(http_post),
        "ntfy": NtfyChannel(http_post),
    }
