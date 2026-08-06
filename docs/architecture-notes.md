# 70mm Sentry — architecture rationale

This is not a tour of what the code does — the README already covers that,
feature by feature, with real numbers. This is the argument for why it's
built this way: the five decisions that shape everything else, what each
one costs, and what I rejected instead. Everything below is checked against
`sentry.py` as it stands, with file references, not against what I intended
to write.

The thesis those five decisions serve: **alert reliability is the product.**
A watcher that misses one drop, silently, is worth strictly less than no
watcher at all — it's the difference between not knowing and *thinking you'd
know*. So the architecture spends its complexity budget on failure modes,
not on the happy path, which is the easy 20% of this file.

---

## Decision 1 — one file, stdlib only

`sentry.py` is 1,673 lines. The only `import`s are `datetime`, `fcntl`,
`http.client`, `json`, `os`, `random`, `re`, `socket`, `sys`, `time`,
`urllib.*`, `zoneinfo` — all standard library. No `requirements.txt`, no
virtualenv, no `pip install` anywhere in the deploy path (the Dockerfile
says so explicitly: "stdlib only — no pip install, no requirements.txt, no
supply chain").

**Rejected: `requests` for the HTTP layer.** It would delete real
boilerplate — `RealtimeSession` (sentry.py:303) hand-rolls a kept-alive
`http.client.HTTPSConnection` with its own retry-on-dead-socket logic
instead of getting a connection pool for free, and every `urllib.request`
call site builds its own header dict and decodes its own JSON. That's a
real cost, paid on purpose: this thing runs unattended, for months, on a
machine that also holds unrelated personal files, polling an endpoint whose
own robots.txt disallows it. Every third-party package is a maintainer I
don't get to vet and an update I have to trust blind. Zero dependencies
means the entire trusted surface is one file I wrote and can read start to
finish in one sitting.

**Rejected: splitting into a package** (`fetcher.py`, `notifier.py`,
`state.py`, ...). At this size a single file is still one `grep` away from
any definition, and the internal section markers (`# ---------- state
----------` at sentry.py:132, then `budget & breaker`, `http`, `detection`,
`main`) give the same navigability a directory tree would, without import
plumbing between modules that all exist to serve one script anyway. Module
boundaries pay for themselves when more than one person has to find their
way around by directory structure. This has exactly one maintainer.

**The cost, honestly:** 1,673 lines in one file is a real reading tax —
there's no import graph telling you what depends on what, and grep-driven
navigation doesn't scale forever. It's already at the size where I notice
it. If this file doubles, the module split stops being over-engineering and
starts being the right call; I'm not there yet, and 280 tests
(`tests/test_sentry.py` is 2,509 lines on its own — bigger than the file it
tests) are what let me still trust a change without reading the whole thing
end to end.

---

## Decision 2 — polling, because there is no push transport, verified

The obvious complaint about a poller is "why not just get pushed to." I
checked. `RealtimeSession`'s docstring (sentry.py:303–311) states the
finding plainly: Fandango's NAPI exposes no websocket, no SSE, and — the
part that actually needed testing, not just reading their docs — **ignores
`If-None-Match`**. Verified 2026-07-27: send a conditional GET with the
ETag the server itself returned, and it answers `200` with the full body
anyway, not `304`. That rules out the one cheap trick (conditional GETs
that let a well-behaved server tell you "nothing changed" in a few hundred
bytes instead of the full payload). There is no signal to subscribe to.
Polling isn't the fallback design — it's the only one the source permits.

Given that constraint, the design question becomes "how stream-like can
polling be made," and the answer is `realtime_loop()` (sentry.py:1269): a
240-second window of 6-second polling over one kept-alive TLS connection
during the one reported drop-hour block (Tuesdays, 06:00–12:00 ET), instead
of paying a fresh handshake every poll. Measured: 21ms per poll warm vs
54ms cold. At the normal 30-second cadence that's noise. Inside a 6-second
loop it's roughly a third of the poll budget — the one place in this
codebase where a millisecond figure is load-bearing rather than trivia.

**Cost:** the realtime window is a second, more complex code path
(`RealtimeSession` plus the loop-and-exit logic) that only exercises for
six hours a week, which means it's also the code path most likely to rot
unnoticed if the drop-hour hypothesis turns out wrong. `dropLog` exists
specifically to eventually confirm or kill that hypothesis with real
timestamps instead of the single reported data point it currently runs on.

---

## Decision 3 — a dumb timer plus a lock, not a daemon

launchd fires `sentry.py` every 10 seconds (`StartInterval 10` in the
plist); the script itself decides whether it's actually due, via
`poll_interval()` (sentry.py:1080). This buys crash recovery for free — a
short-lived process that dies has nothing to recover, launchd just spawns
the next one — no supervision loop, no SIGTERM handling, no daemon that can
leak memory over a month of uptime. The cost is concurrency: a probe-hit
sweep runs 60–90 seconds, so 6–9 timer fires land while one cycle is still
running. Two mechanisms close that gap. `acquire_lock()` (sentry.py:83)
takes a non-blocking `flock(LOCK_EX)` on `.sentry.lock`; a fire that can't
get it just exits, and because the kernel — not the lockfile's mere
existence — is what tracks the hold, a process that dies mid-sweep (crash,
`SIGKILL`, a machine sleeping mid-cycle) releases it automatically. The
naive alternative, a PID file, goes stale on exactly that crash and wedges
the system until a human deletes it. Second, `lastPollAt` is written to
`state.json` *before* the long work starts, not after — so a fire that
loses the flock race still sees a fresh timestamp on its due-check instead
of deciding a duplicate cycle is due and clobbering state at the worst
possible moment, mid-drop.

**Rejected: a long-running daemon that loops and sleeps internally.** It
would remove the per-fire interpreter-start cost (irrelevant at 30-second
cadence — tens of milliseconds) and let it poll faster than any external
timer without a hack. In exchange it reintroduces every problem the
short-lived model gets for free: a supervision story for when it hangs,
graceful-shutdown handling, and in-memory state that either gets rebuilt as
a checkpointing system anyway or silently drifts from `state.json`. The
short-lived model *forces* the durability discipline a reliable daemon
needs regardless — every cycle round-trips its state through disk, so
nothing is ever lost to a crash, at the cost of one JSON read/parse/write
per cycle. For a tool whose whole job is "never silently lose progress,"
forced durability is the side of that trade worth paying for.

The synthesis is `realtime_loop()` (see Decision 2): daemon-like only for
the 240 seconds it can afford to lose, then it exits on purpose so
launchd's 10-second restart stays the supervisor even during the one window
that behaves differently. Full mechanics — what launchd and flock actually
are, the HTTP/1.0-vs-keep-alive framing, line-by-line on the concurrency
hazard — are in Part 2 below; this section is the "why," that's the "how."

---

## Decision 4 — adaptive cadence, a circuit breaker, and a budget: this endpoint said no

Fandango's `robots.txt` disallows `/napi/`. This code polls it anyway,
which means politeness isn't a nicety here, it's the thing standing between
"tolerated" and "blocked" — and a datacenter IP gets refused far more
readily than a residential one for exactly this reason (the VPS path trades
reachability for uptime and `preflight.py` is the gate that decides whether
a given box gets to make that trade at all). Three mechanisms, layered,
enforce restraint at three different timescales:

- **`pace_request()`** (sentry.py:367) holds every request to Fandango's
  own published rate guidance — 1 call/second, the only number they've ever
  published (their partner API's documented limit; the public site
  publishes none). It's the single choke point every code path shares, so
  target-date and restock checks, which iterate several dates with no
  spacing of their own, can't produce a burst — and a burst, not average
  rate, is the shape bot detection actually keys on.
- **`budget_gate()`** (sentry.py:389) enforces a hard daily request ceiling
  (`daily_request_budget`, 25,000 in the shipped config) — a self-imposed
  cap on request *volume*, independent of whether Fandango is complaining.
  `preflight.py` projects a full day's spend against this number before a
  new box is trusted, because budget exhaustion trips the same breaker a
  real block would and produces an alert that looks identical from the
  outside.
- **The circuit breaker** (`breaker_fail`, sentry.py:253) responds to
  *failure*, not volume: 3 strikes opens it for 60 seconds
  (`breaker_base_backoff_seconds`, 60 in the shipped config), doubling on
  every further failure while still open, capped at 4 hours. That curve
  isn't arbitrary, and it isn't the original one either — it's a fix.
  On 2026-07-29 the breaker's first-trip minimum was a flat 30 minutes,
  three read timeouts tripped it minutes before a target date listed, and
  the watchdog correctly reported the sentry blind for the entire half
  hour over what turned out to be a network blip, not a block (commit
  `c39ae45`). The fix made the first trip short — a hiccup now costs a
  minute, not half an hour — while a sustained real block still climbs to
  hours within a few more consecutive failures. `tests/test_sentry.py`'s
  `TestBreakerBackoff` pins this: `test_first_trip_is_short` asserts the
  first open stays under 90 seconds, and a regression back to the old
  30-minute floor would fail it immediately.

**The cost:** these three mechanisms overlap in what they're guarding
against — a sustained block trips the breaker, exhausts the budget more
slowly than it otherwise would, and eventually stops the heartbeat that
feeds the off-box dead-man (Decision 5) — which is deliberate defense in
depth, but it's also three separate pieces of state (`budget`, `breaker`,
the pacer's module-global timestamp) to reason about and keep synchronized,
where a single "requests remaining" counter would be simpler to hold in
your head. And politeness has a direct, permanent latency cost: the
1-second-per-request floor `pace_request()` enforces
(`min_request_spacing_seconds`), and the 30-second base cadence, both mean
this will always be seconds-to-a-minute slower to detect a drop than a tool
willing to hammer the endpoint. That's the trade, made on purpose, for
staying inside the one number Fandango has ever published about what
they'll tolerate.

---

## Decision 5 — an off-box dead-man switch, because nothing on the box can report the box being gone

Three on-box layers already watch each other: `watchdog.py` catches the
process dying, per-fetch telemetry catches empty payloads, and the daily
reconcile catches the parser silently breaking (all three detailed in
README → Observability). Every one of them shares a blind spot: they run
*on* the machine, so a sleeping laptop, a dead battery, or a pulled plug
takes the watcher and every alarm about the watcher down in the same
stroke. A watchdog can't report on a machine that isn't running the
watchdog.

`.github/workflows/deadman.yml` closes that specific gap and no other: an
hourly GitHub Actions cron polls a secret ntfy topic that the sentry
heartbeats to every `heartbeat_every_seconds`, and Telegrams the owner if
the newest heartbeat is more than 90 minutes stale. It's off-box by
construction — GitHub's infrastructure, not this Mac — so it's the one
alarm that survives the failure mode every on-box layer is structurally
blind to.

**Rejected: a second personal server as the watcher.** More reliable, and
overkill — a redundant *watcher* solves a different problem (missing a
drop) than a redundant *alarm* (not knowing the watcher died). The dead-man
only needs to run a `curl` and a conditional once an hour; a full second
deployment is a second attack surface and a second set of secrets to rotate
for a problem that a $0 cron job already covers.

**Cost, and it's a real one:** this workflow runs on infrastructure I don't
control. The `.yml`'s own comments name the accepted caveats — GitHub's
cron can lag minutes under load, and scheduled workflows pause after 60
days with no repository activity, which means the safety net has an expiry
date tied to how often this repo gets touched, not to whether the watcher
is still running. `healthchecks_url` exists as a second, optional dead-man
with tighter grace periods for exactly this reason — belt and braces, at
the cost of a third-party account the GitHub path deliberately avoids
needing.

---

## Known weak points (W1–W6)

These are accepted, not hidden. Each is a real limitation of the current
design, and each is referenced by tag elsewhere in the code and the README
so a future change can find and address the right one.

- **W1 — Fandango's lag behind AMC's own listings is unmeasured.** The
  detection loop's latency is known (~1 min daytime, README → Known
  limits); how much further behind Fandango's feed is from AMC actually
  publishing a date is not. No drop has happened yet to measure it against.
