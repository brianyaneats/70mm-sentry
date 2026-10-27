#!/bin/sh
# Deploy or roll back Dropwatch on the VPS. Run from anywhere:
#   deploy/release.sh              pull main, back up, build <commit>, switch
#   deploy/release.sh rollback TAG switch back to an image built earlier
#   deploy/release.sh tags         list images you can roll back to
# The running tag is pinned in deploy/.env (DROPWATCH_IMAGE_TAG), so a
# reboot or a plain `docker compose up -d` keeps what you chose.
set -eu
cd "$(dirname "$0")"
[ -f .env ] || { echo "deploy/.env missing: cp .env.example .env" >&2; exit 1; }

pin() {
    if grep -q '^DROPWATCH_IMAGE_TAG=' .env; then
        sed -i "s/^DROPWATCH_IMAGE_TAG=.*/DROPWATCH_IMAGE_TAG=$1/" .env
    else
        echo "DROPWATCH_IMAGE_TAG=$1" >> .env
    fi
}

wait_healthy() {
    for _ in $(seq 1 30); do
        s=$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q app)" 2>/dev/null || true)
        [ "$s" = healthy ] && { echo "app healthy"; return 0; }
        sleep 2
    done
    echo "app NOT healthy; check: docker compose logs --tail 50 app" >&2
    return 1
}

case "${1:-deploy}" in
deploy)
    git pull --ff-only
    tag=$(git rev-parse --short HEAD)
    # schema migrations are forward-only: keep a copy from before this one
    docker compose run --rm -T backup || echo "WARNING: pre-deploy backup failed" >&2
    DROPWATCH_IMAGE_TAG=$tag docker compose build app
    pin "$tag"
    docker compose up -d
    wait_healthy
    echo "deployed $tag"
    ;;
rollback)
    tag=${2:?usage: release.sh rollback TAG}
    docker image inspect "dropwatch:$tag" >/dev/null
    pin "$tag"
    docker compose up -d --no-build
    wait_healthy
    echo "rolled back to $tag"
    ;;
tags)
    docker image ls dropwatch --format '{{.Tag}}\t{{.CreatedSince}}'
    ;;
*)
    sed -n '2,7p' "$0"; exit 2
    ;;
esac
