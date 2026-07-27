#!/usr/bin/env python3
"""70mm Sentry — self-owned watcher for The Odyssey IMAX 70mm drops at AMC.

Polls Fandango's NAPI (the only verified machine-readable source; AMC's own
site 403s scripted requests). Steady state costs 1 request per venue per run:
probe the first date past the known schedule horizon. On a hit, a bounded
sweep enumerates the new dates and a push goes out via ntfy.

Design doc: https://claude.ai/code/artifact/e5799536-012f-494f-83bd-5f90044c32b5
Env flags: SENTRY_NOW=1 skips start jitter and the overnight cadence gate.
"""

import datetime as dt
import fcntl
import http.client
import json
import os
import random
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zoneinfo

BASE = os.path.dirname(os.path.abspath(__file__))
# SENTRY_DATA lets the container keep config/state/logs on a mounted volume
# while the code stays read-only in the image
DATA = os.environ.get("SENTRY_DATA") or BASE
CONFIG_PATH = os.path.join(DATA, "config.json")
STATE_PATH = os.path.join(DATA, "state.json")
LOG_PATH = os.path.join(DATA, "sentry.log")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
NAPI = "https://www.fandango.com/napi/theaterMovieShowtimes/{venue}?date={date}"

def apply_env(cfg):
    """Env wins over config.json. On a shared VPS this keeps the bot token
    out of a file on disk — pass it through the orchestrator's secret store
    instead."""
    e = os.environ
    tg = cfg.setdefault("channels", {}).setdefault("telegram", {})
    if e.get("TELEGRAM_TOKEN"):
        tg["token"] = e["TELEGRAM_TOKEN"]
        tg["enabled"] = True
    if e.get("TELEGRAM_CHAT_ID"):
        tg["chat_id"] = e["TELEGRAM_CHAT_ID"]
    if e.get("TELEGRAM_EXTRA_CHAT_IDS"):
        tg["extra_chat_ids"] = [x.strip() for x
                                in e["TELEGRAM_EXTRA_CHAT_IDS"].split(",")
                                if x.strip()]
    if e.get("NTFY_TOPIC"):
        nt = cfg.setdefault("channels", {}).setdefault("ntfy", {})
        nt["topic"] = e["NTFY_TOPIC"]
        nt.setdefault("server", "https://ntfy.sh")
        nt["enabled"] = True
    if e.get("HEALTHCHECKS_URL"):
        cfg["healthchecks_url"] = e["HEALTHCHECKS_URL"]
    if e.get("TARGET_DATES"):
        cfg["target_dates"] = [x.strip() for x in e["TARGET_DATES"].split(",")
                               if x.strip()]
    if e.get("SCHEDULE_TZ"):
        cfg["schedule_tz"] = e["SCHEDULE_TZ"]
    # names the box in ops alert titles, so during a migration a Mac alert
    # and a VPS alert are distinguishable at a glance
    cfg["instance_name"] = (e.get("INSTANCE_NAME")
                            or cfg.get("instance_name")
                            or socket.gethostname())
    return cfg


CFG = apply_env(json.load(open(CONFIG_PATH)))


HISTORY_PATH = os.path.join(DATA, "history.jsonl")
LOCK_PATH = os.path.join(DATA, ".sentry.lock")
_LOCK_FH = None


def acquire_lock():
    """Only one cycle at a time. flock is released by the kernel if the
    process dies, so a crash mid-sweep can't wedge the watcher."""
    global _LOCK_FH
    fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        fh.close()
        return False
    _LOCK_FH = fh  # held for process lifetime; GC would drop the lock
    return True


def rotate_log():
    """Keep the logs bounded — 30s polling writes ~8 MB/month, and launchd
    mirrors every stdout line into launchd.log, doubling it."""
    cap = CFG.get("log_max_bytes", 5_000_000)
    for path in (LOG_PATH, os.path.join(DATA, "launchd.log")):
        try:
            if os.path.getsize(path) < cap:
                continue
            os.replace(path, path + ".1")
        except FileNotFoundError:
            pass
        except Exception:
            pass


def elapsed_seconds(now, iso):
    """Seconds between an isoformat timestamp and now, DST-safe. Accepts
    naive (legacy state, interpreted as host-local) or aware values on
    either side — plain wall-clock subtraction miscounts by an hour twice
    a year, enough to false-trip staleness math or suppress a poll."""
    t = dt.datetime.fromisoformat(iso)
    if t.tzinfo is None:
        t = t.astimezone()
    if now.tzinfo is None:
        now = now.astimezone()
    return (now - t).total_seconds()


def log(msg):
    line = f"{dt.datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


# ---------- state ----------

def default_state():
    seed = CFG["seed_horizon"]
    return {
        "horizon": {v: seed for v in CFG["venues"]},
        "alerted": {v: [] for v in CFG["venues"]},
        "dropLog": [],
        "breaker": {"state": "closed", "failures": 0, "openUntil": None,
                    "degradedSent": False},
        "budget": {"date": "", "spent": 0},
        "lastPollAt": None,
        "lastReconcile": "",
        "lastSelfTest": "",
        "instantAlerted": {},
        "seenAvailability": {},
        "autoRestockDates": [],
        "restockLastPoll": {},
        "targetAlerted": [],
        "targetLastPoll": {},
        "announced": [],
        "restockAlerted": [],
        "histSeen": {},
        "studyLastPoll": {},
    }


def announce_ok(st, date):
    """One announcement per date, whichever path gets there first — the
    target watch and the drop sweep both cover the same dates."""
    return date not in st.get("announced", [])


def mark_announced(st, dates):
    st["announced"] = sorted(set(st.get("announced", [])) | set(dates))


def load_state():
    if not os.path.exists(STATE_PATH):
        return default_state()
    st = json.load(open(STATE_PATH))
    merged = default_state()
    merged.update(st)
    return merged


def save_state(st):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, STATE_PATH)