- **W2 — a sleeping or powered-off Mac polls nothing.** launchd catches up
  on wake, but there's a real dead window while the lid is closed. This is
  the accepted cost of running on a personal machine instead of a
  always-on box (see W3).
- **W3 — a datacenter IP may simply be refused.** Fandango's endpoint
  answers a residential address far more readily than one from a cloud
  provider; `preflight.py` is the gate that decides, per box, whether the
  reachability-for-uptime trade of moving to a VPS is worth making at all.
- **W4 — the steady-state probe is structurally blind to non-contiguous
  additions.** It only ever checks the first date past the known horizon,
  so a date that appears *behind* the horizon (a gap-fill, not an
  extension) is invisible to it. The daily reconcile
  (`reconcile_days`, 21) exists specifically to close this gap once a day,
  not continuously.
- **W5 — a notification channel can fail silently.** A push that never
  arrives looks, from the code's perspective, identical to a push that was
  never needed. Mitigated two ways: a second channel (ntfy) stays enabled
  as a fallback even when Telegram is primary, and a weekly low-priority
  "Sentry alive" self-test proves the whole channel path end to end on a
  schedule, rather than waiting for a real alert to be the first test.
- **W6 — overnight detection latency is up to 60 minutes.** The overnight
  cadence tier trades detection speed for request volume between 23:00 and
  07:00 theater time, on the assumption that a drop at 3am is rare. That
  assumption is unverified — `dropLog` is what would confirm or kill it.

