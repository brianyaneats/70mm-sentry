"""platformd.outbox — dispatch: backoff, TTL exhaustion, provider breaker,
mid-flight STOP honoring, and the [ZERO-DELIVERED] W5 alarm.

dispatch(conn, channels, now) pulls due pending/failed outbox rows and
calls channels[channel].send(address, message) for each. A channel
adapter must return True on success or raise on failure — it must never
return False silently (that would be indistinguishable from "nothing to
report"), so any non-True return is treated as a failure too.

Per-row isolation is load-bearing: one subscriber's channel raising must
never stop another subscriber (same or different channel) from being
tried in the same pass. Every row gets its own try/except; nothing here
lets one exception unwind the whole dispatch loop.

Every due row's subscription status is re-checked at send time (not just
once, at record_event() fan-out time): if the subscriber has since
unsubscribed or been disabled, the row is marked 'cancelled' and is never
sent, logged via [DELIVERY] with cancelled=1. This is what makes STOP
binding even for alerts that were already sitting in the outbox when the
unsubscribe happened.

ZERO-DELIVERED is time-based, not attempt-based (see
_check_zero_delivered_deadlines()'s docstring for the rationale) so it no
longer fires on a lone subscriber's first transient failure.

Webhook rows get one extra wrinkle: platformd.webhook is imported lazily,
only when a due row's channel is actually "webhook" (never at module
import time, and never for a telegram/discord/ntfy row) -- so this module
has no hard dependency on platformd.webhook existing, and a pass with
zero webhook subscribers never touches it. Those rows' send() gets a
context= kwarg carrying the subscription's webhook_secret plus the
event's own fields (so the real channel can sign the payload).

Terminal delivery failures -- a webhook.WebhookGone (410, receiver
deleted the endpoint) or a channels.PermanentDeliveryError (Discord's
404/410 for a deleted webhook, or any future channel's equivalent "this
is never coming back" signal) -- are both treated the same way: the row
goes straight to 'exhausted' (never entering the normal backoff/TTL retry
path, since a dead destination is never coming back and retrying it is
pure noise, not resilience), logged [DELIVERY] gone=1, and the owning
subscription is disabled (status='disabled') so the platform stops
fanning out to a destination that no longer exists. Neither is counted
against the provider breaker -- one subscriber's dead endpoint says
nothing about the provider itself being down. platformd.channels is
imported lazily too, inside the exception handler only, kept optional
(a missing module just means "not a permanent error") for the same
reason platformd.webhook is: this module must never fail to import, or
to dispatch a pass with zero permanent-error rows, just because one of
these sibling modules doesn't happen to be present.
"""

import json
import time

from platformd import log as _log

BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 3600
TTL_SECONDS = 24 * 3600

PROVIDER_BREAKER_FAIL_THRESHOLD = 5
PROVIDER_BREAKER_OPEN_SECONDS = 60

# How long a claimed ('in_flight') row belongs to the worker that claimed
# it. A worker that crashes mid-send leaves its rows in_flight; once the
# lease expires they become due again and another worker re-claims them.
# A crash AFTER the channel send but BEFORE the delivered-update therefore
# re-sends after the lease -- a duplicate alert, never a silent miss,
# which is the side of that trade this codebase always takes.
CLAIM_LEASE_SECONDS = 120

# How long an event is allowed to sit with zero successful deliveries
# before it's alarm-worthy. See _check_zero_delivered_deadlines().
ZERO_DELIVERED_DEADLINE_SECONDS = 300

# Provider-level breaker bookkeeping, in-process, keyed by (conn identity,
# channel name). The governor table's breaker columns are for per-source
# fetch politeness (platformd.governor); this is a separate, simpler,
# fixed-60s breaker over DELIVERY providers, so it's kept out of that
# table rather than overloading its schema. Keying on id(conn) keeps
# tests that build a fresh :memory: connection per case from bleeding
# breaker state into each other.
_provider_breakers = {}


def _breaker_key(conn, channel):
    return (id(conn), channel)


def _breaker_is_open(conn, channel, now):
    st = _provider_breakers.get(_breaker_key(conn, channel))
    if not st:
        return False
    open_until = st.get("open_until")
    if open_until is None:
        return False
    if now < open_until:
        return True
    # probe window has passed -- half-open: clear the streak and let the
    # next attempt through instead of staying dark forever.
    st["open_until"] = None
    st["fails"] = set()
    return False


def _breaker_record_failure(conn, channel, subscription_id, now):
    """Returns True the moment this failure trips the breaker open."""
    key = _breaker_key(conn, channel)
    st = _provider_breakers.setdefault(key, {"fails": set(), "open_until": None})
    st["fails"].add(subscription_id)
    if len(st["fails"]) >= PROVIDER_BREAKER_FAIL_THRESHOLD and st["open_until"] is None:
        st["open_until"] = now + PROVIDER_BREAKER_OPEN_SECONDS
        return True
    return False


