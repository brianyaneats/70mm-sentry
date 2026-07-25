#!/usr/bin/env python3
"""Watchdog for 70mm Sentry — answers "is the watcher still alive?"

Runs as its own launchd job on a DIFFERENT interpreter than sentry.py, so an
interpreter-level break (Xcode license, deleted python, bad PATH) takes down
the sentry but not the alarm that reports it. Reads state.json rather than
polling Fandango, so it costs zero requests.

Blind spot it cannot cover: the whole Mac being asleep or off takes the
watchdog down too. Only an off-box dead-man's switch (healthchecks_url)
closes that one.
"""

import datetime as dt
import json
import os
import sys

import sentry

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("SENTRY_DATA") or BASE
WD_STATE = os.path.join(DATA, "watchdog_state.json")
LOG_PATH = os.path.join(DATA, "watchdog.log")
LAUNCHD_LOG = os.path.join(DATA, "launchd.log")

ERROR_HINTS = ("Traceback", "Error", "error:", "not agreed", "No such file",
               "command not found", "Permission denied", "Killed")


def log(msg):
    line = f"{dt.datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def load_wd_state():
    if not os.path.exists(WD_STATE):
        return {"down": False, "lastAlertAt": None}
    try:
        return json.load(open(WD_STATE))
    except Exception:
        return {"down": False, "lastAlertAt": None}


def save_wd_state(st):
    tmp = WD_STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, WD_STATE)


def poll_age_seconds(now):
    """Seconds since the sentry last completed a cycle; None if unknown."""
    try:
        st = json.load(open(sentry.STATE_PATH))
    except Exception as e:
        log(f"state.json unreadable: {e}")
        return None
    last = st.get("lastPollAt")
    if not last:
        return None
    return (now - dt.datetime.fromisoformat(last)).total_seconds()


def recent_errors(limit=400, keep=4):
    """Error-looking lines from the tail of launchd.log — usually the actual
    reason the sentry stopped, which the alert should carry."""
    try:
        with open(LAUNCHD_LOG) as f:
            lines = f.readlines()[-limit:]
    except Exception:
        return []
    hits = [ln.strip() for ln in lines
            if any(h in ln for h in ERROR_HINTS)]
    # dedupe, keep last few distinct
    seen, out = set(), []
    for ln in reversed(hits):
        if ln not in seen:
            seen.add(ln)
            out.append(ln)
        if len(out) >= keep:
            break
    return list(reversed(out))


def check(now=None):
    now = now or dt.datetime.now()
    cfg = sentry.CFG
    stale_after = cfg.get("watchdog_stale_seconds", 600)
    renotify = cfg.get("watchdog_renotify_hours", 6) * 3600

    wd = load_wd_state()
    age = poll_age_seconds(now)
    down = age is None or age > stale_after

    if down:
        last_alert = wd.get("lastAlertAt")
        due = (not last_alert or
               (now - dt.datetime.fromisoformat(last_alert)).total_seconds()
               >= renotify)
        if not wd.get("down") or due:
            mins = "unknown" if age is None else f"{int(age // 60)} min"
            errs = recent_errors()
            body = (f"Sentry has not completed a cycle in {mins} "
                    f"(threshold {stale_after // 60} min). "
                    f"You are NOT being watched right now.")
            if errs:
                body += "\nLast errors:\n" + "\n".join(errs)
            body += "\nRestart: " + (
                "docker compose restart" if os.environ.get("SENTRY_DATA")
                else f"launchctl kickstart -k gui/{os.getuid()}/"
                     f"com.brianyan.70mm-sentry")
            if sentry.push("SENTRY DOWN", body, priority="urgent"):
                wd["lastAlertAt"] = now.isoformat()
                # only an outage you were TOLD about earns an all-clear
                wd["notified"] = True
        if not wd.get("down"):
            wd["downSince"] = now.isoformat()
        wd["down"] = True
        log(f"DOWN (age={age})")
    else:
        if wd.get("down") and wd.get("notified"):
            since = wd.get("downSince")
            for_ = ""
            if since:
                mins = int((now - dt.datetime.fromisoformat(since))
                           .total_seconds() // 60)
                for_ = f" after {mins} min down" if mins else " after <1 min"
            if sentry.push("SENTRY BACK UP",
                           f"Watcher is polling again{for_} "
                           f"(last cycle {int(age)}s ago). "
                           f"Alerts are live again.",
                           priority="good"):
                wd["notified"] = False
                log("RECOVERED")
            else:
                # keep the flags so the all-clear retries next pass
                save_wd_state(wd)
                return 0
        wd["down"] = False
        wd["lastAlertAt"] = None
        wd["downSince"] = None

    save_wd_state(wd)
    return 0 if not down else 1


if __name__ == "__main__":
    sys.exit(check())
