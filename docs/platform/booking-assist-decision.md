# Assisted booking — decision, and what to build instead

**Date:** 2026-08-04
**Question:** build a tool that stages a recreation.gov booking form and
advances through it the moment a release drops, parking before the final
submit so the human clicks it?
**Answer: no.** Not the attended version, not the park-before-submit
version. The reason is not a technicality, and it changed my own mind
mid-investigation — I started out planning to build it.

---

## The evidence that decided it

Recreation.gov's live Terms of Service (dated **March 18, 2026**, read
directly in a browser at
`help.recreation.gov/helpcenter/en/terms-of-service`, because the page is
a JS app that plain fetching cannot render) contains a section titled
**Automated Agents**. It binds users to a separate **Bot Policy** and
states that Recreation.gov reserves the right to act against bot activity
that, among other categories:

> **Enables Unfair Advantage:** Uses automation to bypass fair access
> systems, such as purchasing limited-availability inventories, **making
> reservations**, or entering lotteries in a manner that disadvantages
> human users or violates our Terms of Service.

That sentence names this exact tool. Not scraping, not load — *using
automation to make reservations for limited-availability inventory in a
manner that disadvantages human users.* The whole purpose of the tool is
to be faster at claiming capped inventory than the people we are racing.
That is the prohibited category, stated in the site's own words.

Two more things from the same document:

> Recreation.gov employs monitoring systems to differentiate human
> traffic from automated bot traffic. We analyze traffic patterns, user
> behavior anomalies, and specific technical indicators to identify
> non-human activity.

> Recreation.gov explicitly reserves the right, at its sole discretion,
> to monitor, block, rate-limit, or otherwise restrict access to its
> services for any automated user or bot.

And `recreation.gov/robots.txt` disallows exactly the surfaces a booking
tool would touch: `/cart`, `/cart/*`, `/account/*`, `/api`, `/api/*`,
with `Crawl-delay: 10`.

### Why "the human clicks submit" doesn't rescue it

That was my own framing, and it doesn't survive contact with the text
above. The clause is about *using automation to gain advantage in
acquiring limited inventory*, not about who performs the final
keystroke. By the time the form is staged and advanced, the automation
has already done the advantage-conferring work; the last click is a
formality. A rule against automated queue-jumping that could be satisfied
by having a person press the last button would not be a rule.

The distinction that *does* hold — and that this project has always drawn
correctly — is **watching versus acting**. Monitoring public availability
and telling a human about it is information. Operating the reservation
form is participation in the transaction. The line is real; this tool is
on the far side of it.

### The cost of being wrong lands on Brian personally

Enforcement here is account-level: the ToS reserves blocking and
restriction at sole discretion, and press reporting on this family of
systems describes bans for accounts caught using bots. A ban would not
just lose one attempt at Haleakalā — it would take the account, and with
it any other reservations and future bookings. This is a bad bet even
setting the ethics aside.

---

## The reframe: you do not need faster fingers

The premise was "I lost because I could not click fast enough." The
verified facts suggest that is not actually the binding constraint.

**Verified (NPS official FAQ):**

- Haleakalā sunrise reservations release **60 days ahead at 7:00 AM
  HST**, and there is a **second, smaller batch released 2 days ahead,
  also at 7:00 AM HST**.
- Inventory "renews at 7:00 am every day, but often sells out within
  fifteen minutes."
- $1 per **vehicle** (not per person), separate from the $30 park
  entrance fee. Non-transferable, non-refundable. One vehicle
  reservation per individual per three days.
- The name on the reservation must show a paper copy plus photo ID at
  the gate.

Two things follow. First, **fifteen minutes is not a reflex contest** —
it is lost on preparation, not on click latency. Seconds spent finding
the right page, choosing the date, typing details, or discovering the
account needs a password reset are where the attempt actually dies.
Second, **the 2-day batch is a whole second lane** that most visitors
never learn about, and it is far less contested than the 60-day
stampede.

The winning strategy is more shots on goal, not a faster trigger.

---

## What to build instead (all clearly legitimate)

### 1. Point Dropwatch at Haleakalā — monitoring, which is what we do

`adapters/recreation_gov.py` already polls public availability read-only.
A Haleakalā sunrise drop would alert on:

- the **2-day-ahead batch** landing (the uncontested lane), and
- **cancellations trickling back** — recreation.gov releases cancelled
  inventory continuously, and a non-refundable $1 permit gets abandoned
  often. This is the same insight as the 70mm refund sniper: the returned
  seat is easier to win than the on-sale seat, because almost nobody is
  watching at 9pm on a Tuesday.

This is pure information delivery, it is the thing this platform is for,
and it does not touch the reservation system.

### 2. A pre-release "get ready" alert

Fire at T-10 minutes before 7:00 AM HST so a human is *sitting there*
with the page open, rather than being paged after the fact. Include the
deep link and the checklist below. The alert is the automation; the
booking is the human.

### 3. A preparation runbook (human, rehearsed, no automation)

Most of the recoverable time is here, and none of it is against the
rules:

- Account exists, password known, **logged in the night before**.
- Payment method saved in the *browser's own autofill* — the browser
  filling a card at the user's own keystroke is a browser feature, not a
  bot.
- Exact reservation page bookmarked; date, vehicle count, and entrance
  decided in advance and written down.
- A dry run on a low-demand, cheap booking so the flow holds no surprises
  — including finding out exactly where the CAPTCHA appears. (Public
  sources could not confirm this; it must be observed, not assumed.)
- Know the local-time conversion cold; have a clock synced to a time
  server open.

### 4. What Dropwatch must never do

The public trust page already promises this and it stays true: never
buys, reserves, or holds anything on anyone's behalf; never asks for or
stores credentials. For subscribers who want automation, the answer
remains the **signed webhook** — their script, their machine, their
account, their risk, and their responsibility to obey the venue's rules.
Shipping staged-form booking as a subscriber feature would be strictly
worse than the personal version: it would sell a reflex advantage to a
paying subset in a zero-sum race for conservation-capped public
inventory, against everyone else including non-subscribers.

---

## The unknowns that remain (and how to close them)

None of these change the decision; they matter for the monitoring build.

- **Where the CAPTCHA sits** in the Haleakalā flow — observe once, by
  hand, during a real release.
- **Whether a queue/waiting room exists** for this specific release. No
  public source confirms one; Half Dome uses a lottery, which is a
  different mechanism. Observe rather than assume.
- **Cancellation-return behavior** for this permit specifically — how
  often, what hours. The 70mm project's own history says sampling
  interval is the binding constraint on what you can see, and that the
  first "nothing ever changes here" conclusion was a sampling artifact.
  Measure before concluding.

---

## One honest note on fairness

Even the legitimate version is not neutral. Alerting is closer to fair
than automating — it hands the same information to more people at the
same moment, including people who would have missed the release entirely
— but every subscriber added is one more informed competitor for a fixed
daily allocation the Park Service capped for conservation reasons, not
market ones. That trade-off gets stated plainly in the product copy, the
way the rest of Dropwatch's honesty rules already require. It is the
reason the answer to "can you click for me" is no, and the reason the
answer to "can you tell me the instant it opens" is yes.
