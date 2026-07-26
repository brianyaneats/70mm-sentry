# AMC Sniper vs. 70mm Sentry — teardown & honest comparison

Reviewed 2026-07-26 by opening **amcsniper.com** in Chrome and pulling its real
computed styles, then comparing against what I built you: the **70mm Sentry**
watcher and its **Architecture for Review** artifact.

What I could see: the public marketing surface (landing page, the Odyssey guide,
footer/compliance pages). What I could not see: anything behind sign-in — I don't
create accounts or enter credentials, so the tracker UI is inferred from their own
descriptions and screenshots.

---

## 1. The one-line verdict

They built a **product**. We built a **machine**. Their site is better because it
was designed to be *seen by strangers*; ours was designed to be *run by you*. The
aesthetic gap is real and worth closing — but the feature gap is smaller than it
looks, and on the thing you actually care about (getting into an Odyssey 70mm
showing), Sentry's free behavior currently beats their free tier.

---

## 2. What AMC Sniper is

Built by Ben Werner (@ solo maker, founder-led, "DM me on X"). Independent tool,
explicitly "not affiliated with AMC Theatres or IMAX."

### Features

| Feature | Detail |
|---|---|
| **Per-seat map watching** | Tracks *individual seats* on AMC's seat map, not just "is the showtime on sale." Detects a specific seat being refunded and reopened. |
| **Two tracker types** | (1) *Watch for refunds* on a chosen showtime; (2) *Watch for new showtimes* over a date range you pick. Exactly Sentry's two modes — but self-serve. |
| **Multi-theater** | Lincoln Square, SF Metreon, and presumably the whole AMC circuit. |
| **Alert channels** | Email (free), Discord, SMS (paid). |
| **Cadence as the paywall** | Free = check every 60s + email. **Plus** = every 5s + text alerts. They're explicit that 5s "is expensive for me to run." |
| **Free tier** | 3 movies, no card required. |
| **Historical seat data** | Every watch is recorded: opened/taken counts over time, replayable. |
| **Seat-map replay widget** | The hero asset — a scrubbable timeline (green = opened, red = taken) above a live-rendered auditorium map. Real production data used as marketing proof. |
| **Content/SEO** | A guide ranking for exactly what a desperate buyer types, with chapter TOC, byline, hero still, and a soft CTA at the end. |
| **Compliance surface** | Privacy, Terms, and a dedicated SMS-consent page; message-frequency and STOP/HELP language on the landing page. |

### The strategy insight worth stealing

Their own guide says it plainly: *"Getting an alert when 400 seats open at once is
a lot easier than fighting everyone over one refunded seat."* That is an
endorsement of the exact thing Sentry does best — drop-watching, not refund-sniping.

Their data also confirms two things we were guessing at:
- Refunds trickle in **constantly and at random hours** (2pm Tuesday, 3am Saturday).
- **2:00am and 6:00am showings are by far the easiest to get** — people wake up,
  refund, and go back to bed, and scalpers don't want those seats. They showed a
  6:00am Lincoln Square show that still had unsold seats when the movie started.

---

## 3. Aesthetic: theirs vs. mine

### Their design system (measured, not guessed)

Ruthlessly small. The entire site is built from this:

```
Font        Geist Variable → ui-sans-serif → system-ui        (one family, everywhere)
Text        #131517                                            (near-black, never pure #000)
Secondary   rgba(19,21,23,0.64)      Tertiary rgba(19,21,23,0.44)
Page bg     #F7F8F9                  Inverted surface #131517
Hairlines   rgba(19,21,23,0.08)
Radius      8px — the ONLY radius on the site
Shadows     none. Zero. Depth comes from hairlines and bg shifts.
Weights     400 / 500 / 600  (no 700, no 800)
Sizes       30 / 16 / 15 / 14 / 13 / 12 px   (h1 is only 30px!)
h1          30px / 36px line-height / 600 / letter-spacing −0.75px
Body        15px / 28px line-height  (1.87 ratio — very airy)
Buttons     13px / 500 / 8px radius / black fill / tight 14px side padding
Dark mode   none — they committed to one look and made it excellent
```

Layout: left-aligned, ~448px measure for prose, enormous whitespace, a sticky
translucent header (0.85 alpha), a black announcement bar, and scroll-triggered
fade-ins. Color appears **only inside data visualization** (green/red seat states) —
the UI itself is pure grayscale.

### What I made you

