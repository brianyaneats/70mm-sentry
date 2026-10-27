"""app.config -- read and validate every environment variable serve.py
uses, before anything starts. A bad deploy should die at boot with one
clear line, not run half-configured (e.g. take money and never grant).

    cfg = load(os.environ)        # raises ConfigError listing every problem

deploy/.env.example documents each variable; keep the two in sync
(app/tests/test_config.py checks that).
"""

import json
import os
import tempfile
from urllib.parse import urlparse

# name -> default (None = unset). The single list of what serve.py reads.
ENV_VARS = {
    "HOST": "0.0.0.0",
    "PORT": "8787",
    "DROPWATCH_DB": "/data/dropwatch.sqlite3",
    "DROPWATCH_CAMPGROUNDS": None,
    "DROPWATCH_CATALOG": None,
    "DROPWATCH_TRUST_PROXY": "0",
    "DROPWATCH_CLIENT_IP_HEADER": None,
    "DROPWATCH_RUNNER_INTERVAL": "5",
    "DROPWATCH_NOTICE": None,
    "DROPWATCH_PUBLIC_URL": None,
    "DROPWATCH_PAYMENT_LINK": None,
    "DROPWATCH_PORTAL_URL": None,
    "DROPWATCH_PRICE_LABEL": None,
    "STRIPE_WEBHOOK_SECRET": None,
}


class ConfigError(Exception):
    """Every problem found, one per line."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("invalid configuration:\n  - " +
                         "\n  - ".join(self.problems))


def _get(env, name):
    value = (env.get(name) or "").strip()
    return value or ENV_VARS[name]


def _https_url(value):
    u = urlparse(value or "")
    return u.scheme == "https" and bool(u.netloc)


def _check_db_dir(path, problems):
    d = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(d, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=d, prefix=".writecheck-"):
            pass
    except OSError as exc:
        problems.append("DROPWATCH_DB directory %s is not writable (%s)"
                        % (d, exc.strerror or type(exc).__name__))


def _check_json_list(name, path, problems):
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        problems.append("%s=%s is unreadable (%s)"
                        % (name, path, getattr(exc, "strerror", None)
                           or type(exc).__name__))
        return
    if not isinstance(data, list):
        problems.append("%s=%s must be a JSON list" % (name, path))


def load(env):
    """Return a dict of typed settings, or raise ConfigError."""
    problems = []
    cfg = {k: _get(env, k) for k in ENV_VARS}

    try:
        cfg["PORT"] = int(cfg["PORT"])
        if not 0 < cfg["PORT"] < 65536:
            raise ValueError
    except ValueError:
        problems.append("PORT must be 1-65535")
    try:
        cfg["DROPWATCH_RUNNER_INTERVAL"] = float(cfg["DROPWATCH_RUNNER_INTERVAL"])
        if cfg["DROPWATCH_RUNNER_INTERVAL"] <= 0:
            raise ValueError
    except ValueError:
        problems.append("DROPWATCH_RUNNER_INTERVAL must be a positive number")

    if cfg["DROPWATCH_TRUST_PROXY"] not in ("0", "1"):
        problems.append("DROPWATCH_TRUST_PROXY must be 0 or 1")
    cfg["DROPWATCH_TRUST_PROXY"] = cfg["DROPWATCH_TRUST_PROXY"] == "1"
    if cfg["DROPWATCH_CLIENT_IP_HEADER"] and not cfg["DROPWATCH_TRUST_PROXY"]:
        problems.append("DROPWATCH_CLIENT_IP_HEADER is ignored unless "
                        "DROPWATCH_TRUST_PROXY=1")

    _check_db_dir(cfg["DROPWATCH_DB"], problems)
    if cfg["DROPWATCH_CAMPGROUNDS"]:
        _check_json_list("DROPWATCH_CAMPGROUNDS", cfg["DROPWATCH_CAMPGROUNDS"],
                         problems)
    if cfg["DROPWATCH_CATALOG"]:
        _check_json_list("DROPWATCH_CATALOG", cfg["DROPWATCH_CATALOG"], problems)

    link, secret = cfg["DROPWATCH_PAYMENT_LINK"], cfg["STRIPE_WEBHOOK_SECRET"]
    if link and not secret:
        problems.append("DROPWATCH_PAYMENT_LINK is set but STRIPE_WEBHOOK_SECRET "
                        "is not: checkout would take money and never grant")
    if secret and not link:
        problems.append("STRIPE_WEBHOOK_SECRET is set but DROPWATCH_PAYMENT_LINK "
                        "is not: set both or neither")
    if secret and not secret.startswith("whsec_"):
        problems.append("STRIPE_WEBHOOK_SECRET must start with whsec_")
    if link and not _https_url(link):
        problems.append("DROPWATCH_PAYMENT_LINK must be an https:// URL")
    if cfg["DROPWATCH_PORTAL_URL"] and not _https_url(cfg["DROPWATCH_PORTAL_URL"]):
        problems.append("DROPWATCH_PORTAL_URL must be an https:// URL")
    cfg["billing"] = bool(link and secret)

    public = cfg["DROPWATCH_PUBLIC_URL"]
    if public:
        if not _https_url(public):
            problems.append("DROPWATCH_PUBLIC_URL must be an https:// URL")
        cfg["DROPWATCH_PUBLIC_URL"] = public.rstrip("/")
    elif link or secret:
        problems.append("DROPWATCH_PUBLIC_URL is required when billing is on "
                        "(Stripe redirects and alert unsubscribe links)")

    if problems:
        raise ConfigError(problems)
    return cfg
