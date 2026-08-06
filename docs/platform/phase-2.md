# Phase 2 — second adapter, chosen to hurt: recreation.gov campsites

## Why campsites, specifically

The point of a second adapter is not "prove the pattern works twice" — one
data point never proves a generalization. The point is to pick the source
most likely to break the kernel's assumptions, on purpose, while the cost
of being wrong is still cheap (one adapter, fully mocked, no subscribers
yet). Fandango's shape is a **showtime feed**: a small, flat list of
discrete time-slots per venue per day, each with one of four states, no
real date-range concept beyond "which single date did you ask for."
recreation.gov's shape is an **availability calendar**: one call returns an
entire month, per campsite, as a date-keyed map — a fundamentally different
axis (a calendar grid, not a slot list), a different unit-of-watch
granularity (site, not showtime), and a payload an order of magnitude
larger per request. If the kernel built in Phase 1 quietly assumed
"Fandango's shape is *the* shape," this is where that assumption surfaces —
cheaply, in a mocked test, instead of expensively, after Phase 3 has built
multi-tenant plumbing on top of a kernel that only actually supports one
data shape.

## What exists already

`adapters/recreation_gov.py` is scaffolded in this tree (see its own
docstring for the full state-mapping table). Summary, for this doc's
purposes:

- **Fetch:** one call per `(campground_id, month)` to
  `recreation.gov/api/camps/availability/campground/{id}/month`, returning
  every site's full-month calendar in one payload — the "one poll per
  target" that the shared governor (`docs/platform/README.md`) assumes
  scales to N subscribers watching the same campground.
- **Parse:** walks `raw["campsites"][site_id]["availabilities"][day_key]`
  and normalizes recreation.gov's own vocabulary (`"Available"`,
  `"Reserved"`, `"Not Available"`, `"Not Reservable"`, `"Closed"`, `"NYR"`,
  and an explicit catch-all for anything unrecognized) onto the four
  contract states — `Available → STATE_AVAILABLE`, `Reserved →
  STATE_SOLDOUT`, everything else → `STATE_RESTRICTED` with the raw string
  preserved in `meta["raw_state"]` rather than discarded. A site simply
  absent from a given day's calendar is *not* treated as an error (a site
  can have a shorter active season than the month requested) — it yields
  no observation for that (site, date), which is a materially different
  "nothing to report" than a `[BLIND]`/`[PARSE]` failure, and the adapter
  is careful to keep those distinct.
- **Health/parse failures raise, per contract:** an unrecognized top-level
  shape (`"campsites"` key missing or not a mapping) raises `ValueError`
  rather than returning `[]` — matching `adapters/base.py`'s explicit
  requirement that a changed feed shape must not silently pass as "nothing
  available" (see `docs/platform/README.md`, `[PARSE]`/`[BLIND]`).
- **`risk_tier = "green"`**, argued in the module's own docstring, not
  asserted for free — see the RIDB-vs-availability-endpoint nuance, below.

**Not yet built:** tests. Phase 2's gate (below) is not yet exercised; this
doc describes the plan those tests need to prove, against the adapter as it
currently stands.

## Build and test fully mocked

No live call to recreation.gov happens as part of building or testing this
adapter — every test constructs a fake month-calendar payload by hand (or
from a captured real response, sanitized and frozen as a fixture) and feeds
it through `parse()`/`health_check()` directly, exactly matching how
`tests/test_sentry.py` mocks `urlopen` today rather than hitting Fandango.
The only real network call recreation.gov ever sees from this project is
the owner-run live preflight, below — deliberately not part of the test
suite, and never automated.

## The gate: kernel needed zero Fandango-shaped special cases

Falsifiable the same way Phase 1's gate is: after wiring
`RecreationGovAdapter` into the kernel's polling loop (mocked upstream,
real kernel code), the kernel-level code — cadence tiering, the dedup
ledger, the drop-registry target model — must require **zero** `if
source == "fandango"` branches, zero fields that exist only because
Fandango's shape needed them, and zero "well, campsites don't have X so
just skip that step" carve-outs bolted on after the fact. If satisfying
recreation.gov's calendar shape means the kernel needs source-specific
branching to keep working for Fandango, that is not a Phase-2 bug to patch
around — it is the kill criterion in `docs/platform/README.md` firing:
**run the verticals as separate products.** This gate is deliberately
adversarial to the platform thesis, not a formality to wave through.

Concretely, this means the drop registry's target model has to
accommodate "one row of interest per (campground, site, date)" using the
exact same `unit_id`/`date`/`state` shape `observation()` already defines
(adapters/base.py:42) — no schema change, no adapter-specific columns.
`unit_id` for recreation.gov is `"{campground_id}:{site_id}:{date}"`
(already how the scaffolded adapter constructs it) precisely so the
dedup ledger's key format never needs to know which source produced it.

## The RIDB nuance, stated honestly

Two different recreation.gov surfaces are involved, and they are not the
same tier of sanctioned-ness — the adapter's own docstring says this
plainly, and it is repeated here because it is exactly the kind of nuance
a risk-tier label can accidentally flatten:

- **RIDB** (`ridb.recreation.gov`) is the real, documented, API-keyed
  surface — for facility and campsite **metadata** (names, amenities, geo
  coordinates). It is a legitimate sanctioned API in every sense
  `docs/platform/README.md`'s `GREEN` tier describes for a contractual
  source. This adapter does not call it — Phase 2's watch is availability,
  not metadata, and metadata is a Phase-4-catalog concern at most.
- **The availability endpoint this adapter actually calls** —
  `recreation.gov/api/camps/availability/campground/{id}/month` — is
  public US-government data with no auth and no key, served directly by
  the same frontend that backs recreation.gov's own booking calendar
  widget. It is **not formally documented or contractually offered** the
  way RIDB is. It is tiered `GREEN` here on the strength of years of
  unenforced third-party tool usage against it, which is a real signal but
  an operational judgment call, not a legal guarantee — the same honest
  hedge `docs/platform/README.md`'s risk-tier table already carries
  forward for this exact adapter.

Calling both of these "the recreation.gov API" without distinguishing them
would misstate the actual legal footing of what's being polled. The
distinction is the point of documenting it, not a technicality.

## Owner-run live preflight, before any real use

Before this adapter is ever pointed at production traffic (i.e., before
any real subscriber watches a real campground through it), the owner
personally: (1) reads recreation.gov's current Terms of Use in full — not
a cached summary, since ToS text changes without notice and Phase 2's
"years of unenforced use" argument is only as good as today's terms; and
(2) verifies the availability endpoint's actual behavior from a
**residential** IP — response shape, rate tolerance, and whether it still
behaves the way the mocked tests assume — mirroring the same
residential-vs-datacenter caution `docs/architecture-notes.md`'s W3 already
names for Fandango. Nothing about the adapter code or its mocked test
suite constitutes this check; `preflight.py`'s existing role (project a
day's request spend before trusting a new box, per Decision 4) is the
template this second preflight follows, not a substitute for it.
