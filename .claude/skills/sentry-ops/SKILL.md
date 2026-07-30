---
name: sentry-ops
description: Operate, debug, test, and safely modify the 70mm Sentry watcher (Fandango poller, launchd jobs, watchdog, notification channels, off-box deadman). Use for any work in this repo - checking status, investigating alerts or outages, changing alert copy or cadence, adding features, or redeploying the daemon.
---

# 70mm Sentry operations

A launchd daemon polls Fandango's NAPI for The Odyssey IMAX 70mm showtimes at
AMC Lincoln Square and pushes phone alerts. Everything below was learned the
hard way — trust it before re-deriving.

## Quick commands

```bash
cd ~/Developer/70mm-sentry
/opt/homebrew/bin/python3 sentry.py --status      # health snapshot (age of last cycle is THE number)
SENTRY_NOW=1 /opt/homebrew/bin/python3 sentry.py  # force one real cycle now
/opt/homebrew/bin/python3 sentry.py --digest-preview  # digest content, never pushes
/opt/homebrew/bin/python3 sentry.py --test-push   # rings all channels (label expectations first!)
/opt/homebrew/bin/python3 -m unittest discover -s tests   # all mocked, safe
tail -f sentry.log
launchctl kickstart -k gui/$(id -u)/com.brianyan.70mm-sentry   # restart
```

launchd jobs (all `RunAtLoad`, plists in `~/Library/LaunchAgents/`):
`com.brianyan.70mm-sentry` (every 10s, self-gates to config cadence),
`-watchdog` (300s), `-startup` (login). All three use a `/bin/sh -c` wrapper
that tries Homebrew → system → /usr/local pythons in turn — keep it that way;
an Xcode install once broke `/usr/bin/python3` and killed polling silently.

## Hard-won facts (do not re-litigate)

- **NAPI needs a venue `Referer` header** or it 403s ("Session expired").
  Detection key: `filmFormat[].filterName` contains `70MM` — never
  `filmFormatHeader` (always "Premium Format"). Availability vocabulary:
  `available|soldout|restricted|pastshowtime` — showtime-level on-sale flag,
  NOT a seat count. Seat maps live behind checkout; deliberately untouched.
