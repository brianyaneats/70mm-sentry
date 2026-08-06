"""platformd.drill — an in-memory, end-to-end walking-skeleton drill.

    python3 -m platformd.drill

Every line this script prints itself is prefixed "DRILL |" so it can
never be mistaken for real log output while it's scrolling past mixed in
with platformd.log's stderr lines. Nothing here touches a real network,
a real Telegram/Discord webhook, or launchd — the "channels" are fake
objects scripted to succeed/fail on cue, and every clock tick is a
`now=` float passed explicitly (no sleeps, time is compressed at will).

Scenario:
  1 drop registered in the catalog, 3 subscribers walked through
  subscribe()+verify() (paid/telegram, free/telegram, paid/discord).
    - event 1 fires -> paid subscribers delivered at t0, free held by its
      60s notify-delay -> delivered at t+61s.
    - event 2 fires -> discord raises BrokenPipeError twice (showing
      [PIPE] + [DELIVERY] + exponential backoff) before succeeding on
      the third attempt.
    - a 4th subscriber (dave, free/telegram) subscribes, verifies, then
      unsubscribes before his queued row for event 2 ever comes due --
      dispatch cancels it instead of sending it, showing STOP is honored
      even for an alert already sitting in the outbox.
    - event 3 fires into a total outage on every channel. Time is
      compressed straight to the 300s zero-delivered deadline (NOT a
      24h TTL wait -- that's the twitchy old behavior this wave fixed)
      to show the time-based [ZERO-DELIVERED] W5 alarm firing. Telegram
      then recovers and its retry succeeds, showing the
      recovered_after_zero_delivered=1 [DELIVERY] line.
"""

import sys

from platformd import core, db, log, outbox

DROP_ID = "drop-1"
T0 = 1_700_000_000.0


def say(msg):
    print("DRILL | %s" % msg)


class ScriptedChannel:
    """A fake channel: pops scripted behaviors off a queue per send(),
    falling back to `default` once the queue is empty. A behavior is
    True (succeed), an Exception instance (raise it), or a zero-arg
    callable (call it for the return/raise)."""

    def __init__(self, name, script=None, default=True):
        self.name = name
        self.script = list(script or [])
        self.default = default
        self.calls = 0

    def send(self, address, message):
        self.calls += 1
        behavior = self.script.pop(0) if self.script else self.default
        if behavior is True:
            return True
        if isinstance(behavior, BaseException):
            raise behavior
        if callable(behavior):
            return behavior()
        raise RuntimeError("bad scripted channel behavior %r" % (behavior,))

    def always_fail(self, exc):
        self.script = []
        self.default = exc

    def always_ok(self):
        self.script = []
        self.default = True


def subscribe_and_verify(conn, channel, address, tier, now, label):
    """subscribe() -> verify() with the code it minted, in one step (a
    real client would receive the code over `channel` and echo it back;
    here we just read it straight off the row since there's no real
    channel to round-trip through)."""
    row = core.subscribe(conn, DROP_ID, channel, address, tier, now)
    verified = core.verify(conn, row["id"], row["verify_code"], now)
    say("subscribed+verified %s: %s/%s tier=%s -> subscription_id=%d" %
        (label, channel, address, tier, verified["id"]))
    return verified


