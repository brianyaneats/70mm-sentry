# 70mm Sentry — platform generalization

> **Paths moved (Oct 2026):** `platformd/` was split into `engine/`, `notify/`, `billing/`, and `app/`, and `adapters/` became `sources/`. Each has its own README. The phase docs keep the old names as written at the time.

This is the plan-of-record for turning a single-tenant Fandango watcher into
a multi-source, multi-tenant alerting platform. It does not overturn any of
the five decisions in `docs/architecture-notes.md` — stdlib-only, poll
because there's no push transport, timer-plus-lock over a daemon, layered
politeness, an off-box dead-man switch — it generalizes them from "the one
thing this box watches" to "any number of things any number of subscribers
watch." Where a decision here conflicts with one over there, the conflict is
named explicitly (there's exactly one: see "The invariant that splits,"
below).

Everything in `docs/platform/phase-*.md` is a plan, not a status report.
Check `git log` and the code, not this file, for what's actually landed.

---

## The four-layer decomposition

```
 [subscriber intent]                              [upstream reality]
        │                                                 │
        ▼                                                 ▼
 ┌──────────────┐   shared, per-source   ┌──────────────────────────┐
 │ subscriptions │◄── who watches what ──│      source adapter       │
 │  & drop       │                       │  (sources/base.py seam)  │
 │  registry     │                       │  fetch / parse / health   │
 └──────┬────────┘                       └─────────────┬─────────────┘
        │                                               │
        │ event fires                     ┌─────────────▼─────────────┐
        ▼                                 │  politeness governor       │
 ┌──────────────┐                         │  (token bucket + daily     │
 │ per-subscriber│                        │   budget + breaker) — ONE  │
 │    outbox     │                        │  instance per source name, │
 │  (N rows,     │                        │  shared across every       │
 │  independent  │                        │  subscriber watching it    │
 │  retry)       │                        └─────────────────────────────┘
 └──────────────┘
```

1. **Source adapter** — one class per upstream (`sources/base.py`'s
   `SourceAdapter`, frozen contract, read it before touching anything in
   this decomposition). Owns fetch shape, parse shape, and "is this payload
   healthy" — nothing else. It never sleeps, never retries, never opens its
   own connection; every network touch goes through the kernel-supplied
   `http_get`.
2. **Shared per-source politeness governor** — token bucket
   (`min_spacing_seconds`) + daily budget (`daily_request_budget`) +
   circuit breaker, keyed on `adapter.name`. One instance per source,
   full stop — not one per subscriber, not one per (subscriber, source)
   pair. This is load-bearing; see "polling happens once per target," below.
3. **Drop registry / subscriptions** — durable state: what targets exist
   (venue+date, campground+site+date, ...), who's subscribed to which
   target on which channel, and the append-only event log + dedup ledger
   that decide whether an observed state change is *new* news.
4. **Per-subscriber outbox delivery** — one row per (subscriber, channel,
   event), each retried independently with its own backoff and TTL. A dead
   channel for subscriber B is B's problem, not A's, and not detection's.

Layers 1–2 are source-facing and shared; layers 3–4 are subscriber-facing
and fan out. The seam between them is where "one watcher, one person" turns
into "one watcher, many people" without multiplying load on the thing being
watched.

---

## The one invariant that splits

Single-tenant sentry.py has a rule stated plainly in `restock_check`'s
docstring (sentry.py:1166, function starting at 1155 — line numbers as of
2026-08-03; this file is under active concurrent revision, check current
line numbers before citing): **a baseline only advances to "available"
once a push has actually delivered.** `push()` (sentry.py:775) fans out to
every enabled channel and returns `True` if *any* delivered; `restock_check`
(the `if freed:` block at sentry.py:1199, gated at the `continue` on
sentry.py:1195) only writes `seenAvailability` forward on that `True`. The
reasoning is airtight for one subscriber: if the one
channel you have is down for a cycle, the flip must still be "new" next
cycle, or the only alert this tool exists to send gets eaten by an outage
that has nothing to do with whether the seat is actually available.

