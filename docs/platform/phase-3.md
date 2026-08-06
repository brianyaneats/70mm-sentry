# Phase 3 — multi-tenant core

## Walking skeleton first, on stdlib `sqlite3`

The no-pip ethos (`docs/architecture-notes.md`, Decision 1) holds here too:
`sqlite3` is standard library, ACID-transactional, and a single file — it
costs nothing to depend on and nothing to operate for a platform that, at
launch, has dozens to low-hundreds of subscribers, not millions. The
schema below is designed so a later port to Postgres (when a real deploy —
concurrent writers across processes, not just concurrent readers — forces
it) is a `CREATE TABLE` translation, not a redesign: every table uses
explicit types, every foreign key is a real column, no SQLite-only
convenience (`ROWID` aliasing tricks, `WITHOUT ROWID`, JSON1-specific
queries beyond a `meta` blob column both engines can store) leaks into the
tables that matter for correctness. The `meta`/`payload` columns are
`TEXT` holding JSON either engine parses natively — Postgres's `jsonb`
replaces SQLite's `TEXT`-as-JSON at that point, one column-type change per
table, not a schema rethink.

**Rejected: designing for Postgres from day one.** It would remove the
future migration step at the cost of a local dev/test dependency this
project has avoided everywhere else — every test in the existing 310-test
suite runs against stdlib with zero setup, and `sqlite3` keeps that true
for Phase 3's tests too (an in-memory `:memory:` database per test case,
no fixture server, no docker-compose for CI). Postgres becomes worth its
own weight only once there's a real multi-process deploy — a walking
skeleton that can't yet prove subscriber count justifies that infra hasn't
earned the dependency yet.

## Schema (illustrative, not final DDL)

```sql
-- one row per upstream source; risk_tier mirrors adapters/base.py exactly
CREATE TABLE sources (
    name        TEXT PRIMARY KEY,   -- adapter.name, e.g. "fandango"
    risk_tier   TEXT NOT NULL,      -- "green" | "yellow" | "red"(never inserted)
    min_spacing_seconds REAL NOT NULL,
    daily_request_budget INTEGER NOT NULL
);

-- one row per pollable thing; unit_id/date match adapters/base.py's
-- observation() vocabulary so no source-specific columns are ever needed
CREATE TABLE targets (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL REFERENCES sources(name),
    unit_id     TEXT NOT NULL,      -- e.g. showtimeHashCode, "cg:site:date"
    date        TEXT NOT NULL,
    UNIQUE(source, unit_id, date)
);

CREATE TABLE subscriptions (
    id          INTEGER PRIMARY KEY,
    subscriber_id TEXT NOT NULL,
    target_id   INTEGER NOT NULL REFERENCES targets(id),
    channel     TEXT NOT NULL,      -- "telegram" | "discord" | "sms" | ...
    channel_addr TEXT NOT NULL,     -- chat_id, webhook, phone — opaque here
    tier        TEXT NOT NULL,      -- notify-delay tier, see dispatch below
    UNIQUE(subscriber_id, target_id, channel)
);

-- append-only. never UPDATEd, never DELETEd — the audit trail IS the log.
CREATE TABLE events (
    id          INTEGER PRIMARY KEY,
    target_id   INTEGER NOT NULL REFERENCES targets(id),
    state       TEXT NOT NULL,      -- STATE_AVAILABLE etc.
    observed_at TEXT NOT NULL,      -- when the kernel's poll saw it
    detail      TEXT                -- JSON: label, meta passthrough
);

-- dedup: "have we already fired for this (target, state-transition)?"
-- one row per target; advances in the SAME transaction as the outbox
-- inserts below — this is the phase-3 half of the split invariant.
CREATE TABLE ledger (
    target_id   INTEGER PRIMARY KEY REFERENCES targets(id),
    last_state  TEXT NOT NULL,
    last_event_id INTEGER NOT NULL REFERENCES events(id)
);

-- one row per (subscriber, channel) per event. retried independently.
CREATE TABLE outbox (
    id          INTEGER PRIMARY KEY,
    event_id    INTEGER NOT NULL REFERENCES events(id),
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
    status      TEXT NOT NULL,      -- "pending" | "delivered" | "failed" | "expired"
    attempts    INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,           -- NULL once delivered/expired
    ttl_at      TEXT NOT NULL,      -- past this, stop retrying -> "expired"
    last_error  TEXT                -- last [DELIVERY] failure, if any
);
```

## The transaction that makes detection durable

This is the concrete mechanics behind `docs/platform/README.md`'s "one
invariant that splits." When the kernel's poll observes a state change
worth firing on:

```
BEGIN;
  INSERT INTO events (target_id, state, observed_at, detail) VALUES (...);
  UPDATE ledger SET last_state=?, last_event_id=? WHERE target_id=?;
  INSERT INTO outbox (event_id, subscription_id, status, ttl_at, ...)
    SELECT <event_id>, id, 'pending', ...
    FROM subscriptions WHERE target_id = ?;
COMMIT;
```

All three writes commit together or not at all. The moment this commits,
**detection is done** — the ledger has advanced, so the next poll cycle
sees the new baseline regardless of what happens to any outbox row
afterward. Delivery is now purely downstream: a separate dispatch process
reads `pending` outbox rows and attempts sends, updating `status` and
`attempts`/`next_attempt_at` per row, independently. A subscriber's
channel being dead affects only their own row's retry loop — it cannot
roll back the transaction above, because that transaction has already
committed by the time any send is attempted. This is the whole point:
single-tenant sentry.py conflated "commit the baseline" with "the push
succeeded" because there was one push to wait on; here, committing the
baseline **is** the durable record, and "did the push succeed" moves
entirely into the outbox's own state machine.

