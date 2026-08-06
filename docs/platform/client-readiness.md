# Client-readiness audit — platform generalization

Read-only audit of `platformd/*.py`, `adapters/*.py`, `docs/platform/*.md`,
and `tests/test_platform_*.py` as they stand in the tree on 2026-08-04.
Scope is deliberately **beyond** the wave already in flight (channel
verification pending→active, unsubscribe tokens, drops catalog table,
time-based `[ZERO-DELIVERED]`, the localhost HTTP API + web UI, per-IP rate
limiting, benchmarks/load tests) — those are assumed to land; this document
is what a real external user, or a real multi-process deploy, would hit
*in addition to* that list. `python3 -m unittest discover -s tests` is
354 tests, all passing, at the time of this audit — nothing here was found
by breaking a test; several findings are gaps no current test exercises.

No files were modified except this one.

---

## P0 — fix before any real subscriber or multi-worker deploy touches this

### P0-1. `add_subscription()` defaults to instantly active — there is no verification gate at the primitive level

**Evidence:** `platformd/core.py:21-34`

```python
def add_subscription(conn, subscriber_id, drop_id, channel, address, tier,
                      active=1):
    ...
    cur = conn.execute(
        "INSERT INTO subscriptions "
        "(subscriber_id, drop_id, channel, address, tier, active) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (subscriber_id, drop_id, channel, address, tier, int(bool(active))),
    )
```

and `platformd/core.py:94-108`, which fans out a real outbox row to
**every** `active = 1` subscription on the very next `record_event()` —
no confirmation step exists anywhere between "row inserted" and "message
sent."

**Failure scenario:** Alice knows Bob's Telegram chat_id (chat_ids are not
secret — they're visible in any group Bob is in, or guessable from a bot
that leaks them). Alice calls whatever ends up wired to
`add_subscription(conn, "alice", some_drop, "telegram", bob_chat_id,
"free")`. Nothing in this function — the one shared primitive every
future caller (the web API, a future Telegram bot command handler, an
admin script, a migration tool) will use — stops the insert from landing
`active=1`. Bob starts receiving unsolicited alert messages from a bot he
never talked to, with no way to know why or who did it, until he blocks
the bot. This is a harassment/spam vector, not just an inconvenience, and
it is present in the reusable library function itself, not just in a
not-yet-written HTTP handler.

**Why "the wave is adding verification" doesn't close this on its own:**
verification lives at whatever call site the web API wires up. If that
call site is careful and always passes `active=0` until a code is
confirmed, this is fine — but the *default* on the shared primitive is
still the unsafe one, which means the very next caller who doesn't know
to override it (a bot-command handler, a bulk-import script, a test
fixture that leaks into a real path) silently reopens the hole. A default
that has to be remembered to be safe is a default that will eventually
not be remembered.

**Fix:** Flip the default to `active=0`. Require every call site that
wants immediate activation to say so explicitly
(`add_subscription(..., active=1)`), and reserve that explicit override
for tests/drills. Add a `verified_at` column and a real `activate(conn,
subscription_id)` function that is the *only* path from `active=0` to
`active=1`, called exclusively after a verification code is confirmed.
`record_event()`'s `WHERE drop_id = ? AND active = 1` filter
(`core.py:96`) already does the right thing once nothing can reach
`active=1` except through that one function.

---

### P0-2. The per-source politeness governor is not process-safe — a multi-worker deploy defeats the anti-burst guarantee it exists to provide

**Evidence:** `platformd/governor.py:62-103`, the whole `acquire()`
function. Contrast with `platformd/core.py:70` (`record_event`), which
wraps its read-modify-write in `conn.execute("BEGIN IMMEDIATE")` —
`governor.py` never does this anywhere in the file.

