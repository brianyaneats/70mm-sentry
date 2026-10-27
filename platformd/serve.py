"""platformd.serve -- the production process: HTTP API + dispatch runner in
one container, against a persistent sqlite file.

    python3 -m platformd.serve

Everything is configured from the environment so the same image runs on
any container host:

    PORT                  listen port (default 8787)
    HOST                  bind address (default 0.0.0.0 -- inside a
                          container, 127.0.0.1 would be unreachable)
    DROPWATCH_DB          sqlite path; MUST live on a persistent volume
                          (default /data/dropwatch.sqlite3)
    DROPWATCH_CATALOG     optional JSON file of drops to register at boot
                          ([{"id", "title", "description", "source",
                          "risk_tier"}]); register_drop is idempotent, so
                          re-running on every boot is safe
    DROPWATCH_TRUST_PROXY "1" only behind a proxy that overwrites
                          X-Forwarded-For (every PaaS edge does); see
                          api.make_server's docstring for why it is off
                          by default
    DROPWATCH_RUNNER_INTERVAL  seconds between dispatch ticks (default 5)
    DROPWATCH_NOTICE      optional banner shown above the drop list
    DROPWATCH_CLIENT_IP_HEADER  a header the edge proxy SETS (not appends)
                          to the real client IP, e.g. Fly-Client-IP;
                          preferred over X-Forwarded-For for rate limiting
    DROPWATCH_CAMPGROUNDS campground index for the watch picker
                          (tools/build_campgrounds.py output)
    STRIPE_WEBHOOK_SECRET whsec_... of the Stripe webhook endpoint (a SECRET:
                          `fly secrets set`, never fly.toml)
    DROPWATCH_PAYMENT_LINK  https://buy.stripe.com/... monthly plan link
    DROPWATCH_PORTAL_URL  Stripe customer-portal login link (cancel/card)
    DROPWATCH_PRICE_LABEL e.g. "$5/mo", shown on the upgrade button
    DROPWATCH_PUBLIC_URL  https://<host> -- puts an unsubscribe link in
                          every alert

ONE process on purpose: sqlite on a single volume means a single writer
host, so there is never a second replica to coordinate with. Scale-out is
a database change, not a flag.

This is the only module in platformd that opens a real socket. Every
channel class takes an injected http_post precisely so the transport
lives in one place; this is that place.
"""

import datetime as dt
import http.client
import json
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request

from adapters.fandango import FandangoAdapter
from adapters.recreation_gov import RecreationGovAdapter
from platformd import api, core, db, log as _log, poller, runner
from platformd.channels import make_default_channels
from platformd.webhook import is_public_ip

HTTP_TIMEOUT_SECONDS = 10
USER_AGENT = "Dropwatch/0.1 (availability alerts)"

