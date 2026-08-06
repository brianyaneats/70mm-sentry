# Phase 0 — gating actions (owner, not code)

Two decisions that no agent and no amount of engineering can make: they're
either a real-world application to a company that will judge it, or a
product/pricing call about money and disclosed risk. Both are listed here,
first, precisely *because* neither blocks Phase 1 or Phase 2 engineering —
the kernel extraction and the second adapter don't need either answer. They
gate Phase 4 (the first public, monetized surface), and one of them has an
unknown, possibly-long lead time. Filing it now instead of waiting until
Phase 4 needs it is the entire argument for calling this "Phase 0" instead
of folding it into Phase 4's checklist.

---

## Action 1 — file the AMC vendor API application

**What:** Submit a vendor request at developers.amctheatres.com
(`GettingStarted/NewVendorRequest`), authenticated via `X-AMC-Vendor-Key`
once issued. This unlocks the **Seating Layout API v3** (true per-seat
data) and the **Embargoed Showtimes** endpoint (future showtimes at the
source, before they hit the public site).

**Why it's an owner action, not something an agent files:** it's a vendor
relationship with a real company, likely requiring a business identity,
a stated use case, and a human at AMC deciding whether to approve it.
No amount of code changes that.

**Why it gates the per-seat movie vertical specifically:** per-seat
availability (which seat, how many remain) does not exist anywhere in the
Fandango NAPI payload — the showtime object carries `type`
(available/soldout/restricted/pastshowtime) and nothing about seat counts
(`ticket-sniper-domain` skill, "measured 2026-07-27"). The only two ways to
get real seat-level data are (a) this vendor API, or (b) scraping AMC's own
seat-map UI, which sits behind Queue-it bot protection and would require
forging or automating around its token — that's exactly the `RED`-tier
circumvention this platform's risk-tier rule refuses to build
(`docs/platform/README.md`, "Source risk tiers"; `ticket-sniper-domain`
skill boundaries). So: no vendor approval, no per-seat movie product, full
stop — showtime-level (buyable/not-buyable) is the ceiling without it, and
that's already what the Fandango-based free vertical does today.

**File it now, not at Phase 4, because:** there is no published SLA for
vendor approval, and hobbyist applications may simply be declined
(`project-state` memory: "AMC vendor-API application never filed" as of the
last check). If it's declined, that's useful information *months* earlier
than finding out at the moment Phase 4 needs a monetizable movie vertical
and doesn't have one. Filing early is a bet with only upside: worst case,
a no that arrives before code depends on a yes.

**If it's declined:** the movie vertical stays exactly what it is today —
a free, showtime-level, YELLOW-tier watcher (see Action 2). No per-seat
product ships. This is survivable on its own (the Kill Criteria in
`README.md` cover the case where it's declined *and* campsites also
doesn't convert); it is not survivable to pretend the vertical will
eventually get per-seat data without this approval.

---

## Action 2 — settle Fandango's posture as YELLOW, free, legacy

**What:** Write down, once, as policy rather than an implicit assumption:
Fandango's NAPI is `risk_tier = "yellow"` and will never be sold on a paid
or prepaid plan. This is a decision to record, not a code change — the
enforcement is already the risk-tier rule itself
(`docs/platform/README.md`); what's missing is the explicit sign-off that
stops a future contributor (human or agent) from "helpfully" wiring
Fandango into a Phase 4 pricing tier because nothing on paper forbade it.

**Why YELLOW, argued from evidence already gathered:**

- `robots.txt` disallows `/napi/*` outright — this is not ambiguous public
  data, it's a source explicitly asking not to be automated there.
- No rate limit is published for the public site at all — the *only*
  official number anywhere is on Fandango's **separate, sanctioned partner
  API** (developer.fandango.com, paid + approved, "limited to 1 call per
  second"). That number describes a different product this repo doesn't
  use; treating it as license for the public NAPI would be borrowing
  authorization that was never granted for this endpoint
  (`ticket-sniper-domain` skill).
- In practice, tolerated via 403/429 enforcement rather than an outright
  block — the endpoint answers, sometimes refuses, and hasn't shut off
  entirely at personal/residential-IP scale (`docs/architecture-notes.md`,
  Decision 4; W3 in the same doc names the datacenter-IP risk explicitly).

That combination — explicitly disallowed by the source, no published
permission, tolerated only at small scale — is the textbook shape of
"tolerated-gray," which is what `YELLOW` means by definition
(`adapters/base.py risk_tier` docstring). It is not `GREEN` (no contract
authorizes it) and it is not `RED` (nothing about *watching* showtime
status requires circumventing an access control — that's a materially
different act from scraping a bot-gated seat map, see Action 1).

**Why "settle," not "reconsider each time":** the risk-tier rule only
protects the platform if it's enforced consistently. A one-off exception
("just this once, offer a Fandango-based paid tier because a user asked")
is exactly the kind of drift a written, dated decision exists to block.
Settling it here means: Fandango stays the **free, legacy, single-venue
vertical this repo already runs** — not a Phase 4 monetized SKU, ever,
regardless of demand. If Fandango is ever going dark, the public status
page (`phase-4.md`) discloses that risk plainly rather than quietly hoping
subscribers don't notice a `YELLOW` source is more fragile than a `GREEN`
one.

**What doesn't change:** the existing single-venue Fandango watcher keeps
running exactly as `docs/architecture-notes.md` describes it. This action
is a label and a policy, not a rewrite.