`acquire()` reads `requests_used`/`last_request_at`/breaker state with a
plain `SELECT` (`_get_or_create_row`, `governor.py:29-44`), makes an
allow/deny decision in Python, and only *then* issues the spend-recording
`UPDATE` (`governor.py:97-102`) as a separate, later statement. Under
`isolation_level=None` (autocommit, `db.py:89`) each of those is its own
implicit transaction — there is no lock held across the read and the
later write.

**Failure scenario:** Two worker processes (the stated near-term
deployment shape — "multi-worker deployment gaps" is explicitly in scope)
both call `governor.acquire(conn, "fandango", now, 1.0, 25000)` within
the same instant. Both read `last_request_at` from a moment before either
has updated it, both compute `elapsed >= min_spacing_seconds` as true,
both return `allowed=True`, and both then fire a request to Fandango's
NAPI in the same fraction of a second. The entire point of this module —
`min_spacing_seconds` as "the single choke point every code path shares,
so ... target-date and restock checks ... can't produce a burst — and a
burst, not average rate, is the shape bot detection actually keys on"
(`docs/platform/README.md:113-124`, echoing
`docs/architecture-notes.md` Decision 4) — is exactly what this race
defeats. The same race lets `requests_used` overshoot `daily_budget` by
however many workers race the same check simultaneously (a much smaller
problem than the spacing-floor burst, but the same root cause). This
isn't a theoretical multi-process concern layered on top of the
in-memory rate limiter/provider breaker gap already known about — it's
the *sqlite-backed* governor, the piece that was supposed to be the
durable, shared-across-processes answer, that also isn't actually safe
under concurrent access as written.

**Fix:** Wrap `acquire()`'s read-check-write sequence in
`BEGIN IMMEDIATE` the same way `record_event()` already does — SQLite
serializes writers under WAL, so a second process's `BEGIN IMMEDIATE`
blocks until the first commits, then re-reads fresh state instead of
racing against a stale one. Add a regression test that opens two
connections against the same on-disk (not `:memory:`) database and
asserts only one of two simultaneous `acquire()` calls at a spacing-
violating interval returns `allowed=True`.

---

### P0-3. `outbox.dispatch()` has no row-claiming step — concurrent or overlapping passes can double-send to a real subscriber

**Evidence:** `platformd/outbox.py:84-137`. The due-rows query
(`outbox.py:90-103`) is a plain `SELECT`; nothing marks a row as "being
worked" before the loop attempts `adapter.send(...)`
(`outbox.py:119-121`) and only afterward updates `status`
(`outbox.py:129-134`). There is no equivalent anywhere in `platformd` of
`sentry.py`'s `acquire_lock()`/`flock` single-flight guard
(`docs/architecture-notes.md`, Decision 3) that stops two overlapping
invocations of the same work from running at once.

**Failure scenario:** two `dispatch()` calls overlap — either two worker
processes both running the dispatch loop (the explicit near-term
deployment shape), or a single-process cron-style caller whose previous
invocation is still mid-loop (slow channel, network stall) when the next
scheduled tick fires, mirroring the exact concurrency hazard
`docs/architecture-notes.md` Decision 3 already solved once for the
single-tenant poller and never re-solved here. Both calls `SELECT` the
same `pending` outbox row before either has committed a `status`
update, both call `adapter.send()` for it, and the subscriber gets the
same alert twice. For a channel like Telegram this is merely annoying;
for a paid subscriber judging the product on "alert reliability is the
product," a duplicate is a visible reliability defect, and for a channel
billed per-message (SMS, deferred but road-mapped in `phase-4.md`) it is
also a direct cost multiplier.

**Fix:** Claim rows atomically before sending — e.g. an `UPDATE outbox
SET status='in_flight' WHERE id IN (...) AND status IN ('pending',
'failed')` (checking `sqlite3.Cursor.rowcount` to confirm the claim
actually took) inside a `BEGIN IMMEDIATE` transaction, *before* the send
loop, or equivalently claim one row at a time with an
`UPDATE ... WHERE id=? AND status IN (...)` guarded by rowcount. Add a
drill/test that runs two `dispatch()` calls against the same on-disk
database with overlapping due rows and asserts the channel's `send()` is
called exactly once per row.

