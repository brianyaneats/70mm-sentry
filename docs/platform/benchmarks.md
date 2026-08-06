# platformd core benchmarks

Run with:

```
python3 tools/bench_core.py
```

Runs in ~3s (well under the 60s budget). stdlib only, no network, every
scenario uses a fresh tmp-file sqlite database (never `:memory:`) so
numbers reflect a real client hitting disk.

## Machine

- Apple M4, macOS 24.5.0 (Darwin), local APFS SSD
- Python 3.9.6, `sqlite3` module 2.6.0, SQLite library 3.43.2
- Numbers below are representative of several runs; wall-clock timings on
  a laptop vary run to run (see "noise" note under Scenario 5). Where a
  candidate's effect was within that noise band, it's called out
  explicitly rather than reported as a real win.

## Result: no code changes applied

Every scenario was measured, and five candidate optimizations were
evaluated against those numbers. **None were applied to
`platformd/db.py`, `platformd/core.py`, or `platformd/outbox.py`** — see
"Optimizations considered" below for the numbers behind each rejection.
`platformd/db.py` and `platformd/core.py` are therefore untouched by this
wave; the only new file besides this doc is `tools/bench_core.py`.

The most important output of this wave isn't a perf number — it's a
correctness finding in Scenario 4 (governor contention). See that section.

## Before/after table

"After" = "Before" for every row: no optimization cleared the bar (see
next section for why). Numbers are from a representative run of
`tools/bench_core.py`.

| # | Scenario | Metric | Before | After |
|---|----------|--------|-------:|------:|
| 1 | FAN-OUT — 5000 active subs, 20 `record_event()` calls | p50 latency | 32.3 ms | 32.3 ms |
| 1 | (same) | p95 latency | 43.9 ms | 43.9 ms |
| 2 | DISPATCH THROUGHPUT — 10000 pending rows, instant success | rows/sec | 35,699 | 35,699 |
| 3 | FAILURE STORM — 10000 rows, 1-in-5 `BrokenPipeError` | rows/sec | 29,850 | 29,850 |
| 3 | (same) | [PIPE] logs / backoff-scheduled rows | 2000 / 2000 | 2000 / 2000 |
| 4 | GOVERNOR CONTENTION — 8×500 `acquire()`, budget=2000 | acquires/sec | ~30,000 | ~30,000 |
| 4 | (same) | **over-budget grants** | **2000** ⚠️ | **2000** ⚠️ |
| 5 | ZERO-DELIVERED SCAN — 200 undelivered events past deadline | scan cost | 3.2 ms | 3.2 ms |

Row 4's "over-budget grants" isn't a performance metric — it's the
scenario's correctness assertion, and it fails every run (see below).

## Optimizations considered

### 1. `CREATE INDEX ... ON outbox (status, next_attempt_at)` — already present

`idx_outbox_due` already exists as `(status, next_attempt_at, not_before)`
— a strict superset of the requested columns. Confirmed via
`EXPLAIN QUERY PLAN` that `outbox.dispatch()`'s due-rows query already
uses it (`SEARCH o USING INDEX idx_outbox_due (status=? AND
next_attempt_at<?)`). No action needed.

One related, *not* actioned observation: that same query still falls back
to `USE TEMP B-TREE FOR ORDER BY` for the `ORDER BY o.id` clause, because
SQLite can't get rowid order "for free" out of a composite index once the
leading column is matched via an `IN (...)` list. This wasn't on the
candidate list for this wave and the 10k-row scenario doesn't show it
costing anything material relative to the per-row send/update work
(Scenario 2 throughput is dominated by other costs — see optimization 5
below) — flagging it here rather than acting on it unasked.

### 2. `CREATE INDEX ... ON subscriptions (drop_id, status)` — already present

`idx_subscriptions_drop` already exists as exactly `(drop_id, status)`.
Confirmed via `EXPLAIN QUERY PLAN` that `record_event()`'s active-subscriber
lookup already uses it (`SEARCH subscriptions USING INDEX
idx_subscriptions_drop (drop_id=? AND status=?)`). No action needed.

