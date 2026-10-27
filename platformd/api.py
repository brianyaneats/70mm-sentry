"""platformd.api — client-facing HTTP API + static UI for the drop platform.

stdlib http.server.ThreadingHTTPServer only. make_server() builds (but does
not start) a server bound to 127.0.0.1 by default with an ephemeral port
(port=0) so tests never race a fixed port. Every request opens its OWN
sqlite3 connection via platformd.db.connect(db_path) -- sqlite3 connections
are not thread-safe across threads, and ThreadingHTTPServer hands each
request its own thread, so sharing one connection would raise. This means
db_path must be a real file (or a shared-cache DSN); a bare ':memory:'
would hand each request a distinct, empty database.

Channel sends (the verify code on subscribe) go through the same contract
as platformd.outbox.dispatch(): channels[name].send(address, message) must
return True or raise -- never return False silently. Tests and --demo both
inject fake/print senders; this module never makes a real network call.

Endpoints, all JSON, request bodies capped at MAX_BODY_BYTES (413 over,
400 on malformed JSON):
    GET  /api/drops        -- catalog + per-drop health
    GET  /api/status       -- per-drop health only, keyed by drop_id
    POST /api/subscribe    -- {drop_id, channel, address, tier?} -> subscription_id
    POST /api/verify       -- {subscription_id, code} -> unsubscribe_token | 403
    POST /api/unsubscribe  -- {token} -> ok
    GET  /api/campgrounds  -- ?q=<name or recreation.gov URL> -> top matches
    POST /api/subscribe    -- also {campground_id, date_from, date_to,
                              channel, address}: a watch on one
                              campground's dates (drop "rg-<id>")
    POST /api/checkout     -- {subscription_id, token} -> {url} (Stripe
                              Payment Link carrying the watch reference)
    POST /api/stripe/webhook -- Stripe-signed events; see platformd.billing.
                              Exempt from the IP limiter (Stripe bursts
                              from few IPs); the signature is the gate.
    GET  /terms, /privacy  -- static legal pages
    GET  /unsubscribe      -- renders a confirm page for ?token=...; the
                              GET itself is inert (safe method -- it
                              changes nothing), a click on the page's own
                              button POSTs /api/unsubscribe via inline JS.
                              Because the token rides a URL, it WILL show
                              up in server access logs and any link
                              scanner/prefetcher that GETs it -- accepted,
                              because the token is single-purpose (can
                              only disable a subscription, nothing else),
                              high-entropy (secrets.token_hex(16), see
                              core.verify()), and never needs rotating on
                              use: there's nothing worth protecting behind
                              it once you already know what it can do.
    GET  /                 -- serves static/index.html

POSTs are per-client-IP rate limited (in-memory token bucket, default 10/
60s, widened via make_server()'s rate_limit_* kwargs for load tests) --
tripping it logs [RATELIMIT] with the client ip only, never the address.
Addresses are never written to any log line anywhere in this module. The
rate-limit key is the raw socket peer by default; make_server(trust_proxy=
True) switches it to the first X-Forwarded-For hop for deployments that
sit behind a real reverse proxy -- see make_server()'s docstring for why
that must never be the default.

Webhook is a channel like any other (VALID_CHANNELS), but its address is
a URL rather than a handle/id, so it gets two extra checks unique to it:
shape (platformd.webhook.WEBHOOK_ADDRESS_PATTERN) and safety
(platformd.webhook.is_safe_url(), an SSRF guard against local/link-local
targets) -- a rejection surfaces is_safe_url's reason text but the URL
itself is never echoed back in the response. platformd.webhook is
imported lazily, only on an actual channel="webhook" request, so this
module has no hard import-time dependency on it. A fresh webhook_secret
(platformd.webhook.generate_secret()) is minted per subscription and
never appears in any API response after the row is created, nor in any
log line -- only subscription_id ever identifies a row in output/logs.
"""

import argparse
import datetime as dt
import json
import os
import re
import secrets
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from platformd import billing, core, db, log as _log

MAX_BODY_BYTES = 16 * 1024
# Stripe event bodies embed whole objects; 16K is too tight for them.
MAX_STRIPE_BODY_BYTES = 256 * 1024

VALID_CHANNELS = ("telegram", "discord", "ntfy", "webhook")

