"""platformd.log — structured, grep-able failure logging.

log(tag, msg, **fields) writes one line:

    ISO8601 [TAG] msg k=v k2=v2 ...

to stderr, and appends the same event to an in-memory ring buffer that
tests can assert against via recent(tag=None). Tags are the standard,
stable set shared with adapters/base.py's kernel log-tag contract, plus
platformd-only additions (BUDGET, BREAKER) needed because governor
denials don't fit the adapter-fetch vocabulary, RATELIMIT for the
subscription-lifecycle's verify-attempt exhaustion, and RUNNER for the
dispatch-loop process's own heartbeat/startup/shutdown lines. Logging an
unknown tag is a programming error and raises immediately rather than
silently letting a typo'd tag go ungrep-able.
"""

import datetime as dt
import sys
import threading
from collections import deque

# ---------- standard tag vocabulary ----------

TAG_PIPE = "PIPE"                    # broken pipe / reset / dead kept-alive socket
TAG_BLIND = "BLIND"                  # HTTP ok, payload empty/mismatched
TAG_REFUSED = "REFUSED"              # 403/429/5xx from upstream
TAG_PARSE = "PARSE"                  # payload fetched, parse() raised
TAG_DELIVERY = "DELIVERY"            # channel send failed (or breaker skip)
TAG_ZERO_DELIVERED = "ZERO-DELIVERED"  # event fired, 0/N subscriber deliveries succeeded
TAG_BUDGET = "BUDGET"                # governor: daily request budget denial
TAG_BREAKER = "BREAKER"              # governor/outbox: breaker-open denial
TAG_RATELIMIT = "RATELIMIT"          # core: verify-attempt budget exhausted
TAG_RUNNER = "RUNNER"                # runner: tick heartbeat, startup/shutdown
TAG_POLL = "POLL"                    # poller: per-fetch latency/outcome, detections
TAG_BILLING = "BILLING"              # stripe webhook: verified events, rejections

KNOWN_TAGS = frozenset({
    TAG_PIPE, TAG_BLIND, TAG_REFUSED, TAG_PARSE,
    TAG_DELIVERY, TAG_ZERO_DELIVERED, TAG_BUDGET, TAG_BREAKER,
    TAG_RATELIMIT, TAG_RUNNER, TAG_POLL, TAG_BILLING,
})

_RING_MAXLEN = 4000
_ring = deque(maxlen=_RING_MAXLEN)
_lock = threading.Lock()


class LogEntry:
    """One structured log line, kept for test assertions."""

    __slots__ = ("ts", "tag", "msg", "fields", "line")

    def __init__(self, ts, tag, msg, fields, line):
        self.ts = ts
        self.tag = tag
        self.msg = msg
        self.fields = fields
        self.line = line

    def __repr__(self):
        return "LogEntry(%r)" % (self.line,)


def log(tag, msg, **fields):
    """Emit one structured log line. Raises ValueError on an unknown tag."""
    if tag not in KNOWN_TAGS:
        raise ValueError(
            "unknown log tag %r — register it in platformd.log.KNOWN_TAGS "
            "before using it (stable grep-able tags only)" % (tag,)
        )

    now = dt.datetime.now(dt.timezone.utc)
    ts = now.isoformat()
    field_str = " ".join("%s=%s" % (k, v) for k, v in fields.items())
    line = "%s [%s] %s" % (ts, tag, msg)
    if field_str:
        line = "%s %s" % (line, field_str)

    entry = LogEntry(ts=ts, tag=tag, msg=msg, fields=dict(fields), line=line)
    with _lock:
        _ring.append(entry)

    print(line, file=sys.stderr)
    return entry


def recent(tag=None):
    """Return ring-buffer entries (oldest first), optionally filtered by tag."""
    with _lock:
        entries = list(_ring)
    if tag is None:
        return entries
    return [e for e in entries if e.tag == tag]


def clear():
    """Test helper: empty the ring buffer between cases."""
    with _lock:
        _ring.clear()