def snapshot_state(st, keep=7):
    """Daily copy of state.json (called from the reconcile, which already
    runs once a day). A wiped or corrupted volume would otherwise reset the
    horizon to seed_horizon and re-announce every date you already know."""
    snap_dir = os.path.join(os.path.dirname(STATE_PATH), "snapshots")
    try:
        os.makedirs(snap_dir, exist_ok=True)
        path = os.path.join(snap_dir,
                            f"state-{dt.date.today().isoformat()}.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f, indent=2)
        os.replace(tmp, path)
        snaps = sorted(f for f in os.listdir(snap_dir)
                       if f.startswith("state-") and f.endswith(".json"))
        for old in snaps[:-keep]:
            os.unlink(os.path.join(snap_dir, old))
    except Exception as e:
        # a failed snapshot must never take down the watcher it protects
        log(f"state snapshot failed: {e}")


# ---------- budget & breaker ----------

def budget_ok(st, n=1):
    today = dt.date.today().isoformat()
    if st["budget"]["date"] != today:
        # yesterday's total feeds the morning digest before it's overwritten
        st["budgetYesterday"] = st["budget"]["spent"]
        st["budget"] = {"date": today, "spent": 0}
    return st["budget"]["spent"] + n <= CFG["daily_request_budget"]


def budget_spend(st, n=1):
    st["budget"]["spent"] += n


def breaker_open(st):
    b = st["breaker"]
    if b["state"] == "open":
        # a missing/zero deadline means "probe now" — an open breaker must
        # never be able to stay dark forever
        if not b["openUntil"] or time.time() >= b["openUntil"]:
            b["state"] = "half-open"
            return False
        return True
    return False


def breaker_fail(st):
    b = st["breaker"]
    b["failures"] += 1
    if b["failures"] >= 3 or b["state"] == "half-open":
        backoff = min(1800 * (2 ** max(0, b["failures"] - 3)), 4 * 3600)
        b["state"] = "open"
        b["openUntil"] = time.time() + backoff
        log(f"breaker OPEN for {backoff}s (failures={b['failures']})")
    if b["failures"] >= 6 and not b["degradedSent"]:
        err = (st.get("lastFetch") or {}).get("error", "")
        # a datacenter IP getting refused is the predicted failure when this
        # runs off-laptop — name it instead of a generic "unreadable"
        blocked = any(code in err for code in ("403", "429", "503"))
        body = (f"Fandango refused {b['failures']} requests in a row "
                f"({err[:120]}).\n") if err else \
            "Fandango unreadable for several cycles.\n"
        if blocked:
            body += ("This looks like IP-level blocking — expected if the "
                     "watcher is running from a datacenter/VPS address. "
                     "Moving back to a residential IP is the fix.")
        else:
            body += "Backing off and retrying automatically."
        # only a DELIVERED alert counts — a push lost to a channel outage
        # retries on the next failure instead of vanishing
        if push("Sentry degraded", body, priority="default", tags="warning"):
            b["degradedSent"] = True


def breaker_success(st):
    b = st["breaker"]
    if b["failures"] or b["state"] != "closed":
        log("breaker CLOSED (recovered)")
    st["breaker"] = {"state": "closed", "failures": 0, "openUntil": None,
                     "degradedSent": False}


# ---------- http ----------

# sentinel: budget exhaustion is self-imposed, not a Fandango failure — it
# must never trip the breaker or masquerade as IP blocking in alerts
BUDGET = object()


class RealtimeSession:
    """One kept-alive HTTPS connection reused across a burst of polls.

    Fandango exposes no websocket, no SSE, and ignores If-None-Match (verified
    2026-07-27: an ETag is returned but a conditional GET still answers 200
    with the full body). Polling is therefore the only transport available;
    the closest thing to a stream is to stop paying for a TLS handshake on
    every request. Reconnects silently — a dropped socket is normal.
    """

    HOST = "www.fandango.com"

    def __init__(self, timeout=12):
        self.timeout = timeout
        self.conn = None
        self.reconnects = 0

    def _connect(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = http.client.HTTPSConnection(self.HOST,
                                                timeout=self.timeout)
        self.reconnects += 1

    def get(self, venue, date):
        path = f"/napi/theaterMovieShowtimes/{venue}?date={date}"
        for attempt in (1, 2):  # one silent retry: keep-alive sockets die
            try:
                if self.conn is None:
                    self._connect()
                self.conn.request("GET", path, headers=napi_headers(venue))
                resp = self.conn.getresponse()
                body = resp.read()  # must drain to reuse the connection
                if resp.status >= 400:
                    raise RuntimeError(f"HTTP {resp.status}")
                return resp.status, body
            except Exception:
                self.conn = None
                if attempt == 2:
                    raise
        raise RuntimeError("unreachable")

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None


def napi_headers(venue):
    return {"User-Agent": UA,
            "Referer": CFG["venues"][venue]["referer"],
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9"}


_LAST_REQUEST_AT = 0.0


def pace_request():
    """Hold every request to Fandango's own published rate guidance.

    Fandango publishes no limits for the public site — no RateLimit headers,
    no Crawl-delay in robots.txt — but their sanctioned partner API documents
    **1 call per second**. That is the only number they have ever published
    about acceptable request rates, so it is the ceiling worth respecting.

    Enforced here, at the single choke point every path shares, because
    average rate was never the risk: a burst of back-to-back requests is the
    behavioural shape bot detection keys on, and target/restock checks
    iterate dates with no spacing of their own. flock guarantees one cycle at
    a time, so a process-local pacer is effectively global.
    """
    global _LAST_REQUEST_AT
    gap = CFG.get("min_request_spacing_seconds", 1.0)
    wait = gap - (time.time() - _LAST_REQUEST_AT)
    if wait > 0:
        time.sleep(wait)
    _LAST_REQUEST_AT = time.time()


def budget_gate(st):
    """True when a request may be spent. Alerts once a day on exhaustion."""
    if budget_ok(st):
        return True
    today = dt.date.today().isoformat()
    if st.get("budgetAlerted") != today:
        if push("Sentry budget exhausted",
                f"Daily request budget ({CFG['daily_request_budget']}) "
                f"spent; polling pauses until midnight. If a drop is "
                f"expected, raise daily_request_budget.",
                priority="default"):
            st["budgetAlerted"] = today
    log("daily request budget exhausted; skipping fetch")
    return False


def fetch_day(st, venue, date, session=None):
    """One NAPI request. Returns viewModel dict, None (failure), or BUDGET
    (self-imposed daily cap reached — not a failure).

    With a RealtimeSession the request rides an already-open TLS connection —
    same bookkeeping, one less handshake, which is what makes second-scale
    polling affordable."""
    if not budget_gate(st):
        return BUDGET
    pace_request()
    budget_spend(st)
    try:
        if session is not None:
            status, body = session.get(venue, date)
        else:
            url = NAPI.format(venue=venue, date=date)
            req = urllib.request.Request(url, headers=napi_headers(venue))
            with urllib.request.urlopen(req, timeout=25) as r:
                body = r.read()
                status = getattr(r, "status", None)
        payload = json.loads(body)
        vm = payload["viewModel"]
    except KeyError:
        # a day whose showtimes have all passed answers {"date": null} with no
        # viewModel at all. That is a valid "nothing listed", not a failure:
        # counting it would trip the breaker every night once the study poll
        # reaches today's expired date, and inflating blindStreak would fire a
        # false SENTRY BLIND. Record it and stay neutral on both counters.
        st["lastFetch"] = {"at": dt.datetime.now().isoformat(), "ok": True,
                           "venue": venue, "date": date, "status": status,
                           "bytes": len(body), "movies": 0, "hits": 0,
                           "note": "no listings for this date"}
        return {}
    except Exception as e:
        st["lastFetch"] = {"at": dt.datetime.now().isoformat(), "ok": False,
                           "venue": venue, "date": date, "error": str(e)[:200]}
        log(f"fetch failed {venue} {date}: {e}")
        return None

    movies = vm.get("movies") or []
    hits = odyssey_70mm(vm)
    record_history(st, venue, date, hits)
    st["lastFetch"] = {"at": dt.datetime.now().isoformat(), "ok": True,
                       "venue": venue, "date": date, "status": status,
                       "bytes": len(body), "movies": len(movies),
                       "hits": len(hits)}
    # a theater page listing zero movies is not a normal response — it means
    # the endpoint changed, is soft-failing, or is rejecting us politely
    if movies:
        st["blindStreak"] = 0
        st["blindAlerted"] = False  # re-arm: blindness can recur
    else:
        st["blindStreak"] = st.get("blindStreak", 0) + 1
        log(f"SUSPECT {venue} {date}: HTTP {status}, {len(body)}B, "
            f"0 movies (streak {st['blindStreak']})")
        maybe_blind_alert(st)
    if CFG.get("capture_last_response"):
        try:
            with open(os.path.join(DATA, "last_response.json"), "wb") as f:
                f.write(body)
        except Exception as e:
            log(f"response capture failed: {e}")
    return vm


def record_history(st, venue, date, hits):
    """Append availability transitions to history.jsonl. Called from fetch_day,
    so every poll on every path contributes and no request is spent for data
    collection alone. Only *changes* are written — the full state at any
    instant is reconstructable from first sightings plus transitions.

    Uses its own last-seen map (histSeen), deliberately not seenAvailability:
    that one is alerting bookkeeping written at different points in the cycle,
    and sharing it would couple recording order to alerting order."""
    if not CFG.get("history_enabled", True) or not hits:
        return
    seen = st.setdefault("histSeen", {})
    now_iso = dt.datetime.now().isoformat(timespec="seconds")
    rows = []
    for h in hits:
        if not slot_qualifies(h["time"]):
            continue
        key = f"{venue}|{date}|{h['time']}"
        prev = seen.get(key)
        if prev == h["type"]:
            continue
        seen[key] = h["type"]
        rows.append({"at": now_iso, "venue": venue, "date": date,
                     "time": h["time"], "from": prev, "to": h["type"]})
    if not rows:
        return
    try:
        rotate_history()
        with open(HISTORY_PATH, "a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    except Exception as e:  # data collection must never break the watcher
        try:
            log(f"history write failed: {e}")
        except Exception:
            pass  # whatever broke the append (a full disk) also breaks the log


def rotate_history():
    cap = CFG.get("history_max_bytes", 20_000_000)
    try:
        if os.path.getsize(HISTORY_PATH) >= cap:
            os.replace(HISTORY_PATH, HISTORY_PATH + ".1")
    except FileNotFoundError:
        pass


def study_due_dates(st, now):
    """Currently-listed dates sampled purely to learn how refunds behave.
    They are NOT alerted on — target_dates own the alerting. Bounded by
    study_days and a relaxed clock so the dataset costs little budget."""
    n = CFG.get("study_days", 0)
    if not n:
        return []
    interval = CFG.get("study_every_seconds", 600)
    today = now.date()
    horizon = min(dt.date.fromisoformat(h) for h in st["horizon"].values())
    due = []
    for i in range(n):
        d = today + dt.timedelta(days=i)
        if d > horizon:  # not listed yet — nothing to observe
            break
        iso = d.isoformat()
        last = st.setdefault("studyLastPoll", {}).get(iso)
        if not last or elapsed_seconds(now, last) >= interval * 0.9:
            due.append(iso)
    return due


def maybe_blind_alert(st):
    """Empty payloads in a row mean we are looking at nothing and would never
    notice a drop. Louder than a breaker trip: fetches are 'succeeding'."""
    n = CFG.get("blind_streak_alert", 20)
    if st.get("blindStreak", 0) < n or st.get("blindAlerted"):
        return
    lf = st.get("lastFetch", {})
    if push("SENTRY BLIND",
            f"{st['blindStreak']} consecutive fetches returned 0 movies "
            f"(HTTP {lf.get('status')}, {lf.get('bytes')}B). Requests are "
            f"succeeding but the payload is empty — endpoint or response "
            f"shape may have changed. Drops would NOT be detected.",
            priority="urgent"):
        st["blindAlerted"] = True


def send_ntfy(ch, title, body, priority, audience="all"):
    # an unconfigured channel must FAIL, never silently "succeed" — push()
    # treats success as delivered and advances the horizon on the strength
    # of it, so a blank topic would swallow a real drop alert
    if not ch.get("topic") or not ch.get("server"):
        raise RuntimeError("ntfy server/topic not configured")
    # JSON publish: title/message ride UTF-8 in the body — plain ntfy headers
    # are latin-1 only and reject em-dashes etc.
    levels = {"urgent": 5, "good": 4, "default": 3, "low": 2}
    tags = {"urgent": "rotating_light", "default": "warning",
            "good": "white_check_mark", "low": "white_check_mark"}
    payload = json.dumps({"topic": ch["topic"], "title": title,
                          "message": body, "priority": levels[priority],
                          "tags": [tags[priority]]}).encode()
    req = urllib.request.Request(ch["server"], data=payload,
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=15)


def send_whatsapp(ch, title, body, priority, audience="all"):
    # WhatsApp has no priority levels; encode urgency in the text itself.
    prefix = {"urgent": "🚨", "default": "⚠️", "good": "✅", "low": "✅"}[priority]
    text = f"{prefix} {title}\n{body}"
    if ch["provider"] == "callmebot":
        if not (ch.get("phone") and ch.get("apikey")):
            raise RuntimeError("callmebot phone/apikey not configured")
        q = urllib.parse.urlencode({"phone": ch["phone"], "text": text,
                                    "apikey": ch["apikey"]})
        with urllib.request.urlopen(
                f"https://api.callmebot.com/whatsapp.php?{q}",
                timeout=30) as r:
            if r.status >= 300:
                raise RuntimeError(f"callmebot HTTP {r.status}")
    elif ch["provider"] == "meta":
        m = ch["meta"]
        if not (m.get("token") and m.get("phone_number_id") and m.get("to")):
            raise RuntimeError("meta token/phone_number_id/to not configured")
        if m.get("template"):
            msg = {"type": "template", "template": {
                "name": m["template"], "language": {"code": "en_US"},
                "components": [{"type": "body", "parameters": [
                    {"type": "text", "text": title},
                    {"type": "text", "text": body}]}]}}
        else:  # free-form: delivered only inside a 24h session window
            msg = {"type": "text", "text": {"body": text}}
        payload = json.dumps({"messaging_product": "whatsapp",
                              "to": m["to"], **msg}).encode()
        req = urllib.request.Request(
            f"https://graph.facebook.com/v21.0/{m['phone_number_id']}/messages",
            data=payload, headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {m['token']}"})
        urllib.request.urlopen(req, timeout=30)
    else:
        raise RuntimeError(f"unknown whatsapp provider {ch['provider']!r}")


def _telegram_send_one(ch, chat_id, text, priority):
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        # low-priority notes arrive silently; drops ring the phone
        "disable_notification": priority == "low",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{ch['token']}/sendMessage",
        data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())
    if not resp.get("ok"):
        raise RuntimeError(f"telegram API said: {resp}")


def send_telegram(ch, title, body, priority, audience="all"):
    if not (ch.get("token") and ch.get("chat_id")):
        raise RuntimeError("telegram token/chat_id not configured")
    # only housekeeping notes stay silent — a recovery ("good") must be as
    # audible as the outage it clears, or the all-clear is never seen
    prefix = {"urgent": "🚨", "default": "⚠️", "good": "✅", "low": "✅"}[priority]
    text = f"{prefix} {title}\n{body}"
    # primary chat failing fails the channel; broadcast extras (e.g. a
    # subscriber channel) are best-effort so a friend's feed can't block
    # your own alert
    _telegram_send_one(ch, ch["chat_id"], text, priority)
    if audience != "all":
        return  # operational noise stays in your own chat
    for extra in ch.get("extra_chat_ids", []):
        try:
            _telegram_send_one(ch, extra, text, priority)
        except Exception as e:
            log(f"telegram broadcast to {extra} failed: {e}")


def telegram_setup(pick=None):
    """One-time helper: after you message your new bot once, this discovers
    your chat_id from getUpdates, saves it to config.json, and test-sends.
    If several people have messaged the bot, refuses to guess — rerun with
    the intended chat id as an argument."""
    ch = CFG["channels"]["telegram"]
    if not ch.get("token"):
        print("Put your BotFather token in config.json → channels.telegram."
              "token first, message the bot once, then rerun.")
        return 1
    with urllib.request.urlopen(
            f"https://api.telegram.org/bot{ch['token']}/getUpdates",
            timeout=30) as r:
        updates = json.loads(r.read())
    chats = [u["message"]["chat"] for u in updates.get("result", [])
             if "message" in u]
    if not chats:
        print("No messages seen yet — open Telegram, send your bot any "
              "message (e.g. 'hi'), then rerun this.")
        return 1
    distinct = {}
    for c in chats:  # dedupe by id, last message wins
        distinct[str(c["id"])] = c
    if pick is not None:
        if str(pick) not in distinct:
            print(f"chat_id {pick} has not messaged the bot recently. Seen: "
                  + ", ".join(f"{i} ({c.get('first_name') or '?'})"
                              for i, c in distinct.items()))
            return 1
        chat = distinct[str(pick)]
    elif len(distinct) > 1:
        # binding alerts to whoever messaged last would silently hand the
        # channel to the wrong person
        print("Multiple people have messaged this bot — not guessing. "
              "Rerun with the right one:")
        for i, c in distinct.items():
            print(f"  python3 sentry.py --telegram-setup {i}"
                  f"   # {c.get('first_name') or '?'}")
        return 1
    else:
        chat = chats[-1]
    ch["chat_id"] = str(chat["id"])
    ch["enabled"] = True
    with open(CONFIG_PATH, "w") as f:
        json.dump(CFG, f, indent=2)
    print(f"chat_id {ch['chat_id']} ({chat.get('first_name', '?')}) saved "
          f"and telegram enabled.")
    send_telegram(ch, "Sentry connected",
                  "Telegram channel is live. Drop alerts will ring; "
                  "housekeeping notes arrive silently.", "low")
    print("Test message sent — check Telegram.")
    return 0


CHANNELS = {"whatsapp": send_whatsapp, "telegram": send_telegram,
            "ntfy": send_ntfy}


def push(title, body, priority="urgent", tags=None, audience="private"):
    """Fan out to every enabled channel. True if ANY channel delivered —
    horizon/alert bookkeeping needs at least one copy to have reached you.

    audience="private" (the default, so anything new is quiet by mistake
    rather than loud by mistake) reaches only your own chat. audience="all"
    also broadcasts to subscriber channels — reserve it for the handful of
    alerts a friend actually wants: a date going live, a drop, a seat
    opening up. Nobody subscribed to hear that a launchd job restarted."""
    # ops alerts name the box they came from; ticket alerts stay clean for
    # the channel. Done here, at the single choke point, so no future ops
    # alert can forget it and no ticket alert can pick it up.
    if audience != "all":
        inst = CFG.get("instance_name")
        if inst:
            title = f"{title} [{inst}]"
    delivered = []
    for name, sender in CHANNELS.items():
        ch = CFG.get("channels", {}).get(name, {})
        if not ch.get("enabled"):
            continue
        try:
            sender(ch, title, body, priority, audience)
            delivered.append(name)
        except Exception as e:
            log(f"PUSH FAILED via {name} ({e}): {title}")
    if delivered:
        log(f"pushed via {'+'.join(delivered)}: {title} | {body[:110]}")
        return True
    log(f"PUSH FAILED on all channels: {title} | {body[:110]}")
    return False


def heartbeat(st=None):
    """Off-box liveness. healthchecks.io if configured, plus a heartbeat
    published to a secret ntfy topic that the repo's GitHub Actions deadman
    workflow polls — silence there means the whole Mac is asleep/off, the one
    failure no local layer (launchd, watchdog, startup check) can report."""
    hc = CFG.get("healthchecks_url")
    if hc:
        try:
            urllib.request.urlopen(hc, timeout=10)
        except Exception as e:
            log(f"heartbeat ping failed: {e}")
    topic = CFG.get("heartbeat_topic")
    if not topic or st is None:
        return
    last = st.get("lastHeartbeat") or 0
    if time.time() - last < CFG.get("heartbeat_every_seconds", 300):
        return
    try:
        payload = json.dumps({
            "topic": topic, "title": "hb",
            "message": f"{CFG.get('instance_name', '?')} "
                       f"{dt.datetime.now().isoformat(timespec='seconds')}",
            # min priority: nothing subscribed to this topic should ever ring
            "priority": 1}).encode()
        req = urllib.request.Request(
            CFG.get("heartbeat_server", "https://ntfy.sh"), data=payload,
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        st["lastHeartbeat"] = time.time()
    except Exception as e:
        log(f"heartbeat publish failed: {e}")


# ---------- detection ----------

def odyssey_70mm(vm):
    """Return list of non-past 70mm showtimes for the target movie."""
    hits = []
    for m in vm.get("movies", []):
        title = (m.get("title") or "").lower()
        if CFG["movie_match"] not in title:
            continue
        # "odyssey" alone would also match a "2001: A Space Odyssey" 70mm
        # revival — a real Lincoln Square occurrence — and a false drop
        # would advance the horizon past real dates
        if any(x in title for x in CFG.get("movie_exclude", [])):
            continue
        for v in m.get("variants", []):
            for g in v.get("amenityGroups", []):
                for s in g.get("showtimes", []):
                    fmts = [f.get("filterName", "")
                            for f in s.get("filmFormat", [])]
                    if not any(CFG["format_match"] in x.upper() for x in fmts):
                        continue
                    if s.get("type") == "pastshowtime" or s.get("expired"):
                        continue
                    hits.append({"time": s.get("date"),
                                 "type": s.get("type"), "formats": fmts,
                                 "url": s.get("ticketingJumpPageURL")})
    return hits


def scan_dates(st, venue, dates):
    """Fetch each date (spaced), return {date: showtime_count}; None = venue
    fetch broke mid-scan (breaker handles it)."""
    found = {}
    empties = 0
    for i, d in enumerate(dates):
        if i:
            time.sleep(CFG["sweep_spacing_seconds"] + random.uniform(0, 2))
        vm = fetch_day(st, venue, d)
        if vm is BUDGET:
            return found  # cap reached: stop scanning, breaker untouched
        if vm is None:
            breaker_fail(st)
            return found
        breaker_success(st)
        hits = odyssey_70mm(vm)
        # seed availability baselines (first sight only — never clobber a
        # pending soldout→available flip the restock poller hasn't seen yet)
        # so a date's very first dedicated restock poll can already detect
        # a flip instead of treating it as baseline
        for h in hits:
            st["seenAvailability"].setdefault(
                f"{venue}|{d}|{h['time']}", h["type"])
        if hits:
            found[d] = {"total": len(hits),
                        "open": sum(1 for h in hits
                                    if h["type"] == "available"),
                        "times": [h["time"] for h in hits
                                  if h["type"] == "available"]}
            empties = 0
        else:
            empties += 1
            if empties >= CFG["sweep_stop_after_empty"]:
                break
    return found


def pretty_date(iso):
    """2026-08-21 -> 'Fri, Aug 21' — friends should not have to parse ISO."""
    try:
        return dt.date.fromisoformat(iso).strftime("%a, %b %-d")
    except Exception:
        return iso


def pretty_time(t):
    """Fandango's '7:00p' -> '7:00 PM'."""
    m = re.match(r"(\d{1,2}:\d{2})([ap])$", t or "")
    return f"{m.group(1)} {m.group(2).upper()}M" if m else t


def fmt_dates(datelist):
    ds = sorted(datelist)
    a = dt.date.fromisoformat(ds[0]).strftime("%b %-d")
    b = dt.date.fromisoformat(ds[-1]).strftime("%b %-d")
    return a if a == b else f"{a}–{b}"


def maybe_instant_alert(st, venue, probe_date):
    """Ring immediately on probe hit — the detail push follows after the
    sweep (~1 min). Deduped per probe date so a failed detail push doesn't
    re-ring every cycle."""
    if st["instantAlerted"].get(venue) == probe_date:
        return
    # the two-stage ping is one event, not two dates; suppress it entirely
    # when the date was already announced elsewhere (e.g. the target watch)
    if not CFG.get("instant_alert", True) or not announce_ok(st, probe_date):
        return
    vcfg = CFG["venues"][venue]
    if push(f"🎬 Odyssey 70mm — new dates dropping now",
            f"New dates just appeared at {vcfg['name']}. Full list in about "
            f"a minute — if you want in, start here now:\n{vcfg['link']}",
            audience="all"):
        st["instantAlerted"][venue] = probe_date


def slot_hour(t):
    """Fandango slot label → decimal hour: '6:00p' → 18.0, '12:30a' → 0.5."""
    m = re.match(r"(\d{1,2}):(\d{2})([ap])", t)
    if not m:
        return None
    h = int(m.group(1)) % 12 + (12 if m.group(3) == "p" else 0)
    return h + int(m.group(2)) / 60


def slot_qualifies(t):
    """In target mode, only slots at/after target_after_hour count — plus
    the late-night block (before 6am), which belongs to the prior evening.
    Unparseable labels pass so a format change can't silently mute alerts."""
    if not CFG.get("target_dates"):
        return True
    h = slot_hour(t)
    if h is None:
        return True
    return h >= CFG.get("target_after_hour", 0) or h < 6


def target_check(st, now):
    """Poll unlisted target dates on their own clock; urgent alert the
    moment one goes live with a qualifying slot. Sold-out qualifying slots
    arm the refund sniper immediately."""
    for date in CFG.get("target_dates", []):
        if (date in st["targetAlerted"] or date < now.date().isoformat()
                or not announce_ok(st, date)):
            continue
        last = st["targetLastPoll"].get(date)
        if (last and elapsed_seconds(now, last)
                < CFG.get("target_every_seconds", 120) * 0.9):
            continue
        st["targetLastPoll"][date] = now.isoformat()
        for venue in CFG["venues"]:
            vm = fetch_day(st, venue, date)
            if vm is BUDGET:
                return
            if vm is None:
                breaker_fail(st)
                return
            breaker_success(st)
            slots = [h for h in odyssey_70mm(vm)
                     if slot_qualifies(h["time"])]
            for h in slots:  # baseline for the refund sniper (first sight)
                st["seenAvailability"].setdefault(
                    f"{venue}|{date}|{h['time']}", h["type"])
            if not slots:
                continue
            vcfg = CFG["venues"][venue]
            open_ = [h for h in slots if h["type"] == "available"]
            gone = [h for h in slots if h["type"] != "available"]
            lines = [f"{pretty_date(date)} just went on sale at "
                     f"{vcfg['name']} ({vcfg['tz_label']}).", ""]
            for h in open_:
                lines.append(f"🎟  {pretty_time(h['time'])} — tap to pick "
                             f"seats:\n{h.get('url') or vcfg['link']}")
            if gone:
                lines.append("Already sold out: " +
                             ", ".join(pretty_time(h["time"]) for h in gone))
            if not open_:
                lines.append("All evening shows are sold out already — "
                             "we'll keep watching for returned seats.")
            lines += ["", "Tip: a showtime can be listed and still have no "
                      "seats left, so check the map before celebrating."]
            if vcfg.get("amc_link"):
                lines.append(f"Prefer the AMC app? "
                             f"{vcfg['amc_link'].format(date=date)}")
            if push(f"🎬 Odyssey 70mm — {pretty_date(date)} tickets are up",
                    "\n".join(lines), audience="all"):
                st["targetAlerted"].append(date)
                mark_announced(st, [date])
                if gone:
                    st["autoRestockDates"] = sorted(
                        set(st["autoRestockDates"]) | {date})


def restock_watchlist(st, today=None):
    """Dates to snipe: user-pinned config dates plus soldout dates the daily
    reconcile discovered, past dates pruned."""
    today = today or dt.date.today().isoformat()
    dates = set(CFG.get("restock_watch_dates", [])) | \
        set(st.get("autoRestockDates", []))
    return sorted(d for d in dates if d >= today)


def soldout_dates(found):
    """Dates from a scan with fewer open showtimes than total."""
    return sorted(d for d, c in found.items() if c["open"] < c["total"])


def restock_due_dates(st, now):
    """Watched dates due for a poll. Most cancellations land in the final
    hours before a screening (and Lincoln Square screens around the clock),
    so today/tomorrow run on the tight clock, later dates on the relaxed
    one."""
    tomorrow = (now.date() + dt.timedelta(days=1)).isoformat()
    due = []
    for d in restock_watchlist(st, today=now.date().isoformat()):
        interval = (CFG.get("restock_burst_seconds", 60) if d <= tomorrow
                    else CFG.get("restock_every_seconds", 300))
        last = st["restockLastPoll"].get(d)
        if not last or elapsed_seconds(now, last) >= interval * 0.9:
            due.append(d)
    return due


def schedule_time(now):
    """Quiet hours and burst windows are about the THEATER's clock (AMC posts
    on Eastern afternoons), not wherever this Mac happens to be."""
    tzname = CFG.get("schedule_tz")
    if not tzname:
        return now
    try:
        if now.tzinfo is None:
            now = now.astimezone()  # attach this machine's zone
        return now.astimezone(zoneinfo.ZoneInfo(tzname))
    except Exception as e:
        log(f"schedule_tz {tzname!r} unusable ({e}); using local time")
        return now


def realtime_interval(now):
    """Seconds between polls inside a realtime window, else None.

    These are the hours a drop is actually expected (reported: Tuesday
    morning ET for the following weeks' dates), evaluated on the THEATER's
    clock. Inside one, the watcher stops behaving like a cron job and holds a
    tight loop on a kept-alive connection."""
    t = schedule_time(now)
    for w in CFG.get("realtime_windows", []):
        if (t.strftime("%a") in w["days"]
                and w["start_hour"] <= t.hour < w["end_hour"]):
            return max(3, int(w.get("interval_seconds", 6)))
    return None


def realtime_seconds_left(now):
    """Seconds until the current realtime window closes (0 if outside one)."""
    t = schedule_time(now)
    for w in CFG.get("realtime_windows", []):
        if (t.strftime("%a") in w["days"]
                and w["start_hour"] <= t.hour < w["end_hour"]):
            end = t.replace(hour=0, minute=0, second=0, microsecond=0) \
                + dt.timedelta(hours=w["end_hour"])
            return max(0, int((end - t).total_seconds()))
    return 0


def poll_interval(now):
    """Seconds between polls right now. Burst windows (configured drop
    windows) beat the overnight stretch; otherwise base cadence."""
    rt = realtime_interval(now)
    if rt:
        return rt
    t = schedule_time(now)
    for w in CFG.get("burst_windows", []):
        if (t.strftime("%a") in w["days"] and
                w["start_hour"] <= t.hour < w["end_hour"]):
            return CFG.get("burst_interval_seconds", 20)
    if t.hour >= CFG["quiet_start_hour"] or t.hour < CFG["quiet_end_hour"]:
        return CFG.get("overnight_interval_seconds", 3600)
    return CFG.get("base_interval_seconds", 60)


def restock_check(st, venue, dates):
    """Poll the given dates; alert on soldout/restricted → available flips."""
    freed = []
    for date in dates:
        vm = fetch_day(st, venue, date)
        if vm is BUDGET:
            return
        if vm is None:
            breaker_fail(st)
            return
        breaker_success(st)
        for h in odyssey_70mm(vm):
            if not slot_qualifies(h["time"]):
                continue
            key = f"{venue}|{date}|{h['time']}"
            prev = st["seenAvailability"].get(key)
            st["seenAvailability"][key] = h["type"]
            if prev in ("soldout", "restricted") and h["type"] == "available":
                # once per showtime: a seat that flaps sold-out/open would
                # otherwise re-ring on every flip
                if key not in st["restockAlerted"]:
                    freed.append((date, h["time"], h.get("url"), key))
    if freed:
        vcfg = CFG["venues"][venue]
        lines = [f"A sold-out show at {vcfg['name']} just reopened "
                 f"({vcfg['tz_label']}) — someone likely returned tickets. "
                 f"These go fast:", ""]
        for date, t, url, _ in freed:
            lines.append(f"🎟  {pretty_date(date)}, {pretty_time(t)}\n"
                         f"{url or vcfg['link']}")
        if push(f"🎟️ Seats just opened — {vcfg['name']}",
                "\n".join(lines), audience="all"):
            st["restockAlerted"].extend(k for *_, k in freed)


REALTIME_SLICE = 55   # seconds of tight looping per invocation


def probe_once(st, venue, session=None):
    """Probe the first date past the horizon. Returns True if a drop was
    found and handled. Shared by the steady cycle and the realtime loop so
    both take exactly the same path on the one event that matters."""
    probe_date = (dt.date.fromisoformat(st["horizon"][venue]) +
                  dt.timedelta(days=1)).isoformat()
    vm = fetch_day(st, venue, probe_date, session=session)
    if vm is BUDGET:
        return False
    if vm is None:
        breaker_fail(st)
        return False
    breaker_success(st)
    hits = odyssey_70mm(vm)
    if not hits:
        lf = st.get("lastFetch", {})
        log(f"quiet {venue}: nothing past {st['horizon'][venue]} "
            f"[HTTP {lf.get('status')} {lf.get('bytes')}B "
            f"{lf.get('movies')} movies] "
            f"(budget {st['budget']['spent']}/{CFG['daily_request_budget']})")
        return False
    log(f"PROBE HIT {venue} {probe_date} — sweeping")
    # ring first, enumerate second: the sweep costs ~a minute and the phone
    # should already be buzzing while it runs
    maybe_instant_alert(st, venue, probe_date)
    save_state(st)
    dates = [(dt.date.fromisoformat(probe_date) + dt.timedelta(days=i))
             .isoformat() for i in range(CFG["sweep_days"])]
    found = scan_dates(st, venue, dates)
    found.setdefault(probe_date, {
        "total": len(hits),
        "open": sum(1 for h in hits if h["type"] == "available"),
        "times": [h["time"] for h in hits if h["type"] == "available"]})
    handle_drop(st, venue, found)
    return True


def realtime_loop(st, now):
    """Tight polling for the hours a drop is actually expected.

    Fandango offers no push transport — no websocket, no SSE, and it ignores
    If-None-Match — so the only way to shorten detection latency is to ask
    more often over a connection that is already open. One invocation holds
    the lock for a slice; launchd restarts it within 10s, which keeps crash
    recovery and the watchdog exactly as they are the rest of the week."""
    interval = realtime_interval(now)
    deadline = time.time() + min(REALTIME_SLICE, realtime_seconds_left(now))
    session = RealtimeSession()
    polls = 0
    try:
        while time.time() < deadline:
            if breaker_open(st):
                log("breaker open; leaving realtime loop")
                break
            if not budget_ok(st):
                break
            started = time.time()
            now = dt.datetime.now().astimezone()
            st["lastPollAt"] = now.isoformat()   # watchdog liveness each pass
            dropped = False
            for venue in CFG["venues"]:
                if probe_once(st, venue, session):
                    dropped = True
            target_check(st, now)
            heartbeat(st)                        # self-throttling
            polls += 1
            if dropped or polls % 5 == 0:
                save_state(st)
            if time.time() >= deadline:
                break
            # jitter so a six-second cadence never looks metronomic
            time.sleep(max(0.5, interval - (time.time() - started)
                           + random.uniform(-0.6, 0.6)))
    finally:
        session.close()
        save_state(st)
    log(f"realtime slice: {polls} polls @ ~{interval}s "
        f"({session.reconnects} connection(s), "
        f"budget {st['budget']['spent']}/{CFG['daily_request_budget']})")


def build_digest(st):
    """Morning one-pager: what's being watched, what came of it. Silent by
    design — it informs without waking anyone; its absence is itself signal."""
    lines = []
    targets = CFG.get("target_dates") or []
    if targets:
        # announced-elsewhere counts as live too: heals any date the drop
        # path claimed before the target watch could
        live = [d for d in targets
                if d in st.get("targetAlerted", [])
                or d in st.get("announced", [])]
        wait = [d for d in targets if d not in live]
        if wait:
            lines.append("Waiting on: " +
                         ", ".join(pretty_date(d) for d in wait))
        if live:
            lines.append("Already on sale: " +
                         ", ".join(pretty_date(d) for d in live))
    for v, vcfg in CFG["venues"].items():
        lines.append(f"{vcfg['name']} schedule runs through "
                     f"{pretty_date(st['horizon'][v])}.")
    watch = restock_watchlist(st)
    if watch:
        lines.append("Refund watch: " +
                     ", ".join(pretty_date(d) for d in watch))
    spent = st.get("budgetYesterday")
    health = (f"Yesterday: {spent} requests" if spent is not None
              else "First day on this box")
    if st["breaker"]["state"] != "closed":
        health += f" · breaker {st['breaker']['state']}"
    lines.append(health + ".")
    if CFG.get("heartbeat_topic"):
        lines.append("Off-box dead-man: armed.")
    return "\n".join(lines)


def handle_drop(st, venue, found):
    vcfg = CFG["venues"][venue]
    new = {d: c for d, c in found.items()
           if d > st["horizon"][venue] and d not in st["alerted"][venue]
           and announce_ok(st, d)}
    if not new:
        # nothing left to say, but the horizon still has to move past dates
        # the target watch already announced, or the probe re-hits forever
        seen = [d for d in found if d > st["horizon"][venue]
                and not announce_ok(st, d)]
        if seen:
            st["horizon"][venue] = max(max(seen), st["horizon"][venue])
        return
    total = sum(c["total"] for c in new.values())
    open_ = sum(c["open"] for c in new.values())
    seats = (f"{open_} with open seats" if open_ == total
             else f"{open_} with open seats, {total - open_} sold out")
    # per-date showtime lines, capped so a big drop still fits one push
    day_lines = []
    for d in sorted(new)[:6]:
        times = new[d].get("times") or []
        if times:
            shown = ", ".join(pretty_time(x) for x in times[:8])
            extra = f" (+{len(times) - 8} more)" if len(times) > 8 else ""
            day_lines.append(f"{pretty_date(d)}: {shown}{extra}")
    if len(new) > 6:
        day_lines.append(f"…and {len(new) - 6} more dates")
    detail = ("\n".join(day_lines) + "\n\n") if day_lines else ""
    body = (f"{len(new)} new date{'s' if len(new) > 1 else ''} just went on "
            f"sale at {vcfg['name']}: {fmt_dates(new)} "
            f"({vcfg['tz_label']}).\n"
            f"{total} showtimes — {seats}.\n\n"
            f"{detail}"
            f"Grab seats: {vcfg['link']}\n\n"
            f"Tip: a showtime can be listed and still have no seats left, so "
            f"check the map before celebrating.")
    if push(f"🎬 Odyssey 70mm — new dates at {vcfg['name']}", body,
            audience="all"):
        st["alerted"][venue].extend(sorted(new))
        mark_announced(st, new)
        st["dropLog"].append({"venue": venue,
                              "detectedAt": dt.datetime.now().isoformat(),
                              "datesAdded": sorted(new),
                              "showtimes": total, "openSeats": open_})
        # advance only on delivered alert so a failed push retries next cycle
        st["horizon"][venue] = max(max(new), st["horizon"][venue])


# ---------- main ----------

def main():
    if not acquire_lock():
        # normal during a sweep: launchd keeps firing while a cycle runs
        log("another cycle already running; skipping")
        return
    rotate_log()

    st = load_state()
    now = dt.datetime.now().astimezone()  # aware: DST-safe interval math
    fast = os.environ.get("SENTRY_NOW") == "1"

    if not fast:
        time.sleep(random.uniform(0, 5))  # jitter so runs never look metronomic

    # cadence gate: launchd fires every 10s; run only when the current
    # tier's interval (burst / base / overnight) has elapsed. The 0.9
    # tolerates launchd scheduling drift.
    if not fast and st["lastPollAt"]:
        if elapsed_seconds(now, st["lastPollAt"]) < poll_interval(now) * 0.9:
            save_state(st)
            return

    if breaker_open(st):
        log("breaker open; skipping cycle")
        save_state(st)
        return

    # claim the slot on disk BEFORE the long work. A probe-hit sweep runs
    # ~60-90s while launchd keeps firing every 10s; without this the next
    # process reads a stale lastPollAt, starts its own cycle, and the two
    # race to save_state — duplicate alerts and clobbered horizon, exactly
    # during a drop.
    st["lastPollAt"] = now.isoformat()
    save_state(st)

    # weekly self-test: proves the notification channel end to end (W5)
    today = dt.date.today().isoformat()
    last_test = st["lastSelfTest"]
    if (not last_test or (dt.date.today() -
                          dt.date.fromisoformat(last_test)).days >=
            CFG["self_test_every_days"]):
        h = ", ".join(f"{CFG['venues'][v]['name']}: {st['horizon'][v]}"
                      for v in CFG["venues"])
        if push("Sentry alive", f"Weekly self-test. Horizons — {h}",
                priority="low", tags="white_check_mark"):
            st["lastSelfTest"] = today

    # daily digest: one silent morning note, gated on the theater's clock
    dh = CFG.get("daily_digest_hour")
    if (dh is not None and st.get("lastDigest") != today
            and schedule_time(now).hour >= dh):
        if push("Sentry daily digest", build_digest(st), priority="low"):
            st["lastDigest"] = today

    # realtime window: hand the whole slice to the tight loop. Everything
    # below (reconcile, restock, study) is deliberately deferred — during the
    # hours a drop is expected, nothing competes with watching for it.
    if realtime_interval(now):
        realtime_loop(st, now)
        return

    # daily reconcile: catches non-contiguous adds the probe can't see (W4).
    # A 22-date sweep takes ~90s, which is exactly the blind spot you do not
    # want inside a drop window — hence it sits after the realtime check.
    if st["lastReconcile"] != today and now.hour >= 6:
        st["lastReconcile"] = today
        auto = set()
        found_any = []
        pre_fail = st["breaker"]["failures"]
        # start at today (not tomorrow): today's sellouts are the prime
        # refund-sniping targets
        for venue in CFG["venues"]:
            start = dt.date.today()
            dates = [(start + dt.timedelta(days=i)).isoformat()
                     for i in range(CFG["reconcile_days"] + 1)]
            found = scan_dates(st, venue, dates)
            found_any.append(bool(found))
            handle_drop(st, venue, found)
            auto.update(soldout_dates(found))
        # in target mode, refund-snipe only the dates the user actually wants
        targets = set(CFG.get("target_dates", []))
        if targets:
            auto &= targets
        # canary: the run is known to be playing inside the reconcile window,
        # so zero hits across all of it means our parser stopped matching —
        # cycles keep "succeeding" while we see nothing. Only diagnose the
        # parser when the scans themselves were clean: a fetch failure or a
        # budget stop also yields zero hits, and those already have their
        # own (correctly attributed) alerts.
        clean = (st["breaker"]["failures"] == pre_fail
                 and st["breaker"]["state"] == "closed"
                 and st.get("budgetAlerted") != today)
        if not any(found_any) and clean:
            if not st.get("canaryAlerted"):
                if push("SENTRY BLIND — parser",
                        f"Full {CFG['reconcile_days']}-day reconcile found 0 "
                        f"IMAX 70mm showtimes, but the schedule runs through "
                        f"{st['horizon'][list(CFG['venues'])[0]]}. The feed's "
                        f"shape likely changed and matching is broken.",
                        priority="urgent"):
                    st["canaryAlerted"] = True
        else:
            st["canaryAlerted"] = False
        st["autoRestockDates"] = sorted(auto)
        st["seenAvailability"] = {
            k: v for k, v in st["seenAvailability"].items()
            if k.split("|")[1] >= today}
        st["restockLastPoll"] = {
            d: t for d, t in st["restockLastPoll"].items() if d >= today}
        st["targetLastPoll"] = {
            d: t for d, t in st["targetLastPoll"].items() if d >= today}
        st["studyLastPoll"] = {
            d: t for d, t in st.get("studyLastPoll", {}).items()
            if d >= today}
        st["histSeen"] = {k: v for k, v in st.get("histSeen", {}).items()
                          if k.split("|")[1] >= today}
        heartbeat(st)
        save_state(st)
        snapshot_state(st)
        log(f"reconcile done (budget spent today: {st['budget']['spent']})")
        return

    # target watch runs BEFORE the probe/sweep: both can discover the same
    # date in one cycle (targets sit only days past the horizon), and
    # whichever announces first claims it. The target path must win — its
    # alert carries per-showtime links and arms the refund sniper instantly,
    # while the drop path's generic alert would leave targetAlerted unset
    # (digest stuck on "Waiting on:") and delay sniper arming to the next
    # reconcile.
    target_check(st, now)

    # steady state: one probe per venue at horizon+1
    for venue in CFG["venues"]:
        probe_once(st, venue)

    # restock sniping: each watched date on its own clock — tight when the
    # show is imminent, relaxed otherwise
    due = restock_due_dates(st, now)
    if due:
        for venue in CFG["venues"]:
            restock_check(st, venue, due)
        for d in due:
            st["restockLastPoll"][d] = now.isoformat()

    # study sampling: observe already-listed dates purely to build the refund
    # dataset. fetch_day records the transitions; nothing here alerts.
    for d in study_due_dates(st, now):
        for venue in CFG["venues"]:
            vm = fetch_day(st, venue, d)
            if vm is BUDGET:
                break
            if vm is None:
                breaker_fail(st)
                break
            breaker_success(st)
        st["studyLastPoll"][d] = now.isoformat()

    heartbeat(st)
    save_state(st)


def status():
    """Human-readable health snapshot: age of last cycle is the number that
    matters — everything else is context."""
    st = load_state()
    now = dt.datetime.now().astimezone()
    last = st.get("lastPollAt")
    age = ("never" if not last else
           f"{int(elapsed_seconds(now, last))}s ago")
    print(f"last cycle:   {age}")
    print(f"cadence now:  {poll_interval(now)}s")
    print(f"budget:       {st['budget']['spent']}/"
          f"{CFG['daily_request_budget']} ({st['budget']['date']})")
    b = st["breaker"]
    print(f"breaker:      {b['state']} (failures {b['failures']})")
    for v in CFG["venues"]:
        print(f"horizon {v}:  {st['horizon'][v]}")
    print(f"targets:      {CFG.get('target_dates') or '(none)'} "
          f"after {CFG.get('target_after_hour')}:00")
    print(f"alerted:      {st.get('targetAlerted') or '(none)'}")
    print(f"restock watch:{restock_watchlist(st) or ' (none)'}")
    print(f"healthchecks: {CFG.get('healthchecks_url') or '(NOT SET)'}")
    hb = st.get("lastHeartbeat")
    hb_age = "never" if not hb else f"last hb {int(time.time() - hb)}s ago"
    print(f"deadman:      "
          f"{'armed, ' + hb_age if CFG.get('heartbeat_topic') else '(NOT SET)'}")
    try:
        n = sum(1 for _ in open(HISTORY_PATH))
    except Exception:
        n = 0
    print(f"history:      {n} transitions "
          f"({'on' if CFG.get('history_enabled', True) else 'OFF'}, "
          f"studying {CFG.get('study_days', 0)} days)")
    print(f"digest:       "
          f"{CFG.get('daily_digest_hour', '(off)')} (last: "
          f"{st.get('lastDigest') or 'never'})")
    print(f"instance:     {CFG.get('instance_name') or '(unnamed)'}")
    return 0


def startup_push():
    """Deploy/restart visibility: a private note naming the box, what it is
    watching, and for which dates. Sent by the container entrypoint, so an
    unexpected restart announces itself instead of passing silently."""
    venues = ", ".join(v["name"] for v in CFG["venues"].values())
    targets = CFG.get("target_dates") or []
    watching = (f"Watching {venues} for {', '.join(targets)}" if targets
                else f"Watching {venues} for new dates")
    ok = push("Sentry started", watching + ".", priority="low")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--status" in sys.argv:
        sys.exit(status())
    if "--telegram-setup" in sys.argv:
        i = sys.argv.index("--telegram-setup")
        pick = (sys.argv[i + 1] if len(sys.argv) > i + 1
                and not sys.argv[i + 1].startswith("-") else None)
        sys.exit(telegram_setup(pick))
    if "--startup-push" in sys.argv:
        sys.exit(startup_push())
    if "--digest-preview" in sys.argv:  # print only — never pushes
        print(build_digest(load_state()))
        sys.exit(0)
    if "--test-push" in sys.argv:
        ok = push("Sentry channel test",
                  "If you can read this, the notification layer reaches you.",
                  priority="low")
        sys.exit(0 if ok else 1)
    sys.exit(main())