# Real senders for "discord" and "ntfy" live in platformd.channels
# (DiscordWebhookChannel / NtfyChannel, both http_post-injected exactly
# like platformd.webhook.WebhookChannel -- see that module's docstring).
# "telegram" has no real sender anywhere yet -- a bot can't DM a bare
# @username without a prior bot-handshake, so make_default_channels()
# deliberately omits it. This module itself never constructs a real
# sender: production callers wire platformd.channels.make_default_channels()
# into make_server()'s channels= themselves; --demo below always uses
# print-only fakes for every channel, real senders included, never a real
# network call.

# Per-channel address sanity checks. These are shape checks only -- no
# network call ever validates an address actually resolves.
_ADDRESS_PATTERNS = {
    "telegram": re.compile(r"^(@[A-Za-z0-9_]{5,32}|-?\d{5,15})\Z"),
    "discord": re.compile(r"^https://discord(?:app)?\.com/api/webhooks/\d+/[\w.-]+\Z"),
    "ntfy": re.compile(r"^[A-Za-z0-9_-]{1,64}\Z"),
}

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
INDEX_PATH = os.path.join(STATIC_DIR, "index.html")
HOW_PATH = os.path.join(STATIC_DIR, "how.html")
LEGAL_PAGES = {"/terms": os.path.join(STATIC_DIR, "terms.html"),
               "/privacy": os.path.join(STATIC_DIR, "privacy.html")}

# A watch covers at most this many nights: each extra month of range is
# another upstream request per pass, paid for out of a shared budget.
MAX_WATCH_DAYS = 31
MAX_WATCH_HORIZON_DAYS = 366
CAMPGROUND_RESULTS = 12
_REC_GOV_URL = re.compile(r"recreation\.gov/camping/campgrounds/(\d+)")


def load_campgrounds(path):
    """{id: {"id", "name", "parent", "state"}} from tools/build_campgrounds.py
    output, or {} when no index is configured."""
    if not path:
        return {}
    with open(path) as fh:
        return {c["id"]: c for c in json.load(fh)}


def search_campgrounds(index, q):
    """Prefix matches on the name first, then substring matches on name
    or parent (park/forest). A pasted recreation.gov URL resolves by id."""
    q = (q or "").strip()
    m = _REC_GOV_URL.search(q)
    if m:
        hit = index.get(m.group(1))
        return [hit] if hit else []
    q = q.lower()
    if len(q) < 2:
        return []
    prefix, contains = [], []
    for c in index.values():
        name = c["name"].lower()
        if name.startswith(q):
            prefix.append(c)
        elif q in name or q in c["parent"].lower():
            contains.append(c)
        if len(prefix) >= CAMPGROUND_RESULTS:
            break
    return (prefix + contains)[:CAMPGROUND_RESULTS]


def _parse_watch_dates(date_from, date_to, today):
    """(date_from, date_to) as ISO strings, or raise ValueError with a
    message fit for the client."""
    try:
        start = dt.date.fromisoformat(date_from)
        end = dt.date.fromisoformat(date_to)
    except (TypeError, ValueError):
        raise ValueError("dates must be YYYY-MM-DD") from None
    if start > end:
        raise ValueError("check-in must be on or before check-out")
    if start < today:
        raise ValueError("dates must not be in the past")
    if (end - start).days >= MAX_WATCH_DAYS:
        raise ValueError("a watch can cover at most %d nights" % MAX_WATCH_DAYS)
    if (end - today).days > MAX_WATCH_HORIZON_DAYS:
        raise ValueError("dates must be within a year")
    return start.isoformat(), end.isoformat()