---

## P1

### P1-4. No attempt-limiting, expiry, or entropy requirement designed into the verification-code path

The wave adds pending→active channel verification, but nothing in the
current schema (`platformd/db.py:16-79`) or `core.py` reserves columns
for a code, its expiry, or an attempt counter — meaning whoever
implements it is starting from zero guardrails, not retrofitting weak
ones. Concretely, the design needs, from day one: a high-entropy code
(not a 4-6 digit numeric-only code guessable in seconds without
throttling), a short expiry (minutes, not days), and a hard attempt cap
per subscription *and* per source IP that locks the code (forces
re-issue) rather than allowing unlimited retries — mirroring the same
"self-imposed ceiling, not just upstream's doing" argument the
`[BUDGET]` tag already embodies for outbound requests
(`platformd/governor.py:1-14`). Without this, a victim's channel can be
brute-forced into "verified" by an attacker who intercepts or guesses the
code, which is a materially worse outcome than P0-1's spam vector — a
verified, attacker-controlled subscription looks legitimate to every
downstream system.

**Fix:** add `verification_code`, `verification_expires_at`,
`verification_attempts` columns (or a small side table) before the
web-API wave wires anything to them; enforce max-attempts and expiry
inside the same function that checks the code, not in the HTTP handler.

### P1-5. Unsubscribe-token design risk: sequential IDs plus no token infrastructure yet is an enumeration and mass-unsubscribe vector

`subscriptions.id` is a plain `INTEGER PRIMARY KEY AUTOINCREMENT`
(`platformd/db.py:39`). If the unsubscribe-token feature encodes or
hashes this ID predictably (or worse, uses it directly), an attacker can
enumerate other subscribers' unsubscribe links and silently cancel their
subscriptions — a low-effort, high-impact denial-of-service against every
subscriber, not just one victim. Separately: an unsubscribe link that
performs the actual state change on a bare `GET` is vulnerable to email
link-prescanning (Outlook Safe Links, corporate mail-gateway scanners,
Slack/Discord embed unfurlers) silently visiting the link and triggering
an unintended unsubscribe before the subscriber ever clicks it — a
well-known real-world gotcha for any unsubscribe-by-link design, not
specific to this codebase, but worth naming explicitly since the token
work is starting from scratch here.

**Fix:** generate the unsubscribe token as an independent
high-entropy random value (`secrets.token_urlsafe`, stored or comparable
via HMAC — not derived from `subscriptions.id`), and require the actual
unsubscribe action to be a state-changing request a prescanner won't
trigger (a `GET` that shows a confirm page + button which `POST`s, not a
`GET` that unsubscribes directly).

### P1-6. Exception text is logged and stored verbatim — a future channel adapter can leak subscriber PII into logs and the database

**Evidence:** `platformd/outbox.py:146-148` —

```python
_log.log(_log.TAG_DELIVERY, "delivery failed",
          channel=channel, subscription_id=row["subscription_id"],
          exc_class=type(exc).__name__, error=str(exc)[:200])
```

— and `outbox.py:160-163`/`168-172`, which persist the same
`str(exc)[:500]` into the `outbox.last_error` column. Today's code
already does the right structural thing by logging `subscription_id`
rather than `row["address"]` directly (credit where due — no current log
call in `platformd/` passes a raw address). But `str(exc)` is not
sanitized, and channel-provider client libraries commonly embed the
recipient in their own exception messages (`"failed to deliver to
+15551234567: timeout"`, `"webhook https://discord.com/.../<id>/<token>
returned 404"`). The moment a real Telegram/Discord/SMS `send()`
implementation lands, this becomes a live PII-in-logs violation without
anyone having to write a new bug — the existing code will do it
automatically, because it never filters what an adapter's exception is
allowed to say.