The Architecture artifact: warm-gray page, **serif body text**, sans-serif bold
headings, a **teal accent** on eyebrows and section numbers, numbered sections,
~740px measure, and long-form prose — a *document*, in the register of an
engineering RFC.

### Where mine loses, specifically

1. **Two type families + an accent hue.** They use one family and no hue at all.
   Every extra axis I add is a chance to look "generated"; their restraint reads
   as intent.
2. **Type too big, hierarchy too loud.** My headings shout; their h1 is 30px and
   still dominates because *everything around it is quiet*. Contrast comes from
   weight and spacing, not size.
3. **Prose where a component belonged.** I described the drop mechanics in
   paragraphs. They *rendered the seat map* and let you scrub it. One interactive
   component out-argues a thousand words — and it doubles as marketing.
4. **No product surface at all.** Mine has no header, no CTA, no next action. It
   ends where a real page would start converting.
5. **Decoration instead of data.** My color carries no information. Theirs carries
   exactly one thing: seat state. That's the whole rule.

### Where mine actually wins (keep these)

- **Honesty as a design feature.** My doc enumerates its own weak points (W1–W6)
  and says "not yet implemented." Their site never shows a failure mode.
- **Serif for long reading.** For a 3,000-word RFC, serif at a generous measure is
  genuinely more readable than 15px sans. The mistake wasn't serif — it was
  bringing document styling to something you wanted to feel like a product.

---

## 4. Feature-for-feature: Sentry vs. AMC Sniper

| | 70mm Sentry (yours) | AMC Sniper |
|---|---|---|
| Granularity | Showtime-level (on-sale / sold-out) | **Per-seat** |
| Drop detection | Yes — probe past horizon + 21-day reconcile | Yes — date-range tracker |
| Refund detection | Yes — sold-out → on-sale flips | Yes, per seat |
| Cadence | **30s, free, always** | 60s free / 5s paid |
| Channels | Telegram (private + friends channel), ntfy | Email / Discord / SMS |
| Theaters | Lincoln Square only | Full circuit |
| Users | You (+ friends via broadcast) | Anyone, self-serve |
| UI | CLI + push notifications | Web app |
| History/analytics | dropLog + state snapshots (unused) | Rich, replayable, public |
| Reliability | **Watchdog + off-box GitHub dead-man + CI + circuit breaker** | Unknown (not surfaced) |
| Cost | $0, self-owned | Free tier / paid Plus |
| Data source | Fandango NAPI | AMC seat maps |

**Their real edge:** per-seat data. Ours is blind to *which* seat and to how many
remain — we only know "purchasable or not." That's the one gap that changes
outcomes, and it comes from their data source, not their code.

**Our real edge:** speed for free, and an alerting stack built to survive its own
failures. Also: no third party between you and the alert.

---

## 5. What I'd steal (ranked)

1. **The 2am/6am insight** — free, immediate. Sentry currently filters to
   `target_after_hour: 18`, which *excludes* the easiest seats in the house. Worth
   a config change tonight if you'd take a 6am show.
2. **The seat-map replay component** — even without per-seat data we can plot
   showtime-level availability over time from `seenAvailability`. It'd make the
   drop/refund pattern legible and would be a genuinely strong artifact.
3. **Radical type restraint** — one family, no accent hue, 8px radius, no shadows.
   Now encoded in the `minimal-product-ui` skill so I default to it.
4. **A landing surface** if you ever open Sentry to friends — the pinned-message
   welcome is doing that job today, badly.
5. **Compliance pages** — only if SMS ever enters the picture. Telegram avoids all
   of it.

## 6. What I would not copy

- **Paid cadence tiers.** Their 5s tier exists because they pay for infrastructure
  per user. Yours runs on a Mac you already own, at 30s, for nothing.
- **Per-seat scraping of AMC.** That's the Queue-it side of the fence. If per-seat
  matters enough, the sanctioned route is the AMC vendor API (Seating Layout v3) —
  still unfiled.
- **A public product.** Every user you add is a competitor for the same 16
  accessible seats at Lincoln Square. Their existence already means more people
  are watching your showtimes than last week — speed matters more now than it did.

---

## 7. Files this produced

- `~/.claude/skills/minimal-product-ui/` — the measured design system above, as a
  first-try recipe (tokens, rules, starter CSS, self-check).
- `~/.claude/skills/site-teardown/` — the repeatable method used here: open a site
  in Chrome, extract real tokens, map features and monetization.
- `~/.claude/skills/ticket-sniper-domain/` — the domain facts (data sources, refund
  dynamics, competitor feature set, legal boundaries) for whatever you build next.
