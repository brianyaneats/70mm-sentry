# sources

Read-only availability adapters. One module per upstream, all speaking the
contract in `base.py`. Imports nothing else from this repo; stdlib only.

| Module | Upstream | risk_tier | Spacing / daily budget |
|---|---|---|---|
| `recreation_gov.py` | recreation.gov month-availability endpoint (public, unauthenticated) | green | 2s / 5,000 |
| `fandango.py` | Fandango showtimes feed (used by the retired `sentry.py`) | yellow | 1s / 25,000 |

## Contract (`base.py`)

An adapter owns how to **fetch**, **parse**, and **health-check** one
upstream, and nothing else: no sleeping, retrying, pacing, or sockets of
its own. The caller injects `http_get(url, headers) -> parsed JSON`.

- `fetch(target, date, http_get)` returns the raw payload. Transport errors propagate.
- `parse(raw, target, date)` returns `observation(...)` dicts. It raises on an unknown shape and never returns `[]` for drift.
- `health_check(raw)` returns `(ok, reason)`. It tells "nothing available" apart from "the payload is blind".
- Optional `fetch_key(target, date)`: dates that share a key share one fetch. recreation.gov keys by month.
- Class attributes: `name`, `risk_tier`, `min_spacing_seconds`, `daily_request_budget`.

States: `available`, `soldout`, `restricted`, `gone`. Log tags a caller
should emit for adapter failures: `[PIPE]`, `[BLIND]`, `[REFUSED]`, `[PARSE]`.

We read public availability only. Adapters never book, hold carts, log in,
or work around bot detection.

## Adding a source

Subclass `SourceAdapter`, map every raw state you know, and raise on any
shape you don't. Copy `tests/test_recreation_gov.py`, which drives the
adapter only through the contract with a fake `http_get`.

## Tests

From the repo root:

    python3 -m unittest discover -s sources/tests -t .