ADAPTERS = {a.name: a for a in (RecreationGovAdapter(), FandangoAdapter())}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect. webhook.is_safe_url() vets the subscriber's
    URL once; following a 30x would let that endpoint bounce the POST to
    an internal address the SSRF guard never saw."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UnsafeDestination(OSError):
    """The hostname resolved to a non-public address. An OSError so
    outbox treats it like any other transport failure ([PIPE])."""


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Resolve, vet, then connect to the vetted address -- one lookup.

    webhook.is_safe_url() deliberately skips DNS, so a subscriber's
    https://anything.example could resolve to 127.0.0.1, the 169.254
    metadata range, or Fly's private fdaa:: network. Checking a lookup
    and then letting the socket do its own second lookup would lose to
    DNS rebinding; dialing the exact IP we checked closes that window.
    TLS still verifies against the hostname via SNI.
    """

    def connect(self):
        infos = socket.getaddrinfo(self.host, self.port,
                                   type=socket.SOCK_STREAM)
        last = None
        for family, socktype, proto, _, sockaddr in infos:
            if not is_public_ip(sockaddr[0]):
                last = UnsafeDestination("resolved to a non-public address")
                continue
            sock = socket.socket(family, socktype, proto)
            sock.settimeout(self.timeout)
            try:
                sock.connect(sockaddr)
            except OSError as exc:
                sock.close()
                last = exc
                continue
            self.sock = self._context.wrap_socket(sock,
                                                  server_hostname=self.host)
            return
        raise last or UnsafeDestination("no address to connect to")


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self):
        super().__init__(context=ssl.create_default_context())

    def https_open(self, req):
        return self.do_open(_PinnedHTTPSConnection, req,
                            context=self._context)


_opener = urllib.request.build_opener(_NoRedirect, _PinnedHTTPSHandler)


def http_post(url, body, headers):
    """Real transport for platformd.channels: returns (status, body).
    Non-2xx comes back as a status, not an exception -- the channel
    classes own the status-to-error mapping. Socket-level failures
    propagate so outbox's [PIPE] logging sees them."""
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method="POST")
    try:
        with _opener.open(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        # urllib wraps every connect-time OSError; surface the SSRF refusal
        # as itself so logs/tests can tell it from an ordinary outage.
        if isinstance(exc.reason, UnsafeDestination):
            raise exc.reason from None
        raise


def _http_post_status(url, body, headers):
    """platformd.webhook's narrower contract: a bare status int."""
    return http_post(url, body, headers)[0]


def http_get(url, headers):
    """Upstream transport for adapters: GET -> parsed JSON. 403/429/5xx
    raise poller.UpstreamRefused; socket failures propagate as OSError."""
    headers = dict(headers)
    headers.setdefault("User-Agent", USER_AGENT)
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _opener.open(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise poller.UpstreamRefused(exc.code) from None
    except urllib.error.URLError as exc:
        raise exc.reason if isinstance(exc.reason, OSError) else exc


def build_channels():
    from platformd.webhook import WebhookChannel
    channels = make_default_channels(http_post)
    channels["webhook"] = WebhookChannel(_http_post_status)
    return channels


def load_catalog(conn, path):
    with open(path) as fh:
        drops = json.load(fh)
    now = time.time()
    for d in drops:
        core.register_drop(conn, d["id"], d["title"],
                           description=d.get("description"),
                           source=d.get("source"),
                           risk_tier=d.get("risk_tier"), now=now)
    return len(drops)


def load_watches(path, today=None):
    """Catalog entries carrying "adapter" + "target" become poll watches.
    "window_days": N watches today..today+N-1 so the catalog never goes
    stale; explicit "dates" are used as given."""
    today = today or dt.date.today()
    with open(path) as fh:
        drops = json.load(fh)
    watches = []
    for d in drops:
        if "adapter" not in d:
            continue
        if d["adapter"] not in ADAPTERS:
            raise ValueError("drop %r: unknown adapter %r" % (d["id"], d["adapter"]))
        dates = d.get("dates") or [
            (today + dt.timedelta(days=i)).isoformat()
            for i in range(d.get("window_days", 0))]
        watches.append({"drop_id": d["id"], "adapter": d["adapter"],
                        "target": d["target"], "dates": dates,
                        "interval_seconds": d.get(
                            "interval_seconds", poller.DEFAULT_INTERVAL_SECONDS)})
    return watches


def _poller_loop(db_path, catalog_watches):
    conn = db.connect(db_path)

    def watch_source():
        return catalog_watches + core.active_watches(conn, dt.date.today())

    poller.run_forever(conn, watch_source, ADAPTERS, http_get)


def _runner_loop(db_path, channels, interval, public_url):
    conn = db.connect(db_path)
    while True:
        try:
            runner.run_once(conn, channels, public_url=public_url)
        except Exception as exc:  # noqa: BLE001 -- one bad tick must not
            # kill delivery for the life of the container
            _log.log(_log.TAG_RUNNER, "tick crashed",
                     exc_class=type(exc).__name__)
        time.sleep(interval)


def main():
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8787"))
    db_path = os.environ.get("DROPWATCH_DB", "/data/dropwatch.sqlite3")
    catalog = os.environ.get("DROPWATCH_CATALOG")
    trust_proxy = os.environ.get("DROPWATCH_TRUST_PROXY") == "1"
    interval = float(os.environ.get("DROPWATCH_RUNNER_INTERVAL", "5"))

    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = db.connect(db_path)
    n = load_catalog(conn, catalog) if catalog else 0
    conn.close()

    watches = load_watches(catalog) if catalog else []
    threading.Thread(target=_poller_loop, args=(db_path, watches),
                     daemon=True, name="poller").start()

    channels = build_channels()
    threading.Thread(target=_runner_loop,
                     args=(db_path, channels, interval,
                           os.environ.get("DROPWATCH_PUBLIC_URL")),
                     daemon=True, name="runner").start()

    httpd = api.make_server(db_path, channels, host=host, port=port,
                            trust_proxy=trust_proxy,
                            notice=os.environ.get("DROPWATCH_NOTICE"),
                            client_ip_header=os.environ.get(
                                "DROPWATCH_CLIENT_IP_HEADER"),
                            campgrounds=api.load_campgrounds(
                                os.environ.get("DROPWATCH_CAMPGROUNDS")),
                            stripe_webhook_secret=os.environ.get(
                                "STRIPE_WEBHOOK_SECRET"),
                            payment_link=os.environ.get("DROPWATCH_PAYMENT_LINK"),
                            portal_url=os.environ.get("DROPWATCH_PORTAL_URL"),
                            price_label=os.environ.get("DROPWATCH_PRICE_LABEL"))
    _log.log(_log.TAG_RUNNER, "serve starting", port=port,
             catalog_drops=n, catalog_watches=len(watches),
             trust_proxy=trust_proxy)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
