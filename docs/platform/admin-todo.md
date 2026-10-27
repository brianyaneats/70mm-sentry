# Production admin to-do

Owner-only tasks: accounts, money, legal, and keys. Code can't do these.
Work through them in order, because each section unblocks the next.

## 1. Business basics
- [ ] Pick the operator: you personally, or an LLC. An LLC keeps refunds and disputes off your personal liability, but it can come later.
- [ ] Set up a support inbox, e.g. `support@<domain>`. Stripe requires one and the legal pages cite it.
- [ ] Fill every `[bracketed]` field in `app/static/terms.html` and `privacy.html`: legal name, city/state, support email, hosting provider, backup storage provider.
- [ ] Decide on price and refund policy. The terms page currently says 14 days, refund on request.
- [ ] Read recreation.gov's current Terms of Use in a browser. We only read public availability and never book. Note the date you checked.
- [ ] Do a quick name check on "Dropwatch" (USPTO search plus domain availability) before you print it anywhere.

## 2. Domain
- [ ] Buy the domain from a registrar with DNSSEC and WHOIS privacy (Cloudflare Registrar or Porkbun).
- [ ] Point an `A` record (and `AAAA` if the VPS has IPv6) at the VPS. This happens after step 3.
- [ ] Set up email for the domain. Forwarding to Gmail is enough to start (Cloudflare Email Routing or ImprovMX).

## 3. VPS host
- [ ] Pick a provider. A 2 GB box is plenty, because sqlite plus one process is about 50 MB of RAM.
  - Hetzner CX22: about €4/mo, EU or US-East.
  - DigitalOcean or Vultr: about $6/mo, US regions.
- [ ] Install Ubuntu 24.04 LTS and sign in with an SSH key only.
- [ ] Harden the box:
  - Create a non-root sudo user.
  - Set `PermitRootLogin no` and `PasswordAuthentication no`.
  - `ufw allow 22,80,443` and nothing else.
  - Install `unattended-upgrades`.
- [ ] Install Docker Engine and the compose plugin, from Docker's apt repo rather than snap.
- [ ] Turn on the provider's weekly snapshots, about +20% of the box price.
- [ ] Create off-box backup storage: a private Backblaze B2 (or S3) bucket, an app key scoped to that one bucket, and a lifecycle rule (e.g. keep 30 days). Put the values in `BACKUP_S3_*` in `deploy/.env`.
- [ ] Add an uptime monitor: Healthchecks.io or UptimeRobot hitting `https://<domain>/api/status`, alerting your phone.
- [ ] Add a read-only GitHub deploy key for the box, so it can `git clone`/`pull` the private repo.
- [ ] First deploy: follow `deploy/README.md` "First deploy". In short:
  - Clone to `/opt/dropwatch`.
  - `cp deploy/.env.example deploy/.env` and `chmod 600` it.
  - Set `DROPWATCH_DOMAIN`, `ACME_EMAIL`, and `DROPWATCH_PUBLIC_URL`.
  - `sudo install -d -o 10001 -g 10001 deploy/backups`.
  - Run `deploy/release.sh`.
- [ ] Right after the first deploy, run `docker compose exec app python -m app.probe 232447` in `deploy/`. It sends one real request from the VPS. `ok=True` means recreation.gov answers this IP. `problems=REFUSED` means it blocks datacenter IPs; the fix is a different provider or region, not working around the block. Then watch the logs for `[REFUSED]` for a day.
- [ ] Do one real subscribe → verify with your own ntfy topic, on the live site.
- [ ] Enable the nightly backup timer (`deploy/README.md` "Backups"), run `docker compose run --rm backup` once, and confirm the `[BACKUP] uploaded` line and the object in the bucket.
- [x] Fly decided: `fly.toml` was removed, and the VPS is the only target.

## 4. Stripe
- [ ] Create the account and activate it. You enter the business details, bank account, and SSN/EIN; never paste them into a chat.
- [ ] Set the public business name, support email, and statement descriptor (e.g. `DROPWATCH`). These show on card statements and cut down on chargebacks.
- [ ] Do everything below in **test mode** first, then repeat it in live mode.
- [ ] Create a Product called "Dropwatch instant alerts" with a monthly recurring price.
- [ ] Create a Payment Link for that price:
  - Set the after-payment redirect to `https://<domain>/?paid=1`.
  - Turn on "collect email" so receipts and the portal work.
- [ ] Configure the Customer portal:
  - Allow cancel at period end and card updates.
  - Copy the portal **login link** into `DROPWATCH_PORTAL_URL`.
- [ ] Add a webhook endpoint:
  - URL: `https://<domain>/api/stripe/webhook`.
  - Events: `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`.
  - Copy its signing secret (`whsec_…`).
- [ ] Put the secret **only** in `deploy/.env` on the VPS (`chmod 600`, gitignored), never in the repo. The public values go in the same file: `DROPWATCH_PAYMENT_LINK`, `DROPWATCH_PORTAL_URL`, `DROPWATCH_PRICE_LABEL`, `DROPWATCH_PUBLIC_URL`. Set the link and the secret together; the app refuses to boot with only one of them. Apply with `docker compose up -d` in `deploy/`.
- [ ] Settings:
  - Turn on email receipts.
  - Keep Radar's default rules.
  - Decide on Stripe Tax. Some US states tax digital subscriptions, so check before revenue matters.
- [ ] Run a test-mode purchase end to end:
  - Card `4242 4242 4242 4242`.
  - Confirm `[BILLING] webhook applied ... outcome=granted` in the logs and that the alert arrives with no 60s delay.
  - Cancel in the portal and confirm `outcome=inactive x1`.
- [ ] Go live by switching all of the above to live mode with a real card of your own. Buy, check that alerts are instant, then refund yourself.

## 5. Launch checks
- [ ] Watch the logs for a day:
  - `[POLL] budget-bound` means watch demand exceeds the request budget, so alerts are slowing.
  - `[REFUSED]` or `[BREAKER]` means upstream is unhappy.
  - `[ZERO-DELIVERED]` means an alert reached nobody.
- [ ] Restore a backup onto a scratch box once (`deploy/README.md` "Restore"). An untested backup isn't a backup.
- [ ] Optional next channel: an email provider account (Postmark or Resend) with domain verification (SPF/DKIM). It's the biggest conversion win for campers, who won't install ntfy.