**Fix:** channel adapters should raise a small typed exception (e.g.
`ChannelSendError(reason)`) whose message is guaranteed not to embed the
address, and `_handle_failure` should log/store that message, not
`str(exc)` from an arbitrary third-party exception type. At minimum,
strip or redact anything matching the row's own `address` value out of
`str(exc)` before it reaches `_log.log` or the `last_error` column.

### P1-7. No deletion or right-to-forget path for subscriber PII

`subscriptions.address` (`db.py:43`) — a phone number, Discord webhook,
or chat_id — is written once and never has a deletion path anywhere in
`platformd/core.py` or `db.py`; there is no `delete_subscription`,
`anonymize_subscriber`, or equivalent. `events`/`ledger` are meant to be
durable/append-only by design (`phase-3.md:61`: "append-only. never
UPDATEd, never DELETEd — the audit trail IS the log"), which is correct
for the event log itself, but `subscriptions.address` and
`outbox.last_error` are not audit trail — they're live PII with no purge
mechanism, and a real subscriber base means real deletion requests
(GDPR/CCPA-shaped or just "I don't want you to have my number anymore").

**Fix:** add a subscriber-deletion path that deactivates the
subscription and overwrites/nulls `address` (and any PII embedded in
`outbox.last_error` rows referencing that subscription), while leaving
the `events`/`ledger` audit trail itself untouched — deletion and
audit-durability are not in tension if the PII lives only in
`subscriptions`/`outbox`, not in `events`.

### P1-8. No schema migration or versioning story, with three more schema changes about to land in the same wave

**Evidence:** `platformd/db.py:82-94` — `connect()` runs
`executescript(SCHEMA)` with `CREATE TABLE IF NOT EXISTS` only. There is
no `PRAGMA user_version`, no migrations directory, and no story for
adding a column to an existing table (`ALTER TABLE ... ADD COLUMN`) on a
database that already has rows — which is exactly what verification
columns, unsubscribe-token columns, and the drops-catalog table (all
named as landing this wave) will need on any database that isn't freshly
created. `docs/platform/phase-3.md:1-28` explicitly plans a future
Postgres migration as "a `CREATE TABLE` translation, not a redesign" —
that plan assumes a disciplined schema evolution story that doesn't
exist yet even for the SQLite-to-SQLite case.

**Fix:** track a schema version (`PRAGMA user_version` is built into
sqlite3 for exactly this) and gate new `ALTER TABLE`/`CREATE TABLE`
statements behind a version check in `connect()`, so a database created
before this wave's columns existed gets upgraded in place rather than
silently missing them (an `INSERT` against a column that doesn't exist
yet fails loudly, which is at least safe — but only if every code path
that assumes the new column runs `connect()` through the same upgrade
gate first).

### P1-9. No backup/restore procedure for the WAL-mode sqlite database

`db.connect()` turns on `PRAGMA journal_mode = WAL` (`db.py:92`), which
means the database's true state is split across the main file and a
`-wal`/`-shm` sidecar until a checkpoint happens. A naive `cp
platformd.db backup/` while the process is live can capture the main
file without the WAL contents that haven't been checkpointed yet,
producing a backup that silently drops recent subscriptions/events/
outbox rows. Nothing in the tree (no script, no doc) describes a backup
procedure, and unlike `sentry.py`'s `state.json` (a single JSON file,
trivially backed up by copying it), this is the first piece of this
project holding subscriber PII and durable event history in a format
that isn't safe to `cp` while running.

**Fix:** document and script a real backup path — either
`sqlite3.Connection.backup()` (stdlib, does this correctly including WAL)
or a `PRAGMA wal_checkpoint(TRUNCATE)` immediately before a file-level
copy — and say explicitly, next to `docker-compose.yml`'s existing
volume-mount comments, where the platform db file lives and that it
needs this treatment, not a plain `cp`/`rsync`.

