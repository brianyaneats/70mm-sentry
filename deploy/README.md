# deploy: Dropwatch on one VPS

Two containers on one box. Caddy holds `:80`/`:443`, gets TLS from Let's
Encrypt, and proxies to the app on the private compose network. The app
container publishes no ports. It runs the API, poller, and dispatch runner
in one process against `/data/dropwatch.sqlite3` on the `data` volume. Run
exactly one app container, ever: sqlite means one writer.

| File | What |
|---|---|
| `compose.yml` | `app`, `caddy`, and the one-shot `backup` service (profile `ops`) |
| `Caddyfile` | TLS + reverse proxy. Sets `X-Real-IP` to the real peer |
| `Dockerfile` | app image, built from the repo root, stdlib only |
| `.env.example` | every setting. Copy to `.env` (gitignored, `chmod 600`) |
| `release.sh` | deploy (backup, build `<commit>`, switch), `rollback TAG`, `tags` |
| `backup.py` | online sqlite backup, retention, B2/S3 upload, verify, restore |
| `systemd/` | nightly backup timer |

Owner accounts, keys, and money: `docs/platform/admin-todo.md`.

## First deploy (bare Ubuntu 24.04)

1. Harden the box (admin-todo §3): SSH keys only, a sudo user, `ufw allow 22,80,443`, `unattended-upgrades`.
2. Install Docker Engine and the compose plugin from Docker's apt repo (docs.docker.com/engine/install/ubuntu), not snap. Add your user to the `docker` group.
3. Point DNS `A` (and `AAAA` if you use IPv6, see below) for the domain at the box. Wait until `dig +short <domain>` shows it. Otherwise Let's Encrypt fails and rate-limits you.
4. Get the code and fill in settings:
   ```sh
   # private repo: add a read-only deploy key for this box on GitHub first
   sudo git clone git@github.com:brianyaneats/70mm-sentry.git /opt/dropwatch
   sudo chown -R $USER /opt/dropwatch && cd /opt/dropwatch/deploy
   cp .env.example .env && chmod 600 .env && nano .env     # DROPWATCH_DOMAIN, ACME_EMAIL, DROPWATCH_PUBLIC_URL at least
   sudo install -d -o 10001 -g 10001 backups               # the app's uid writes backups here
   ```
5. Start it:
   ```sh
   ./release.sh            # build, start, wait for healthy
   docker compose ps       # app (healthy), caddy (up)
   curl -fsS https://<domain>/api/status
   ```
   If the app exits right away, `docker compose logs app` shows `invalid configuration:` with one line per problem.
6. Check that this IP can read recreation.gov. This sends one request and doesn't touch the db:
   ```sh
   docker compose exec app python -m app.probe 232447     # Upper Pines; expect ok=True sites=...
   ```
   `problems=REFUSED` means recreation.gov blocks this provider's IPs. Move to another provider or region. Don't work around the block.
7. Turn on nightly backups (below) and an uptime monitor on `https://<domain>/api/status`.

## Updates and rollback

```sh
cd /opt/dropwatch/deploy
./release.sh                   # git pull, backup, build dropwatch:<commit>, switch, wait healthy
./release.sh tags              # images on the box
./release.sh rollback 4fec975  # switch back; pinned in .env so reboots keep it
```

Schema migrations only go forward. If a release changed the schema, an
older image refuses the newer db ("upgrade the engine"). In that case,
roll back and restore the pre-deploy backup that `release.sh` took.
Changing only `.env` doesn't need a rebuild: `docker compose up -d`.

Shutdown: compose sends SIGTERM. The app stops its threads and server and
logs `[RUNNER] serve stopped clean=True`. The grace period is 30s.

## Logs

```sh
docker compose logs -f app                       # everything
docker compose logs --since 24h app | grep -E '\[(REFUSED|BREAKER|ZERO-DELIVERED)\]'
docker compose logs caddy                        # TLS/ACME problems
```

Logs rotate at 10 MB x 5 per container. No line contains an address,
topic, token, or secret.

