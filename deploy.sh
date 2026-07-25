#!/bin/sh
# One-command redeploy on the box: pull, rebuild, restart, prove it's alive.
# Run from the repo checkout on the VPS.
set -eu

git pull
docker compose up -d --build

echo "waiting 45s for the first cycle..."
sleep 45

docker compose exec -T sentry python /app/sentry.py --status
docker compose logs -f --tail=50
