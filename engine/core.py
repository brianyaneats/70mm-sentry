"""engine.core — drops catalog, subscription lifecycle, and the
event-log + ledger + outbox invariant.

record_event() is THE invariant this whole phase exists to protect: one
event insert, one ledger advance, and one pending outbox row per active
subscriber of that drop all happen in a single transaction, or none of
them do. Duplicate (drop_id, event_id) pairs are idempotent no-ops so a
re-detected event (poller retry, at-least-once redelivery upstream)
never double-fans-out to subscribers.

A drop must be registered and active before anything can be recorded
against it (register_drop()) -- record_event() raises ValueError
otherwise. That's a programming error (an adapter that forgot to
register its own drop, or a stale drop_id), not a runtime failure, so
it isn't logged under any of the standard grep tags.

Subscriptions move through a small lifecycle: pending_verify -> active
-> disabled. subscribe() mints a fresh row (or hands back an existing
active one -- see its docstring for the exact dedup rule), verify()
activates it and mints its unsub_token, unsubscribe() disables it by
token. record_event()'s fan-out only ever targets status='active' rows;
engine.outbox.dispatch() re-checks status per-row at send time so a
STOP is honored even for alerts already sitting in the outbox.
"""

import datetime as dt
import json
import random
import secrets
import time

from engine import log as _log

# Channel names a subscription may carry. Senders live outside the engine
# (notify/, or anything with send(address, message[, context])).
VALID_CHANNELS = ("telegram", "discord", "ntfy", "webhook")

# Tier -> notify-delay in seconds, applied to an outbox row's not_before.
# Paid subscribers see it the instant it's recorded; free subscribers are
# embargoed long enough for paid to have first look.
TIER_DELAYS = {"paid": 0, "free": 60}
DEFAULT_TIER_DELAY = TIER_DELAYS["free"]

VERIFY_CODE_DIGITS = 6
MAX_VERIFY_ATTEMPTS = 5

# A verify code is a proof-of-control challenge, not a standing
# credential -- 15 minutes is long enough for a real subscriber to read
# their channel and type six digits, short enough that a code leaked or
# guessed later (address reused, screenshot shared) is dead by then.
VERIFY_CODE_TTL_SECONDS = 15 * 60


class VerifyCodeExpired(Exception):
    """Raised by verify() when subscription_id's code existed and was a
    real pending_verify row, but its VERIFY_CODE_TTL_SECONDS window has
    elapsed. Deliberately distinct from a wrong-code guess: callers must
    not count this against MAX_VERIFY_ATTEMPTS (a subscriber who took too
    long to check their phone made no guess at all), and it must map to
    its own outcome one layer up (app.api returns 403 "code
    expired -- subscribe again", not "incorrect code").

    The row itself is left untouched (still pending_verify) -- subscribe()
    is what disables an expired pending row, the moment someone tries to
    replace it with a fresh one; see its docstring.
    """


# ---------------------------------------------------------------- drops --

def register_drop(conn, drop_id, title, description=None, source=None,
                   risk_tier=None, status="active", now=None, adapter=None,
                   target=None):
    """Register drop_id in the catalog. Returns the drop row.

    Idempotent: if drop_id is already registered this is a no-op that
    returns the existing row untouched -- register_drop() never
    resurrects a paused drop back to active or edits a live catalog
    entry's fields. (A separate "update/pause a drop" operation is out
    of scope for this wave; nothing here needs it yet.)

    adapter/target: set for a drop the poller should watch (see
    active_watches()); target is the adapter's opaque target dict.
    """
    if status not in ("active", "paused"):
        raise ValueError("unknown drop status %r (expected 'active' or "
                          "'paused')" % (status,))
    if now is None:
        now = time.time()

    existing = get_drop(conn, drop_id)
    if existing is not None:
        return existing

    conn.execute(
        "INSERT INTO drops "
        "(id, title, description, source, risk_tier, status, created_at, "
        " adapter, target_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (drop_id, title, description, source, risk_tier, status, now,
         adapter, json.dumps(target) if target is not None else None),
    )
    conn.commit()
    return get_drop(conn, drop_id)


def get_drop(conn, drop_id):
    """Return the drop row for drop_id, or None if it isn't registered."""
    return conn.execute(
        "SELECT * FROM drops WHERE id = ?", (drop_id,)
    ).fetchone()