def _breaker_record_success(conn, channel):
    st = _provider_breakers.get(_breaker_key(conn, channel))
    if st:
        st["fails"] = set()
        st["open_until"] = None


def reset_provider_breakers():
    """Test helper: clear all in-process provider breaker state."""
    _provider_breakers.clear()


# Poller event kinds read as jargon in a push notification.
_KIND_LABELS = {"opened": "site opened", "new_listing": "new site listed"}


def _format_message(row, public_url=None):
    """"<drop title>: <kind> — <unit label>", then one line each for what
    the event carries: the night, a booking link, and (when the public
    URL is configured) a one-click unsubscribe link -- an alert someone
    can't act on or stop is not a product.

    label prefers the event's own payload_json["label"] (what adapters'
    observation() calls it -- e.g. "7:00 PM", "Site A12"; see
    adapters/base.py), falling back to unit_id and then a generic
    "update" for the rare event that carries neither (a kind not tied to
    any one inventory unit).
    """
    payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
    label = payload.get("label") or row["unit_id"] or "update"
    kind = _KIND_LABELS.get(row["kind"], row["kind"])
    lines = ["%s: %s — %s" % (row["drop_title"], kind, label)]
    if payload.get("date"):
        lines.append("Night of %s" % payload["date"])
    if payload.get("url"):
        lines.append("Book: %s" % payload["url"])
    if public_url and row["unsub_token"]:
        lines.append("Stop these alerts: %s/unsubscribe?token=%s"
                     % (public_url.rstrip("/"), row["unsub_token"]))
    return "\n".join(lines)


def dispatch(conn, channels, now=None,
             zero_delivered_deadline_seconds=ZERO_DELIVERED_DEADLINE_SECONDS,
             public_url=None):
    """Run one dispatch pass. `now` must be passed explicitly by callers
    (tests never rely on wall-clock time / sleeps to cross due-boundaries).
    """
    if now is None:
        now = time.time()

    rows = conn.execute(
        "SELECT o.id AS id, o.event_row_id AS event_row_id, "
        "       o.subscription_id AS subscription_id, o.attempts AS attempts, "
        "       o.created_at AS created_at, "
        "       s.channel AS channel, s.address AS address, "
        "       s.status AS sub_status, s.webhook_secret AS webhook_secret, "
        "       s.unsub_token AS unsub_token, "
        "       e.drop_id AS drop_id, e.event_id AS event_id, "
        "       e.kind AS kind, e.unit_id AS unit_id, e.recorded_at AS recorded_at, "
        "       e.payload_json AS payload_json, d.title AS drop_title "
        "FROM outbox o "
        "JOIN subscriptions s ON s.id = o.subscription_id "
        "JOIN events e ON e.id = o.event_row_id "
        "JOIN drops d ON d.id = e.drop_id "
        "WHERE (o.status IN ('pending', 'failed') "
        "       OR (o.status = 'in_flight' AND o.claimed_at <= ?)) "
        "  AND o.next_attempt_at <= ? AND o.not_before <= ? "
        "ORDER BY o.id",
        (now - CLAIM_LEASE_SECONDS, now, now),
    ).fetchall()

    breaker_logged_this_pass = set()

    for row in rows:
        channel = row["channel"]

        if row["sub_status"] != "active":
            # STOP wins even for an alert that was already queued: never
            # call the channel, mark the row terminal, log it once.
            conn.execute(
                "UPDATE outbox SET status='cancelled' WHERE id=? "
                "AND status IN ('pending', 'failed', 'in_flight')",
                (row["id"],),
            )
            conn.commit()
            _log.log(_log.TAG_DELIVERY,
                      "subscription no longer active, cancelling queued alert",
                      channel=channel, subscription_id=row["subscription_id"],
                      cancelled=1)
            continue

        if _breaker_is_open(conn, channel, now):
            if channel not in breaker_logged_this_pass:
                _log.log(_log.TAG_DELIVERY, "provider breaker open, skipping channel",
                          channel=channel)
                breaker_logged_this_pass.add(channel)
            continue

        # Claim the row atomically before touching the channel: the
        # conditional UPDATE is the critical section, so two dispatch
        # workers scanning the same due set can never both send this row.
        # A worker that loses the claim just moves on.
        claim = conn.execute(
            "UPDATE outbox SET status='in_flight', claimed_at=? "
            "WHERE id=? AND (status IN ('pending', 'failed') "
            "                OR (status='in_flight' AND claimed_at <= ?))",
            (now, row["id"], now - CLAIM_LEASE_SECONDS),
        )
        conn.commit()
        if claim.rowcount == 0:
            continue

        webhook_mod = None
        context = None
        if channel == "webhook":
            # Lazy, per-row: this module must never fail to import (or
            # touch platformd.webhook at all) just because a pass happens
            # to contain zero webhook rows. Field names here match what
            # platformd.webhook.build_event_payload() reads via .get() --
            # event_id/kind/unit_id/drop_id/recorded_at (it falls back to
            # recorded_at when detected_at is absent, which it always is
            # here since platformd.core doesn't track a separate
            # detection timestamp yet).
            from platformd import webhook as webhook_mod
            context = {
                "webhook_secret": row["webhook_secret"],
                "event_id": row["event_id"],
                "kind": row["kind"],
                "drop_id": row["drop_id"],
                "unit_id": row["unit_id"],
                "recorded_at": row["recorded_at"],
            }

        try:
            adapter = channels[channel]
            if context is not None:
                ok = adapter.send(row["address"], _format_message(row, public_url),
                                  context=context)
            else:
                ok = adapter.send(row["address"], _format_message(row, public_url))
            if ok is not True:
                raise RuntimeError(
                    "channel %r send() returned %r, expected True" % (channel, ok))
        except Exception as exc:  # noqa: BLE001 -- must not stop the loop
            if _is_permanent_delivery_error(exc, webhook_mod):
                _handle_permanent_delivery_error(conn, row, exc, channel)
                continue
            _handle_failure(conn, row, exc, now, channel)
        else:
            _breaker_record_success(conn, channel)
            conn.execute(
                "UPDATE outbox SET status='delivered', delivered_at=?, "
                "attempts=attempts+1 WHERE id=?",
                (now, row["id"]),
            )
            conn.commit()
            _maybe_log_recovered_after_zero_delivered(
                conn, row["event_row_id"], channel, row["subscription_id"])

    _check_zero_delivered_deadlines(conn, now, zero_delivered_deadline_seconds)


