"""engine.runner — the dispatch-loop process. The production analog
of sentry.py's timer tick: instead of polling a source and detecting
changes, each tick opens a fresh sqlite3 connection and asks
engine.outbox.dispatch() to send whatever's due.

    python3 -m engine.runner --db PATH [--interval 5] [--once]
                                 [--demo-channels]

Each tick:
    1. opens a connection via engine.db.connect(db_path)
    2. calls engine.outbox.dispatch(conn, channels, now=time.time())
    3. logs one [RUNNER] heartbeat line with the pending-row count before
       and after, so a stalled dispatcher (queue growing, never draining)
       is visible in the log stream without needing a debugger

--once runs exactly one tick and returns -- this is what the test suite
uses; there are no sleeps anywhere in this module's tests. Without
--once, the runner loops forever with `interval` seconds between ticks
until SIGINT (Ctrl-C, or `kill -INT`), at which point it finishes the
tick already in flight, logs a clean-shutdown [RUNNER] line, and
returns. There is no special interrupted-tick cleanup to do: the outbox
itself is already crash-safe (CLAIM_LEASE_SECONDS reclaims any row an
interrupted tick left 'in_flight' -- see engine.outbox's module
docstring), so a runner that dies mid-tick just leaves work for the
next one.

This CLI's channel construction is intentionally minimal: --demo-channels
wires one print-only fake sender per engine.core.VALID_CHANNELS entry,
so the runner has something to dispatch through without a real
credential anywhere. Real senders (notify/) are wired by a composition
root such as app/serve.py, which calls run_once() itself; without
--demo-channels this CLI dispatches
through an empty channels dict, which just means every row's send()
KeyErrors and gets treated as a normal per-row delivery failure (per
outbox.dispatch's per-row isolation) rather than crashing the tick.
"""

import argparse
import signal
import time

from engine import db, log as _log, outbox
from engine.core import VALID_CHANNELS


class _PrintChannel:
    """Fake channel sender for --demo-channels: prints instead of
    sending. Mirrors app.api._PrintChannel; kept as its own small copy
    because the engine never imports the app.
    """

    def __init__(self, name):
        self.name = name

    def send(self, address, message, context=None):
        print("[demo:%s -> %s] %s" % (self.name, address, message), flush=True)
        return True


def _demo_channels():
    return {name: _PrintChannel(name) for name in VALID_CHANNELS}


def _pending_count(conn):
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM outbox WHERE status IN ('pending', 'failed')"
    ).fetchone()
    return row["n"]


def run_once(conn, channels, now=None, public_url=None):
    """Run exactly one dispatch tick and log its [RUNNER] heartbeat.
    Returns (pending_before, pending_after) -- mostly for tests.
    """
    if now is None:
        now = time.time()
    before = _pending_count(conn)
    outbox.dispatch(conn, channels, now=now, public_url=public_url)
    after = _pending_count(conn)
    _log.log(_log.TAG_RUNNER, "tick complete",
              pending_before=before, pending_after=after)
    return before, after


class _Stop(Exception):
    """Raised by the SIGINT handler installed in main()'s loop to unwind
    the dispatch loop cleanly -- never escapes main()."""


def _install_sigint_handler():
    def _handler(signum, frame):
        raise _Stop()
    return signal.signal(signal.SIGINT, _handler)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python3 -m engine.runner",
        description="Dispatch-loop process: repeatedly calls "
                     "engine.outbox.dispatch() against a live db.")
    parser.add_argument("--db", required=True, help="sqlite db path")
    parser.add_argument("--interval", type=float, default=5.0,
                         help="seconds between dispatch ticks (default: 5)")
    parser.add_argument("--once", action="store_true",
                         help="run exactly one tick and exit (what tests use)")
    parser.add_argument("--demo-channels", action="store_true",
                         help="dispatch through print-only fake channel "
                              "senders instead of an empty channels dict "
                              "(see module docstring)")
    args = parser.parse_args(argv)

    channels = _demo_channels() if args.demo_channels else {}

    conn = db.connect(args.db)
    _log.log(_log.TAG_RUNNER, "runner starting",
              db=args.db, interval=args.interval, once=args.once)
    try:
        if args.once:
            run_once(conn, channels)
            return

        previous_handler = _install_sigint_handler()
        try:
            while True:
                run_once(conn, channels)
                time.sleep(args.interval)
        except _Stop:
            pass
        finally:
            signal.signal(signal.SIGINT, previous_handler)
    finally:
        _log.log(_log.TAG_RUNNER, "runner shutting down", db=args.db)
        conn.close()


if __name__ == "__main__":
    main()