def list_drops(conn, status=None):
    """All drops, oldest-registered first. Filter by status if given."""
    if status is None:
        return conn.execute("SELECT * FROM drops ORDER BY created_at").fetchall()
    return conn.execute(
        "SELECT * FROM drops WHERE status = ? ORDER BY created_at", (status,)
    ).fetchall()


# --------------------------------------------------------- subscriptions --

def get_subscription(conn, subscription_id):
    """Return the subscription row, or None if subscription_id is unknown."""
    return conn.execute(
        "SELECT * FROM subscriptions WHERE id = ?", (subscription_id,)
    ).fetchone()


def _generate_verify_code():
    # random.SystemRandom, per spec -- this is a proof-of-control code
    # sent back over the subscriber's own channel, not a cryptographic
    # secret, but SystemRandom costs nothing and is stdlib.
    n = random.SystemRandom().randrange(10 ** VERIFY_CODE_DIGITS)
    return "%0*d" % (VERIFY_CODE_DIGITS, n)


# A destination gets at most one verification code per this window, and
# only ever one live code at a time. Without it, every subscribe call for
# the same address minted and SENT another code: an unauthenticated
# message cannon at any ntfy topic / Discord webhook / URL, and N live
# rows meant N parallel 6-digit codes to brute-force.
RESEND_COOLDOWN_SECONDS = 60


class ResendCooldown(Exception):
    """A code went to this destination less than RESEND_COOLDOWN_SECONDS
    ago for a different watch. Raised rather than silently minting, so
    the API can answer 429 and the destination gets one code a minute
    no matter how many campgrounds a caller cycles through."""

    def __init__(self, retry_after):
        super().__init__("verification code sent recently")
        self.retry_after = retry_after


def subscribe(conn, drop_id, channel, address, tier, now, webhook_secret=None,
              date_from=None, date_to=None):
    """Register (channel, address)'s interest in drop_id. Returns the row.

    A watch is (drop_id, channel, address, date_from, date_to); the dates
    ("YYYY-MM-DD", inclusive, None = every date) narrow which events fan
    out to it. Everything below that says "triple" means that key.

    Dedup: an existing status='active' subscription for the same
    (drop_id, channel, address) is returned as-is -- no new row, no new
    verify_code minted, its unsub_token is left alone. Any other prior
    state for that triple (pending_verify never completed, or disabled)
    does NOT dedupe; a fresh pending_verify row is inserted, which is
    exactly what lets someone who let their verification lapse, or who
    unsubscribed, subscribe again from scratch.

    A pending_verify row for the same triple whose verify code has
    expired (see VERIFY_CODE_TTL_SECONDS) is a dead end -- its code can
    never be verified again -- so it is explicitly disabled here before
    the fresh row is minted, rather than left to accumulate as orphaned
    pending rows every time someone re-tries a lapsed subscribe. A
    still-live (unexpired) pending row is left alone, unchanged from the
    prior behavior.

    webhook_secret: only meaningful for channel="webhook" -- the caller
    (app.api, via notify.webhook.generate_secret()) mints it and
    passes it through; stored as-is, never generated or interpreted here
    so engine.core carries no dependency on notify.webhook.
    """
    if tier not in TIER_DELAYS:
        raise ValueError("unknown tier %r (expected one of %r)" %
                          (tier, tuple(TIER_DELAYS)))

    # Risk-tier policy, enforced here rather than trusted to call sites:
    # a YELLOW source is tolerated-gray (it works, it may go dark, and
    # nobody has authorized us to resell it), so it may never carry a
    # paid subscription; a RED source may never be watched at all. The
    # platform README claimed this was enforced in code well before it
    # actually was -- red-team audit 2026-08-04 verified a paid
    # subscription against a yellow drop succeeded. Documented policy
    # that code doesn't enforce is just a wish.
    drop = get_drop(conn, drop_id)
    if drop is not None:
        risk = (drop["risk_tier"] or "").lower()
        if risk == "red":
            raise ValueError(
                "drop %r is risk_tier=red; it must never be watched" % drop_id)
        if risk == "yellow" and tier == "paid":
            raise ValueError(
                "drop %r is risk_tier=yellow; paid subscriptions are not "
                "allowed on a tolerated-gray source" % drop_id)

    key_sql = ("drop_id = ? AND channel = ? AND address = ? "
               "AND date_from IS ? AND date_to IS ?")
    key = (drop_id, channel, address, date_from, date_to)
    existing = conn.execute(
        "SELECT * FROM subscriptions WHERE " + key_sql + " AND status = 'active'",
        key,
    ).fetchone()
    if existing is not None:
        return existing

    pending = conn.execute(
        "SELECT * FROM subscriptions WHERE " + key_sql +
        " AND status = 'pending_verify' ORDER BY created_at DESC",
        key,
    ).fetchall()
    if pending and now - pending[0]["created_at"] < RESEND_COOLDOWN_SECONDS:
        # Inside the cooldown: hand back the live row, mint and send
        # nothing (callers detect "fresh" via created_at == now).
        return pending[0]
    last_sent = conn.execute(
        "SELECT MAX(created_at) FROM subscriptions "
        "WHERE channel = ? AND address = ? AND verify_code IS NOT NULL",
        (channel, address),
    ).fetchone()[0]
    if last_sent is not None and now - last_sent < RESEND_COOLDOWN_SECONDS:
        raise ResendCooldown(RESEND_COOLDOWN_SECONDS - (now - last_sent))
    # Past the cooldown, the new code supersedes every earlier one --
    # live or expired -- so a destination never has two codes in play.
    for old in pending:
        conn.execute(
            "UPDATE subscriptions SET status='disabled' WHERE id=?",
            (old["id"],),
        )
    conn.commit()

    code = _generate_verify_code()
    cur = conn.execute(
        "INSERT INTO subscriptions "
        "(drop_id, channel, address, tier, status, verify_code, "
        " verify_attempts, webhook_secret, created_at, date_from, date_to) "
        "VALUES (?, ?, ?, ?, 'pending_verify', ?, 0, ?, ?, ?, ?)",
        (drop_id, channel, address, tier, code, webhook_secret, now,
         date_from, date_to),
    )
    conn.commit()
    return get_subscription(conn, cur.lastrowid)


