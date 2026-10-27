"""platformd.db — sqlite3 schema and connection helper.

stdlib only (sqlite3). WAL mode + foreign keys on for file-backed
databases; ':memory:' connections work too (WAL is a silent no-op there,
which sqlite3 tolerates) so the whole test suite can run without ever
touching disk.

Manual transaction control: connections are opened with
isolation_level=None (autocommit) so platformd.core can wrap
insert-event + advance-ledger + fan-out-outbox in one explicit
BEGIN/COMMIT and roll the whole thing back atomically on error.
"""

import sqlite3

# Bump when SCHEMA changes. Additive columns also go in _ADDITIVE_COLUMNS
# so an existing on-disk db gets them via ALTER TABLE (see
# _apply_migrations); a structural change needs a real migration step.
SCHEMA_VERSION = 2

# (table, column, "column DDL") for every column added after a table's
# original shape -- applied to older databases in order.
_ADDITIVE_COLUMNS = (
    ("subscriptions", "webhook_secret", "webhook_secret TEXT"),
    ("outbox", "claimed_at", "claimed_at REAL"),
    ("events", "zero_delivered_logged",
     "zero_delivered_logged INTEGER NOT NULL DEFAULT 0"),
    ("events", "recovered_after_zero_logged",
     "recovered_after_zero_logged INTEGER NOT NULL DEFAULT 0"),
    ("drops", "adapter", "adapter TEXT"),
    ("drops", "target_json", "target_json TEXT"),
    ("subscriptions", "date_from", "date_from TEXT"),
    ("subscriptions", "date_to", "date_to TEXT"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS drops (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    source TEXT,
    risk_tier TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused')),
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    drop_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    unit_id TEXT,
    payload_json TEXT,
    recorded_at REAL NOT NULL,
    zero_delivered_logged INTEGER NOT NULL DEFAULT 0,
    recovered_after_zero_logged INTEGER NOT NULL DEFAULT 0,
    UNIQUE (drop_id, event_id)
);

CREATE TABLE IF NOT EXISTS ledger (
    drop_id TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    last_state TEXT,
    ever_available INTEGER NOT NULL DEFAULT 0,
    updated_at REAL,
    PRIMARY KEY (drop_id, unit_id)
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    drop_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    address TEXT NOT NULL,
    tier TEXT NOT NULL CHECK (tier IN ('paid', 'free')),
    status TEXT NOT NULL DEFAULT 'pending_verify'
        CHECK (status IN ('pending_verify', 'active', 'disabled')),
    verify_code TEXT,
    verify_attempts INTEGER NOT NULL DEFAULT 0,
    verified_at REAL,
    unsub_token TEXT,
    webhook_secret TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_drop
    ON subscriptions (drop_id, status);

CREATE UNIQUE INDEX IF NOT EXISTS idx_subscriptions_unsub_token
    ON subscriptions (unsub_token) WHERE unsub_token IS NOT NULL;

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_row_id INTEGER NOT NULL REFERENCES events(id),
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'in_flight', 'delivered', 'failed',
                          'exhausted', 'cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    not_before REAL NOT NULL,
    created_at REAL NOT NULL,
    claimed_at REAL,
    delivered_at REAL,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_outbox_due
    ON outbox (status, next_attempt_at, not_before);

CREATE INDEX IF NOT EXISTS idx_outbox_event
    ON outbox (event_row_id);

CREATE TABLE IF NOT EXISTS governor (
    source TEXT PRIMARY KEY,
    day TEXT,
    requests_used INTEGER NOT NULL DEFAULT 0,
    breaker_fails INTEGER NOT NULL DEFAULT 0,
    breaker_open_until REAL,
    last_request_at REAL
);

-- (drop, date) pairs the poller has seen at least once. A unit is only a
-- "new listing" if its date was already baselined; otherwise a date
-- rolling into a watch window would page for every site already open.
-- Paid access, per destination (channel+address) rather than per
-- subscription: one Stripe subscription covers every watch that
-- destination has. Written only by the verified Stripe webhook.
CREATE TABLE IF NOT EXISTS entitlements (
    channel TEXT NOT NULL,
    address TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'inactive')),
    stripe_customer TEXT,
    stripe_subscription TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (channel, address)
);

CREATE INDEX IF NOT EXISTS idx_entitlements_stripe_sub
    ON entitlements(stripe_subscription);

CREATE TABLE IF NOT EXISTS poll_baseline (
    drop_id TEXT NOT NULL,
    date TEXT NOT NULL,
    PRIMARY KEY (drop_id, date)
);
"""


def connect(path):
    """Open a platformd database at path (or ':memory:'), schema applied.

    WAL + foreign_keys are best-effort on ':memory:' (sqlite silently
    keeps the in-memory journal mode there instead of erroring), so the
    same connect() works for both production files and test doubles.
    """
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # Concurrent writers (the API and the runner both hold their own
    # connections) should QUEUE, not fail: without this a writer that
    # collides raises "database is locked" immediately. sqlite3's own
    # connect(timeout=) default is 5s and implements the same pragma;
    # setting it explicitly means the guarantee survives someone later
    # passing a different timeout.
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA)
    _apply_migrations(conn)
    return conn


def _apply_migrations(conn):
    """Bring an existing database up to SCHEMA_VERSION, or refuse.

    `CREATE TABLE IF NOT EXISTS` silently no-ops against a table that
    already exists in an OLDER shape, so a db written before a column was
    added would otherwise sail through connect() and then fail at some
    random later query with "no such column". Columns are additive so far,
    so the migration is ADD COLUMN for anything missing; a db from a
    FUTURE version is refused outright rather than corrupted.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            "database schema version %d is newer than this code supports "
            "(%d) -- upgrade platformd or point at a different db"
            % (version, SCHEMA_VERSION))

    for table, column, ddl in _ADDITIVE_COLUMNS:
        existing = {r["name"] for r in
                    conn.execute("PRAGMA table_info(%s)" % table).fetchall()}
        if column not in existing:
            conn.execute("ALTER TABLE %s ADD COLUMN %s" % (table, ddl))

    if version != SCHEMA_VERSION:
        conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