### P1-10. The channel-provider breaker is in-process only — confirmed gap, and it has the same failure shape as the not-yet-built per-IP rate limiter

**Evidence:** `platformd/outbox.py:27-34` —

```python
# Provider-level breaker bookkeeping, in-process, keyed by (conn identity,
# channel name). ...
_provider_breakers = {}
```

The module's own docstring is honest that this is in-process, and frames
it as a deliberate test-isolation choice (keying on `id(conn)` so
`:memory:` test connections don't bleed state into each other) — that
part is fine for tests. What the comment doesn't say is the production
implication: under N worker processes, each has its own
`_provider_breakers` dict, so "opens after 5 distinct-subscriber
failures" (`PROVIDER_BREAKER_FAIL_THRESHOLD = 5`, `outbox.py:24`)
actually becomes "opens after 5 failures *per worker*" — a channel
provider that's genuinely down gets hammered by every worker
independently until each one individually accumulates 5 failures, and a
worker whose breaker is open still lets other workers keep sending into
the outage. This is the exact shape of gap already flagged for the
in-flight per-IP rate limiter — worth stating plainly here with the
concrete evidence this one already has, since it's a real, already-
written piece of code rather than a forward-looking risk about
not-yet-written code.

**Fix:** move provider-breaker state into the same `governor` table (or
a sibling table keyed on `channel` instead of `source`), following the
exact pattern `platformd/governor.py` already uses for per-source
breakers — sqlite as the cross-process shared state, the same fix this
audit already prescribes for the rate limiter about to be built.

### P1-11. `db.connect()` never sets `PRAGMA busy_timeout` — concurrent writers fail immediately instead of queuing, compounding P0-2/P0-3

**Evidence:** `platformd/db.py:82-94`. Under WAL mode, SQLite allows one
writer at a time; without a `busy_timeout`, a second connection that
tries to write while another holds the write lock gets an immediate
`sqlite3.OperationalError: database is locked` rather than blocking and
retrying for a bounded window. Nothing in `governor.py` or `outbox.py`
catches or retries on that error. Combined with P0-2/P0-3, this means
the failure mode of concurrent access isn't "slightly delayed, correctly
serialized" — it's "one of the two racing calls raises an unhandled
exception," which is a worse outcome for the exact code paths (fetch
politeness, dispatch) that most need to keep running under a multi-
worker deploy.

**Fix:** `conn.execute("PRAGMA busy_timeout = 5000")` (or similar) in
`connect()`, in addition to the transactional fixes in P0-2/P0-3 — this
makes contention wait instead of crash, it doesn't replace the need to
actually serialize the read-check-write sequences.

### P1-12. UX: a web subscribe form is very likely the wrong first surface for Telegram/Discord-only channels — argued, not assumed

`phase-4.md` plans a web catalog + subscribe form as the first public
surface, with channel verification bolted on specifically because a web
form lets anyone type in *anyone's* Telegram chat_id or Discord webhook
(P0-1). But the two channels actually shipping at launch — Telegram and
Discord (`phase-4.md:41-50`) — are both chat platforms with native bot
frameworks where **the act of subscribing can itself be the proof of
channel ownership**: a `/watch <target>` command sent to the bot
supplies the chat_id the platform will message *by construction* — a
subscriber cannot type in someone else's chat_id via a bot command the
way they can type it into a web `<input>`, because Telegram/Discord hand
the bot the sender's own identity with the interaction, not a
free-text field. A bot-command-first subscribe flow doesn't mitigate
the "subscribe a victim" attack surface with a verification code — it
removes the surface, for exactly the two channels this platform is
actually launching with.

