# Things I learned building this

A watcher is a small program with an unusually harsh grading rubric: it is
either awake at one unpredictable moment or it was pointless. Almost everything
below is something that broke first and became a design rule second.

---

## On reading a source that never agreed to be read

**A 403 is a sentence, not a verdict — read which word is wrong.**
The first `403` looked like rate limiting, and the instinct was to slow down and
add backoff. Slowing down changed nothing. The actual cause was a **missing
`Referer` header**; with it, the same endpoint answers `200` to a plain `curl`.
An entire polling strategy was nearly designed around a constraint that did not
exist. Isolate the variable — headers, cookies, rate, IP — *before* designing
around the failure. One matrix of six requests, spaced a minute apart, answered
what a week of guessing would not have.

**The obvious field is often the decoy.**
Each showtime carries `filmFormatHeader`, which reads `"Premium Format"` —
useless. The real signal is nested one level down in
`filmFormat[].filterName` (`"IMAX 70MM"`, `"70MM"`). If the first detector had
shipped against the obvious field, it would have matched nothing and reported
"no drops" forever — the worst possible bug, because it looks exactly like good
news. This is why the parser has its own alarm: the reconcile knows the film is
playing, so *zero* hits across 21 days means the parser broke, not that the
world went quiet.

**"Listed" and "buyable" are different events.**
AMC posts showtimes that are **already sold out** — placeholder listings that
appear complete but have no seats. The naive alert ("a new date appeared!")
therefore fires on news you cannot act on, and the alert that actually matters
comes minutes-to-days later, when the map opens or someone returns a ticket. The
fix was a second detector with its own clock, plus an `everAvailable` ledger to
tell "this show just genuinely went on sale" apart from "someone returned a
seat" — because those deserve different words on your phone at 2am.

**Verify there is no push transport before accepting polling.**
Polling felt like giving up, so it got tested properly: no websocket, no SSE,
and `If-None-Match` is ignored — an ETag comes back, but a conditional GET still
returns `200` with the full body. Polling *was* the only option. Knowing that
turned the goal from "avoid polling" into "make polling as stream-like as the
source allows" — kept-alive connections (54ms → 21ms per poll) and tight slices
during the hours that matter.

---

## On alerting

**Send, confirm, then record. Never record then send.**
The original code marked a date "announced" and *then* pushed. A single channel
outage meant the alert vanished permanently — every later cycle saw the date as
old news. Now every ledger write happens only after a push is confirmed
delivered, so a failed alert simply retries next cycle. This is the single most
important line of reasoning in the codebase and it is pinned by several tests.

**An alert that can't reach you is not an alert.**
The very first Telegram-adjacent push crashed on an em-dash: HTTP headers are
latin-1, and the title contained `—`. It failed silently in exactly the way that
would have mattered most. Notification paths need a test that *actually sends*,
plus a periodic self-test — the weekly "Sentry alive" ping exists purely so the
channel proves itself while nothing is happening.

**Not every alert deserves every audience.**
Once friends subscribed, the split became obvious: they signed up to hear about
tickets, not about launchd. Operational messages carry hostnames, restart
commands, and HTTP diagnostics. The default audience is therefore `private`, so
anything added later is quiet by mistake rather than loud by mistake — a default
chosen for its failure mode, not its convenience.

**A missed alert must not become a permanently missed alert.**
Muting a showtime forever after one alert seemed like sensible anti-spam, until
the realization that each target date has exactly *one* 70mm screening: sleep
through the first reopening and you would never be told again. Replaced with a
30-minute cooldown — flapping is still suppressed, but the news can recur.

---

## On staying alive

**The alarm cannot live on the thing it watches.**
Every on-box guard — watchdog, startup check, healthcheck — dies with the box.
A sleeping laptop looks identical to a healthy one from the inside. The only
component that can report total loss is one running somewhere else, which is why
an hourly GitHub Actions cron polls a heartbeat topic and shouts when it goes
stale. Free, no account, and the only thing that has ever caught the whole
machine being gone.

**Your interpreter is a dependency, even when you have none.**
An Xcode install invalidated `/usr/bin/python3`. The watcher died — and so did
its watchdog, because both plists hardcoded the same interpreter. Now each job
picks the first working interpreter from a list, so one break cannot take down
the watcher *and* its alarm together. Zero pip dependencies still leaves you
depending on the thing that runs them.

**Tune backoff to the cost of being wrong.**
The circuit breaker opened for 30 minutes after 3 failures. Then three read
timeouts — an ordinary network blip — tripped it minutes before a target date
listed, and the watchdog correctly reported the sentry blind for the full half
hour. Three timeouts are far more often a blip than a block, so the floor
dropped to 60s with fast doubling to a 4h cap. Same protection against a real
block, one-thirtieth the damage from a hiccup.

**A quiet log looks exactly like a healthy log.**
This is the whole reason there are four separate alarms instead of one
"is it running" check. Each proves something the others cannot: the process
runs / the payloads have content / our parsing still matches / the machine
exists at all. Silent failure is the default state of an unattended program, and
it has to be attacked explicitly.

---

## On testing

**The test that pays for itself is the one on the recovery path.**
A unit test found a breaker bug that production would have hidden for months: an
open breaker with a falsy `openUntil` would refuse to ever probe again —
permanently dark, while looking fine. Nobody writes that scenario by hand.
Writing tests for *recovery* rather than *failure* is where it surfaced.

**Ship risky detectors in dry-run first.**
A rolling "scan dates we haven't announced" walk seemed like an obvious upgrade.
It fired a false ticket alert to the live subscriber channel within two minutes
and was reverted the same night. The trap: the `announced` ledger only knows
what *this watcher* announced, so every date already on sale before it existed
looked like breaking news. The horizon was the only thing encoding "already
known," and the new walk threw it away. That failure is now a comment block at
the point of temptation, explaining what a correct version would need — which is
better documentation than any wiki page.

**The tests outgrew the code, and that is the right ratio here.**
584 tests against ~1,700 lines of watcher. Not discipline for its own sake:
every branch matters at a moment you cannot reproduce on demand, and network
mocking is the only way to exercise "Fandango returns garbage during a drop."

---

## On scope

**Measure the seam before optimizing it.**
Process startup costs ~12s. At 55-second realtime slices that was ~18% of the
window lost to respawn; at 240s it is ~5%. Both numbers came from measurement,
and they set the slice length. Guessing would have picked a round number and
been wrong in a way nobody would ever notice.

**Know the line you will not cross, before you're tempted.**
No auto-purchase, no checkout automation, no CAPTCHA solving, no queue bypass,
no seat-map scraping, no credentials, ever. The tool's job ends at *telling a
human to go buy a ticket*. Writing that boundary down early made later feature
requests easy to answer — including the fun-sounding ones.

**The specific tool generalizes better than the general one would have.**
This was built for exactly one film at two theaters. Making it work *properly*
for that — cadence tiers, delivery-gated state, four alarms — produced a shape
that dropped onto a completely different source (`recreation_gov.py`) almost
unchanged. Building the "general drop-watching platform" first would have
produced abstractions over problems that had never been felt.