def _is_permanent_delivery_error(exc, webhook_mod):
    """True iff exc is one of the two documented "gone for good" markers:
    webhook_mod.WebhookGone (webhook_mod is None unless this row's
    channel was "webhook" -- see dispatch()) or
    platformd.channels.PermanentDeliveryError. platformd.channels is
    imported lazily here, and only inside a try/except ImportError, so
    a pass with zero permanent-error rows -- or an environment where
    platformd.channels somehow isn't present -- never depends on it.
    """
    if webhook_mod is not None and isinstance(exc, webhook_mod.WebhookGone):
        return True
    try:
        from platformd import channels as channels_mod
    except ImportError:
        return False
    return isinstance(exc, channels_mod.PermanentDeliveryError)


def _handle_permanent_delivery_error(conn, row, exc, channel):
    """A destination that answers "gone for good" (webhook 410, Discord
    404/410, ...) is never coming back -- mark the row terminal
    immediately instead of entering the normal backoff/TTL retry path, so
    a dead destination doesn't get hammered for the full TTL_SECONDS
    window before finally being given up on. Never counted against the
    provider breaker either: one subscriber's endpoint disappearing says
    nothing about the provider being down.

    Also disables the owning subscription (status='disabled') -- the
    endpoint is gone, so the platform must stop fanning future events out
    to it too, not just give up on this one queued row.
    """
    attempts = row["attempts"] + 1
    conn.execute(
        "UPDATE outbox SET status='exhausted', attempts=?, "
        "last_error=? WHERE id=?",
        (attempts, str(exc)[:500], row["id"]),
    )
    conn.execute(
        "UPDATE subscriptions SET status='disabled' WHERE id=?",
        (row["subscription_id"],),
    )
    conn.commit()
    _log.log(_log.TAG_DELIVERY, "destination gone for good, disabling subscription",
              channel=channel, subscription_id=row["subscription_id"], gone=1)


def _handle_failure(conn, row, exc, now, channel):
    if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
        _log.log(_log.TAG_PIPE, "broken pipe/reset delivering to subscriber",
                  channel=channel, subscription_id=row["subscription_id"],
                  errno=getattr(exc, "errno", None), exc_class=type(exc).__name__)

    _log.log(_log.TAG_DELIVERY, "delivery failed",
              channel=channel, subscription_id=row["subscription_id"],
              exc_class=type(exc).__name__, error=str(exc)[:200])

    opened = _breaker_record_failure(conn, channel, row["subscription_id"], now)
    if opened:
        _log.log(_log.TAG_DELIVERY, "provider breaker opened",
                  channel=channel, open_seconds=PROVIDER_BREAKER_OPEN_SECONDS)

    attempts = row["attempts"] + 1
    age_seconds = now - row["created_at"]

    if age_seconds >= TTL_SECONDS:
        conn.execute(
            "UPDATE outbox SET status='exhausted', attempts=?, last_error=? "
            "WHERE id=?",
            (attempts, str(exc)[:500], row["id"]),
        )
        conn.commit()
        return

    backoff = _backoff_for(exc, attempts)
    conn.execute(
        "UPDATE outbox SET status='failed', attempts=?, next_attempt_at=?, "
        "last_error=? WHERE id=?",
        (attempts, now + backoff, str(exc)[:500], row["id"]),
    )
    conn.commit()