That rule does not survive contact with N subscribers. If "advance the
baseline" waits on "at least one delivery succeeded," and subscriber B's
Telegram bot is misconfigured, does subscriber A's identical, correctly
configured channel also get denied its alert because B's delivery hasn't
resolved? Does detection itself stall on B, blocking the *next* poll cycle
from running because the current one hasn't finished fanning out? Neither
answer is acceptable — a working subscriber must never wait on a broken one,
and the watcher must never stop watching because delivery is slow.

**The split:** detection and delivery stop being the same bit.

- **Detection advances on a durable transactional record** — the event log
  row, the dedup ledger row, and one outbox row per (subscriber, channel)
  currently subscribed to that target, all created in **one** database
  transaction, the moment the state change is confirmed. The ledger no
  longer waits on any push; it waits on the write committing.
- **Delivery is per-(subscriber, channel) outbox rows, retried
  independently.** Each row owns its own backoff and TTL. A subscriber's
  broken channel produces failed retries on their own row, forever if
  need be, and touches nothing else.

This is not "the single-user rule was wrong" — it was correct, for one
subscriber sharing fate with one channel. It's what "delivered" has to mean
once "delivered to whom" stops being a single answer. See
`docs/platform/phase-3.md` for the schema and the drill that proves it
(kill one subscriber's channel mid-run; the others don't notice).

---

## Polling happens once per target, never per subscriber

Directly downstream of the shared governor (layer 2): if ten subscribers
watch the same venue and date, that's one poll, fanning out to up to ten
outbox rows on a hit — never ten polls. The politeness budget
(`min_spacing_seconds`, `daily_request_budget`) is a property of the
relationship with the *upstream*, not a per-subscriber allowance. Fandango's
NAPI doesn't know or care how many people are downstream of a request; it
only sees request volume against one IP. Scaling subscriber count linearly
against poll count would turn "ten friends want the same date" into "ten
times the burst risk," which is exactly the shape bot detection keys on
(architecture-notes.md, Decision 4). The drop registry's job is to
deduplicate *targets*, not just events — many subscriptions, one target,
one poll.

---

## Paywall is notify-delay, not poll-delay

Detection always runs at the platform's best safe cadence against the
shared governor, for every subscriber, free or paid, on a given source.
What a paid tier buys is how fast the *notification* reaches that
subscriber once detection has already happened — dispatch-time tiering on
already-detected events, not a second, slower detection loop for
non-payers. Two reasons this is the right axis to sell, not a compromise:

- **It doesn't touch the shared budget.** A free-tier subscriber sitting on
  a slower notify queue costs the platform nothing extra in requests to the
  upstream; a free-tier subscriber sitting on a slower *poll* loop would
  mean running two politeness governors against the same source, which
  defeats the entire point of layer 2.
- **It matches the precedent.** AMC Sniper's own paywall is cadence
  (free 60s vs Plus 5s, see `docs/amcsniper-teardown.md`) — but that's
  cadence at the *poll*, because they don't share a governor across tiers.
  Selling notify-delay instead of poll-delay is available specifically
  *because* this platform already centralized polling in layer 2; a
  cadence-paywall would have re-fragmented it back to one governor per
  price tier.

---

## Source risk tiers

Enforced in code via `sources/base.py`'s `risk_tier` class attribute — not
a policy document elsewhere that code can drift from. The kernel refuses to
load a `RED` adapter at all; it refuses to place a `YELLOW` adapter's source
on any paid or prepaid plan.

