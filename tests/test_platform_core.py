"""Unit tests for platformd.core — drops catalog, subscription lifecycle,
and the event/ledger/outbox invariant.

sqlite3 ':memory:' only. No network, no sleeps; every clock value is
passed explicitly as `now=`.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from platformd import core, db, log  # noqa: E402

DROP = "drop-1"
T0 = 1_700_000_000.0


class FlakyConnProxy:
    """Wraps a real sqlite3 connection, raising on the first execute()
    whose SQL starts with `boom_on_prefix`. Used to force an error mid
    transaction without depending on sqlite3.Connection's C-level
    internals allowing attribute monkeypatching."""

    def __init__(self, conn, boom_on_prefix):
        self._conn = conn
        self._boom_on_prefix = boom_on_prefix
        self.sql_seen = []

    def execute(self, sql, params=()):
        self.sql_seen.append(sql)
        if sql.strip().startswith(self._boom_on_prefix):
            raise RuntimeError("boom mid-transaction")
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def counts(conn):
    return {
        "events": conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"],
        "ledger": conn.execute("SELECT COUNT(*) AS n FROM ledger").fetchone()["n"],
        "outbox": conn.execute("SELECT COUNT(*) AS n FROM outbox").fetchone()["n"],
    }


def active_sub(conn, drop_id, channel, address, tier, now):
    """Test helper: subscribe + verify in one step, returning the
    subscription id of the resulting active row."""
    row = core.subscribe(conn, drop_id, channel, address, tier, now)
    verified = core.verify(conn, row["id"], row["verify_code"], now)
    assert verified["status"] == "active"
    return verified["id"]


class DropsCatalogTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")

    def test_register_get_and_list(self):
        row = core.register_drop(
            self.conn, DROP, "Drop One", description="d", source="fandango",
            risk_tier="low", now=T0)
        self.assertEqual(row["id"], DROP)
        self.assertEqual(row["status"], "active")

        self.assertEqual(core.get_drop(self.conn, DROP)["id"], DROP)
        self.assertIsNone(core.get_drop(self.conn, "nope"))

        core.register_drop(self.conn, "drop-2", "Drop Two", status="paused", now=T0)
        all_drops = core.list_drops(self.conn)
        self.assertEqual(len(all_drops), 2)
        active_only = core.list_drops(self.conn, status="active")
        self.assertEqual([r["id"] for r in active_only], [DROP])

    def test_register_drop_is_idempotent(self):
        first = core.register_drop(self.conn, DROP, "Drop One", now=T0)
        second = core.register_drop(self.conn, DROP, "A Different Title", now=T0 + 5)
        self.assertEqual(second["title"], first["title"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM drops").fetchone()["n"], 1)

    def test_unknown_status_rejected(self):
        with self.assertRaises(ValueError):
            core.register_drop(self.conn, DROP, "Drop One", status="bogus", now=T0)


class SubscriptionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        core.register_drop(self.conn, DROP, "Drop One", now=T0)
        log.clear()

    def test_subscribe_creates_pending_verify_with_six_digit_code(self):
        row = core.subscribe(self.conn, DROP, "telegram", "a", "paid", T0)
        self.assertEqual(row["status"], "pending_verify")
        self.assertEqual(row["verify_attempts"], 0)
        self.assertIsNone(row["unsub_token"])
        self.assertRegex(row["verify_code"], r"^\d{6}$")

    def test_subscribe_dedupes_only_an_existing_active_subscription(self):
        sub_id = active_sub(self.conn, DROP, "telegram", "a", "paid", T0)
        again = core.subscribe(self.conn, DROP, "telegram", "a", "paid", T0 + 5)
        self.assertEqual(again["id"], sub_id)
        self.assertEqual(again["status"], "active")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM subscriptions"
            ).fetchone()["n"], 1)

    def test_resubscribe_inside_cooldown_returns_live_row_without_new_code(self):
        # Each call used to mint AND send another code: a free message
        # cannon at any destination, and N live codes to brute-force.
        pending = core.subscribe(self.conn, DROP, "telegram", "a", "paid", T0)
        again = core.subscribe(self.conn, DROP, "telegram", "a", "paid", T0 + 1)
        self.assertEqual(again["id"], pending["id"])
        self.assertEqual(again["verify_code"], pending["verify_code"])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM subscriptions"
            ).fetchone()["n"], 1)

    def test_resubscribe_after_cooldown_supersedes_the_live_code(self):
        first = core.subscribe(self.conn, DROP, "telegram", "a", "paid", T0)
        later = T0 + core.RESEND_COOLDOWN_SECONDS
        second = core.subscribe(self.conn, DROP, "telegram", "a", "paid", later)
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(core.get_subscription(self.conn, first["id"])["status"],
                         "disabled")
        live = self.conn.execute(
            "SELECT COUNT(*) AS n FROM subscriptions "
            "WHERE status='pending_verify'").fetchone()["n"]
        self.assertEqual(live, 1, "a destination never has two codes in play")

    def test_subscribe_stores_webhook_secret_when_given(self):
        row = core.subscribe(self.conn, DROP, "webhook", "https://x.example/hook",
                              "paid", T0, webhook_secret="s3cr3t-value")
        self.assertEqual(row["webhook_secret"], "s3cr3t-value")

        # core itself never generates or interprets it -- omitting it
        # (the non-webhook-channel case) just stores NULL.
        row2 = core.subscribe(self.conn, DROP, "telegram", "b", "paid", T0)
        self.assertIsNone(row2["webhook_secret"])

    def test_subscribe_unknown_tier_rejected(self):
        with self.assertRaises(ValueError):
            core.subscribe(self.conn, DROP, "telegram", "a", "vip", T0)

    def test_verify_correct_code_activates_and_mints_unsub_token(self):
        row = core.subscribe(self.conn, DROP, "telegram", "a", "paid", T0)
        verified = core.verify(self.conn, row["id"], row["verify_code"], T0 + 1)
        self.assertEqual(verified["status"], "active")
        self.assertEqual(verified["verified_at"], T0 + 1)
        self.assertIsNotNone(verified["unsub_token"])

    def test_verify_wrong_code_increments_attempts_without_disabling(self):
        row = core.subscribe(self.conn, DROP, "telegram", "a", "paid", T0)
        wrong = "000000" if row["verify_code"] != "000000" else "111111"
        result = core.verify(self.conn, row["id"], wrong, T0)
        self.assertEqual(result["status"], "pending_verify")
        self.assertEqual(result["verify_attempts"], 1)
        self.assertEqual(log.recent(tag=log.TAG_RATELIMIT), [])

    def test_verify_exhausts_after_five_wrong_attempts(self):
        row = core.subscribe(self.conn, DROP, "telegram", "a", "paid", T0)
        wrong = "000000" if row["verify_code"] != "000000" else "111111"
        result = None
        for _ in range(core.MAX_VERIFY_ATTEMPTS):
            result = core.verify(self.conn, row["id"], wrong, T0)
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["verify_attempts"], core.MAX_VERIFY_ATTEMPTS)

        ratelimit_logs = log.recent(tag=log.TAG_RATELIMIT)
        self.assertEqual(len(ratelimit_logs), 1)
        self.assertIn(str(row["id"]), ratelimit_logs[0].line)

        # disabled means dead-end: a correct code no longer works, a
        # fresh subscribe() is required instead.
        with self.assertRaises(ValueError):
            core.verify(self.conn, row["id"], row["verify_code"], T0)

    def test_verify_unknown_or_non_pending_subscription_raises(self):
        with self.assertRaises(ValueError):
            core.verify(self.conn, 9999, "000000", T0)

        sub_id = active_sub(self.conn, DROP, "telegram", "a", "paid", T0)
        with self.assertRaises(ValueError):
            core.verify(self.conn, sub_id, "000000", T0)

    def test_verify_within_ttl_still_succeeds(self):
        row = core.subscribe(self.conn, DROP, "telegram", "a", "paid", T0)
        verified = core.verify(
            self.conn, row["id"], row["verify_code"],
            T0 + core.VERIFY_CODE_TTL_SECONDS - 1)
        self.assertEqual(verified["status"], "active")

    def test_verify_past_ttl_raises_expired_without_side_effects(self):
        row = core.subscribe(self.conn, DROP, "telegram", "a", "paid", T0)
        with self.assertRaises(core.VerifyCodeExpired):
            core.verify(self.conn, row["id"], row["verify_code"],
                        T0 + core.VERIFY_CODE_TTL_SECONDS)

        # expiry is distinct from a wrong-code guess: no attempt is
        # counted, and the row is left exactly as subscribe() minted it
        # (subscribe() is what retires it, not verify()).
        still_pending = core.get_subscription(self.conn, row["id"])
        self.assertEqual(still_pending["status"], "pending_verify")
        self.assertEqual(still_pending["verify_attempts"], 0)

    def test_expired_pending_subscription_is_disabled_and_replaced_on_resubscribe(self):
        first = core.subscribe(self.conn, DROP, "telegram", "a", "paid", T0)
        later = T0 + core.VERIFY_CODE_TTL_SECONDS + 1

        second = core.subscribe(self.conn, DROP, "telegram", "a", "paid", later)

        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(second["status"], "pending_verify")
        self.assertRegex(second["verify_code"], r"^\d{6}$")

        stale = core.get_subscription(self.conn, first["id"])
        self.assertEqual(stale["status"], "disabled")

        # and the fresh row verifies normally
        verified = core.verify(self.conn, second["id"], second["verify_code"], later)
        self.assertEqual(verified["status"], "active")

    def test_still_valid_pending_subscription_is_untouched_by_resubscribe(self):
        first = core.subscribe(self.conn, DROP, "telegram", "a", "paid", T0)
        core.subscribe(self.conn, DROP, "telegram", "a", "paid", T0 + 1)

        still_there = core.get_subscription(self.conn, first["id"])
        self.assertEqual(still_there["status"], "pending_verify")

    def test_unsubscribe_disables_and_unknown_token_is_a_no_op(self):
        row = core.subscribe(self.conn, DROP, "telegram", "a", "paid", T0)
        verified = core.verify(self.conn, row["id"], row["verify_code"], T0)
        token = verified["unsub_token"]

        result = core.unsubscribe(self.conn, token, T0 + 5)
        self.assertEqual(result["status"], "disabled")

        self.assertIsNone(core.unsubscribe(self.conn, "not-a-real-token", T0 + 5))

        # replaying the same (already-consumed) token a second time is a
        # harmless no-op too -- still finds the row, it's just already
        # disabled -- not an error, and not required to return None.
        replay = core.unsubscribe(self.conn, token, T0 + 6)
        self.assertEqual(replay["status"], "disabled")


class RecordEventTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        core.register_drop(self.conn, DROP, "Drop One", now=T0)

    def test_single_transaction_writes_event_ledger_and_outbox(self):
        active_sub(self.conn, DROP, "telegram", "a", "paid", T0)
        active_sub(self.conn, DROP, "telegram", "b", "free", T0)

        row_id = core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "new_showtime", "unit_id": "unit-1",
            "state": "available",
        }, now=T0)

        self.assertIsInstance(row_id, int)
        c = counts(self.conn)
        self.assertEqual(c["events"], 1)
        self.assertEqual(c["ledger"], 1)
        self.assertEqual(c["outbox"], 2)

        ledger_row = self.conn.execute(
            "SELECT * FROM ledger WHERE drop_id=? AND unit_id=?", (DROP, "unit-1")
        ).fetchone()
        self.assertEqual(ledger_row["last_state"], "available")
        self.assertEqual(ledger_row["ever_available"], 1)

    def test_notify_delay_ordering_paid_vs_free(self):
        active_sub(self.conn, DROP, "telegram", "a", "paid", T0)
        active_sub(self.conn, DROP, "telegram", "b", "free", T0)

        core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "new_showtime", "unit_id": "unit-1",
            "state": "available",
        }, now=T0)

        rows = {r["subscription_id"]: r for r in self.conn.execute(
            "SELECT o.not_before, o.subscription_id, s.tier "
            "FROM outbox o JOIN subscriptions s ON s.id = o.subscription_id"
        ).fetchall()}
        by_tier = {r["tier"]: r["not_before"] for r in rows.values()}
        self.assertEqual(by_tier["paid"], T0)
        self.assertEqual(by_tier["free"], T0 + 60)
        # free must not be due before the 60s embargo elapses
        self.assertGreater(by_tier["free"], T0 + 59)

    def test_duplicate_event_is_idempotent_no_op(self):
        active_sub(self.conn, DROP, "telegram", "a", "paid", T0)

        first_id = core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "new_showtime", "unit_id": "unit-1",
            "state": "available",
        }, now=T0)
        second_id = core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "new_showtime", "unit_id": "unit-1",
            "state": "available",
        }, now=T0 + 5)

        self.assertEqual(first_id, second_id)
        c = counts(self.conn)
        self.assertEqual(c["events"], 1)
        self.assertEqual(c["outbox"], 1)

    def test_different_drops_are_independent_for_dedup(self):
        core.register_drop(self.conn, "drop-2", "Drop Two", now=T0)
        active_sub(self.conn, DROP, "telegram", "a", "paid", T0)
        # one code per destination per cooldown, across drops
        active_sub(self.conn, "drop-2", "telegram", "a", "paid",
                   T0 + core.RESEND_COOLDOWN_SECONDS)

        core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "new_showtime", "unit_id": "unit-1",
            "state": "available",
        }, now=T0)
        core.record_event(self.conn, "drop-2", {
            "event_id": "evt-1", "kind": "new_showtime", "unit_id": "unit-1",
            "state": "available",
        }, now=T0)

        c = counts(self.conn)
        self.assertEqual(c["events"], 2)
        self.assertEqual(c["outbox"], 2)

    def test_unverified_subscription_gets_no_outbox_row(self):
        core.subscribe(self.conn, DROP, "telegram", "a", "paid", T0)  # never verified

        core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "new_showtime", "unit_id": "unit-1",
            "state": "available",
        }, now=T0)

        c = counts(self.conn)
        self.assertEqual(c["events"], 1)
        self.assertEqual(c["outbox"], 0)

    def test_disabled_subscription_gets_no_outbox_row(self):
        row = core.subscribe(self.conn, DROP, "telegram", "a", "paid", T0)
        verified = core.verify(self.conn, row["id"], row["verify_code"], T0)
        core.unsubscribe(self.conn, verified["unsub_token"], T0)

        core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "new_showtime", "unit_id": "unit-1",
            "state": "available",
        }, now=T0)

        c = counts(self.conn)
        self.assertEqual(c["events"], 1)
        self.assertEqual(c["outbox"], 0)

    def test_record_event_requires_registered_drop(self):
        with self.assertRaises(ValueError):
            core.record_event(self.conn, "never-registered", {
                "event_id": "evt-1", "kind": "new_showtime",
            }, now=T0)

    def test_record_event_requires_active_not_paused_drop(self):
        core.register_drop(self.conn, "paused-drop", "Paused", status="paused", now=T0)
        with self.assertRaises(ValueError):
            core.record_event(self.conn, "paused-drop", {
                "event_id": "evt-1", "kind": "new_showtime",
            }, now=T0)

    def test_atomicity_on_forced_mid_transaction_error(self):
        active_sub(self.conn, DROP, "telegram", "a", "paid", T0)
        active_sub(self.conn, DROP, "telegram", "b", "free", T0)

        proxy = FlakyConnProxy(self.conn, boom_on_prefix="INSERT INTO outbox")

        with self.assertRaises(RuntimeError):
            core.record_event(proxy, DROP, {
                "event_id": "evt-1", "kind": "new_showtime", "unit_id": "unit-1",
                "state": "available",
            }, now=T0)

        # the event insert and ledger advance happened before the forced
        # failure, inside the same transaction as the (failed) outbox
        # fan-out -- the rollback must undo ALL of it, not just the part
        # that raised.
        c = counts(self.conn)
        self.assertEqual(c["events"], 0, "event row should have rolled back")
        self.assertEqual(c["ledger"], 0, "ledger row should have rolled back")
        self.assertEqual(c["outbox"], 0, "outbox rows should have rolled back")

        # and a clean retry afterwards must work normally (connection is
        # not left in a broken transaction state)
        row_id = core.record_event(self.conn, DROP, {
            "event_id": "evt-1", "kind": "new_showtime", "unit_id": "unit-1",
            "state": "available",
        }, now=T0)
        self.assertIsInstance(row_id, int)
        self.assertEqual(counts(self.conn)["outbox"], 2)


class RiskTierPolicyTests(unittest.TestCase):
    """The GREEN/YELLOW/RED policy has to live in code, not just in the
    README. Red-team audit 2026-08-04 verified a paid subscription
    against a yellow-tier drop succeeded while the docs claimed it was
    refused -- these pin the enforcement so the claim stays true."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        core.register_drop(self.conn, "green-d", "Green", risk_tier="green",
                            now=T0)
        core.register_drop(self.conn, "yellow-d", "Yellow", risk_tier="yellow",
                            now=T0)
        core.register_drop(self.conn, "red-d", "Red", risk_tier="red", now=T0)

    def test_paid_on_yellow_drop_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            core.subscribe(self.conn, "yellow-d", "telegram", "@a", "paid", T0)
        self.assertIn("yellow", str(ctx.exception))

    def test_free_on_yellow_drop_is_allowed(self):
        row = core.subscribe(self.conn, "yellow-d", "telegram", "@a", "free", T0)
        self.assertEqual(row["status"], "pending_verify")

    def test_paid_on_green_drop_is_allowed(self):
        row = core.subscribe(self.conn, "green-d", "telegram", "@a", "paid", T0)
        self.assertEqual(row["status"], "pending_verify")

    def test_red_drop_refuses_any_subscription(self):
        for tier in ("free", "paid"):
            with self.assertRaises(ValueError) as ctx:
                core.subscribe(self.conn, "red-d", "telegram", "@a", tier, T0)
            self.assertIn("red", str(ctx.exception))