---

## The analytics side-car

`record_history()` (sentry.py:470) appends every observed availability
transition to `history.jsonl`; `report.py` turns that log into a
self-contained HTML replay page — no server, safe to open as a static file
— answering the question only time can: how often does a sold-out showtime
reopen, at what hour, and how long does a returned seat survive before
someone else takes it.

The one design choice worth stating: a transition only charts as "when it
happened" if the gap since the last read was under 15 minutes
(`PRECISE_WITHIN = 900`, report.py:32). How tight that gap is depends on
which path is watching a given date — a restock-watched date is re-read
every 60–300 seconds, a `study`-sampled date (`study_due_dates()`,
sentry.py:535 — up to `study_days` currently-listed dates, purely to learn
refund behavior, never alerted on) every `study_every_seconds` (600), a
realtime-window date every 6. A date only the daily reconcile reaches
carries up to 24 hours of uncertainty about exactly when it flipped.
Charting those as if the reconcile's read time *was* the event time would
plot the watcher's own polling schedule, not the box office's — a chart
that looks confident and measures the wrong thing. Refusing to fake
precision it doesn't have costs the report some data points; the
alternative costs it correctness.

---

## Part 2 — Process model deep-dive: launchd, flock, and short-lived vs long-running

### What launchd is

macOS's init system and service manager — the first process the kernel
starts (PID 1) and the ancestor of every process on the machine. Apple's
rough equivalent of `systemd` on Linux, with `cron`'s job folded in. Jobs
are declared in plist files (per-user "agents" live in
`~/Library/LaunchAgents`); launchd owns scheduling, restarting, and log
plumbing.