### 3. `CREATE INDEX ... ON outbox (event_row_id, status)` — tested, rejected (no measurable gain)

This one *doesn't* already exist (`idx_outbox_event` is `(event_row_id)`
only) and was a real candidate, since `_check_zero_delivered_deadlines()`'s
per-event query (`SELECT status FROM outbox WHERE event_row_id = ?`) could
become a covering index scan with `status` folded in.

Measured directly (isolated from the rest of dispatch, 500 events × 50
outbox rows = 25,000 rows, 3 trials each):

| | trial 1 | trial 2 | trial 3 |
|---|---:|---:|---:|
| without the index | 7.48 ms | 5.90 ms | 5.85 ms |
| with the index | 8.00 ms | 5.30 ms | 5.21 ms |

`EXPLAIN QUERY PLAN` confirmed the new index does get picked up as a
`COVERING INDEX`, but the wall-clock difference is within run-to-run noise
in both directions. At this row scale SQLite's per-event query already
does a single small B-tree seek regardless of whether the leaf page also
carries `status` — there's nothing to save. Rejected: the index would add
a third B-tree to maintain on every `outbox` insert (permanent write-side
cost) for zero measured read-side benefit.

### 4. `executemany()` for `record_event()`'s subscriber fan-out — tested, rejected (no measurable gain)

`record_event()` currently loops over active subscribers and issues one
`conn.execute(INSERT ...)` per row inside its single `BEGIN
IMMEDIATE`/`COMMIT` transaction. Candidate: replace the loop with one
`conn.executemany()` call.

Measured directly (5000-subscriber fan-out, real FK-valid rows, same
transaction wrapping in both cases):

```
loop:        26.13 ms
executemany: 26.32 ms
```

No difference. `executemany()` mainly saves *Python-level* call overhead
between statements; here the cost is dominated by SQLite itself doing
5000 real B-tree inserts (into `outbox` plus its two indexes) inside an
already-single transaction, which is the part `executemany()` doesn't
change. Rejected — the code would get marginally less readable for a ~1%
(noise-level) change.

### 5. One transaction per `dispatch()` pass — tested, **real gain (~4x), rejected anyway** (durability tradeoff)

`outbox.dispatch()` currently calls `conn.commit()` once per row (via the
cancel path, the success path, and both branches of `_handle_failure()`).
Candidate: wrap the whole pass in one `BEGIN IMMEDIATE` / single final
`COMMIT`, matching how `core.record_event()` already batches its whole
transaction.

This is the one candidate that showed a real, reproducible effect. Built
an experimental copy of `dispatch()`'s core loop (same SELECT, same
per-row `UPDATE`s, single external channel) with per-row commits removed
and one commit added at the end; ran 4 trials, alternating which variant
ran first to control for disk-cache ordering effects, 10,000 rows each:

| trial | per-row commit (current) | one commit per pass | speedup |
|---|---:|---:|---:|
| 1 | 300.5 ms | 67.9 ms | 4.43x |
| 2 | 265.1 ms | 76.3 ms | 3.47x |
| 3 | 268.6 ms | 66.1 ms | 4.06x |
| 4 | 283.6 ms | 59.8 ms | 4.75x |

Consistently ~3.5–4.75x. (By contrast, the same batching trick applied to
`_check_zero_delivered_deadlines()`'s per-event commits, tested the same
way at n=2000, showed **no** reliable improvement across 4 alternating
trials — that loop's `UPDATE events SET zero_delivered_logged=1` touches
one un-indexed column on a small table, so there's much less to amortize.
Not applying that one either.)

