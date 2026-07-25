#!/bin/sh
# Replaces launchd inside the container: a fast tick for the sentry (which
# applies its own cadence gate and single-flight lock) and a slow tick for
# the watchdog. Mirrors the macOS plists so behaviour is identical.
set -eu

DATA="${SENTRY_DATA:-/data}"
TICK="${SENTRY_TICK_SECONDS:-10}"
WATCHDOG_TICK="${WATCHDOG_TICK_SECONDS:-300}"

if [ ! -f "$DATA/config.json" ]; then
    echo "FATAL: $DATA/config.json not found."
    echo "Mount it read-only, e.g.  -v \$PWD/config.json:/data/config.json:ro"
    echo "(secrets can also come from TELEGRAM_TOKEN / TELEGRAM_CHAT_ID etc.)"
    exit 1
fi

# forward SIGTERM so 'docker stop' is fast and clean rather than a 10s kill
trap 'kill 0' TERM INT

python /app/sentry.py --status || true
# deploys AND unexpected restarts announce themselves (private audience)
python /app/sentry.py --startup-push || true

while true; do
    python /app/watchdog.py >/dev/null 2>&1 || true
    sleep "$WATCHDOG_TICK"
done &

# stderr also lands in launchd.log so the watchdog's SENTRY DOWN alert can
# quote the actual traceback, same as launchd gave it on the Mac
while true; do
    python /app/sentry.py 2>> "$DATA/launchd.log" || true
    sleep "$TICK"
done &

wait