# Minimal standalone confirm page for GET /unsubscribe?token=... -- reuses
# index.html's design tokens (ink/bg/surface/hairline/radius) for a
# consistent look without pulling in the whole static bundle. %(token_js)s
# is a JSON-encoded string literal, substituted by _serve_unsubscribe_page.
_UNSUBSCRIBE_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Unsubscribe — Dropwatch</title>
<style>
  :root {
    --ink: #131517;
    --ink-64: rgba(19, 21, 23, .64);
    --hairline: rgba(19, 21, 23, .08);
    --bg: #F7F8F9;
    --surface: #FFFFFF;
    --radius: 8px;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --ink: #F2F3F4;
      --ink-64: rgba(242, 243, 244, .64);
      --hairline: rgba(242, 243, 244, .10);
      --bg: #0E0F10;
      --surface: #161819;
    }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--bg);
    color: var(--ink);
    font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  .card {
    width: calc(100%% - 48px);
    max-width: 380px;
    background: var(--surface);
    border: 1px solid var(--hairline);
    border-radius: var(--radius);
    padding: 32px;
    text-align: center;
  }
  h1 { font-size: 18px; font-weight: 600; margin: 0 0 12px; }
  p { font-size: 13px; line-height: 22px; color: var(--ink-64); margin: 0 0 20px; }
  .btn {
    appearance: none;
    -webkit-appearance: none;
    font-family: inherit;
    font-size: 13px;
    font-weight: 500;
    height: 36px;
    padding: 0 20px;
    border-radius: var(--radius);
    cursor: pointer;
    background: var(--surface);
    color: var(--ink);
    border: 1px solid var(--hairline);
  }
  .btn:disabled { opacity: .5; cursor: default; }
  .msg { margin-top: 16px; font-size: 13px; }
</style>
</head>
<body>
<div class="card">
  <h1>Stop these alerts?</h1>
  <p>This page does nothing on its own. Click below to confirm you want
     this address unsubscribed.</p>
  <button class="btn" id="confirm-btn" type="button">Unsubscribe</button>
  <p class="msg" id="msg"></p>
</div>
<script>
(function () {
  "use strict";
  var token = %(token_js)s;
  var btn = document.getElementById("confirm-btn");
  var msg = document.getElementById("msg");
  btn.addEventListener("click", function () {
    if (!token) {
      msg.textContent = "Missing unsubscribe token.";
      return;
    }
    btn.disabled = true;
    fetch("/api/unsubscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: token })
    }).then(function () {
      msg.textContent = "You have been unsubscribed.";
    }).catch(function () {
      btn.disabled = false;
      msg.textContent = "Network error. Try again.";
    });
  });
})();
</script>
</body>
</html>
"""

DEMO_HOST = "127.0.0.1"
DEMO_PORT = 8787


_BARE_TELEGRAM_HANDLE = re.compile(r"^[A-Za-z0-9_]{5,32}\Z")


def _normalize_address(channel, address):
    """Canonicalize before validating. Telegram handles arrive with or
    without the leading @ (people type what they see in their profile,
    which shows both forms) -- a bare handle is canonicalized to
    @handle rather than rejected. Real-user friction, found live
    2026-08-04: 'easy_score' bounced off the @-only pattern."""
    if not isinstance(address, str):
        return address
    address = address.strip()
    if channel == "telegram" and _BARE_TELEGRAM_HANDLE.match(address):
        return "@" + address
    return address


def _valid_address(channel, address):
    if not isinstance(address, str) or not address:
        return False
    if channel == "webhook":
        from platformd import webhook  # lazy: see module docstring
        pattern = webhook.WEBHOOK_ADDRESS_PATTERN
    else:
        pattern = _ADDRESS_PATTERNS.get(channel)
    if pattern is None:
        return False
    return pattern.match(address) is not None


class RateLimiter:
    """In-memory per-key token bucket. Not persisted, not shared across
    processes -- restarting the server resets everyone's budget, which is
    fine for a single-box client-facing API.
    """

    def __init__(self, capacity=10, window_seconds=60.0):
        self.capacity = float(capacity)
        self.window_seconds = float(window_seconds)
        self._lock = threading.Lock()
        self._buckets = {}  # key -> (tokens, last_refill_ts)

    def allow(self, key, now=None):
        if now is None:
            now = time.time()
        refill_rate = self.capacity / self.window_seconds
        with self._lock:
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * refill_rate)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return False
            tokens -= 1.0
            self._buckets[key] = (tokens, now)
            return True

    def reset(self):
        with self._lock:
            self._buckets.clear()


def _drop_health(conn, drop_row):
    last_event = conn.execute(
        "SELECT MAX(recorded_at) AS ts FROM events WHERE drop_id = ?",
        (drop_row["id"],),
    ).fetchone()
    last_event_at = last_event["ts"] if last_event is not None else None

    if drop_row["risk_tier"] == "yellow":
        return {
            "state": "amber",
            "last_event_at": last_event_at,
            "detail": "unofficial data source - may go dark without notice",
        }
    return {"state": "green", "last_event_at": last_event_at, "detail": None}


def _send_json(handler, status, payload):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        pass


# Sent on every response. HTML gets a per-response CSP nonce on top (see
# _html_with_nonce); JSON responses get the strictest possible policy.
_BASELINE_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),     # /unsubscribe carries its token
    ("X-Frame-Options", "DENY"),             # legacy twin of frame-ancestors
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
    # Browsers ignore HSTS over plain http, so this is a no-op locally and
    # binding behind the TLS edge in production.
    ("Strict-Transport-Security", "max-age=31536000; includeSubDomains"),
)

_CSP_HTML = ("default-src 'none'; script-src 'nonce-%s'; "
             "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
             "connect-src 'self'; base-uri 'none'; form-action 'self'; "
             "frame-ancestors 'none'")
_CSP_JSON = "default-src 'none'; frame-ancestors 'none'"


def _html_with_nonce(body):
    """Stamp a fresh nonce on every <script> tag; returns (body, nonce).
    A nonce instead of 'unsafe-inline' means an injected <script> --
    should any template ever echo user input -- simply doesn't run."""
    nonce = secrets.token_urlsafe(16)
    return body.replace(b"<script>", b'<script nonce="%s">' % nonce.encode()), nonce


