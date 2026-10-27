# Production admin to-do

Owner-only tasks: accounts, money, legal, and keys. Code can't do these.
Work through them in order, because each section unblocks the next.

## 1. Business basics
- [ ] Pick the operator: you personally, or an LLC. An LLC keeps refunds and disputes off your personal liability, but it can come later.
- [ ] Set up a support inbox, e.g. `support@<domain>`. Stripe requires one and the legal pages cite it.
- [ ] Fill every `[bracketed]` field in `platformd/static/terms.html` and `privacy.html`: legal name, city/state, support email.
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
- [ ] Create off-box backup storage: a Backblaze B2 or S3 bucket, plus an app key scoped to that one bucket. The deploy session ships a nightly `sqlite3 .backup` job that uploads to it.
- [ ] Add an uptime monitor: Healthchecks.io or UptimeRobot hitting `https://<domain>/api/status`, alerting your phone.
- [ ] After the first deploy, watch the logs for `[REFUSED]`. Some sites block datacenter IPs. If recreation.gov does, the fix is a different provider or region, not working around the block.
- [ ] Decide on Fly: once the VPS is live, delete `fly.toml` or keep it as a fallback.

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
- [ ] Put the secret **only** in the VPS env file (`chmod 600`), never in the repo. The public values go in the same file: `DROPWATCH_PAYMENT_LINK`, `DROPWATCH_PORTAL_URL`, `DROPWATCH_PRICE_LABEL`, `DROPWATCH_PUBLIC_URL`.
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
- [ ] Restore a backup onto a scratch box once. An untested backup isn't a backup.
- [ ] Optional next channel: an email provider account (Postmark or Resend) with domain verification (SPF/DKIM). It's the biggest conversion win for campers, who won't install ntfy.
