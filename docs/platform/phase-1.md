# Phase 1 — kernel extraction (strangler-fig, zero behavior change)

## The move

`sentry.py` does not get rewritten. It gets a seam cut into it —
`adapters/base.py`'s `SourceAdapter` contract — and everything Fandango-
specific on the fetch/parse/health side moves behind a `FandangoAdapter`
that wraps the *existing* functions (`fetch_day`'s NAPI-shape parsing,
`napi_headers`, the `type` → availability mapping) rather than
reimplementing them. This is the strangler-fig pattern: the new seam grows
around the old code, calling into it, until the old direct call sites are
gone and only the seam remains. At the end of Phase 1, single-tenant
Fandango watching runs *through* the adapter contract, single-tenant,
single-source, stdlib, with the same config file and the same `state.json`
shape it has today.

**What explicitly does not happen in Phase 1:** no multi-tenancy, no
second adapter (that's Phase 2), no event log or outbox (that's Phase 3).
This phase is scoped to one question: does the frozen contract fit the one
adapter that already has fourteen months of production behavior to not
break?

---

## The gate is falsifiable, not a design review

**Every test in the current suite passes through the new seam, unmodified
in intent.** `python3 -m unittest discover -s tests` was **310 tests,
passing** when this doc was drafted (2026-08-03, morning), then **354
tests, still passing**, minutes later after concurrent Phase-1 work already
landed more of the extraction below. (Earlier planning notes cite 280 —
also already stale by the time this doc was written.) The number moves
constantly while this phase is actively worked and is not itself the gate
— **a fresh `unittest discover` run, immediately before declaring this
phase done, all green,** is the actual gate. Anyone tempted to cite a
specific count as proof should re-run it instead; it will already be
wrong.

This gate is deliberately mechanical: run the suite, read the pass count,
done. It is not "does the extraction look clean" — a seam that passes
review but breaks a test is a failed gate; a seam that looks inelegant but
passes every test, unmodified, is a passed one. Elegance is Phase 3's
problem once there's more than one adapter to abstract over.

---

## The load-bearing constraint: `http_get` has to paper over two transports

The frozen contract's `fetch(target, date, http_get)` (adapters/base.py:89)
takes one callable: `http_get(url, headers) -> bytes`. `sentry.py` has
**two different transport paths** that `fetch_day` (sentry.py:449)
switches between via its `session` parameter, and that duality predates
this generalization entirely:

1. **Cold path:** a plain `urllib.request.urlopen(req, timeout=25)` call
   (sentry.py:468) — one connection, one request, closed after.
2. **Warm path:** `RealtimeSession.get()` (sentry.py:335), which reuses a
   kept-alive `http.client.HTTPSConnection` across a burst of polls inside
   a realtime window (`docs/architecture-notes.md`, Decision 2) —
   deliberately a *different* stdlib module (`http.client`, not
   `urllib.request`) because it needs raw request/getresponse control that
   `urlopen` doesn't expose.

`FandangoAdapter.fetch()` (`adapters/fandango.py`) already takes its
`http_get` callable per the contract and simply calls it — it does not
know or build the transport itself. What has **not** landed yet as of this
writing is the kernel side wiring `fetch_day` to actually call
`FANDANGO.fetch(target, date, http_get)`: today `fetch_day` still builds
the URL/headers from `fandango_adapter.build_url`/`build_headers` and
calls `urllib.request.urlopen` (or `session.get`) directly, inline —
correct output, but it means the same dispatch logic exists in two places
(the adapter's `fetch()` method, unused; and `fetch_day`'s own inline
branch, actually used). Closing that gap means wrapping both branches
behind one `http_get(url, headers)` callable — built as a small closure
inside `fetch_day` that picks cold or warm transport based on whether
`session` is set, and is then passed into `FANDANGO.fetch(target, date,
http_get)` instead of `fetch_day` doing the branching and the parsing
itself. **The adapter never knows or cares which transport served a
given call** either way — that's exactly what the contract's docstring
means by "the ONLY way an adapter may touch the network"
(adapters/base.py:96): one narrow waist, regardless of what's plumbed
behind it. Whether that waist is a module-level function or a per-call
closure is an implementation detail the contract doesn't care about; what
it does care about is that no adapter code ever imports `urllib` or
`http.client` itself.