class _Handler(BaseHTTPRequestHandler):
    # Set on the subclass built by make_server().
    db_path = None
    channels = None
    limiter = None
    trust_proxy = False
    client_ip_header = None
    campgrounds = {}
    stripe_webhook_secret = None
    payment_link = None
    portal_url = None
    price_label = None
    server_version = "DropwatchAPI/1"
    sys_version = ""          # don't advertise the Python build in Server:
    # Per-connection socket timeout: without one, a client that opens a
    # connection and never finishes the request pins a thread forever.
    timeout = 30
    _csp_nonce = None

    def end_headers(self):
        for name, value in _BASELINE_HEADERS:
            self.send_header(name, value)
        if self._csp_nonce:
            self.send_header("Content-Security-Policy",
                             _CSP_HTML % self._csp_nonce)
        else:
            self.send_header("Content-Security-Policy", _CSP_JSON)
        self._csp_nonce = None     # never leak onto a keep-alive sibling
        super().end_headers()

    def log_message(self, fmt, *args):
        # Silence BaseHTTPRequestHandler's default per-request stderr line.
        # Our own failure logging goes through platformd.log's fixed,
        # grep-able tag vocabulary instead; a free-form access log here
        # would be a second, untagged channel that could end up carrying
        # a subscriber address in the request line or query string.
        pass

    # ---------------------------------------------------------- helpers --

    def _conn(self):
        return db.connect(self.db_path)

    def _client_ip(self):
        """Rate-limit bucket key.

        X-Forwarded-For is attacker-controlled by default -- any client
        can set it to a fresh value on every request and get a brand new
        rate-limit bucket every time, defeating the limiter entirely.
        It's honored ONLY when trust_proxy=True, an explicit deployment-
        time opt-in for a topology where a real reverse proxy sits in
        front of us and can be trusted to have set (or overwritten) it.
        Even then, only the LAST hop is used. Proxies APPEND the peer
        they saw (nginx's $proxy_add_x_forwarded_for, Fly's edge, ...),
        so everything left of the final entry is whatever the client
        sent -- keying on the first hop let a client mint a fresh bucket
        per request by sending its own X-Forwarded-For.

        client_ip_header names a header the proxy SETS outright (Fly's
        Fly-Client-IP); it wins over X-Forwarded-For because there is
        nothing for the client to prepend to.
        """
        if self.trust_proxy:
            if self.client_ip_header:
                owned = (self.headers.get(self.client_ip_header) or "").strip()
                if owned:
                    return owned
            xff = self.headers.get("X-Forwarded-For")
            if xff:
                last_hop = xff.split(",")[-1].strip()
                if last_hop:
                    return last_hop
        return self.client_address[0]

    def _read_body_or_error(self):
        """Returns (dict, None) on success, or (None, (status, payload))
        on a body-level error the caller should send as-is.
        """
        length_header = self.headers.get("Content-Length")
        if not length_header:
            return {}, None
        try:
            length = int(length_header)
        except ValueError:
            return None, (400, {"error": "malformed JSON"})

        if length > MAX_BODY_BYTES:
            return None, (413, {"error": "request body too large"})

        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}, None

        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None, (400, {"error": "malformed JSON"})

        if not isinstance(data, dict):
            return None, (400, {"error": "malformed JSON"})

        return data, None

    # ------------------------------------------------------------- GET --

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            return self._serve_index()
        if path == "/how":
            return self._serve_html_file(HOW_PATH)
        if path == "/api/drops":
            return self._get_drops()
        if path == "/api/status":
            return self._get_status()
        if path == "/unsubscribe":
            return self._serve_unsubscribe_page(parsed)
        if path == "/api/campgrounds":
            q = (parse_qs(parsed.query).get("q") or [""])[0]
            return _send_json(self, 200, {
                "campgrounds": search_campgrounds(self.campgrounds, q)})
        if path in LEGAL_PAGES:
            return self._serve_html_file(LEGAL_PAGES[path])
        _send_json(self, 404, {"error": "not found"})

    def _serve_unsubscribe_page(self, parsed):
        # Safe method: this GET must never disable anything by itself --
        # it only renders a page whose own button POSTs /api/unsubscribe
        # via inline JS once the user clicks it.
        token = (parse_qs(parsed.query).get("token") or [""])[0]
        # The token is embedded as a JSON string literal inside the
        # inline <script>, never as raw HTML -- json.dumps handles quote/
        # backslash escaping, and the "</" guard stops a token containing
        # "</script>" from breaking out of the script block early.
        # Escape every "<", not just "</": "<!--" inside a script block
        # flips the parser into script-data-escaped state, and \u003c is
        # identical to "<" once the JS string is evaluated.
        token_js = json.dumps(token).replace("<", "\\u003c")
        body = (_UNSUBSCRIBE_PAGE_HTML % {"token_js": token_js}).encode("utf-8")
        body, self._csp_nonce = _html_with_nonce(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_index(self):
        return self._serve_html_file(INDEX_PATH)

    def _serve_html_file(self, file_path):
        try:
            with open(file_path, "rb") as f:
                body = f.read()
        except OSError:
            _send_json(self, 500, {"error": "ui not found"})
            return
        body, self._csp_nonce = _html_with_nonce(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _get_drops(self):
        conn = self._conn()
        try:
            drops = core.list_drops(conn, status="active")
            catalog = []
            for d in drops:
                if d["adapter"]:
                    continue  # a campground someone watches, not a listing
                catalog.append({
                    "id": d["id"],
                    "title": d["title"],
                    "description": d["description"],
                    "risk_tier": d["risk_tier"],
                    "status": d["status"],
                    "health": _drop_health(conn, d),
                })
            _send_json(self, 200, {
                "drops": catalog, "demo": self.demo, "notice": self.notice,
                "campgrounds": bool(self.campgrounds),
                "billing": {"enabled": bool(self.payment_link
                                            and self.stripe_webhook_secret),
                            "price_label": self.price_label,
                            "portal_url": self.portal_url}})
        finally:
            conn.close()

    def _get_status(self):
        conn = self._conn()
        try:
            drops = core.list_drops(conn)
            status = {d["id"]: _drop_health(conn, d) for d in drops}
            _send_json(self, 200, status)
        finally:
            conn.close()

    # ------------------------------------------------------------ POST --

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/stripe/webhook":
            return self._post_stripe_webhook()
        ip = self._client_ip()

        if not self.limiter.allow(ip):
            _log.log(_log.TAG_RATELIMIT, "POST rate limit exceeded",
                      ip=ip, path=path)
            # Drain whatever body was sent so the client doesn't see a
            # broken connection mid-write; its contents are never parsed
            # or logged.
            length_header = self.headers.get("Content-Length")
            if length_header:
                try:
                    self.rfile.read(min(int(length_header), MAX_BODY_BYTES))
                except (ValueError, OSError):
                    pass
            _send_json(self, 429, {"error": "rate limit exceeded"})
            return

        data, err = self._read_body_or_error()
        if err is not None:
            status, payload = err
            _send_json(self, status, payload)
            return

        if path == "/api/subscribe":
            return self._post_subscribe(data)
        if path == "/api/verify":
            return self._post_verify(data)
        if path == "/api/unsubscribe":
            return self._post_unsubscribe(data)
        if path == "/api/checkout":
            return self._post_checkout(data)
        _send_json(self, 404, {"error": "not found"})

    def _post_subscribe(self, data):
        drop_id = data.get("drop_id")
        date_from = date_to = campground = None
        if data.get("campground_id") is not None:
            campground = self.campgrounds.get(str(data["campground_id"]))
            if campground is None:
                _send_json(self, 404, {"error": "unknown campground"})
                return
            try:
                date_from, date_to = _parse_watch_dates(
                    data.get("date_from"), data.get("date_to"),
                    dt.date.today())
            except ValueError as exc:
                _send_json(self, 400, {"error": str(exc)})
                return
            drop_id = "rg-%s" % campground["id"]
        channel = data.get("channel")
        address = data.get("address")
        # Tier is NEVER taken from the client. The notify-delay split is
        # the entire monetization mechanism (core.TIER_DELAYS), so a
        # self-declared "paid" was a free upgrade for anyone who could
        # POST -- found by red-team audit 2026-08-04. Until a real
        # entitlement lookup exists, everyone is free; the field is
        # accepted and ignored rather than 400'd so an early client
        # sending it doesn't break.
        tier = "free"

        if not isinstance(drop_id, str) or not drop_id:
            _send_json(self, 400, {"error": "drop_id is required"})
            return
        if channel not in VALID_CHANNELS:
            _send_json(self, 400, {
                "error": "channel must be one of: %s" % ", ".join(VALID_CHANNELS)})
            return
        address = _normalize_address(channel, address)
        if not _valid_address(channel, address):
            _send_json(self, 400, {"error": "address is not valid for channel"})
            return

        webhook_secret = None
        if channel == "webhook":
            from platformd import webhook  # lazy: see module docstring
            safe, reason = webhook.is_safe_url(address)
            if not safe:
                # `reason` is is_safe_url's own text -- it must never be
                # built by echoing `address` back into it; that contract
                # lives in platformd.webhook, not enforced here.
                _send_json(self, 400, {"error": reason})
                return
            webhook_secret = webhook.generate_secret()

        conn = self._conn()
        try:
            now = time.time()
            if campground is not None:
                core.register_drop(
                    conn, drop_id, campground["name"],
                    description=campground["parent"], source="recreation.gov",
                    risk_tier="green", now=now, adapter="recreation_gov",
                    target={"campground_id": campground["id"]})
            drop = core.get_drop(conn, drop_id)
            if drop is None or drop["status"] != "active":
                _send_json(self, 404, {"error": "unknown drop"})
                return

            try:
                row = core.subscribe(conn, drop_id, channel, address, tier,
                                     now, webhook_secret=webhook_secret,
                                     date_from=date_from, date_to=date_to)
            except core.ResendCooldown as exc:
                _send_json(self, 429, {
                    "error": "a code was just sent there - try again in a minute",
                    "retry_after": int(exc.retry_after) + 1})
                return

            # An already-active dedup hit (see core.subscribe's docstring)
            # has no fresh verify_code to send -- nothing to deliver.
            # created_at == now marks a row core.subscribe just minted; a
            # row returned from inside the resend cooldown already had its
            # code delivered and must not be sent again.
            if (row["status"] == "pending_verify" and row["verify_code"]
                    and row["created_at"] == now):
                sender = self.channels[channel]
                try:
                    if channel == "webhook":
                        # Proof-of-control for a URL means proving the
                        # subscriber controls the endpoint, not a handle
                        # -- deliver the code as a normal signed webhook
                        # delivery, exactly like a real alert would
                        # arrive. context has no event_id, so
                        # platformd.webhook.WebhookChannel wraps this
                        # message in its standard minimal envelope
                        # ({"schema": "dropwatch.event.v1", "message":
                        # ...}) rather than a full event payload -- there
                        # is no event to describe yet, just a signed ping.
                        ok = sender.send(
                            address,
                            "Your Dropwatch verification code is %s" % row["verify_code"],
                            context={"webhook_secret": row["webhook_secret"]})
                    else:
                        ok = sender.send(
                            address,
                            "Your Dropwatch verification code is %s" % row["verify_code"])
                    if ok is not True:
                        raise RuntimeError(
                            "channel %r send() returned %r, expected True" %
                            (channel, ok))
                except Exception as exc:  # noqa: BLE001 -- report, don't crash
                    _log.log(_log.TAG_DELIVERY, "verify code send failed",
                              channel=channel, subscription_id=row["id"],
                              exc_class=type(exc).__name__)
                    _send_json(self, 502, {
                        "error": "could not send verification code"})
                    return

            _send_json(self, 200, {"subscription_id": row["id"]})
        finally:
            conn.close()

    def _post_verify(self, data):
        raw_subscription_id = data.get("subscription_id")
        code = data.get("code")

        if not isinstance(code, str) or not code:
            _send_json(self, 400, {"error": "code is required"})
            return
        try:
            subscription_id = int(raw_subscription_id)
        except (TypeError, ValueError):
            _send_json(self, 400, {"error": "subscription_id is required"})
            return

        conn = self._conn()
        try:
            row = core.get_subscription(conn, subscription_id)
            if row is None or row["status"] != "pending_verify":
                _send_json(self, 403, {"error": "invalid or already-resolved subscription"})
                return

            now = time.time()
            try:
                updated = core.verify(conn, subscription_id, code, now)
            except core.VerifyCodeExpired:
                _send_json(self, 403, {"error": "code expired — subscribe again"})
                return
            if updated["status"] == "active":
                _send_json(self, 200, {"unsubscribe_token": updated["unsub_token"]})
            else:
                _send_json(self, 403, {"error": "incorrect code"})
        finally:
            conn.close()

    def _post_unsubscribe(self, data):
        token = data.get("token")
        if not isinstance(token, str) or not token:
            _send_json(self, 400, {"error": "token is required"})
            return

        conn = self._conn()
        try:
            core.unsubscribe(conn, token, time.time())
            # unsubscribe() itself treats an unknown token as a no-op, not
            # an error (replayed/double-clicked unsub links happen) --
            # mirror that here rather than leaking whether a token ever
            # existed.
            _send_json(self, 200, {"ok": True})
        finally:
            conn.close()


    def _post_checkout(self, data):
        """Hand back the Payment Link for a verified watch. The unsubscribe
        token proves the caller is the one who just verified it."""
        if not (self.payment_link and self.stripe_webhook_secret):
            _send_json(self, 404, {"error": "payments are not enabled"})
            return
        token = data.get("token")
        try:
            subscription_id = int(data.get("subscription_id"))
        except (TypeError, ValueError):
            _send_json(self, 400, {"error": "subscription_id is required"})
            return
        conn = self._conn()
        try:
            row = core.get_subscription(conn, subscription_id)
            if (row is None or row["status"] != "active"
                    or not isinstance(token, str)
                    or not secrets.compare_digest(row["unsub_token"] or "", token)):
                _send_json(self, 403, {"error": "unknown watch"})
                return
            _send_json(self, 200, {
                "url": billing.checkout_url(self.payment_link, subscription_id)})
        finally:
            conn.close()

    def _post_stripe_webhook(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 < length <= MAX_STRIPE_BODY_BYTES:
            _send_json(self, 413 if length > 0 else 400, {"error": "bad body"})
            return
        raw = self.rfile.read(length)
        now = time.time()
        if not billing.verify_signature(raw, self.headers.get("Stripe-Signature"),
                                        self.stripe_webhook_secret, now):
            _log.log(_log.TAG_BILLING, "webhook signature rejected",
                     ip=self._client_ip())
            _send_json(self, 400, {"error": "bad signature"})
            return
        try:
            event = json.loads(raw)
        except ValueError:
            _send_json(self, 400, {"error": "malformed JSON"})
            return
        conn = self._conn()
        try:
            outcome = billing.handle_event(conn, event, now)
        finally:
            conn.close()
        _log.log(_log.TAG_BILLING, "webhook applied",
                 event_type=event.get("type"), outcome=outcome)
        _send_json(self, 200, {"received": True})


def make_server(db_path, channels, host="127.0.0.1", port=0,
                 rate_limit_capacity=10, rate_limit_window_seconds=60.0,
                 demo=False, trust_proxy=False, notice=None,
                 client_ip_header=None, campgrounds=None,
                 stripe_webhook_secret=None, payment_link=None,
                 portal_url=None, price_label=None):
    """Build (but do not start) a ThreadingHTTPServer for the drop platform.

    db_path: sqlite file path. Every request opens its own connection
        against this path -- pass a real file, not ':memory:' (see module
        docstring).
    channels: dict of channel name -> object with .send(address, message)
        -> True or raises. Never a real network sender in tests/demo.
    rate_limit_capacity / rate_limit_window_seconds: token-bucket knobs,
        widened by load tests that don't want to trip the default 10/60s.
    trust_proxy: False by default -- the rate limiter keys on the raw
        socket peer address, and X-Forwarded-For is ignored outright.
        Pass True only when this server sits behind a reverse proxy that
        is trusted to set/overwrite that header itself; otherwise any
        client can spoof a fresh X-Forwarded-For per request and dodge
        the rate limit entirely (see _Handler._client_ip's docstring).

    Returns the httpd instance; caller is responsible for serve_forever()
    (or serve one request at a time) and server_close().
    """
    # Touch the db up front so schema exists before the first request.
    _bootstrap = db.connect(db_path)
    _bootstrap.close()

    limiter = RateLimiter(capacity=rate_limit_capacity,
                           window_seconds=rate_limit_window_seconds)

    class Handler(_Handler):
        pass

    Handler.db_path = db_path
    Handler.channels = channels
    Handler.limiter = limiter
    Handler.trust_proxy = bool(trust_proxy)
    Handler.client_ip_header = client_ip_header or None
    # Demo mode is a client-visible fact, not a secret: the UI must tell
    # the user that codes go to the server console, not their phone --
    # otherwise the flow reads as broken to the first real person who
    # tries it with a real handle.
    Handler.demo = bool(demo)
    # Operator-set banner for a deployment whose promise is ahead of its
    # plumbing (e.g. signups live, detection not yet) -- the page must
    # say so rather than let a subscriber wait on an alert that can't come.
    Handler.notice = notice or None
    Handler.campgrounds = campgrounds or {}
    # Payments are on only when BOTH halves exist: a checkout link with no
    # webhook secret would take money and never grant anything.
    Handler.stripe_webhook_secret = stripe_webhook_secret or None
    Handler.payment_link = payment_link or None
    Handler.portal_url = portal_url or None
    Handler.price_label = price_label or None

    return ThreadingHTTPServer((host, port), Handler)


# ---------------------------------------------------------------- demo --

class _PrintChannel:
    """Fake channel sender for --demo: prints instead of sending. Never a
    real Telegram/Discord/ntfy/webhook call. Accepts (and ignores) the
    context= kwarg webhook rows are sent with -- see outbox.dispatch()
    and api._post_subscribe's webhook branch.
    """

    def __init__(self, name):
        self.name = name

    def send(self, address, message, context=None):
        # flush: launchd/preview harnesses block-buffer stdout, and a
        # verification code the user can't find is a broken demo.
        print("[demo:%s -> %s] %s" % (self.name, address, message), flush=True)
        return True


def _build_demo_db(db_path):
    conn = db.connect(db_path)
    now = time.time()
    core.register_drop(
        conn, "yosemite-upper-pines",
        "Yosemite Upper Pines cancellations [DEMO]",
        description=("Watching Upper Pines Campground for cancellation "
                      "openings on your dates."),
        source="recreation.gov", risk_tier="green", now=now)
    core.register_drop(
        conn, "odyssey-70mm-lincoln-square",
        "The Odyssey 70mm - Lincoln Square [DEMO]",
        description=("Watching for The Odyssey 70mm showtimes at Lincoln "
                      "Square to go on sale."),
        source="fandango", risk_tier="yellow", now=now)
    conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m platformd.api")
    parser.add_argument("--demo", action="store_true",
                         help="serve %s:%d against a seeded temp sqlite db "
                              "with print-only fake channel senders" %
                              (DEMO_HOST, DEMO_PORT))
    parser.add_argument("--host", default=DEMO_HOST)
    parser.add_argument("--port", type=int, default=DEMO_PORT)
    args = parser.parse_args(argv)

    if not args.demo:
        parser.error("only --demo is currently supported")

    tmp_dir = tempfile.mkdtemp(prefix="dropwatch-demo-")
    db_path = os.path.join(tmp_dir, "demo.sqlite3")
    _build_demo_db(db_path)

    # Print-only fakes for every channel, deliberately -- including
    # discord/ntfy, even though platformd.channels now has real senders
    # for both. --demo must never make a real network call.
    channels = {name: _PrintChannel(name) for name in VALID_CHANNELS}
    httpd = make_server(db_path, channels, host=args.host, port=args.port,
                        demo=True)
    host, port = httpd.server_address[0], httpd.server_address[1]
    print("Dropwatch demo serving on http://%s:%d (db: %s)" %
          (host, port, db_path), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