This doesn't make the web surface useless: the read-only public catalog
and status page (`phase-4.md`'s own scarcity-honesty and reliability-
proxy arguments) are still better served by a browsable page than a bot
command, and Discord webhook addresses (as opposed to bot-DM chat_ids)
are less clearly "proven by the interaction" than a direct bot
conversation is. The concrete recommendation: keep the web surface for
**discovery only** — catalog, status page, and a "subscribe via
Telegram/Discord" deep link that opens the bot with the target
pre-filled (`t.me/<bot>?start=<target_id>`) — and treat the *subscribe
action itself* as a bot command, not a web form submission. This
demotes the verification-code workstream from "the primary defense
against subscribing a victim" to "defense in depth for the Discord-
webhook and any future non-chat channel case," which is a meaningfully
smaller and lower-stakes problem than defending a general-purpose web
form. If a web-form subscribe path is kept anyway (e.g. because it's
simpler to ship first), P0-1 and P1-4 are not optional hardening — they
are the entire safety mechanism for that path, and should be treated
with correspondingly higher priority than they would be if the bot-first
design were adopted instead.

---

## P2

### P2-13. Secrets posture for the new HTTP API/web UI has no stated discipline yet

The existing single-tenant deployment keeps the Docker image secret-free
by design (`Dockerfile:13-14`: "the image stays read-only and secret-
free so it is safe to push to a registry"; `docker-compose.yml`'s
`.env`-sourced `TELEGRAM_TOKEN` etc.). Nothing yet says whether the new
localhost HTTP API will need its own secrets (an admin/ops auth token
for any non-public endpoint, a Stripe secret key once payment lands,
a session-signing secret) or how they'll be supplied. This isn't a
finding against code that exists — it's a gap in the plan that should be
closed before the API's admin surface (if any) is written, not after,
given how deliberately the existing secret-handling discipline is
documented elsewhere in this repo.

**Fix:** before the HTTP API lands, write down (in `phase-4.md` or a
sibling doc) which endpoints are public/no-auth (catalog, status) versus
which need an admin credential, and where that credential is sourced
from (env var, not `config.json`, matching the existing pattern).

### P2-14. Confirm subscriber/subscription enumeration stays impossible even though the drops catalog is intentionally public

The drops catalog is *supposed* to be publicly browsable, no auth,
per `phase-4.md:11-15` — that's a deliberate, correct design choice, not
a finding. The thing worth flagging pre-emptively is that no endpoint
derived from the catalog (a per-drop "who's watching this" list, a
per-drop subscriber count broken down finely enough to reveal identity
in a small pool, etc.) should ever expose `subscriptions.subscriber_id`
or `address`. Nothing in the current schema prevents a future "show
watcher count per target" catalog feature from accidentally joining
through to identity-bearing columns if it's built by copy-pasting a
query that already has `subscriptions` in its `FROM` clause. Worth a
one-line rule in `phase-4.md` now, before the catalog endpoint is
written, rather than caught in review after.

### P2-15. Data-directory file permissions for the new sqlite db are unspecified

The Docker deployment already runs as a non-root `sentry` user with a
`/data` volume (`Dockerfile:9-10`), which is good practice to carry
forward — but nothing yet says what file mode the new platform sqlite db
should be created with, or whether the `/data` volume (shared with
`config.json`, `state.json`, logs) is an appropriate place for a file
that now also holds subscriber phone numbers/webhooks, versus a
separate, more tightly permissioned path. Worth a one-line decision
before the db file's first production write, not a code defect today.

---

## Summary for the orchestrator

**P0**
1. `add_subscription()` (`platformd/core.py:21-34`) defaults to
   `active=1` with no verification gate — subscribing a victim's channel
   address currently requires zero confirmation at the primitive level.
2. `governor.acquire()` (`platformd/governor.py:62-103`) has a
   read-then-write race with no transaction wrapper — under a
   multi-worker deploy this defeats the anti-burst politeness guarantee
   the entire architecture depends on to avoid getting blocked upstream.