| Tier | Meaning | Enforcement | Current example |
|---|---|---|---|
| **GREEN** | Sanctioned API, contractually authorized (or a public-data endpoint judged safe under sustained third-party precedent, with an owner-run live preflight before production traffic) | Paid tiers allowed | recreation.gov's availability endpoint (`sources/recreation_gov.py`) — public government data, not formally an API contract, but years of unenforced third-party use; the module's own docstring calls this "an operational judgment call, not a legal guarantee." AMC's vendor API, once approved (`phase-0.md`), lands here properly — a real contract, not a judgment call. |
| **YELLOW** | Public endpoint, tolerated-gray | Free tier only — never prepaid, never a paid SKU; disclosed "may go dark" risk in product copy | Fandango NAPI — `robots.txt` disallows `/napi/`, no published rate limit for the public site, tolerated in practice via 403/429 enforcement rather than an outright block (`docs/architecture-notes.md`, Decision 4; `ticket-sniper-domain` skill). Settled as permanently free in `phase-0.md`. |
| **RED** | Requires circumvention of an access control | Never built, full stop | Queue-it token forging or unattended headless polling of AMC's public site to get seat-map data without the vendor API. Named explicitly in the `ticket-sniper-domain` skill's boundaries; no code in this repo does this and none is planned to. |

A tier is a property of the *source*, checked once at adapter load, not a
judgment call re-litigated per feature. If a source's legal posture
changes, the fix is changing one class attribute, and everything downstream
(paywall eligibility, status-page badge, breaker leniency) follows from it
automatically.

---

## Observability standard

Every failure path in this platform logs a stable, grep-able tag. This is
not a suggestion — tests assert the log line appears on the failure path
that produces it, the same way `TestBreakerBackoff` in the existing suite
pins the breaker's first-trip timing (`architecture-notes.md`, Decision 4).
A tag that isn't asserted by a test is a tag that will silently stop firing
the first time someone refactors the catch site around it.

Four tags are adapter-facing and already named in the frozen contract
(`sources/base.py`, lines 21–26):