def verify(conn, subscription_id, code, now):
    """Attempt to verify a pending_verify subscription. Returns the row
    as it stands after the attempt.

    Match: activates it (status='active'), stamps verified_at=now, mints
    a fresh unsub_token (secrets.token_hex(16), stdlib).

    Mismatch: increments verify_attempts. The MAX_VERIFY_ATTEMPTS-th
    (5th) wrong attempt disables the subscription instead (status=
    'disabled') and logs [RATELIMIT] with the subscription_id only --
    never the code, never the address. A disabled pending subscription
    cannot be retried; subscribe() must be called again from scratch.

    Expired: raises VerifyCodeExpired if now is VERIFY_CODE_TTL_SECONDS
    or more past the row's created_at, checked BEFORE the code is even
    compared -- a stale code offers no chance to guess right or wrong,
    it's simply dead. Does not touch verify_attempts and does not change
    the row's status; subscribe() is what retires an expired pending row
    (see its docstring), the next time someone tries to replace it.

    Raises ValueError if subscription_id doesn't exist or isn't
    currently pending_verify -- a programming error (callers are
    expected to know the state of the row they're verifying; this isn't
    a public probe endpoint that should be usable to test guesses
    against an already-active or already-disabled row).
    """
    row = get_subscription(conn, subscription_id)
    if row is None or row["status"] != "pending_verify":
        raise ValueError(
            "subscription %r is not awaiting verification" % (subscription_id,))

    if now - row["created_at"] >= VERIFY_CODE_TTL_SECONDS:
        raise VerifyCodeExpired(
            "subscription %r's verify code expired" % (subscription_id,))

    if code == row["verify_code"]:
        conn.execute(
            "UPDATE subscriptions SET status='active', verified_at=?, "
            "unsub_token=? WHERE id=?",
            (now, secrets.token_hex(16), subscription_id),
        )
        conn.commit()
        return get_subscription(conn, subscription_id)

    attempts = row["verify_attempts"] + 1
    if attempts >= MAX_VERIFY_ATTEMPTS:
        conn.execute(
            "UPDATE subscriptions SET verify_attempts=?, status='disabled' "
            "WHERE id=?",
            (attempts, subscription_id),
        )
        conn.commit()
        _log.log(_log.TAG_RATELIMIT,
                  "verify attempts exhausted, subscription disabled",
                  subscription_id=subscription_id)
    else:
        conn.execute(
            "UPDATE subscriptions SET verify_attempts=? WHERE id=?",
            (attempts, subscription_id),
        )
        conn.commit()

    return get_subscription(conn, subscription_id)