**Rejected for `dispatch()` despite the real number.** Per-row commits
aren't accidental here — they bound the blast radius of a crash to at
most one row. Today, if the process dies between `adapter.send()`
succeeding and that row's `UPDATE ... status='delivered'` + `commit()`,
at most the *one row in flight* gets redelivered on the next pass.
Batching the whole pass into one transaction would multiply that window
from 1 row to up to the whole pass (10,000 in this benchmark; whatever
the real due-row count is in production) — a crash mid-pass would leave
every already-sent-but-not-yet-committed row still `status='pending'`,
and the next `dispatch()` run would resend all of them, double-notifying
however many subscribers had already been reached in that window. Given
this platform's own docstrings treat "alert latency/reliability IS the
product" as the design center, and per-row commit is exactly the
mechanism that keeps the mid-flight STOP-honoring and backoff bookkeeping
crash-safe one row at a time, this trade isn't a pure performance call —
it changes what a crash costs subscribers. Flagging the ~4x number for
whoever owns a future reliability-vs-throughput review, but not
applying it under a "perf-only, no behavior change" mandate.

## Finding: governor.acquire() over-grants under real concurrency

Not a performance result — a correctness one, surfaced by Scenario 4
(out of scope to fix here: `platformd/governor.py` isn't in this wave's
file ownership).

Setup: 8 threads, each with its own real sqlite connection to the same
tmp-file db (`busy_timeout=5000` set per-connection so lock contention
retries instead of raising), 500 `governor.acquire()` calls each against
one source, `daily_budget=2000`, `min_spacing_seconds=0`, fixed `now`.
Expected correct behavior: exactly 2000 of the 4000 calls return
`allowed=True`; the rest get `"budget-exhausted"`.

Measured (every run so far):

```
total_calls=4000  daily_budget=2000
total_allowed=4000        <- should be <= 2000
final requests_used=500..1918   <- varies run to run, always < total_allowed
over-budget grants=2000
```