class SchemaMigrationTests(unittest.TestCase):
    """CREATE TABLE IF NOT EXISTS silently no-ops against an older shape,
    so a db written before a column existed used to sail through
    connect() and fail later at a random query. connect() now migrates
    additive columns and refuses a future-version db."""

    def _tmp_db(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        os.unlink(path)
        self.addCleanup(lambda: [
            os.path.exists(path + sfx) and os.unlink(path + sfx)
            for sfx in ("", "-wal", "-shm")])
        return path

    def test_missing_additive_column_is_added_on_connect(self):
        import sqlite3
        path = self._tmp_db()
        # Simulate an older db: subscriptions without webhook_secret.
        old = sqlite3.connect(path)
        old.execute(
            "CREATE TABLE subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "drop_id TEXT NOT NULL, channel TEXT NOT NULL, address TEXT NOT NULL, "
            "tier TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending_verify', "
            "verify_code TEXT, verify_attempts INTEGER NOT NULL DEFAULT 0, "
            "verified_at REAL, unsub_token TEXT, created_at REAL NOT NULL)")
        old.commit()
        old.close()

        conn = db.connect(path)
        try:
            cols = {r["name"] for r in
                    conn.execute("PRAGMA table_info(subscriptions)").fetchall()}
            self.assertIn("webhook_secret", cols)
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0],
                db.SCHEMA_VERSION)
        finally:
            conn.close()

    def test_future_schema_version_is_refused(self):
        path = self._tmp_db()
        conn = db.connect(path)
        conn.execute("PRAGMA user_version = %d" % (db.SCHEMA_VERSION + 5))
        conn.close()
        with self.assertRaises(RuntimeError) as ctx:
            db.connect(path)
        self.assertIn("newer than this code supports", str(ctx.exception))

    def test_busy_timeout_is_set(self):
        path = self._tmp_db()
        conn = db.connect(path)
        try:
            self.assertEqual(
                conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