def unsubscribe(conn, token, now):
    """Disable the subscription owning unsub_token `token`. Returns the
    updated row, or None if the token doesn't match anything.

    A miss is a no-op, not an error: unsub links get clicked twice,
    replayed by mail-client link scanners, etc.

    `now` is accepted for signature symmetry with the rest of the
    lifecycle (subscribe/verify both take it) even though there's
    currently no disabled_at column to stamp with it.
    """
    del now  # not persisted yet; see docstring
    row = conn.execute(
        "SELECT * FROM subscriptions WHERE unsub_token = ?", (token,)
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE subscriptions SET status='disabled' WHERE id=?", (row["id"],)
    )
    conn.commit()
    return get_subscription(conn, row["id"])


# -------------------------------------------------------------- events --

def record_event(conn, drop_id, event, now=None):
    """Record one event for drop_id, atomically.

    event: dict with keys
        event_id   -- unique (with drop_id) id for dedup/idempotency
        kind       -- event kind, e.g. "new_showtime", "restock", "returned"
        unit_id    -- inventory unit this event concerns (optional)
        state      -- normalized state for the ledger (optional)
        payload    -- arbitrary JSON-serializable dict passed to copy (optional)
        recorded_at -- override the event's timestamp (optional; defaults to now)

    Returns the events.id row id. If (drop_id, event_id) was already
    recorded, this is a no-op that returns the existing row id — no new
    ledger write, no new outbox rows, no log line (idempotent
    re-detection is expected, not a failure).

    Raises ValueError if drop_id isn't a registered, active drop. This
    is a programming error -- the caller/adapter owns registering its
    drop before ever recording events against it -- so it isn't logged
    under any grep tag, just raised.
    """
    if now is None:
        now = time.time()

    drop = get_drop(conn, drop_id)
    if drop is None:
        raise ValueError(
            "drop_id %r is not registered -- call register_drop() first" %
            (drop_id,))
    if drop["status"] != "active":
        raise ValueError(
            "drop_id %r is %s, not active -- cannot record events against "
            "a non-active drop" % (drop_id, drop["status"]))

    event_id = event["event_id"]
    existing = conn.execute(
        "SELECT id FROM events WHERE drop_id = ? AND event_id = ?",
        (drop_id, event_id),
    ).fetchone()
    if existing is not None:
        return existing["id"]

    kind = event["kind"]
    unit_id = event.get("unit_id")
    state = event.get("state")
    recorded_at = event.get("recorded_at", now)
    payload_json = json.dumps(event.get("payload", {}))

    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO events "
            "(drop_id, event_id, kind, unit_id, payload_json, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (drop_id, event_id, kind, unit_id, payload_json, recorded_at),
        )
        event_row_id = cur.lastrowid

        if unit_id is not None:
            ever_available = 1 if state == "available" else 0
            conn.execute(
                "INSERT INTO ledger "
                "(drop_id, unit_id, last_state, ever_available, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(drop_id, unit_id) DO UPDATE SET "
                "  last_state = excluded.last_state, "
                "  ever_available = MAX(ledger.ever_available, "
                "                        excluded.ever_available), "
                "  updated_at = excluded.updated_at",
                (drop_id, unit_id, state, ever_available, recorded_at),
            )

        # A watch with dates only hears about events on those dates; a
        # destination with several overlapping watches gets ONE alert.
        # Paid = the row's own tier or an active entitlement for the
        # destination -- except on a yellow drop, which is never sold.
        date = event.get("date") or event.get("payload", {}).get("date")
        subs = conn.execute(
            "SELECT s.id, s.tier, s.channel, s.address, "
            "       e.status AS entitlement FROM subscriptions s "
            "LEFT JOIN entitlements e "
            "  ON e.channel = s.channel AND e.address = s.address "
            "WHERE s.drop_id = ? AND s.status = 'active' "
            "  AND (? IS NULL OR s.date_from IS NULL OR s.date_from <= ?) "
            "  AND (? IS NULL OR s.date_to IS NULL OR s.date_to >= ?) "
            "ORDER BY s.id",
            (drop_id, date, date, date, date),
        ).fetchall()
        sellable = (drop["risk_tier"] or "").lower() != "yellow"
        seen = set()
        for sub in subs:
            if (sub["channel"], sub["address"]) in seen:
                continue
            seen.add((sub["channel"], sub["address"]))
            tier = sub["tier"]
            if sellable and sub["entitlement"] == "active":
                tier = "paid"
            delay = TIER_DELAYS.get(tier, DEFAULT_TIER_DELAY)
            not_before = recorded_at + delay
            conn.execute(
                "INSERT INTO outbox "
                "(event_row_id, subscription_id, status, attempts, "
                " next_attempt_at, not_before, created_at) "
                "VALUES (?, ?, 'pending', 0, ?, ?, ?)",
                (event_row_id, sub["id"], not_before, not_before, recorded_at),
            )
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()

    return event_row_id