| Tag | Meaning | Action |
|---|---|---|
| `[POLL]` | fetch timings and detections (`detected ... events=N`). `budget-bound` means watch demand exceeds the 5,000/day budget, so intervals are stretched | Occasional `budget-bound` is fine. Constant means alerts are slowing |
| `[PIPE]` | network failure on a fetch or send (reset, DNS, timeout) | Ignore a few. A steady stream means the box's network or DNS is broken |
| `[REFUSED]` | recreation.gov answered 403/429/5xx | Sustained means you are blocked or too fast. Don't raise the rate |
| `[BLIND]` | HTTP 200 with an empty or unexpected payload | Upstream changed or is degraded. Run `app.probe` |
| `[PARSE]` | payload fetched but the adapter couldn't read it | Upstream format changed. Needs a code fix |
| `[BUDGET]` | governor denied a fetch: daily budget spent | Resets at UTC midnight |
| `[BREAKER]` | governor or delivery breaker open after repeated failures | Self-heals. Look at what preceded it |
| `[DELIVERY]` | an alert or verify-code send failed. `gone=1`: destination deleted, subscription disabled. `cancelled=1`: user unsubscribed | Many on one channel means a provider outage |
| `[ZERO-DELIVERED]` | an opening reached nobody within 5 min | Page-worthy: users are missing alerts |
| `[RATELIMIT]` | a client IP hit the POST limit (10/min) | Normal for abuse. Check the IP isn't Caddy's own (see IPv6) |
| `[RUNNER]` | startup, `tick complete` heartbeat, shutdown | No ticks means the process is wedged |
| `[BILLING]` | Stripe webhook `applied ... outcome=granted/inactive xN`, or `signature rejected` | Rejections without a cause: secret mismatch or probing |
| `[BACKUP]` | backup `snapshot written`, `uploaded`, `FAILED` | `FAILED` needs a look the same day |

## Backups

`backup` runs sqlite's online backup API (same as the sqlite3 shell's
`.backup`), safe while the app writes. It integrity-checks the copy,
gzips it to `deploy/backups/dropwatch-<UTC>.sqlite3.gz`, deletes local
copies older than `BACKUP_KEEP_DAYS`, and uploads when all `BACKUP_S3_*`
values are set.

```sh
sudo cp systemd/dropwatch-backup.* /etc/systemd/system/   # assumes /opt/dropwatch
sudo systemctl daemon-reload && sudo systemctl enable --now dropwatch-backup.timer
systemctl list-timers dropwatch-backup.timer
docker compose run --rm backup                          # run one now
```

Backblaze B2: create a private bucket and an application key limited to
it, then fill `BACKUP_S3_*` in `.env`. The endpoint and region are on the
bucket page, e.g. `https://s3.us-west-004.backblazeb2.com` and
`us-west-004`. Add a lifecycle rule to the bucket (e.g. keep 30 days),
because the tool only prunes locally. Any S3-compatible store works.

## Restore

```sh
cd /opt/dropwatch/deploy
ls backups/                                              # or download one from B2 into backups/
docker compose run --rm backup python -m deploy.backup verify /backups/<file>
docker compose stop app                                  # restore needs the app stopped
docker compose run --rm backup python -m deploy.backup restore /backups/<file>
docker compose start app
```

Restore integrity-checks first and leaves the live db untouched if the
backup is bad. The current db and its `-wal`/`-shm` files are moved to
`*.pre-restore-<UTC>` on the volume, not deleted. Restore onto a scratch
box once (admin-todo §5). CI covers this path in `deploy/tests/test_backup.py`.

## Security notes

- Only Caddy is reachable. `X-Real-IP` is set by Caddy, never appended, so a client can't choose its rate-limit bucket by sending `X-Real-IP` or `X-Forwarded-For`. Don't add `ports:` to `app`.
- Docker-published ports bypass `ufw`. That's fine for 80/443, which are public anyway. Don't publish anything else.
- IPv6: if the domain has an `AAAA` record but Docker's IPv6 isn't enabled, Docker's userland proxy forwards v6 clients from one internal address, so they all share one rate-limit bucket. Either skip `AAAA`, or enable IPv6 in `/etc/docker/daemon.json` (`"ipv6": true, "fixed-cidr-v6": ...`).
- Secrets live only in `deploy/.env` (`chmod 600`): `STRIPE_WEBHOOK_SECRET` and `BACKUP_S3_SECRET_ACCESS_KEY`.