The watcher's plist (`launchd/com.brianyan.70mm-sentry.plist.template`)
uses three keys:

- `StartInterval 10` — fire every 10 seconds.
- `RunAtLoad` — fire once at load (i.e., at every login), so the jobs
  come back on their own after a reboot.
- `StandardOutPath` / `StandardErrorPath` — stdout/stderr → `launchd.log`.

Useful launchd properties for this design:

- **Missed fires coalesce.** Mac asleep 8 hours → on wake it runs once,
  not 2,880 times.
- **Login persistence is free.** No @reboot cron hacks, no systemd enable.
- **Interval jobs are cheap.** launchd is built for "spawn a short process
  on a schedule"; conversely, a `KeepAlive` daemon that crash-loops gets
  throttled.

One deliberate oddity: the plist doesn't exec python directly. It runs a
`/bin/sh` wrapper that tries a list of interpreters
(`/opt/homebrew/bin/python3`, then `/usr/bin/python3`, …) and execs the
first that works. Reason: an Xcode install invalidated `/usr/bin/python3`
on 2026-07-24; a hardcoded path would have killed the watcher *and its
watchdog* in the same stroke. The interpreter fallback keeps the failure
domains separate.

### What flock is

A kernel file-locking syscall. `acquire_lock()` (sentry.py:83) opens
`.sentry.lock` and requests `LOCK_EX | LOCK_NB` — exclusive and
non-blocking. If another process holds the lock, the call fails instantly
and that cycle simply exits; the next fire, 10 seconds later, tries again.

Two properties make flock the right tool:

1. **It's advisory.** It doesn't protect the file's contents — the lock
   file is just a rendezvous point; its content is irrelevant. Only
   processes that ask about the lock respect it, which is fine here
   because every contender is a copy of the same script.
2. **The kernel owns it.** When the holding process dies — *for any
   reason, including SIGKILL or a crash mid-sweep* — the kernel releases
   the lock automatically. This is the whole argument for flock over the
   naive alternatives (a PID file, or "does the lockfile exist?"): those
   go stale after a crash and wedge the system until a human deletes the
   file. The docstring on `acquire_lock` states exactly this.

Python footnote: the file handle is stashed in a module global
(`_LOCK_FH`) because if the handle were garbage-collected, the lock would
silently drop while the process was still running.

