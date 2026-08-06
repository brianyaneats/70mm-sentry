# Phase 4 — first public surface

This is the phase where the platform stops being one person's tool and
starts being something strangers subscribe to. Everything here sits on top
of Phase 3's event log / ledger / outbox and Phase 2's second adapter; if
either phase's gate hasn't actually passed, this phase has nothing real to
put a storefront in front of.

## Surface: catalog, subscribe, verification, cap

- **Catalog** — a list of watchable targets per source (campgrounds and
  sites for recreation.gov, the one Fandango venue for the legacy vertical
  — see below). Read-only, no auth required to browse; matches the
  scarcity-honesty rule (`docs/platform/README.md`) by stating plainly, on
  the page, that watching a popular target doesn't create more inventory
  of it.
- **Subscribe flow** — pick a target, pick a channel, and (Stripe-gated,
  below) a tier. This is the one place `subscriptions` rows
  (`docs/platform/phase-3.md`) get created; it's also the one place a form
  is submitted and personal contact info (a phone number, a Discord
  handle) is entered — ordinary product surface, but worth naming since
  it's the first time this project collects anything from someone who
  isn't the owner.
- **Channel verification** — before a subscription goes live, the channel
  address gets a one-time confirmation send (a "you're now watching X"
  message the subscriber has to have actually received). This closes the
  same gap W5 names for the single-tenant tool (`docs/architecture-notes.md`:
  "a notification channel can fail silently... a weekly low-priority
  self-test proves the whole channel path") but earlier — proving the
  channel works *before* the first real alert depends on it, not after.
- **Stripe, free-tier watch cap** — the free tier is capped by **number of
  simultaneous watches**, not by cadence (cadence tiering lives at
  dispatch, per Phase 3's notify-delay design, not here). This mirrors AMC
  Sniper's own free-tier shape (`docs/amcsniper-teardown.md`: "3 movies, no
  card") rather than inventing a new axis — a watch-count cap is legible to
  a subscriber in a way "your alerts arrive slower" alone isn't, and it
  gives the free tier a hard, cheap-to-enforce ceiling on how many
  `subscriptions` rows one free subscriber can hold, independent of the
  notify-delay tiering already doing separate work at the dispatch layer.

## Channels: Telegram + Discord first, SMS deferred

**Ship Telegram and Discord at launch.** Both already have zero-cost bot
APIs, no carrier-side compliance regime, and Telegram is the proven channel
from the single-tenant tool (`docs/architecture-notes.md`'s channel-vendor
verdict; `sentry.py`'s existing `send_telegram`/`_telegram_send_one`).
Discord adds a second, similarly zero-friction channel for a different
audience (server-based, not per-person chat) at comparable integration
cost.

**Defer SMS until a paid cohort justifies the compliance work.** SMS in
the US requires A2P 10DLC registration — carrier vetting, a registered
brand and campaign, and per-message fees that scale with volume — none of
which is a one-time integration cost the way a bot token is. Standing that
up before there's a single paying subscriber means carrying ongoing
carrier compliance overhead against zero revenue to justify it. The
deferral isn't "SMS is worse" — `docs/amcsniper-teardown.md` shows a
competitor charging specifically for SMS+5s cadence as their paid tier's
value prop, so the demand signal is real — it's "pay the compliance cost
once the free channels have proven there's a paying cohort to build it
for," rather than funding carrier registration on spec.

## Flagship vertical: campsites, paid; 70mm, free hook

**Campsites is the paid flagship**, on the strength of Phase 2's `GREEN`
tier (a real, if judgment-called, path to charging for it — see
`phase-2.md`'s RIDB nuance) and a market that already pays for camping-
availability tools (the third-party ecosystem the adapter's own docstring
cites as precedent). **The Fandango/70mm vertical stays the free hook** —
per `phase-0.md`'s settled `YELLOW` posture, it can never be a paid SKU,
so its role in Phase 4 is acquisition, not revenue: it's the vertical this
project already has real production history and a name (70mm Sentry) built
around, and it's free precisely because a `YELLOW` source's risk profile
(architecture-notes.md, W3; `phase-0.md`) makes charging for it dishonest
about how durable it is. A free hook with real production pedigree pulls
people toward the platform; the paid vertical is what they convert to once
they're in the door and want a second thing watched. Charging for the
`YELLOW` source instead would optimize short-term revenue against the
platform's own risk-tier rule — exactly the kind of exception
`phase-0.md`'s "settle, don't reconsider" argument exists to foreclose.

## Public status page: the visible reliability proxy

`README.md`'s thesis — alert reliability is the product — needs a public
face once there are subscribers who aren't the owner and can't just ask
in person whether something's wrong. The status page is that face, with
two hard rules:

- **Translated copy, never raw ops diagnostics.** A subscriber sees
  "Fandango watch: operating normally" or "degraded — retrying," not a
  breaker's open/closed state, a request-budget percentage, or a stack
  trace. The internal observability standard (`[PIPE]`/`[BLIND]`/
  `[REFUSED]`/`[PARSE]`/`[DELIVERY]`/`[ZERO-DELIVERED]`,
  `docs/platform/README.md`) exists for operators and tests to reason
  about failure precisely; none of those tags, or the internals they
  imply, belong on a page a non-technical subscriber reads. Translating
  precisely *because* the internal signal is precise is the point — a
  vague internal state would make for a vague, unhelpful public one too.
- **Scraped sources get honest fragility badges.** A `GREEN`-tier,
  contractually sanctioned source (AMC's vendor API, once approved) and a
  `YELLOW`-tier tolerated-gray source (Fandango) are not equally durable,
  and the status page says so — a visible badge distinguishing "sanctioned
  API" from "public endpoint, may go dark without notice" per source,
  directly downstream of the `risk_tier` already enforced in code
  (`adapters/base.py`). This is the public-facing enforcement of the
  scarcity-honesty rule's sibling principle: don't just avoid implying
  more inventory than exists, don't imply more durability than the risk
  tier actually earns either.

The status page is a proxy for reliability, not reliability itself — it
can only report what the observability standard actually measures. If a
failure mode exists that none of the six tags catch, the status page will
look green while something's actually broken, which is exactly the "a
quiet log looks identical to a healthy log" trap `docs/architecture-notes.md`
already names as the reason every silent failure mode needs its own alarm.
Phase 4 doesn't add new alarms; it puts a public, translated window onto
the alarms Phase 1–3 already built.