## Per-subscriber independent retry, backoff, and TTL

Each `outbox` row owns its own `attempts` counter and `next_attempt_at`,
computed with the same shape of backoff the existing single-tenant breaker
already uses (`breaker_fail` — exponential, capped, see
`docs/architecture-notes.md` Decision 4 for the curve and its history; the
line number in `sentry.py` moves during the concurrent Phase-1 work, check
current file rather than citing one here): a
failed send bumps `attempts`, sets `next_attempt_at` further out, and logs
`[DELIVERY] <channel>:<subscriber> attempt <n> failed: <reason>`. Past
`ttl_at` (a per-subscription config, defaulting to something short — a
drop alert that's still unsent after an hour is not "late," it's useless),
the row moves to `expired` rather than retrying forever; an `expired` row
is itself worth alerting on if it recurs for the same subscriber across
multiple events (a standing-broken channel), which is a Phase 4 status/
notification concern, not a Phase 3 mechanism.

## Notify-delay tiering happens at dispatch, not at detection

Per `docs/platform/README.md`'s "paywall is notify-delay" argument: every
outbox row for a given event is created in the same transaction,
regardless of `subscriptions.tier` — the *insert* doesn't discriminate.
The dispatch process reads `tier` and simply delays *picking up* a
`pending` row proportionally (a free-tier row's `next_attempt_at` starts
later than a paid-tier row's, even though both were written at the same
instant). This keeps tiering entirely inside the delivery layer, touching
nothing about detection, the shared governor, or the ledger.

## Per-source shared politeness governor

One row per `sources.name`, holding token-bucket state (last request
timestamp, per `min_spacing_seconds`), a running daily count (reset at
midnight, per `daily_request_budget`), and breaker state (`open`/`closed`,
`open_until`, consecutive-failure count) — the multi-source, multi-
subscriber generalization of `pace_request()`/`budget_gate()`/
`breaker_fail()` (`docs/architecture-notes.md`, Decision 4). Every poll
against a given source, regardless of how many subscribers or targets are
behind it, goes through this one gate. This is the mechanism that makes
"polling happens once per target, never per subscriber" actually true at
runtime, not just true by design intent.

## Channel-provider breaker, distinct from per-subscriber retry

A per-subscriber outbox row retrying is not the same failure mode as
Telegram's API itself being down for everyone. If `[DELIVERY]` failures
for a channel spike across many *different* subscribers in a short window,
that's evidence the channel provider is down, not that N subscribers
independently misconfigured their bots at the same moment — treating it as
N unrelated retries would hammer a provider that's already failing, and
delay recognizing the actual outage. A separate breaker, keyed on
`channel` (not on any one subscription), opens after a cross-subscriber
failure-rate threshold and pauses dispatch attempts for that channel
platform-wide for a cooldown — distinct state from, and layered on top of,
each row's own independent retry/backoff. A subscriber's own row keeps its
`next_attempt_at` and TTL clock running underneath; the channel breaker
just declines to *attempt* sends on that channel until it closes again, the
same "don't retry into a wall" logic the single-tenant breaker already
applies to the Fandango fetch path, now applied to the delivery side.

## The gate: kill one subscriber's channel mid-drill

**This is a drill, and it must be unmistakably labeled one at every step**
— per the standing lesson from a prior fire-drill that reached a real
phone with no recovery message (`feedback-lessons` memory: "test drills
must be unmistakably drills... anything that can reach a real channel
during a test gets a TEST prefix or runs against sandboxed state AND
config with channels disabled"). Concretely: the drill runs against an
in-memory or scratch sqlite database, with every channel sender replaced
by a mock that can be told to fail on command — no real Telegram/Discord/
SMS call happens, and any log line the drill produces is prefixed
`[DRILL]` so it's never mistaken for a real `[DELIVERY]`/`[ZERO-DELIVERED]`
event by anyone scanning logs later.

The scenario: two subscribers, A and B, both subscribed to the same
target on different channels. B's channel mock is set to always fail.
Fire a state-change event.

**Pass conditions, each independently assertable:**

1. **Detection never stalls.** The next poll cycle against that target
   proceeds normally — the ledger already advanced in the same
   transaction that created both outbox rows, so B's dispatch failures
   have no path back to blocking detection. Assert: a second, unrelated
   target's event still processes end-to-end while B's row is mid-retry.
2. **A's delivery is unaffected by B's failure.** A's outbox row reaches
   `delivered` on the first attempt, independent of how many times B's
   row has failed or retried. Assert: A's `status` and `attempts` never
   reflect B's failures.
3. **`[ZERO-DELIVERED]` fires only when every subscriber's delivery for
   that event has failed** — not when B fails while A succeeds. A second
   scenario, with *both* mocks set to fail, is required to actually
   exercise this tag; the two-subscriber A/B scenario above proves
   `[DELIVERY]` isolation, and a same-shape all-fail scenario proves the
   rollup. Both are needed — one gate, two drill runs.

A gate this specific either passes or it doesn't; there's no partial
credit for "detection mostly kept going."