**Existing test patch-points must keep working, unmodified, through this
change.** The suite patches `sentry.urllib.request.urlopen` directly at
every cold-path test (`tests/test_sentry.py`, e.g. around lines 216–217,
271–357 as of this writing), not a wrapper function. The constraint this
puts on the remaining wiring: whatever closure or function ends up as
`http_get`'s cold-path branch must still resolve to a call on
`sentry.urllib.request.urlopen` at runtime — i.e. it wraps the existing
call, it does not replace `urllib.request` usage with something else
underneath (a `requests`-based rewrite, a raw-socket rewrite, anything
that changes what attribute a test would need to patch). This constraint
is already satisfied by the code as it stands today (`fetch_day`'s cold
branch at sentry.py:468 still calls `urllib.request.urlopen` directly) —
it just needs to survive the remaining step of actually routing that call
through the adapter's `fetch()` method rather than duplicating its logic
inline. Get this wrong and every cold-path test needs rewriting to match a
new implementation — which is not "zero behavior change," it's a rewrite
wearing zero-behavior-change's clothes.

---

## What's in scope vs. what isn't: fetch-path vs. channel-path `urlopen` calls

`sentry.py` calls `urllib.request.urlopen` at eight sites today. Only two
of them are source-fetch paths subject to the adapter contract:
`fetch_day`'s cold GET (sentry.py:468) and `RealtimeSession.get()`'s
`http.client` equivalent (sentry.py:335–346). The other six —
`send_ntfy` (sentry.py:624), `send_whatsapp`'s two providers
(sentry.py:643, around 652 and 676), `_telegram_send_one` (sentry.py:681),
`telegram_setup` (sentry.py:717), and `heartbeat`'s two pings
(sentry.py:808) — are **delivery-layer and ops-layer** calls. (Exact line
numbers here will keep drifting while Phase 1 work is in flight — the
categorical split, not the line numbers, is the point.) They are
explicitly out of scope for `SourceAdapter.fetch`/`http_get`: nothing in
the frozen contract concerns itself with how an alert gets *sent*, only
with how a source gets *read*. Those six call sites become Phase 3's
outbox-provider concern (per-channel send, with `[DELIVERY]` logging on
failure), not Phase 1's. Conflating the two would mean channel breakage
tripping the same breaker as upstream refusal — the same category error
`[REFUSED]` vs `[PIPE]` vs `[DELIVERY]` exists to prevent (see
`docs/platform/README.md`, "Observability standard").

---

## Where `[PIPE]` logging lands, precisely