**All 4000 calls were granted, 2x the daily budget**, and
`requests_used`'s final value doesn't even match the count of grants
(itself proof of lost updates). `governor.acquire()`'s
check-then-act — `SELECT` the row, decide in Python, `UPDATE` — isn't
wrapped in a serializing transaction, so under real multi-connection
concurrency (this repo's own db docstring: "isolation_level=None
(autocommit)") two threads can both read `requests_used` below budget,
both decide "allowed", and both proceed to increment, and the interleaving
also drops updates outright (hence `requests_used` undercounting its own
grant count). One caller alone (the common case today) never notices;
concurrent callers against the same governor `source` would blow through
the daily budget and the breaker's failure-count bookkeeping the same
way. Reproducible every run of `tools/bench_core.py`'s Scenario 4 — see
`scenario_governor_contention()` for the exact repro. This needs
`BEGIN IMMEDIATE` (or an equivalent single-writer serialization) around
`acquire()`'s read-decide-write, which is a `platformd/governor.py`
change outside this wave's file ownership.

## Files touched

- `tools/bench_core.py` (new) — the benchmark harness, runnable as
  `python3 tools/bench_core.py`
- `docs/platform/benchmarks.md` (new, this file)
- No changes to `platformd/db.py`, `platformd/core.py`, or
  `platformd/outbox.py` — every candidate optimization was measured and
  rejected (see above); the just-landed hardening semantics (backoff
  curve, provider breaker curve, mid-flight STOP-honoring, time-based
  ZERO-DELIVERED alarm) are untouched.

## Test suite

`python3 -m unittest discover -s tests` was green before and after this
wave (385 tests passing at the end — this repo has other agents landing
work in parallel this wave, e.g. `tests/test_platform_api.py`; this
wave's own scope added zero new tests to that suite, by design, since it
touches no production code path).

---

# HTTP API load test (`tools/loadtest_api.py`)

Run with:

```
python3 tools/loadtest_api.py
```

Runs in ~4-5s (well under the 90s budget). stdlib only, no network:
`platformd.api.make_server()` is started in-process on an ephemeral
127.0.0.1 port against a fresh tmp-file sqlite database, with fake
in-process channel senders standing in for Telegram/Discord/ntfy (see
`FakeChannel` in the tool). The rate limiter is widened via
`make_server()`'s existing `rate_limit_capacity`/`rate_limit_window_seconds`
constructor knobs (already present in `platformd/api.py` — no change
needed to add it) so this file measures concurrency, not the rate
limiter's own trip point (that's `PlatformAPIRateLimitTestCase`'s job).

## Machine

Same box as the core benchmarks above: Apple M4, macOS 15.5 (24.5.0
Darwin), local APFS SSD, Python 3.9.6, `sqlite3` module 2.6.0, SQLite
library 3.43.2.

## Result: no code changes applied

**Zero failures at any load level this file drives — `platformd/api.py`
and `platformd/db.py` are both untouched by this wave.** Every hard
assertion passed on every run (6 consecutive runs measured while
developing this file, plus the run recorded below): zero 5xx, zero
"database is locked" errors, scenario A's p95 comfortably under the
250ms budget, scenario D's outbox delivered exactly the expected row
count with zero lost and zero duplicated. See "Why no sqlite contention
appeared" below for the concurrency investigation the ground rules asked
for even though it turned up nothing to fix.

## Representative run

```
=== A READ STORM: 2000 GET /api/drops via 32 threads ===
[A READ STORM] requests=2000 errors=0 (0.00%) p50=13.24ms p95=51.79ms p99=163.35ms

=== B SUBSCRIBE STORM: 500 concurrent POST /api/subscribe via 32 threads ===
[B SUBSCRIBE STORM] requests=500 errors=0 (0.00%) p50=9.90ms p95=54.91ms p99=130.86ms
[B SUBSCRIBE STORM] parsed subscription_id for 500/500 responses
[B SUBSCRIBE STORM] end-to-end verify+unsubscribe on 100/500 sampled: 100 ok, 0 failed

=== C MIXED 80/20 read/write: 2000 requests via 32 threads ===
[C MIXED 80/20] requests=2000 errors=0 (0.00%) p50=13.81ms p95=58.37ms p99=224.44ms

=== D DISPATCH UNDER LOAD: 50 events x 200 subscribers ===
[D DISPATCH UNDER LOAD] passes=1 expected=10000 delivered=10000 total_outbox_rows=10000 non_delivered_rows=0 -> PASS

=== SUMMARY ===
elapsed: 4.4s
database-is-locked occurrences in server stderr: 0
HARD ASSERTIONS: PASS
```

| # | Scenario | requests | errors | p50 | p95 | p99 |
|---|----------|---------:|-------:|----:|----:|----:|
| A | READ STORM (2000 GET, 32 threads) | 2000 | 0 | 13.2 ms | 51.8 ms | 163.4 ms |
| B | SUBSCRIBE STORM (500 POST, 32 threads) | 500 | 0 | 9.9 ms | 54.9 ms | 130.9 ms |
| C | MIXED 80/20 (2000 req, 32 threads) | 2000 | 0 | 13.8 ms | 58.4 ms | 224.4 ms |

Scenario B's end-to-end sample (verify + unsubscribe on a random 100 of
the 500 subscribers, sequential, correctness not throughput) was 100/100
successful on every run. Scenario D (50 events x 200 subscribers = 10,000
expected outbox rows, driven concurrently with scenario C's traffic
against the same db file) delivered exactly 10,000/10,000 on every run,
zero lost, zero duplicated, in a single `dispatch()` pass every time
(subscribers provisioned at `tier="paid"` so `not_before` has no delay,
and 10,000 rows apparently clears in one pass comfortably inside this
scenario's 60s drain budget).

p50/p95/p99 stayed in the same ~10-60ms / ~100-225ms band across 6
consecutive runs taken while developing this file — no run came close to
tripping the 250ms p95 budget on scenario A (worst observed p95 across
those runs: 74.0ms), and no run produced a single 5xx or "database is
locked" occurrence.

## Why no sqlite contention appeared

The ground rules for this wave called for measuring for sqlite
contention and, if it appeared, fixing it with a `busy_timeout` pragma,
confirming WAL, and/or single-writer serialization in `api.py` — in that
order, cheapest first. Checked directly against a connection from
`platformd.db.connect()`:

```
journal_mode: wal
busy_timeout (default): 5000
synchronous: 1        (NORMAL)
```

Both of the first two mitigations are **already in effect** without any
code change:

- **WAL is already on** — `platformd/db.py`'s `connect()` already runs
  `PRAGMA journal_mode = WAL` on every connection, so readers (scenario
  A/C's GETs) never block on writers and vice versa.
- **A 5-second busy timeout is already in effect** — `sqlite3.connect()`
  defaults its `timeout` parameter to 5.0 seconds when the caller doesn't
  pass one (`platformd/db.py`'s `connect()` doesn't), which the sqlite3
  module implements as exactly the same `PRAGMA busy_timeout` mechanism
  the ground rules asked to add. It was already there, just implicit
  rather than spelled out — confirmed via the `PRAGMA busy_timeout` query
  above.

To confirm this default timeout is actually load-bearing here (not just
coincidentally unneeded), rather than only trusting that it's present,
ran a throwaway isolated repro: 16 threads x 300 `BEGIN IMMEDIATE` +
insert + commit against one shared tmp-file db, with `sqlite3.connect`'s
`timeout` explicitly forced to `0` vs left at the `5.0` default (same
schema, same WAL mode, same machine):

| `connect(timeout=...)` | writes attempted | `database is locked` errors |
|---|---:|---:|
| `0` (busy_timeout disabled) | 4800 | 4550 |
| `5.0` (this repo's actual default) | 4800 | 0 |

So the contention this wave was watching for is real and reproducible
the moment the timeout is removed — `platformd/db.py`'s existing,
already-shipped defaults (WAL + the implicit 5s busy timeout) are what's
preventing it in production today, not luck or under-provisioned load.
Single-writer serialization in `api.py` (the third, most invasive
mitigation on the list) was never reached because the first two already
account for zero contention at every load level this file drives (2000
GET / 500 POST / 2000 mixed / 10,000 concurrent outbox writes). Not
adding serialization that isn't needed — it would only add latency and
complexity to every request for a failure mode that doesn't reproduce.

## Files touched

- `tools/loadtest_api.py` (new) — the load test harness, runnable as
  `python3 tools/loadtest_api.py`
- `docs/platform/benchmarks.md` (this section, appended)
- No changes to `platformd/api.py` or `platformd/db.py` — the load test
  exposed zero failures at the specified load levels, so per this wave's
  file-ownership rule ("ONLY if the load test exposes failures") neither
  file was touched. The rate-limiter constructor knobs it relies on
  (`rate_limit_capacity`, `rate_limit_window_seconds`) already existed in
  `make_server()` before this wave.

## Test suite

`python3 -m unittest discover -s tests` was green before and after this
wave: 385 tests passing, unchanged (this wave added no new test files —
`tools/loadtest_api.py` is a standalone script, not part of the
`unittest discover` suite).

## Addendum 2026-08-04 — the governor over-grant is fixed

Scenario 4's finding (acquire() over-granting under contention — up to
2000 grants past a 2000 budget, with lost updates in requests_used) was
fixed the same day: the grant is now a single conditional UPDATE, so the
breaker/budget/spacing checks and the spend happen inside one statement
that SQLite serializes. Post-fix numbers on the same scenario:

    8 threads x 500 calls = 4000 acquire() calls
    daily_budget=2000  total_allowed=2000  final requests_used=2000
    over-budget grants=0  (~34,500 acquires/sec)

The bench now hard-asserts both invariants (exact budget, no lost
updates) and `tests/test_platform_governor.py` pins them in the suite
(`GovernorConcurrencyTests`). The companion double-send hazard (audit
P0-3) got the same treatment: dispatch() claims each row via a
conditional UPDATE to `in_flight` with a 120 s lease before touching the
channel; `ClaimTests` pins exclusivity, exactly-once delivery across 4
concurrent dispatchers, lease reclaim, and no-steal of fresh claims.
Full suite after both fixes: 390 tests, OK.
