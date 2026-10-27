# engine

Drops, subscriptions, change detection, and crash-safe delivery on one
sqlite file. Depends only on the `sources` contract; stdlib only. It never
opens a socket: senders and `http_get` are injected.

| Module | Job |
|---|---|
| `db.py` | `connect(path)`: schema, migrations, WAL, busy timeout |
| `core.py` | drops catalog, subscribe/verify/unsubscribe, `record_event` fan-out, ledger-backed `active_watches`, entitlements (`Entitlements` is the write interface billing uses), `VALID_CHANNELS` |
| `poller.py` | `run_forever`/`poll_watch`: most-overdue watch first, budget-stretched intervals, transition detection (first pass only seeds) |
| `governor.py` | per-source spacing floor, daily budget, breaker, persisted in sqlite |
| `outbox.py` | `dispatch`: claim/lease, backoff, 24h TTL, provider breaker, STOP honored mid-flight, `[ZERO-DELIVERED]` alarm |
| `runner.py` | one dispatch tick plus `[RUNNER]` heartbeat (`--once` CLI for drills) |
| `log.py` | `log(tag, msg, **fields)`: stable grep tags, unknown tag raises |
| `drill.py` | in-memory end-to-end walkthrough: `python3 -m engine.drill` |

## Contracts with the outside

- **Sender**: `send(address, message[, context]) -> True`, or it raises. Webhook rows get `context` (secret plus event fields). An exception class with `permanent = True` means gone for good: the row is exhausted and the subscription disabled. `retry_after` on an exception overrides the backoff.
- **http_get**: `(url, headers) -> JSON`. `poller.UpstreamRefused(status)` maps to `[REFUSED]` and `OSError` maps to `[PIPE]`.
- **Policy, enforced in `core`**: risk tier `red` is never watched, and `yellow` is never paid. Free alerts wait 60s (`TIER_DELAYS`).

One writer per db file. Run every thread in one process (see `app/serve.py`).

## Tests

From the repo root:

    python3 -m unittest discover -s engine/tests -t .