`RealtimeSession.get()` (sentry.py:335–360) already has the retry
*behavior* the observability standard asks for — one silent reconnect-and-
retry on any `Exception`, then re-raise on the second failure
(sentry.py:358–359). As of this writing, the `[PIPE]` tag has already
landed at exactly this catch site (sentry.py:347–357): the bare `except
Exception as e` clause checks `isinstance(e, OSError)` — the common
superclass of `BrokenPipeError`, `ConnectionResetError`, and
`socket.timeout` (an alias for `TimeoutError`, itself an `OSError`
subclass since Python 3.10) — and logs
`[PIPE] fandango realtime session <venue> (errno=<n>, <ExceptionClassName>):
<e> — retrying on a fresh connection` (or "— giving up" on the second
attempt) before the existing retry-then-raise logic runs. Checking the
shared superclass rather than naming the three exception types
individually is the simpler and arguably more correct choice — it also
catches `ConnectionAbortedError`/`ConnectionRefusedError` and any other
OS-level transport failure, which are exactly as "expected on a reused
connection, not anomalous" as the three named in the contract's docstring.
This is additive, not a behavior change: the retry still happens exactly
as before, on the exact same conditions; the change is only that it now
leaves a grep-able line behind it. A regression test should assert the
`[PIPE]` line appears in the log when the mocked connection raises
`BrokenPipeError` on the first attempt and a normal response on the
retry — proving the tag fires without proving anything about whether the
retry itself changed (it doesn't). The equivalent tagging for `fetch_day`'s
cold path already exists too, via `classify_fetch_error()` (sentry.py, near
line 411) — it inspects the exception and returns `"PARSE"` for a
`FandangoFeedShapeError`/`JSONDecodeError`, `"REFUSED"` for a message that
names an HTTP status code, `"PIPE"` for any other `OSError`, and a generic
`"FETCH"` otherwise, then `fetch_day` logs
`[<tag>] fandango fetch failed <venue> <date> (...)`.

---

## Sequencing inside Phase 1

Status of each step as of this writing (2026-08-03), since Phase 1 is
being actively worked concurrently with this document:

1. **Done.** `FandangoAdapter(SourceAdapter)` exists in
   `adapters/fandango.py`: `risk_tier = "yellow"` (per `phase-0.md`'s
   settled posture), `min_spacing_seconds = 1.0` and
   `daily_request_budget = 25000` declared matching the values
   `pace_request()`/`budget_gate()` already enforce
   (`docs/architecture-notes.md`, Decision 4) — the adapter *declares* the
   numbers, it does not enforce them; enforcement stays in the kernel.
2. **Not yet done.** `sentry.py` still has `fetch_day` branch inline
   between cold `urllib.request.urlopen` and warm `RealtimeSession`,
   rather than closing over both as one `http_get` callable handed to
   `FANDANGO.fetch(target, date, http_get)`. The shared URL/header-
   building logic (`fandango_adapter.build_url`/`build_headers`) has
   already moved to the adapter module and `fetch_day` calls those
   helpers directly — but it does not yet call through
   `FandangoAdapter.fetch()` itself, so the adapter's own `fetch()` method
   is currently unexercised dead weight, duplicating `fetch_day`'s logic
   rather than being called by it. Closing this gap is the remaining
   concrete step, per the constraint above.
3. **Partially done.** `FandangoAdapter.parse()` and
   `.health_check()` exist and correctly implement the NAPI shape mapping
   (`type` → available/soldout/restricted, one-for-one against
   `adapters/base.py`'s `STATES` vocabulary), reusing the same
   `extract_viewmodel`/`match_showtimes` helpers `fetch_day` and
   `odyssey_70mm` also call directly. Same duplication as step 2: the
   logic is shared at the helper-function level already, but `fetch_day`
   does not yet call `FANDANGO.parse()`/`FANDANGO.health_check()`
   themselves — it reimplements the same handful of lines inline. This is
   an acceptable intermediate state for a strangler-fig extraction (shared
   logic first, generic per-adapter dispatch loop once there's a second
   adapter's dispatch to generalize against, in Phase 3) — it is not the
   end state, and the duplication should be named explicitly as debt, not
   quietly left as if it were the finished design.
4. **Done.** `RealtimeSession.get()`'s catch clause already checks
   `isinstance(e, OSError)` and logs `[PIPE]`; `fetch_day`'s cold path
   already classifies and logs `[PARSE]`/`[REFUSED]`/`[PIPE]`/`[BLIND]`
   via `classify_fetch_error()` (sentry.py:411) — see above.
5. **Ongoing.** `python3 -m unittest discover -s tests` — 354 tests
   passing at last check during this writing. Gate passes only on a full
   pass at the moment Phase 1 is declared complete, not on any count cited
   in a doc. If anything fails, the seam is wrong, not the test — the
   whole premise of this phase is that pre-existing test behavior is
   ground truth.

Nothing here touches `adapters/base.py` itself. If any of the above turns
out not to fit the frozen contract as written — if the two-transport
`http_get` requirement or the six-vs-two `urlopen` split reveals a genuine
gap in the contract — that's a blocker to report upward, not a reason to
edit the contract file unilaterally.
