# Longer notes

The main discoveries are in the [README](../README.md). This is the stuff that
didn't fit there — mostly about testing and about knowing when to stop.

## Testing

**The test that paid for itself was on the recovery path, not the failure
path.** I had tests for "the circuit breaker opens after 3 failures." Writing
the boring companion test — "and then it closes again" — is what surfaced a bug
where an open breaker with a falsy `openUntil` would never probe again.
Permanently dark, while `--status` looked fine. Production would have hidden
that for months, and I'd have found it by missing a drop.

**Mocking the network is the only way to test the interesting cases.** "What
happens if Fandango returns garbage during a drop" is not something you can
reproduce on demand. Every test mocks the wire, which means the whole suite
runs in ~76 seconds and I actually run it.

**The em-dash bug.** My first push crashed because HTTP headers are latin-1 and
my alert title had a `—` in it. It failed in exactly the way that would have
mattered most — during a real alert. Notification code needs a test that
actually sends, plus a periodic self-test. The weekly "Sentry alive" ping
exists only so the channel proves itself while nothing is happening.

**584 tests against 1,707 lines is a weird ratio and I stand by it.** The
grading rubric here is unusual: the program is either awake at one unpredictable
moment or it was pointless.

## Alerting people who aren't me

Once friends subscribed, the split got obvious fast: they signed up to hear
about tickets, not about launchd. Ops messages carry hostnames, restart
commands, HTTP status codes. So `push()` takes an audience, and the default is
`private` — anything I add later is quiet by mistake rather than loud by
mistake. Default to the safer failure.

The copy matters more than I expected. `2026-08-21` became `Fri, Aug 21`,
`7:00p` became `7:00 PM`, every showtime gets its own tap-to-book link, and
every ticket alert says outright that a listed showtime can still have zero
seats. People who've never seen this repo are reading these at 2am.

## Measuring instead of guessing

Process startup costs ~12 seconds — launchd respawn plus config and state
reload. At 55-second realtime slices that was ~18% of the window lost to seam;
at 240 seconds it's ~5%. Both numbers came from measuring. If I'd guessed I'd
have picked a round number and been wrong in a way nobody would ever notice.

Same with the keep-alive: 54ms cold, 21ms warm. Worth restructuring the loop
for. I wouldn't have believed it without the number.

## Scope

**I wrote down the line I wouldn't cross before I got tempted.** No
auto-purchase, no checkout automation, no CAPTCHA solving, no queue bypass, no
seat-map scraping, no credentials. Deciding that early made every later "what
if it also…" easy to answer, including the fun-sounding ones.

**Building the specific thing first was right.** This was for one film at two
theaters. Making it actually work for that — cadence tiers, delivery-gated
state, four alarms — produced a shape that dropped onto recreation.gov nearly
unchanged. If I'd started by building "a general drop-watching platform" I'd
have abstracted over problems I hadn't felt yet.

**The failed experiment is now a comment block.** The rolling-scan walk that
false-alerted my friends is documented at the exact spot in `sentry.py` where
someone (me, in three months) would be tempted to rebuild it, including what a
correct version would need. That's more useful than a wiki page nobody opens.
