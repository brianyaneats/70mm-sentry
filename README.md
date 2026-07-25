# 70mm Sentry

Self-owned watcher that detects newly released **The Odyssey IMAX 70mm** showtimes
at AMC's two 70mm venues and pushes to your iPhone. No third-party trackers, no
Claude dependency — one Python file (stdlib only), a config, and a launchd timer.

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
- **Cadence tiers:** launchd fires every 20 s; the script itself decides
  whether it's due. `burst_windows` (e.g. `[{"days": ["Tue"], "start_hour":
  9, "end_hour": 12}]`) poll at `burst_interval_seconds` (20 s) — populate
  these from `dropLog` once real drops are observed; otherwise
  `base_interval_seconds`; overnight (23:00–07:00) runs at
  `overnight_interval_seconds` (default hourly). Burst windows beat the
  overnight tier. **All window hours are evaluated in `schedule_tz`** (the
  theater's zone, `America/New_York`), not the host's — AMC posts on Eastern
  afternoons and this Mac is on Pacific.
- **Politeness:** hard daily request budget (enforced in code), start jitter,
  circuit breaker on failures
  (3 strikes → open 30 min, exponential to 4 h cap, one degraded alert at 6).
- **W5 guard:** weekly low-priority "Sentry alive" push proves the whole channel.

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

```bash
cp config.example.json config.json      # fill in venues/targets, leave secrets blank
cat > .env <<'EOF'
TELEGRAM_TOKEN=<botfather-token>
TELEGRAM_CHAT_ID=<your-chat-id>
TELEGRAM_EXTRA_CHAT_IDS=<subscriber-channel-id>
HEALTHCHECKS_URL=https://hc-ping.com/<uuid>
TARGET_DATES=2026-08-21,2026-08-22,2026-08-26
EOF
chmod 600 .env
docker compose up -d --build
docker compose logs -f
```

Secrets come from the environment and **override** `config.json`, so nothing
sensitive has to sit on the VPS disk. `entrypoint.sh` replaces launchd: a
10 s tick for the sentry (which applies its own cadence gate and flock) and
a 300 s tick for the watchdog — identical behaviour to the macOS plists.
`docker compose restart` is the documented recovery, and the watchdog's
SENTRY DOWN alert prints that command instead of the `launchctl` one when it
detects it is containerised.

`TZ` and `SCHEDULE_TZ` are both set to `America/New_York` in the compose
file: cadence windows follow the theater's clock wherever the box lives.

**Keep `./data` across deploys.** Losing `state.json` re-announces every date
you have already been told about and resets the horizon to `seed_horizon`.

### The IP caveat

Fandango's `robots.txt` disallows `/napi/`, and this endpoint answers a
residential address far more readily than a datacenter one. A VPS is
therefore *more* likely to be refused, not less — the trade is uptime for
reachability. The breaker already backs off on failures, and the degraded
alert now names IP-level blocking explicitly when it sees 403/429/503 so the
diagnosis is immediate rather than a mystery outage. If that fires
repeatedly, the fix is moving back to a residential IP, not tuning the
watcher.

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
last-cycle age, cadence, budget, breaker, horizon, and targets on demand.

Not covered on-box: Mac asleep or powered off takes the watchdog down too.
Only an off-box dead-man's switch (`healthchecks_url`, pinged every cycle)
closes that.

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

97 unit tests, all network mocked: 70mm detection (incl. soldout/restricted
typing), drop handling (horizon advances only on delivered alerts; failed
push retries), sweep cutoff, budget rollover/exhaustion, breaker
open/half-open/recover + single degraded alert, notifier fan-out (one channel
failing doesn't lose the alert) incl. best-effort channel broadcast,
CallMeBot URL + Meta template payloads, the Referer header on every fetch,
cadence tiers + burst windows, target-date watch and evening slot filtering,
response telemetry + blind-streak alerting, and watchdog liveness
(staleness, dedup, re-nag, recovery, error capture). Live layer checks:
`--test-push` plus polling the ntfy topic JSON to confirm receipt.

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

## Operate

```bash
# run once now (skips jitter/overnight gate)
SENTRY_NOW=1 python3 ~/Developer/70mm-sentry/sentry.py

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
`self_test_every_days`, `ntfy_topic`, and `healthchecks_url` — set the last one
to a healthchecks.io ping URL to get dead-man alerts if the sentry itself dies
(strongly recommended; free tier is fine — the watcher pings it every cycle,
and silence beyond your grace period alerts you).

## Known limits (accepted trade-offs)

- **Mac asleep = no polling** (launchd catches up on wake). Chosen over a cloud
  VM because residential IP reputation beats datacenter for this (W3).
- **Fandango's lag behind AMC's own listings is still unmeasured** (W1) — on the
  next real drop, compare the alert timestamp against when AMC first showed the
  dates; if the gap is bad, the design doc has the fallback path.
- Detection latency: ~1 min daytime (base cadence), ≤60 min overnight.
  `dropLog` accumulates real drop timestamps; once two exist, add matching
  `burst_windows` to config for 20 s polling in those windows. Research note:
  AMC does not announce batch-drop timing and no community-verified schedule
  exists — the sourced signal is refunds clustering in the final hours before
  a showing, which the refund sniper's today/tomorrow burst clock covers.
