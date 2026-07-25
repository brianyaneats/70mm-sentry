# 70mm Sentry

Self-owned watcher that detects newly released **The Odyssey IMAX 70mm** showtimes
at the AMC venues in `config → venues` and pushes to your iPhone. No third-party
trackers, no Claude dependency — one Python file (stdlib only), a config, and a
timer: launchd on a Mac, or the Docker entrypoint on a VPS.

Design doc (weak points W1–W6 enumerated): https://claude.ai/code/artifact/e5799536-012f-494f-83bd-5f90044c32b5

## How it works

- **Source:** Fandango's NAPI (`fandango.com/napi/theaterMovieShowtimes/{id}?date=`).
  Verified: returns full JSON to plain HTTP **as long as a venue `Referer` header
  is sent** (without it: 403 "Session expired or invalid token"). AMC's own site
  403s all scripted requests and is deliberately not in the loop.
- **Detection:** a showtime counts as 70mm when `filmFormat[].filterName`
  contains `70MM` and `type != pastshowtime`. (`filmFormatHeader` is just
  "Premium Format" — don't use it.)
- **Open seats:** Fandango's per-showtime `type` field is the availability
  signal — observed vocabulary: `available`, `soldout`, `restricted`,
  `pastshowtime`. Drop alerts report it per drop ("32 showtimes — 30 with
  open seats, 2 sold out"). This is showtime-level (are seats purchasable),
  not a seat count; exact remaining-seat maps live behind
  `tickets.fandango.com`'s checkout session, which this tool deliberately
  never touches (checkout scraping is fraud-system territory).
- **Steady state:** 1 request per venue per run — probe the first date past the
  known horizon. Probe hit → bounded sweep (≤14 dates, 4s spacing, stops after
  3 empty dates) → urgent ntfy push → horizon advances **only after the push
  delivers**, so an alert lost to an ntfy outage retries next cycle.
- **Daily reconcile:** first run after 06:00 sweeps today + the next 21 days per
  venue to catch non-contiguous additions the probe can't see (design weak
  point W4). It also records which dates have sold-out showtimes into
  `state.autoRestockDates` — the refund-sniper watch list.
- **Target mode:** when `config → target_dates` is set (with
  `target_after_hour`), the sentry polls those not-yet-listed dates every
  `target_every_seconds` and rings the moment one goes live with a qualifying
  slot (at/after the cutoff hour, plus the pre-6am late-night block). Sold-out
  qualifying slots arm the refund sniper immediately, and auto-discovery +
  restock alerts are restricted to target dates and qualifying slots only.
- **Refund sniping:** watched dates (auto-discovered sellouts plus any pinned
  in `config → restock_watch_dates`) are re-polled on their own clock — every
  `restock_burst_seconds` (60) when the date is today/tomorrow, since most
  cancellations land in the final hours before a screening, else every
  `restock_every_seconds` (300). A soldout→available flip rings the phone.
- **Cadence tiers:** the timer (launchd or the container loop) fires every
  10 s; the script itself decides whether it's due. `burst_windows` (e.g.
  `[{"days": ["Tue"], "start_hour": 9, "end_hour": 12}]`) poll at
  `burst_interval_seconds` — populate these from `dropLog` once real drops
  are observed; otherwise `base_interval_seconds`; overnight (23:00–07:00)
  runs at `overnight_interval_seconds`. The shipped config sets all three
  tiers to 30 s (the run window is close enough that overnight drops
  matter); the code default for overnight is hourly. Burst windows beat the
  overnight tier. **All window hours are evaluated in `schedule_tz`** (the
  theater's zone, `America/New_York`), not the host's — AMC posts on Eastern
  afternoons and this Mac is on Pacific.
- **Politeness:** hard daily request budget (enforced in code), start jitter,
  circuit breaker on failures
  (3 strikes → open 30 min, exponential to 4 h cap, one degraded alert at 6).
- **W5 guard:** weekly low-priority "Sentry alive" push proves the whole channel.

## Who hears what

`push(audience=...)` splits the two audiences. **`private`** is the default,
so anything added later is quiet by mistake rather than loud by mistake:

| Audience | Reaches | Alerts |
|---|---|---|
| `private` | your own chat + ntfy | SENTRY DOWN / BACK UP / BLIND, degraded, startup check, weekly self-test |
| `all` | the above **plus** `extra_chat_ids` | a date going on sale, a drop, seats reopening |

Subscribers signed up to hear about tickets, not about launchd. Operational
messages carry hostnames, restart commands, and HTTP diagnostics — none of
that belongs in a channel full of friends, so the subscriber broadcast is
opt-in per alert and the watchdog and startup check never use it (pinned by
test).

Friend-facing copy is written for people who have never seen this repo:
`pretty_date()` renders `2026-08-21` as `Fri, Aug 21` and `pretty_time()`
renders `7:00p` as `7:00 PM`, each showtime gets its own tap-to-book line,
and every ticket alert carries the caveat that a listed showtime can still
have zero seats.

## One alert per date

Three paths can reach the same date (the target watch, the probe's instant
ping, the sweep's detail alert), so `state.announced` is the single ledger:
whichever path announces a date first claims it, and the others go quiet.
The horizon still advances past suppressed dates — otherwise the probe
re-hits the same date forever. Restock alerts dedupe per showtime
(`state.restockAlerted`) so a seat that flaps sold-out/open rings once.
Nothing is marked announced unless the push actually delivered, so a failed
alert retries next cycle. `instant_alert: false` collapses the two-stage
"detected now, details in ~1 min" ping into a single alert.

## Concurrency

launchd fires every 10 s but a probe-hit sweep runs 60–90 s. `flock` on
`.sentry.lock` keeps it single-flight, and each cycle persists `lastPollAt`
*before* the long work so a later fire sees a fresh timestamp instead of
starting a duplicate cycle and clobbering state at the moment of a drop.

## Running on a VPS (Docker)

The image is stdlib-only Python on `python:3.12-slim` — no `pip install`, no
requirements file, no supply chain. Code lives read-only in the image; every
mutable file (`config.json`, `state.json`, logs, lock) lives on a mounted
volume, so the image itself is secret-free and safe to push to a registry.

Secrets come from the environment and **override** `config.json`, so nothing
sensitive has to sit on the VPS disk. `entrypoint.sh` replaces launchd: a
10 s tick for the sentry (which applies its own cadence gate and flock) and
a 300 s tick for the watchdog — identical behaviour to the macOS plists. On
start it also sends a private "Sentry started" push, so deploys and
unexpected restarts announce themselves. `docker compose restart` is the
documented recovery, and the watchdog's SENTRY DOWN alert prints that
command instead of the `launchctl` one when it detects it is containerised.

`TZ` and `SCHEDULE_TZ` are both set to `America/New_York` in the compose
file: cadence windows follow the theater's clock wherever the box lives.
`INSTANCE_NAME` (e.g. `vps-nyc3`) is stamped on every **ops** alert title —
`Sentry degraded [vps-nyc3]` — so during a migration a Mac alert and a VPS
alert are distinguishable at a glance. Ticket alerts stay clean.

**Keep `./data` across deploys.** Losing `state.json` re-announces every date
you have already been told about and resets the horizon to `seed_horizon`.
The daily reconcile also writes a snapshot to `./data/snapshots/` (keeps 7)
as a restore point.

### The two hard gates

Do not cut over from a working setup until both are green:

1. **Preflight passes from the box's IP.** See "The IP caveat" below — a
   datacenter IP may simply be refused. `preflight.py` fetches several real
   dates inside the known run window through the sentry's own request path
   and also projects a full day's request spend against
   `daily_request_budget` (budget exhaustion trips the breaker and produces
   a degraded alert indistinguishable from an IP block). Run it through the
   image so it uses the container's exact egress path:

   ```bash
   docker compose build
   docker compose run --rm --entrypoint python sentry /app/preflight.py
   ```

   Non-zero exit → do not cut over. Re-run it after any change to
   `TARGET_DATES` or the cadence knobs — the budget projection is only as
   current as the config it read.

2. **`healthchecks_url` set and verified in both directions.** With no other
   machine watching, this is the only thing that can tell you the box died.
   Create a check (period 5 m, grace 15 m, email on), set `HEALTHCHECKS_URL`
   in `.env`, confirm the dashboard goes green — then `docker compose stop`,
   wait 20 minutes, and confirm it goes **red and emails you** before
   starting again. A check that has only ever been green proves nothing.

### Provision (Ubuntu 24.04, ~20 min)

Any $6/mo 1 GB droplet/instance is generous — the container is capped at
128 MB. No inbound ports are needed at all: the sentry only makes outbound
requests, nothing listens.

1. Create with **SSH key only** (no password auth); non-root sudo user;
   `ufw default deny incoming && ufw allow OpenSSH && ufw enable`.
2. `unattended-upgrades` for security patches.
3. Docker Engine + compose plugin (official convenience script).
4. Read-only **deploy key**: `ssh-keygen -t ed25519` on the box, add the
   public half under the repo's Deploy keys on GitHub.
5. Clone and configure:

```bash
git clone git@github.com:<you>/70mm-sentry.git && cd 70mm-sentry
cp config.example.json config.json      # required BEFORE anything imports sentry
cat > .env <<'EOF'
TELEGRAM_TOKEN=<botfather-token>
TELEGRAM_CHAT_ID=<your-chat-id>
TELEGRAM_EXTRA_CHAT_IDS=<subscriber-channel-id>
HEALTHCHECKS_URL=https://hc-ping.com/<uuid>
TARGET_DATES=2026-08-21,2026-08-22,2026-08-26
INSTANCE_NAME=vps-nyc3
EOF
chmod 600 .env
```

Leave `channels.ntfy.enabled: false` and don't set `NTFY_TOPIC` on a shared
box: `send_ntfy` ignores the audience split, so an enabled topic would
publish every private ops alert to anyone holding the topic string — and
ntfy topics grant publish rights to holders too.

6. **Carry your state over.** Copy `state.json` from the old machine into
   `./data/` so `budget`, `announced`, `horizon` and `targetAlerted` survive
   the move instead of resetting to `seed_horizon` and re-announcing every
   known date. Then `chown -R 10001 ./data` — the container runs as uid
   10001 and the bind mount masks the image's ownership.
7. `./deploy.sh` — pulls, rebuilds, restarts, prints `--status`, tails logs.

### Cutting over from a Mac

Only after both gates are green: confirm cycles in `docker compose logs` and
a green healthchecks dashboard, then stop the launchd jobs
(`launchctl bootout gui/$(id -u)/com.brianyan.70mm-sentry` plus the
`-watchdog` and `-startup` jobs). **Leave the plists in place** — rollback is
one `launchctl bootstrap` per job. Finish with
`docker compose exec sentry python /app/sentry.py --test-push` to prove the
alert path end to end from its new home.

Morning-after review, one paste:

```bash
ssh <box> 'cd ~/70mm-sentry && \
  docker compose exec -T sentry python /app/sentry.py --status && \
  docker inspect -f "restarts={{.RestartCount}} health={{.State.Health.Status}}" 70mm-sentry && \
  grep -c "HTTP 200" data/sentry.log && \
  grep -iE "403|429|503|degraded|BLIND|breaker OPEN" data/sentry.log | tail -20 && \
  tail -40 data/watchdog.log'
```

What good looks like: HTTP 200 counts climbing and no 403/429/503 lines (the
IP held), zero restarts and health=healthy (it stayed up), cycle timestamps
~30 s apart (the cadence is honest), `--status` budget tracking preflight's
projection (no exhaustion masquerading as a block), and no alerts overnight —
or better, a real one.

### The IP caveat

Fandango's `robots.txt` disallows `/napi/`, and this endpoint answers a
residential address far more readily than a datacenter one. A VPS is
therefore *more* likely to be refused, not less — the trade is uptime for
reachability. `preflight.py` is the deploy-time gate; the breaker backs off
on failures, and the degraded alert names IP-level blocking explicitly when
it sees 403/429/503 so the diagnosis is immediate rather than a mystery
outage. A sustained block also stops the healthchecks pings (heartbeats only
fire on completed cycles), so the dead-man's switch goes red on its own. If
that fires repeatedly, the fix is moving back to a residential IP, not
tuning the watcher.

## Observability

Three distinct silent-failure modes, three separate alarms — a quiet log looks
identical to a healthy one, so each is detected explicitly:

- **Dead** (process never runs): `watchdog.py` on its own launchd job every
  5 min reads `state.json`'s `lastPollAt`; stale past
  `watchdog_stale_seconds` (600) → urgent **SENTRY DOWN** push carrying the
  error lines it scraped from `launchd.log`. Its plist picks the first
  working interpreter from a list, so an interpreter break (e.g. an Xcode
  install invalidating `/usr/bin/python3`, which happened 2026-07-24) kills
  the sentry but not its alarm. Alerts once per outage, re-nags every
  `watchdog_renotify_hours`, sends a quiet note on recovery.
- **Blind** (requests succeed, payload is empty): every fetch records
  `state.lastFetch` = HTTP status, byte count, movie count, 70mm hit count,
  and the per-cycle log line carries them (`[HTTP 200 15545B 1 movies]`).
  `blind_streak_alert` (20) consecutive zero-movie responses → urgent
  **SENTRY BLIND** push.
- **Mis-parsing** (payload fine, matching broken): the daily reconcile knows
  the run is playing inside its own window, so finding 0 IMAX 70mm showtimes
  across all 21 days means `odyssey_70mm()` stopped matching a changed feed
  shape → urgent **SENTRY BLIND — parser** push.

`capture_last_response` writes each raw payload to `last_response.json` for
diffing when the shape changes. `python3 sentry.py --status` prints
last-cycle age, cadence, budget, breaker, horizon, targets, and instance on
demand.

`preflight.py` is the deploy-time complement: it answers "will this IP be
served at all?" and "does the configured cadence fit the daily budget?"
before you trust a new box, with a non-zero exit on any refusal, blind
payload, or over-budget projection. It reuses the sentry's own
`fetch_day`/`poll_interval`, pushes nothing, and writes nothing.

`watchdog.py --check` is a pure liveness probe (exit 0/1, no pushes, no
state writes) — it backs the container `HEALTHCHECK`, leaving the
entrypoint's watchdog loop as the single writer of alert state.

Not covered on-box: the machine asleep or powered off takes the watchdog
down too. Two off-box dead-man's switches close that:

- **GitHub Actions deadman** (`.github/workflows/deadman.yml`, active): the
  sentry publishes a heartbeat to a secret ntfy topic (`heartbeat_topic`,
  every `heartbeat_every_seconds`); an hourly Actions cron polls the topic
  and Telegrams you if the newest heartbeat is >90 min old. Re-alerts are
  suppressed for 6 h via a marker message on the same topic. No third-party
  account needed — repo secrets `HEARTBEAT_TOPIC`, `TELEGRAM_TOKEN`,
  `TELEGRAM_CHAT_ID`. Caveats: GitHub cron can lag minutes, and scheduled
  workflows pause after 60 days without repo activity.
- **healthchecks.io** (`healthchecks_url`, optional alternative): pinged
  every cycle if set; tighter grace periods, but requires an account.

A **daily digest** (`daily_digest_hour`, theater time; silent priority)
summarizes each morning: target dates still waited on, how far the listed
schedule runs, refund watches, yesterday's request count, breaker state.
Its *absence* is itself a liveness signal you'll notice with your coffee.
Preview without pushing: `python3 sentry.py --digest-preview`.

## Notification layer

Alerts fan out to every enabled channel in `config.json → channels`; an alert
counts as delivered (and the horizon advances) if ANY channel got through.

### Telegram (recommended primary) — one-time setup, ~2 minutes

First-party Bot API, free, the de-facto standard alert channel for personal
servers. Drops ring the phone; housekeeping notes arrive silently
(`disable_notification` maps to the sentry's priority levels).

1. In Telegram, message **@BotFather**: `/newbot` → pick a name → copy the
   **token** it gives you into `config.json → channels.telegram.token`.
2. Open your new bot's chat and send it anything (e.g. `hi`).
3. Run: `python3 ~/Developer/70mm-sentry/sentry.py --telegram-setup`
   — it discovers your chat_id, enables the channel, saves config, and sends
   a confirmation message.

### WhatsApp (optional) — one-time setup, ~3 minutes

Provider `callmebot` (free, personal-alerts-only relay — no account, but a
third party sees the message text; fine for movie alerts):

1. Get the current CallMeBot WhatsApp bot number from
   https://www.callmebot.com/blog/free-api-whatsapp-messages/ and add it to
   your contacts.
2. From your WhatsApp, send it: `I allow callmebot to send me messages`
3. It replies with your **apikey**. Put it in `config.json`:
   `channels.whatsapp.enabled: true`, `phone: "+1XXXXXXXXXX"` (your number,
   country code included), `apikey: "..."`.
4. Verify: `python3 ~/Developer/70mm-sentry/sentry.py --test-push`

Provider `meta` (first-party WhatsApp Business Cloud API — no relay, but you
create a Meta developer app, and template messages are billed per message
since July 2025): set `provider: "meta"` and fill `channels.whatsapp.meta`
with `token`, `phone_number_id`, `to`, and optionally an approved 2-variable
`template` name (without a template, messages deliver only inside a 24h
session window — not good enough for surprise drops, so use a template).

### ntfy (fallback, already live)

Keep it enabled — a second channel is the W5 guard. To see these alerts too:
ntfy app → subscribe to `<your-secret-topic> (see config.json)` on ntfy.sh.
To go WhatsApp-only, set `channels.ntfy.enabled: false`.

## Tests

```bash
cd ~/Developer/70mm-sentry && python3 -m unittest discover -s tests -v
```

123 unit tests, all network mocked: 70mm detection (incl. soldout/restricted
typing), drop handling (horizon advances only on delivered alerts; failed
push retries), sweep cutoff, budget rollover/exhaustion, breaker
open/half-open/recover + degraded alert dedup-with-retry, notifier fan-out
(one channel failing doesn't lose the alert) incl. best-effort channel
broadcast, CallMeBot URL + Meta template payloads, the Referer header on
every fetch, cadence tiers + burst windows, target-date watch and evening
slot filtering, response telemetry + blind-streak alerting, instance-name
stamping (ops titles only, never ticket alerts), preflight verdicts
(refusal, blind payload, zero hits, budget projection), watchdog liveness
(staleness, dedup, re-nag, recovery, error capture), and the read-only
`--check` probe. Live layer checks: `--test-push` plus polling the ntfy
topic JSON to confirm receipt.

A fresh clone needs `cp config.example.json config.json` first —
`sentry.py` reads it at import time.

## Channel vendor verdict (checked 2026-07-24)

- **Telegram Bot API** — recommended primary. First-party, free, no relay,
  generous rate limits, per-message silent/ring control. Standard alert
  channel in Uptime Kuma/PRTG/Checkly for a reason.
- **ntfy.sh** — keep as fallback (W5 second channel). Free, instant, topic
  name is the only secret.
- **CallMeBot** — legit (6+ year domain, clean trust scans) but hobbyist:
  no SLA, no status page, shared relay sees message text. Acceptable as an
  extra channel, not as the only one.
- **iMessage (AppleScript)** — investigated and rejected: Messages.app
  refuses to send to your own account, and manual self-messages don't banner
  on the iPhone anyway. The only workaround is a second Apple ID signed into
  Messages on the Mac, plus TCC automation grants inside launchd — fragile
  and Apple-unsupported. Skip.
- **Meta WhatsApp Cloud API direct** — first-party WhatsApp, ~$0.015 per US
  utility template message, needs dev app + approved template + permanent
  system-user token. Wired in (`provider: "meta"`) if you ever want it.
- **Twilio (WhatsApp or SMS)** — Meta's fees plus Twilio's margin plus
  business verification (or A2P 10DLC registration for SMS); sandbox
  unsuitable for sporadic alerts. Dominated at this volume — skip.
- **Email→SMS carrier gateways** — dying tech, silently unreliable; skip.

## launchd jobs (macOS)

Three agents in `~/Library/LaunchAgents`, all with `RunAtLoad`, so they come
back on their own at every login:

| Label | Cadence | Job |
|---|---|---|
| `com.brianyan.70mm-sentry` | every 10 s | the watcher (self-gates to the configured interval) |
| `com.brianyan.70mm-sentry-watchdog` | every 300 s | liveness alarm |
| `com.brianyan.70mm-sentry-startup` | once per login | proves the other two came back |

`startup_check.py` is the login-time proof. launchd already relaunches the
jobs — this is what *tells you it worked*, since a boot-time failure (broken
interpreter, unreadable config, network not up yet) would otherwise sit
unnoticed until the watchdog's staleness threshold expires ~10 min later. It
re-bootstraps any job that failed to load, forces one real poll to prove the
whole path end to end, and reports: silent "Sentry armed at startup" when
healthy, urgent **SENTRY DID NOT COME BACK** when not. The live poll is
retried 5× at 15 s intervals because Wi-Fi is usually not up yet at login,
and an alert that cried wolf every boot would train you to ignore it.

The plists are not versioned here — they carry absolute paths. Each is a
`ProgramArguments` shell wrapper that picks the first working interpreter
(`/opt/homebrew/bin/python3`, then `/usr/bin/python3`, …) rather than
hardcoding one, so an interpreter break cannot take down the watcher and its
alarms together.

## Operate

```bash
# run once now (skips jitter/overnight gate)
SENTRY_NOW=1 python3 ~/Developer/70mm-sentry/sentry.py

# confirm everything is running and armed (also repairs missing jobs)
python3 ~/Developer/70mm-sentry/startup_check.py

# health snapshot
python3 ~/Developer/70mm-sentry/sentry.py --status

# watch it work
tail -f ~/Developer/70mm-sentry/sentry.log

# pause / resume the schedule
launchctl bootout   gui/$(id -u)/com.brianyan.70mm-sentry
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.brianyan.70mm-sentry.plist

# uninstall completely
launchctl bootout gui/$(id -u)/com.brianyan.70mm-sentry
rm ~/Library/LaunchAgents/com.brianyan.70mm-sentry.plist
rm -rf ~/Developer/70mm-sentry
```

State lives in `state.json` (horizons, alerted dates, drop log, breaker, budget).
To simulate a drop for testing: set a venue's `horizon` back a week and run with
`SENTRY_NOW=1` — real Fandango data past that date will alert. Afterward clear
`alerted`/`dropLog` for that venue since it wasn't a real drop.

## Knobs (config.json)

`sweep_days`, `daily_request_budget`, `quiet_*_hour`, `reconcile_days`,
`self_test_every_days`, `log_max_bytes` (rotates `sentry.log` and
`launchd.log`), `instance_name` (suffix on ops-alert titles),
`daily_digest_hour` (silent morning digest, theater time; omit to disable),
`heartbeat_topic` + `heartbeat_every_seconds` (feeds the GitHub Actions
deadman — generate a long random topic, it is a credential), and
`healthchecks_url` (optional second dead-man).

## Known limits (accepted trade-offs)

- **Mac asleep = no polling** (launchd catches up on wake). Residential IP
  reputation beats datacenter for this endpoint (W3), so the Docker/VPS path
  trades reachability for uptime — `preflight.py` is the gate that decides
  whether a given box gets to make that trade at all.
- **Fandango's lag behind AMC's own listings is still unmeasured** (W1) — on the
  next real drop, compare the alert timestamp against when AMC first showed the
  dates; if the gap is bad, the design doc has the fallback path.
- Detection latency: ~1 min daytime (base cadence), ≤60 min overnight.
  `dropLog` accumulates real drop timestamps; once two exist, add matching
  `burst_windows` to config for 20 s polling in those windows. Research note:
  AMC does not announce batch-drop timing and no community-verified schedule
  exists — the sourced signal is refunds clustering in the final hours before
  a showing, which the refund sniper's today/tomorrow burst clock covers.