- **`[PIPE]`** — broken pipe, connection reset, or a dead kept-alive socket
  on fetch. `BrokenPipeError`, `ConnectionResetError`, and
  `socket.timeout` on a reused connection are *expected events on a
  long-lived HTTP connection*, not anomalies. `RealtimeSession.get()`
  already retries once silently on any `Exception`; the kernel extraction
  (`phase-1.md`) adds an `isinstance(e, OSError)` check ahead of that catch
  — `OSError` being the shared superclass of all three named exceptions —
  to log `[PIPE] <adapter> <context> (errno=<n>, <exception class>): <e>`
  before the existing retry/re-raise logic runs (already landed as of
  `phase-1.md`'s last check; see that doc for the exact current line).
  Logging the event is additive, it does not change whether the retry
  happens.
- **`[BLIND]`** — HTTP came back OK, but the payload is empty or doesn't
  match the shape the adapter expects. `health_check()` is where this gets
  decided; a `False` from it is a `[BLIND]` log line, not a silent pass.
- **`[REFUSED]`** — 403 / 429 / 5xx from upstream. Distinct from `[PIPE]`
  on purpose: a refusal is the upstream *answering*, which is a different
  signal (rate-limited or blocked) than the connection dying mid-flight.
  Conflating the two would open the breaker for the wrong diagnosed reason
  (`ticket-sniper-domain` skill: "distinguish self-imposed limits from
  upstream failures").
- **`[PARSE]`** — the payload fetched fine but `parse()` raised. Per the
  contract's own docstring (sources/base.py:105–109), a parser must raise
  on an unrecognized shape rather than return `[]` — a silent empty list on
  a changed feed shape is indistinguishable from "nothing's on sale," which
  is precisely the failure mode this tag exists to surface instead of hide.

Two more are kernel/delivery-facing, not adapter-facing — they don't belong
in the frozen adapter contract because no adapter method produces them; they
belong to layers 3–4, specified in `phase-3.md`:

- **`[DELIVERY]`** — one subscriber's one channel send failed. Fires per
  outbox row, independent of every other row for the same event.
- **`[ZERO-DELIVERED]`** — an event fired and every single subscriber
  delivery for it failed (0/N succeeded). This is the rollup that says "the
  thing this platform exists to do — tell someone — did not happen for
  anyone," and it is the one condition that should page a human, not just
  sit in a log.

Two more are governor-facing (`engine/governor.py`), covering the
self-imposed denials that are deliberately distinct from upstream failures —
conflating them is how a budget exhaustion gets misdiagnosed as an IP block:

- **`[BUDGET]`** — a request was denied because the source's daily request
  ceiling is spent. Self-imposed, not upstream's doing.
- **`[BREAKER]`** — a request was denied because the source's circuit
  breaker is open (accumulated real failures). The breaker curve mirrors
  the sentry's tuned one: first trip short, doubling to a 4 h cap.

The rule, stated once so it doesn't need restating per phase doc: **every
failure path logs exactly one of these eight tags, and every test that
exercises that failure path asserts the tag's presence in the log output.**
Not "logs something" — logs the specific stable string, so `grep` across
months of logs answers "how many `[REFUSED]` events has source X had this
week" without parsing anything.

---

## Scarcity honesty

Every subscriber added to a target competes for the same, unchanged
inventory — the same seats, the same campsites. Adding a hundred watchers
to one showtime doesn't create a hundred more tickets; it means a hundred
phones buzz for the same handful of returned seats, and the fastest hand
still wins. This is a fact about the product, not an implementation detail,
and it does not stay in an internal doc: public-facing copy (the catalog,
the subscribe flow, the status page — `phase-4.md`) must say it plainly,
the same way `ticket-sniper-domain`'s own boundary note puts it: "every
added user competes for the same scarce seats — name that trade-off." A
watcher that's honest about not creating supply is worth more trust than
one that implies otherwise by omission.

---

## Kill criteria

Two conditions end this generalization, not just slow it:

- **Phase-2 transfer failure.** If the recreation.gov vertical needs
  Fandango-shaped special cases bolted onto the kernel to fit — if
  `phase-2.md`'s gate (zero Fandango-shaped special cases) fails — that is
  evidence the "one shared kernel, many sources" thesis is wrong at the
  architecture level, not evidence the second adapter needs more work.
  **Response: run the verticals as separate products.** Two small,
  independent watchers are a better outcome than one kernel straining to
  abstract over two things that don't actually share a shape.
- **AMC declined AND campsites don't convert.** If the AMC vendor
  application (`phase-0.md`) comes back declined *and* the campsites
  vertical (`phase-2.md`/`phase-4.md`) doesn't find paying subscribers,
  that's both legs of the platform thesis failing at once — no sanctioned
  per-seat movie vertical, and no market for the one vertical that did
  ship. **Response: platform thesis is wrong. Keep the campsite watcher**
  as a single-purpose tool (it still works, still has value, just isn't a
  platform) and stop investing in multi-tenant/multi-source generalization.

Either kill criterion firing is a successful outcome of asking the
question, not a failure of execution — the whole point of Phase 2 as "the
second adapter chosen to hurt" is to find out cheaply, before Phase 3/4
engineering is sunk into a thesis that doesn't hold.

---

## Estimate: ~3 months solo to launchable Phase 4

Same constraint as `architecture-notes.md` Decision 1: one maintainer,
stdlib-only, no team to divide phases across — the bottleneck resource is
one person's review bandwidth, not raw coding throughput. Rough allocation,
each phase gated on the previous one's gate passing (not calendar time):

| Phase | Work | Rough weight |
|---|---|---|
| 0 | Owner actions (file, decide) — filed early, runs in parallel with 1–2, not blocking them | days, then wait on AMC's unknown SLA |
| 1 | Kernel extraction behind the frozen seam, zero behavior change | ~2–3 weeks |
| 2 | Second adapter (recreation.gov), fully mocked, kernel-shape gate | ~2 weeks |
| 3 | Multi-tenant core: event log, ledger, outbox, governor, breaker | ~4–5 weeks |
| 4 | First public surface: catalog, subscribe, Stripe cap, two channels, status page | ~3–4 weeks |

That totals to roughly three months of solo, part-time-adjacent effort,
assuming Phase 0's AMC application doesn't gate Phase 4's movie vertical —
which is exactly why `phase-0.md` says to file it now, before Phase 1 code
exists, rather than waiting until Phase 4 needs an answer.