- **robots.txt disallows /napi/*** — non-compliant at any polling rate;
  politeness = budget + jitter + breaker; enforcement is server-side 403/429.
- **AMC direct is Queue-it-protected**: never forge/replay queue tokens, no
  unattended headless polling of AMC, no auto-checkout. Sanctioned path is
  their vendor API (developers.amctheatres.com). Driving Brian's own Chrome
  attended is fine.
- **Residential IP is a design assumption.** Datacenter IPs likely get 403s
  (preflight.py exists to prove a new box before trusting it).
- **Invariants:** horizon/announced/degradedSent/budgetAlerted advance ONLY
  on a delivered push (failed push = retry next cycle) — and since
  2026-07-30 so do `seenAvailability` baselines for alert-worthy flips in
  `restock_check` (advancing on a failed push files the flip as old news
  and the alert never retries). One announcement per
  date via `state.announced`, whichever path wins — and `target_check` MUST
  run before the probe/sweep in `main()` (pinned by test) or the drop path
  claims target dates, corrupting `targetAlerted`. Unconfigured channel must
  raise, never fake success. `audience="all"` is only for
  drops/targets/restocks; ops noise stays private (gets `[instance]`
  suffix). Recovery alerts use priority `good` (audible) — `low` is
  silent-by-design. Budget exhaustion returns the `BUDGET` sentinel from
  `fetch_day` — it must NEVER trip the breaker (self-imposed cap ≠ Fandango
  failure). Title matching is `movie_match: "the odyssey"` +
  `movie_exclude: ["space odyssey"]` — a bare "odyssey" match false-alerts
  on 2001: A Space Odyssey revivals (real at Lincoln Square). All interval
  math goes through `elapsed_seconds()` (DST-safe, naive/aware tolerant) —
  never subtract `fromisoformat` values directly.
- **flock single-flight** (`.sentry.lock`) — launchd fires every 10s while
  sweeps run 60-90s; without the lock, races duplicated alerts and clobbered
  state. Don't remove it.
- Cadence windows evaluate in `schedule_tz` (America/New_York — theater
  time), not host time. The Mac's clock is Pacific; a 3h skew bug existed.
- **No push transport exists.** Verified 2026-07-27 by instrumenting
  WebSocket/EventSource/fetch on Fandango's theater page: no WS, no SSE, no
  SockJS/Socket.IO/Pusher/Ably, and no background polling. The endpoint
  returns an ETag but **ignores `If-None-Match`** (conditional GET still
  answers 200 with the full 15.5KB). Polling is the only option — don't
  re-investigate websockets.
- **Realtime windows** (`realtime_windows`, currently Tue 06:00–12:00 ET at
  6s) hand the cycle to `realtime_loop()`: 240-second slices of tight polling
  over one kept-alive TLS connection (21ms/poll warm vs 54ms cold, measured).
  Slices keep launchd supervision, the flock and the watchdog unchanged.
  `lastPollAt` updates every pass; state persists immediately on a drop;
  sleep is floored at 0.5s so no config typo can hammer the endpoint. The
  daily reconcile is deliberately deferred out of these windows — its ~90s
  sweep is the worst possible blind spot during a drop hour. Due restock
  checks are NOT deferred (since 2026-07-30): they ride inside the loop on
  the kept-alive session, because placeholder listings arm silently and the
  armed-date flip is precisely this window's drop.
- **Placeholder handling** (2026-07-30): listings that arrive already sold
  out announce silently and arm the sniper; `restock_check` owns the only
  ring those dates ever get. It fires on soldout/restricted→available flips
  AND on a brand-new buyable qualifying slot appearing on an armed,
  previously-observed date (AMC swapping the fake time for the real one) —
  `scan_dates` deliberately leaves exactly those keys unseeded. The
  monotonic `state.everAvailable` ledger picks the copy: never-seen-buyable
  → "went on sale for REAL", else "returned tickets". A date with no
  observed keys still baselines silently (freshly pinned watch date ≠
  news — the gap-walk trap).

## Alarms and what they mean

- **SENTRY DOWN** (watchdog): no completed cycle in 10 min; body carries real
  error lines from launchd.log. **SENTRY BACK UP** (`good`) clears it.
- **SENTRY BLIND**: HTTP 200s but 0 movies repeatedly — endpoint/shape drift.
- **SENTRY BLIND — parser**: reconcile found 0 hits in a window where the
  run is definitely playing — `odyssey_70mm()` stopped matching.
- **Off-box deadman** (GitHub Actions, hourly): sentry publishes a heartbeat
  to secret ntfy topic `config.heartbeat_topic` every ~5 min;
  `.github/workflows/deadman.yml` Telegrams if stale >90 min (re-alert
  suppressed 6h via a marker on the same topic). Covers Mac asleep/off —
  the one failure no local layer can report. Secrets: `HEARTBEAT_TOPIC`,
  `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` in repo Actions secrets. Gotcha: a
  freshly added schedule may not fire until the NEXT push touching the
  workflow file (observed 2026-07-25: two silent slots, fired right after a
  nudge commit) — after any deadman change, verify the first scheduled run
  actually happens; `workflow_dispatch` succeeding proves nothing about cron.
- Silent **daily digest** at 8am ET; its absence is itself a signal.

## Safe-change protocol

0. CI runs the suite on every push (`.github/workflows/tests.yml`, 3.9+3.13)
   — but run locally first anyway. Plist changes go through
   `launchd/*.plist.template` + `./launchd/install.sh` (idempotent), never
   by hand-editing `~/Library/LaunchAgents`.
1. Develop in a git worktree; run the full suite there (both
   `/opt/homebrew/bin/python3` and `python3` — launchd uses Homebrew's).
2. Deploy = brief `launchctl bootout` → `git merge --ff-only` → `bootstrap`
   (each 10s fire is a fresh process; never edit live files in place).
3. Verify: `SENTRY_NOW=1` cycle, `--status`, then watch 2-3 daemon cycles in
   `sentry.log`. Mocks don't catch deploy bugs; a real cycle does.
4. **Drills:** anything that can reach a real channel must be TEST-labeled
   and silent (`disable_notification`), and never leave an alarm un-cleared.
   Never test-fire against the friends broadcast channel.
4b. **Every writable path the suite can reach must be redirected in
   `SentryTest.setUp`** (currently `LOG_PATH` and `HISTORY_PATH`). `fetch_day`
   records history as a side effect, so any test mocking `urlopen` rather than
   `fetch_day` writes wherever the module constant points — this silently
   appended 16 fabricated rows to the production `history.jsonl` on
   2026-07-27. Two regression tests now assert the paths sit outside the repo
   and that a recording fetch leaves the real file untouched; when adding any
   new file the watcher writes, redirect it there too.
5. To simulate a drop: set a venue horizon back a week in state.json, run
   `SENTRY_NOW=1`, then clean `alerted`/`dropLog`/`announced` afterward.
6. Waiting on a daemon cycle: background task/Monitor — never `sleep && tail`.

## Config gotchas

`config.json` is untracked and holds live tokens — never commit it; the
Telegram bot token doubles as a credential in GitHub Actions secrets (rotate
both together). ntfy topic names are publish AND subscribe credentials
(spoofable — anyone with the name can push fake alerts). The friends channel
is `channels.telegram.extra_chat_ids`; per-friend delivery is best-effort.