def _backoff_for(exc, attempts):
    """Seconds to wait before the next attempt.

    A provider that answers 429 with a retry_after is telling us exactly
    when it will accept traffic again -- guessing with our own doubling
    curve instead is how you turn one rate-limit into a rolling one. So
    an explicit hint WINS, clamped to the same cap that bounds the
    default curve (a hostile or fat-fingered header must not park a row
    for a week). Everything else keeps the exponential backoff.
    """
    retry_after = getattr(exc, "retry_after", None)
    if isinstance(retry_after, (int, float)) and retry_after > 0:
        return min(float(retry_after), BACKOFF_CAP_SECONDS)
    return min(BACKOFF_BASE_SECONDS * (2 ** attempts), BACKOFF_CAP_SECONDS)


def _check_zero_delivered_deadlines(conn, now, deadline_seconds):
    """Fire [ZERO-DELIVERED] for any not-yet-logged event whose deadline
    has passed, regardless of whether this dispatch() pass happened to
    touch any of its rows.

    An event fires exactly once, iff:
      - now >= recorded_at + deadline_seconds
      - no row for it has ever succeeded (no 'delivered' row)
      - it has at least one non-cancelled row (an event whose every
        subscriber unsubscribed has nothing left to alarm about)

    Rationale: alert latency IS the product. Five minutes of nobody
    having heard about a drop is a page-worthy failure; the same silence
    24 hours later (the old TTL-exhaustion-triggered behavior) is
    useless -- by then everyone who cared has moved on. But a single
    transient blip that recovers in 30 seconds is neither: it shouldn't
    page anyone either, which is exactly why this is time-based instead
    of attempt-based (the old "first failure" trigger was paging on a
    lone subscriber's first hiccup and was flagged as too twitchy).

    If a delivery later succeeds after the alarm already fired, exactly
    one recovery line is logged (see
    _maybe_log_recovered_after_zero_delivered) -- and this function never
    re-fires for that event afterwards, because zero_delivered_logged is
    permanent once set.
    """
    candidates = conn.execute(
        "SELECT id, drop_id, event_id FROM events "
        "WHERE zero_delivered_logged = 0 AND recorded_at <= ?",
        (now - deadline_seconds,),
    ).fetchall()

    for event in candidates:
        statuses = [r["status"] for r in conn.execute(
            "SELECT status FROM outbox WHERE event_row_id = ?", (event["id"],)
        ).fetchall()]
        if not statuses:
            continue  # no subscribers -- nothing to have "zero delivered" against
        if any(s == "delivered" for s in statuses):
            continue  # succeeded at least once -- never fires
        if all(s == "cancelled" for s in statuses):
            continue  # every subscriber unsubscribed -- nothing to alarm about

        _log.log(_log.TAG_ZERO_DELIVERED, "event delivered to 0 subscribers",
                  drop_id=event["drop_id"], event_id=event["event_id"],
                  subscriber_count=len(statuses))
        conn.execute(
            "UPDATE events SET zero_delivered_logged = 1 WHERE id = ?",
            (event["id"],),
        )
        conn.commit()


def _maybe_log_recovered_after_zero_delivered(conn, event_row_id, channel,
                                               subscription_id):
    """If this success is the first one to land after [ZERO-DELIVERED]
    already fired for its event, log exactly one recovery line. Never
    re-logs for the same event even if further rows succeed afterwards.
    """
    event = conn.execute(
        "SELECT zero_delivered_logged, recovered_after_zero_logged "
        "FROM events WHERE id = ?",
        (event_row_id,),
    ).fetchone()
    if event is None:
        return
    if not event["zero_delivered_logged"] or event["recovered_after_zero_logged"]:
        return

    _log.log(_log.TAG_DELIVERY, "delivery succeeded after zero-delivered alarm",
              channel=channel, subscription_id=subscription_id,
              recovered_after_zero_delivered=1)
    conn.execute(
        "UPDATE events SET recovered_after_zero_logged = 1 WHERE id = ?",
        (event_row_id,),
    )
    conn.commit()
