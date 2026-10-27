"""app.probe -- one real poll pass against recreation.gov, from the box
the app runs on, through the app's own transport, adapter, governor and
detection code. Read-only upstream, one request, and a throwaway
in-memory db: the live database and its budget are never touched.

    docker compose exec app python -m app.probe 232447 2026-11-14

Use it after a deploy (does this IP get answers, or [REFUSED]?) and
whenever [BLIND]/[PARSE] lines suggest the upstream payload changed.
Exit 0 when the payload was healthy and parsed, 1 otherwise.
"""

import datetime as dt
import sys

from app import serve
from engine import core, db, log as _log, poller
from sources.base import STATE_AVAILABLE


def probe(campground_id, date, http_get=None):
    """Returns a summary dict; never raises on upstream trouble."""
    adapter = serve.ADAPTERS["recreation_gov"]
    seen = len(_log.recent())
    conn = db.connect(":memory:")
    drop_id = "rg-%s" % campground_id
    core.register_drop(conn, drop_id, "probe", risk_tier="green")
    watch = {"drop_id": drop_id, "adapter": adapter.name,
             "target": {"campground_id": campground_id}, "dates": [date]}
    stats = poller.poll_watch(conn, watch, adapter, http_get or serve.http_get)
    rows = conn.execute("SELECT last_state FROM ledger WHERE drop_id = ?",
                        (drop_id,)).fetchall()
    conn.close()
    tags = sorted({e.tag for e in _log.recent()[seen:]
                   if e.tag in (_log.TAG_REFUSED, _log.TAG_PIPE,
                                _log.TAG_BLIND, _log.TAG_PARSE)})
    return {"fetched": stats["fetched"], "sites": len(rows),
            "available": sum(r["last_state"] == STATE_AVAILABLE for r in rows),
            "events": stats["events"], "problems": tags,
            "ok": stats["fetched"] == 1 and not tags}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) not in (1, 2) or not argv[0].isdigit():
        print("usage: python -m app.probe CAMPGROUND_ID [YYYY-MM-DD]",
              file=sys.stderr)
        return 2
    date = argv[1] if len(argv) == 2 else (
        dt.date.today() + dt.timedelta(days=14)).isoformat()
    out = probe(argv[0], date)
    print("probe campground=%s date=%s %s" % (
        argv[0], date, " ".join("%s=%s" % (k, ",".join(v) if isinstance(v, list)
                                           else v) for k, v in out.items())))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
