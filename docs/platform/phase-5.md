# Phase 5 — LLM-assisted drop onboarding, deliberately last

## Why last, argued

Every earlier phase exists to prove a discipline by hand before any of it
gets automated: the risk-tier rule (`adapters/base.py risk_tier`,
`docs/platform/README.md`) only means something if it's been enforced
correctly, by a human, across at least two real sources (Fandango,
recreation.gov) and one real rejection-shaped decision (the AMC vendor gate
in `phase-0.md`) before an LLM pipeline gets to apply the same judgment at
speed. An onboarding pipeline that scaffolds new source adapters
automatically is, by construction, the single highest-leverage place for
this project's one non-negotiable line to erode — this project has already
had "just automate the browser a little" proposed for AMC and held the
line by hand (`user-profile` memory: "no Queue-it token forging, no
unattended headless polling of AMC, no auto-checkout... keep holding it").
An eager, helpful pipeline that scaffolds a plausible-looking adapter for a
source that actually requires circumventing a bot-detection or purchase-
access control is exactly how that line erodes — not through a deliberate
decision to cross it, but through automation quietly doing the crossing on
someone's behalf before a human notices what the generated code actually
does. Phase 5 exists to automate the *first four stages* of onboarding
(the parts that are genuinely mechanical) while making the one stage that
isn't mechanical — the legal/circumvention judgment — structurally
impossible to route around, automate past, or reach with code before a
human has ruled.

## The pipeline

```
announcement + job-to-be-done
        │
        ▼
source match ──────────► existing adapter? → self-serve config, no code
        │ (no match)
        ▼
feasibility crawl (read-only: does a plausible endpoint exist,
                   what shape does it return, is auth required)
        │
        ▼
╔═══════════════════════════════════════════════════════════╗
║  HUMAN LEGAL GATE — the intake stop-rule                   ║
║  "circumvention required = not onboardable"                ║
║  Evaluated BEFORE any adapter code is scaffolded.           ║
║  Every rejection is recorded, with reason, permanently.     ║
╚═══════════════════════════════╤═══════════════════════════╝
                                 │ (approved)
                                 ▼
        scaffolded adapter (fetch/parse/health_check draft)
                                 │
                                 ▼
        human-reviewed diff ── changes requested? → loop back
                                 │ (approved)
                                 ▼
        shadow preflight (no fan-out: runs fetch/parse against
        real traffic, produces observations, delivers to NOBODY;
        projects budget against the shared source governor)
                                 │
                                 ▼
        publish, with alarms pre-wired
```

## Stage 1 — announcement + job-to-be-done

Input is a plain description of what someone wants watched — "tell me when
X shows up available at Y" — not a URL or an API spec. This stage's only
job is turning that into a structured job-to-be-done: what's the
subscribable axis (a date? a slot? a unit?), what states matter, what
counts as "worth an alert." No network activity happens here.

## Stage 2 — source match

Check whether an existing adapter already covers the requested source
(same domain, same API). If yes, **this is self-serve config, not a new
adapter** — a new `targets` row and the catalog entry that exposes it,
nothing more. This is the cheap, common case (someone wants a different
campground or a different theater on a source already onboarded) and it
should resolve in seconds with zero LLM-scaffolded code and zero legal
review, because the legal review already happened when that source was
first onboarded.

## Stage 3 — feasibility crawl

Only for a genuinely new source. Read-only reconnaissance: does a
plausible public or documented endpoint exist for this job-to-be-done,
what shape does its response take, does it require auth, does
`robots.txt` say anything. This produces the evidence the next stage
needs to actually rule on — it does not itself decide anything, and it
does not write any code.

## Stage 4 — the human legal gate (the load-bearing stage)

**Evaluated before any adapter code is scaffolded — not after, not
alongside, not as a "flag it for review while I keep building" step.**
The stop-rule is one sentence: **if satisfying this job-to-be-done requires
circumventing an access control (bot-detection tokens, auth bypass,
scraping behind a login the source didn't intend for this purpose,
anything shaped like `RED` tier in the risk-tier table), it is not
onboardable, full stop** — the same boundary already drawn for AMC's
public site (`ticket-sniper-domain` skill: "No Queue-it token forging, no
unattended headless polling... circumventing purchase-access controls is
BOTS-Act territory"). This gate is a **human** decision by design — not a
model classifying "is this circumvention" and self-approving, because the
whole failure mode being guarded against is an automated pipeline talking
itself into a plausible-sounding yes. **Every rejection is recorded** —
source, reason, date — permanently, in the same spirit as `phase-0.md`
recording the settled Fandango posture: a rejected source shouldn't get
re-proposed and re-litigated by a future run of the same pipeline that
already ruled on it once.

An approval here assigns the new source a risk tier (`GREEN`/`YELLOW`,
never `RED` — a `RED` ruling *is* the rejection) using the exact same
argument shape `phase-0.md` and `phase-2.md`'s RIDB nuance already
demonstrate: what's actually authorized, what's merely tolerated, and what
that implies for pricing and status-page disclosure.

## Stage 5 — scaffolded adapter, human-reviewed diff

Only after Stage 4 approves does any code get generated: a draft
`fetch`/`parse`/`health_check` implementation against the frozen contract
(`adapters/base.py`), following the same shape `adapters/recreation_gov.py`
already demonstrates by hand (state-mapping table in the docstring,
raise-don't-swallow on unrecognized shapes). This is a normal code review,
not a rubber stamp — a generated `parse()` that returns `[]` on an
unrecognized shape instead of raising is a real, plausible mistake for
this stage to produce, and it's exactly the kind of bug the frozen
contract's own docstring warns about (adapters/base.py:105–109). Nothing
in this stage is exempt from the review a hand-written adapter would get.

## Stage 6 — shadow preflight

Before any subscriber can subscribe to the new source, it runs for real —
real `fetch`/`parse` calls against real upstream traffic — with **no
fan-out**: observations are produced and logged, but zero outbox rows are
ever created and zero subscribers are notified, because there are none
yet to notify. This stage's other job is a budget projection: given the
new source's declared `min_spacing_seconds`/`daily_request_budget`,
project its request volume against the **shared** politeness governor
(`docs/platform/phase-3.md`) it will actually run inside — the same
projection `preflight.py` already does for a new box's Fandango traffic
(`docs/architecture-notes.md`, Decision 4), generalized to "does this
source's honest budget fit alongside every other source already running,"
not evaluated in isolation.

## Stage 7 — publish, with alarms pre-wired

Only once shadow preflight has run clean does the source become
subscribable. "Pre-wired" means literally: the six observability tags
(`docs/platform/README.md`) are already live from the first real
subscriber's first real poll — there is no bootstrapping period where a
brand-new source runs unmonitored while alarms get added after the fact.
A source that's good enough to publish is, by the time it's published,
already wired into every failure mode this platform knows how to detect.