def main():
    say("=== platformd walking-skeleton drill starting ===")
    say("this is a DRILL: in-memory sqlite, fake channels, no real network")

    conn = db.connect(":memory:")

    core.register_drop(conn, DROP_ID, "Drill Drop", description="drill fixture",
                        source="drill", risk_tier="low", now=T0)
    say("registered drop %r in the catalog" % DROP_ID)

    alice = subscribe_and_verify(conn, "telegram", "alice-chat", "paid", T0, "alice")
    bob = subscribe_and_verify(conn, "telegram", "bob-chat", "free", T0, "bob")
    carol = subscribe_and_verify(conn, "discord", "carol-webhook", "paid", T0, "carol")

    tg_channel = ScriptedChannel("telegram", default=True)
    dc_channel = ScriptedChannel(
        "discord",
        script=[True, BrokenPipeError("simulated dead kept-alive socket"),
                ConnectionResetError("simulated reset")],
        default=True,
    )
    channels = {"telegram": tg_channel, "discord": dc_channel}

    # ---------- event 1: notify-delay ordering ----------
    say("")
    say("--- event 1: a fresh showtime appears ---")
    row_id = core.record_event(conn, DROP_ID, {
        "event_id": "evt-1", "kind": "new_showtime", "unit_id": "unit-1",
        "state": "available", "payload": {"label": "7:00 PM"},
    }, now=T0)
    say("record_event -> events.id=%d (idempotent re-detection would "
        "return the same id)" % row_id)

    outbox.dispatch(conn, channels, now=T0)
    say("pass @ t0: paid/telegram + paid/discord due now, "
        "free/telegram held by its 60s notify-delay")
    say("telegram.calls=%d discord.calls=%d" % (tg_channel.calls, dc_channel.calls))

    outbox.dispatch(conn, channels, now=T0 + 61)
    say("pass @ t0+61s: free/telegram's not_before has passed -> delivered")
    say("telegram.calls=%d discord.calls=%d" % (tg_channel.calls, dc_channel.calls))

    # ---------- event 2: broken-pipe + backoff, and a mid-flight STOP ----------
    say("")
    say("--- event 2: discord's transport misbehaves, and dave has second thoughts ---")
    t2 = T0 + 120
    dave = subscribe_and_verify(conn, "telegram", "dave-chat", "free", t2, "dave")

    core.record_event(conn, DROP_ID, {
        "event_id": "evt-2", "kind": "restock", "unit_id": "unit-1",
        "state": "available", "payload": {"label": "7:00 PM restocked"},
    }, now=t2)
    say("dave's row for evt-2 is queued but not due for 60s (free tier)")

    core.unsubscribe(conn, dave["unsub_token"], t2 + 1)
    say("dave unsubscribes at t2+1s, well before his row comes due")

    outbox.dispatch(conn, channels, now=t2)
    say("pass @ t2: discord raises BrokenPipeError (attempt 1) -> "
        "[PIPE] + [DELIVERY] logged, backoff scheduled")

    t3 = t2 + 61  # clears the 60s (30*2^1) backoff from attempt 1
    outbox.dispatch(conn, channels, now=t3)
    say("pass @ t2+61s: discord's retry raises ConnectionResetError "
        "(attempt 2) -> [PIPE] + [DELIVERY] again, backoff doubles; "
        "dave's now-due row is cancelled instead of sent (STOP honored)")
    cancelled = [e for e in log.recent(tag=log.TAG_DELIVERY) if e.fields.get("cancelled") == 1]
    say("cancelled-delivery log lines so far: %d" % len(cancelled))

    t4 = t3 + 121  # clears the 120s (30*2^2) backoff from attempt 2
    outbox.dispatch(conn, channels, now=t4)
    say("pass @ t3+121s: discord's third attempt succeeds -> delivered")
    say("discord.calls=%d" % dc_channel.calls)

    # ---------- event 3: total outage -> time-based ZERO-DELIVERED, then recovery ----------
    say("")
    say("--- event 3: every channel is down ---")
    t5 = t4 + 1
    core.record_event(conn, DROP_ID, {
        "event_id": "evt-3", "kind": "restock", "unit_id": "unit-1",
        "state": "available", "payload": {"label": "7:00 PM restocked again"},
    }, now=t5)

    tg_channel.always_fail(BrokenPipeError("simulated total outage"))
    dc_channel.always_fail(BrokenPipeError("simulated total outage"))
    say("both channels scripted to fail every send from here on")

    outbox.dispatch(conn, channels, now=t5)
    say("pass @ t5: first attempt on both channels fails -- this alone must NOT "
        "page (a single blip is not the alarm)")
    say("[ZERO-DELIVERED] entries so far: %d (must be 0)" %
        len(log.recent(tag=log.TAG_ZERO_DELIVERED)))

    deadline_hit = t5 + outbox.ZERO_DELIVERED_DEADLINE_SECONDS
    say("compressing time straight to the %.0fs zero-delivered deadline -- "
        "NOT a 24h TTL wait, that was the old twitchy behavior this wave fixed"
        % outbox.ZERO_DELIVERED_DEADLINE_SECONDS)
    outbox.dispatch(conn, channels, now=deadline_hit)

    zero_delivered = log.recent(tag=log.TAG_ZERO_DELIVERED)
    say("[ZERO-DELIVERED] entries logged at the deadline: %d (must be 1)" %
        len(zero_delivered))

    say("telegram recovers; its next scheduled retry succeeds")
    tg_channel.always_ok()
    outbox.dispatch(conn, channels, now=deadline_hit + 3600)
    recovered = [e for e in log.recent(tag=log.TAG_DELIVERY)
                 if e.fields.get("recovered_after_zero_delivered") == 1]
    say("recovered_after_zero_delivered=1 lines: %d (must be 1)" % len(recovered))

    say("")
    say("=== tag lines captured in platformd.log's ring buffer ===")
    for tag in (log.TAG_PIPE, log.TAG_DELIVERY, log.TAG_ZERO_DELIVERED, log.TAG_RATELIMIT):
        entries = log.recent(tag=tag)
        say("%s x%d" % (tag, len(entries)))
        for entry in entries:
            say("  " + entry.line)

    say("")
    say("this was a drill — no real channels, no real polls.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