### The tradeoff: timer-spawned short-lived process vs long-running daemon

The HTTP analogy is exactly right: this is connection-per-request
(HTTP/1.0) vs keep-alive (HTTP/1.1), applied to processes.

**Tradeoff 1 — setup/teardown overhead.** Real. Each fire pays
interpreter start + imports + config parse + state read (tens of
milliseconds — irrelevant at a 30-second cadence). The overhead that
actually mattered was the network's, not the interpreter's: a cold TLS
handshake makes a poll cost 54ms vs 21ms on a warm connection. At 30s
polling, noise. At the 6-second cadence inside a Tuesday drop window,
cold-start overhead is a third of the latency budget — and during a drop,
latency is the product.

**Tradeoff 2 — no in-process state.** Also real, but taken as a feature.
Because the process dies every cycle, *all* state must round-trip through
`state.json` — every cycle becomes a transaction against durable state.
Crash at any point → the next fire reloads and continues; nothing is
lost. A long-running daemon's in-memory state (a) dies with the process
unless you build checkpointing anyway, and (b) drifts from what's on
disk. The short-lived model *forces* the persistence discipline a
reliable daemon would need regardless. Cost: one JSON
read/parse/write per cycle — kilobytes, trivial. For a watcher whose
product is "never silently lose progress," forced durability is the right
side of the trade.

**The rest of the ledger.**

Short-lived buys:

- **Crash recovery with zero code.** No supervision logic, no
  retry-wrapping the main loop, no graceful-shutdown (SIGTERM) handling,
  no memory leaks accumulating over weeks of uptime.
- **Config re-read every cycle.** No SIGHUP / hot-reload machinery.
- **Failure domain of one cycle.** A crashing bug costs one poll, not the
  watcher's future.
- **The OS is the scheduler.** Sleep/wake, login persistence, and log
  redirection are launchd's problem.

Short-lived costs:

- **Per-fire overhead** (interpreter + cold connections) — solved where
  it matters by the hybrid, below.
- **Scheduling floor** — can't poll faster than the timer fires without
  looping inside the process (again: the hybrid).
- **Concurrency hazard** — launchd fires every 10s but a probe-hit sweep
  runs 60–90s, so fires overlap. Two mechanisms handle it: the flock
  (single-flight), and persisting `lastPollAt` *before* the long work so
  a later fire's due-check sees a fresh timestamp instead of deciding a
  duplicate cycle is due — which would otherwise clobber state at the
  exact moment of a drop, the worst possible time.

### The hybrid: upgrade to long-running only when it pays

`realtime_loop()` (sentry.py:1269) is the synthesis. Inside a drop window
(Tue 06:00–12:00 ET), the watcher stops being a cron job: it holds a
240-second slice of tight 6-second polling over a single kept-alive TLS
connection (`RealtimeSession`, sentry.py:303) — then **exits on
purpose** so launchd restarts it. Due refund-sniper checks ride inside
the loop on the same warm connection.

Why cap at 240s instead of looping forever? Because the moment the
process becomes long-running, every daemon problem returns: a hang or
crash inside the window would need supervision to detect. Capping the run
means launchd's 10-second restart remains the supervisor; the flock and
watchdog semantics stay identical to the rest of the week. The design
never holds a process longer than it can afford to lose.

In HTTP terms: connection-per-request by default; keep-alive precisely
when the request rate justifies holding the connection; and the
connection lease is short enough that a broken peer can't wedge you.

### Questions this raises (good talk material)

- When would you flip the default — daemon-first with checkpointing?
  (Answer: when per-event latency always matters, e.g. a websocket
  consumer. Here the source is poll-only, so the timer model fits.)
- Why flock and not `state.json` itself as the lock? (Separation: the
  lock is a rendezvous, state is data; locking the data file couples
  crash-release semantics to file rewrites.)
- Why write `lastPollAt` before the work rather than after? (It's a
  lease/claim, not a completion record — the delivered-push ledger is
  the completion record.)
- What does Docker's `entrypoint.sh` do about all this? (Replicates the
  launchd contract exactly: a 10s tick loop spawning the same short-lived
  process, so the process model is identical on both platforms.)

---

## One-liners for a talk

- "The timer is dumb so the script can be smart."
- "The failure mode is a duplicate alert, never a silent miss."
- "A quiet log looks identical to a healthy log — so every way of being
  silently broken gets its own alarm."
- "More test lines than code lines, because the product is trust."