3. `outbox.dispatch()` (`platformd/outbox.py:84-137`) has no row-claiming
   step — concurrent or overlapping dispatch passes can double-send an
   alert to a real subscriber, with no flock-equivalent guard anywhere
   in `platformd` (unlike `sentry.py`'s single-tenant `acquire_lock()`).

**P1**
4. No attempt-limiting/expiry/entropy design for verification codes —
   schema has zero supporting columns yet.
5. Unsubscribe-token design risk: sequential `subscriptions.id` +
   no token infra yet = enumeration/mass-unsubscribe and GET-prescan
   risk if not designed as high-entropy + POST-confirmed from the start.
6. `outbox._handle_failure` (`platformd/outbox.py:146-172`) logs and
   persists raw `str(exc)`, which a future channel adapter's exception
   can easily embed the subscriber's address in — PII-in-logs by
   default, not by new bug.
7. No deletion/right-to-forget path for `subscriptions.address` or
   `outbox.last_error`.
8. No schema migration/versioning story (`platformd/db.py:82-94`) with
   three more schema changes landing this wave.
9. No backup/restore procedure for the WAL-mode sqlite db.
10. Provider breaker (`platformd/outbox.py:27-34`) is in-process only —
    confirmed, concrete instance of the multi-worker-state gap already
    flagged for the rate limiter.
11. `db.connect()` (`platformd/db.py:82-94`) never sets
    `PRAGMA busy_timeout` — concurrent writers fail loudly instead of
    queuing, compounding P0-2/P0-3.
12. UX: argued that a bot-command subscribe flow (Telegram/Discord being
    the actual launch channels) structurally removes the "subscribe a
    victim" surface instead of merely mitigating it with a verification
    code; recommend web surface stays discovery-only, subscribe action
    moves to a bot command.

**P2**
13. No stated secrets posture yet for the new HTTP API's own credentials
    (admin auth, future Stripe key, session secret).
14. Pre-emptive rule needed: no catalog-derived endpoint should ever
    join through to `subscriptions.subscriber_id`/`address`.
15. Sqlite db file permissions/location for subscriber PII unspecified.

## Addendum 2026-08-04 — Telegram cannot deliver codes to a username (found live)

The first real subscribe attempt (bare handle `easy_score`) surfaced two
layered defects:

1. **Validation friction (fixed):** the Telegram pattern required the
   `@` prefix; bare handles now canonicalize to `@handle` server-side
   (`_normalize_address`, pinned by `AddressNormalizationTestCase`).
2. **Structural (open, pre-launch blocker for Telegram/Discord):** the
   Bot API cannot initiate a DM to a username — a bot may only message
   users who have already messaged it. "Type your handle, we DM a code"
   is therefore unimplementable on Telegram regardless of code quality.
   The tests pass because channels are injected fakes that accept any
   address string; the defect lives in platform semantics no mocked test
   can reach. The launch-correct flow is the bot handshake: subscribe
   returns a `t.me/<bot>?start=<one-time-token>` deep link, the user taps
   it, the bot receives `/start <token>` + the chat_id, and the
   subscription auto-verifies — the multi-tenant generalization of
   sentry.py's `--telegram-setup`. Discord needs the equivalent
   (server-invite or webhook-only). ntfy is the only channel where
   type-an-address code delivery genuinely works today.

Demo honesty (fixed same day): `--demo` now stamps `"demo": true` on the
catalog payload and the UI shows "codes are printed to the server
console" — a demo that silently looks broken to the first real user is a
demo defect, not a user error.

## Addendum 2026-08-04 — unsubscribe link tradeoff (accepted, documented)

`GET /unsubscribe?token=...` (client-surface hardening pass) renders a
confirm page; the token itself still rides the URL to get there. That
means it WILL appear in server access logs, browser history, and any
link-scanning/prefetching mail client or proxy that GETs the link
without a human clicking anything. This is an accepted tradeoff, not an
oversight:

- The token is **single-purpose**: it can only disable one subscription.
  There is nothing else to leverage even with the raw token in hand.
- The GET route itself is inert (a safe method by construction — see
  `api.py`'s `_serve_unsubscribe_page`); a prefetch/scan can *render* the
  page but cannot *submit* it, since that requires the page's own inline
  JS to fire the POST. A scanner that also executes JS and clicks is a
  meaningfully different (and much rarer) threat than a scanner that
  merely GETs a URL, and is the same class of risk every "click to
  unsubscribe" email link on the internet already carries.
- The token is **high-entropy** (`secrets.token_hex(16)`, minted by
  `core.verify()` on activation — 128 bits, unguessable by brute force).
- **Rotating it on use is unnecessary.** A disable-only credential that's
  already been used has nothing left worth protecting; there's no second
  use to prevent. (Contrast a login or payment token, where reuse after
  first use is exactly the attack — not the case here.)

Net: accept that the token shows up in logs/history by design, and treat
that as the cost of a one-click unsubscribe UX, not a defect to fix.

## Addendum 2026-08-04 (second) — red-team wave, and what it found

Two read-only auditors swept the client surfaces and the adapter/ops
story. Everything below was verified against code, not inferred, and the
live ones were fixed the same day.

**Fixed this wave:**

- **`GET /how` returned 404.** The public trust page existed as a file
  for a full wave with no route serving it. A page nobody can reach is
  not a page; pinned now by `TrustPageTestCase`.
- **Risk-tier enforcement was documentation, not code.** README claimed
  a YELLOW source could never carry a paid plan; an auditor verified a
  paid+yellow subscription succeeded. Now enforced in `core.subscribe`
  (yellow refuses paid, red refuses everything) with
  `RiskTierPolicyTests`.
- **`tier` was client-supplied.** `POST /api/subscribe {"tier":"paid"}`
  bought the instant-notify tier for free — the whole paywall. Tier is
  now forced server-side to `free` until a real entitlement lookup
  exists.
- **No migration story, no `busy_timeout`.** `CREATE TABLE IF NOT
  EXISTS` no-ops against an older table shape, so a db from an earlier
  wave would fail at some later query with "no such column". `connect()`
  now applies additive migrations under `PRAGMA user_version` and
  refuses a future-version db outright.
- **No channel actually delivered anything.** Every send path in the
  tree was an injected fake; the trust page described three working
  channels and zero worked. Real `DiscordWebhookChannel` and
  `NtfyChannel` now exist in `platformd/channels.py` (injected
  transport, no live calls in tests), with a `PermanentDeliveryError`
  that takes a deleted endpoint terminal — 24 hours of retries against a
  404'd Discord webhook is noise, not resilience — and disables the
  owning subscription.
- **Provider `retry_after` was ignored.** A 429 carrying an explicit
  hint is the provider naming when it will accept traffic again;
  guessing with our own doubling curve instead is how one rate-limit
  becomes a rolling one. The hint now wins, clamped to the backoff cap.

**Still open, structural (named so they stop being rediscovered):**

1. **No production runner wiring the watch loop.** `platformd/runner.py`
   dispatches the outbox, but nothing calls adapter `fetch` →
   `governor.acquire` → `parse` → `record_event` on a schedule. The
   politeness governor is still dead code outside tests — the choke
   point the whole legal posture depends on has never run in anger.
2. **`drops` has no `target_json`.** Nothing machine-readable ties a
   drop to what to poll (campground_id, venue+date scope), so even a
   finished runner would have nowhere to read its work from.
3. **Unsubscribe is a one-time secret.** The token is shown once in the
   verify response and never appears in an alert, so a subscriber who
   closes that tab cannot stop. Fix with the token in every alert
   message, which needs a configured public base URL.
4. **Telegram remains unimplementable as designed** — bot handshake
   still unbuilt; the channel must not be offered until it is.
5. **recreation.gov: no `STATE_GONE`, no site-type filter, per-date
   fetches of an identical month payload, and no declared timezone for
   "date."** All four only bite once the runner exists, which is exactly
   the shape that hid the Telegram defect.
