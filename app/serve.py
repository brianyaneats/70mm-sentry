"""app.serve -- the production process: HTTP API + poller + dispatch
runner in one container, against a persistent sqlite file.

    python3 -m app.serve

Configured entirely from the environment; app/config.py validates it
at boot and deploy/.env.example documents every variable.

ONE process on purpose: sqlite on a single volume means a single writer
host, so there is never a second replica to coordinate with. Scale-out is
a database change, not a flag.

SIGTERM (what `docker compose up -d`/`down` sends) or SIGINT stops the
poller and runner threads at their next wait, stops accepting requests,
and closes the server; the outbox is crash-safe either way.

This is the composition root: the only module that wires sources,
engine, notify and billing together. Sockets live in notify.transport;
this module adds the upstream GET for adapters on the same opener.
"""

import datetime as dt
import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

from sources.fandango import FandangoAdapter
from sources.recreation_gov import RecreationGovAdapter
from app import api, config
from engine import core, db, log as _log, poller, runner
from notify import transport
from notify.channels import make_default_channels
from notify.webhook import WebhookChannel

USER_AGENT = "Dropwatch/0.1 (availability alerts)"

ADAPTERS = {a.name: a for a in (RecreationGovAdapter(), FandangoAdapter())}


def http_get(url, headers):
    """Upstream transport for adapters: GET -> parsed JSON. 403/429/5xx
    raise poller.UpstreamRefused; socket failures propagate as OSError."""
    headers = dict(headers)
    headers.setdefault("User-Agent", USER_AGENT)
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with transport.OPENER.open(
                req, timeout=transport.HTTP_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise poller.UpstreamRefused(exc.code) from None
    except urllib.error.URLError as exc:
        raise exc.reason if isinstance(exc.reason, OSError) else exc


def build_channels():
    channels = make_default_channels(transport.http_post)
    channels["webhook"] = WebhookChannel(transport.http_post_status)
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


def _poller_loop(db_path, catalog_watches, stop):
    conn = db.connect(db_path)

    def watch_source():
        return catalog_watches + core.active_watches(conn, dt.date.today())

    try:
        poller.run_forever(conn, watch_source, ADAPTERS, http_get, stop=stop,
                           sleep=stop.wait)
    finally:
        conn.close()


def _runner_loop(db_path, channels, interval, public_url, stop):
    conn = db.connect(db_path)
    try:
        while not stop.is_set():
            try:
                runner.run_once(conn, channels, public_url=public_url)
            except Exception as exc:  # noqa: BLE001 -- one bad tick must not
                # kill delivery for the life of the container
                _log.log(_log.TAG_RUNNER, "tick crashed",
                         exc_class=type(exc).__name__)
            stop.wait(interval)
    finally:
        conn.close()


def start(cfg):
    """Build the server and start the poller + runner threads. Returns
    (httpd, stop, threads); serve() or a test drives the rest."""
    db_path = cfg["DROPWATCH_DB"]
    catalog = cfg["DROPWATCH_CATALOG"]
    conn = db.connect(db_path)
    n = load_catalog(conn, catalog) if catalog else 0
    conn.close()
    watches = load_watches(catalog) if catalog else []

    stop = threading.Event()
    channels = build_channels()
    httpd = api.make_server(
        db_path, channels, host=cfg["HOST"], port=cfg["PORT"],
        trust_proxy=cfg["DROPWATCH_TRUST_PROXY"],
        notice=cfg["DROPWATCH_NOTICE"],
        client_ip_header=cfg["DROPWATCH_CLIENT_IP_HEADER"],
        campgrounds=api.load_campgrounds(cfg["DROPWATCH_CAMPGROUNDS"]),
        stripe_webhook_secret=cfg["STRIPE_WEBHOOK_SECRET"],
        payment_link=cfg["DROPWATCH_PAYMENT_LINK"],
        portal_url=cfg["DROPWATCH_PORTAL_URL"],
        price_label=cfg["DROPWATCH_PRICE_LABEL"])
    threads = [
        threading.Thread(target=_poller_loop, args=(db_path, watches, stop),
                         daemon=True, name="poller"),
        threading.Thread(target=_runner_loop,
                         args=(db_path, channels,
                               cfg["DROPWATCH_RUNNER_INTERVAL"],
                               cfg["DROPWATCH_PUBLIC_URL"], stop),
                         daemon=True, name="runner"),
    ]
    for t in threads:
        t.start()
    _log.log(_log.TAG_RUNNER, "serve starting", port=httpd.server_address[1],
             catalog_drops=n, catalog_watches=len(watches),
             trust_proxy=cfg["DROPWATCH_TRUST_PROXY"],
             billing=cfg["billing"])
    return httpd, stop, threads


def shutdown(httpd, stop, threads, timeout=15.0):
    """Stop everything start() started. Safe to call once from any thread
    except the one running serve_forever()."""
    stop.set()
    httpd.shutdown()
    deadline = time.monotonic() + timeout
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    httpd.server_close()
    alive = [t.name for t in threads if t.is_alive()]
    _log.log(_log.TAG_RUNNER, "serve stopped", clean=not alive,
             still_running=",".join(alive) or "-")
    return not alive


def main(env=None):
    try:
        cfg = config.load(os.environ if env is None else env)
    except config.ConfigError as exc:
        print("dropwatch: %s" % exc, file=sys.stderr, flush=True)
        return 2

    httpd, stop, threads = start(cfg)
    server = threading.Thread(target=httpd.serve_forever, name="http",
                              daemon=True)
    server.start()

    got = threading.Event()

    def _on_signal(signum, frame):
        got.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _on_signal)
    while not got.wait(1.0):
        if not server.is_alive():
            break
    _log.log(_log.TAG_RUNNER, "serve stopping", signal=got.is_set())
    return 0 if shutdown(httpd, stop, threads) else 1


if __name__ == "__main__":
    sys.exit(main())