# --------------------------------------------------------- entitlements --

def is_entitled(conn, channel, address):
    row = conn.execute(
        "SELECT status FROM entitlements WHERE channel = ? AND address = ?",
        (channel, address)).fetchone()
    return row is not None and row["status"] == "active"


def grant_entitlement(conn, subscription_id, stripe_customer,
                      stripe_subscription, now):
    """Checkout completed for subscription_id: its destination goes paid.
    Returns the (channel, address) granted, or None for an unknown id."""
    sub = get_subscription(conn, subscription_id)
    if sub is None:
        return None
    conn.execute(
        "INSERT INTO entitlements (channel, address, status, stripe_customer, "
        "stripe_subscription, updated_at) VALUES (?, ?, 'active', ?, ?, ?) "
        "ON CONFLICT(channel, address) DO UPDATE SET status = 'active', "
        "  stripe_customer = excluded.stripe_customer, "
        "  stripe_subscription = excluded.stripe_subscription, "
        "  updated_at = excluded.updated_at",
        (sub["channel"], sub["address"], stripe_customer,
         stripe_subscription, now))
    conn.commit()
    return sub["channel"], sub["address"]


def set_entitlement_by_stripe_subscription(conn, stripe_subscription,
                                           active, now):
    """Stripe says a subscription changed state (renewed, cancelled,
    unpaid). Returns how many entitlements it touched."""
    cur = conn.execute(
        "UPDATE entitlements SET status = ?, updated_at = ? "
        "WHERE stripe_subscription = ?",
        ("active" if active else "inactive", now, stripe_subscription))
    conn.commit()
    return cur.rowcount


class Entitlements:
    """The narrow write interface billing/ uses (see billing.stripe):
    the only way a payment provider changes who is paid."""

    def __init__(self, conn):
        self._conn = conn

    def grant(self, subscription_id, stripe_customer, stripe_subscription,
              now):
        return grant_entitlement(self._conn, subscription_id,
                                 stripe_customer, stripe_subscription, now)

    def set_by_stripe_subscription(self, stripe_subscription, active, now):
        return set_entitlement_by_stripe_subscription(
            self._conn, stripe_subscription, active, now)


# ---------------------------------------------------------------- watches --

def active_watches(conn, today, horizon_days=366):
    """Poll work implied by live subscriptions: one watch per adapter-
    backed drop with at least one active subscription, over the union of
    their date ranges clamped to [today, today + horizon_days). A drop
    with nobody watching it costs no upstream requests at all."""
    end = today + dt.timedelta(days=horizon_days)
    rows = conn.execute(
        "SELECT d.id, d.adapter, d.target_json, s.date_from, s.date_to "
        "FROM drops d JOIN subscriptions s ON s.drop_id = d.id "
        "WHERE d.adapter IS NOT NULL AND d.status = 'active' "
        "  AND s.status = 'active' AND s.date_to IS NOT NULL "
        "  AND s.date_to >= ?",
        (today.isoformat(),)).fetchall()
    watches = {}
    for r in rows:
        w = watches.setdefault(r["id"], {
            "drop_id": r["id"], "adapter": r["adapter"],
            "target": json.loads(r["target_json"]), "dates": set()})
        day = max(dt.date.fromisoformat(r["date_from"] or r["date_to"]), today)
        last = min(dt.date.fromisoformat(r["date_to"]), end)
        while day <= last:
            w["dates"].add(day.isoformat())
            day += dt.timedelta(days=1)
    for w in watches.values():
        w["dates"] = sorted(w["dates"])
    return [w for w in watches.values() if w["dates"]]
