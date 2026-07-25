#!/usr/bin/env python3
"""Login-time proof that the watcher came back with the machine.

launchd already relaunches both jobs at login (RunAtLoad), so this is not
what starts them — it is what tells you they actually started. A boot-time
failure (broken interpreter, unreadable config, network still down) would
otherwise sit unnoticed until the watchdog's staleness threshold expires.

Runs once per login: repairs the jobs if they are missing, forces one real
poll to prove the whole path works end to end, and reports. Quiet when
healthy, loud when not.
"""

import datetime as dt
import json
import os
import subprocess
import sys
import time

import sentry

JOBS = ("com.brianyan.70mm-sentry", "com.brianyan.70mm-sentry-watchdog")
PLIST_DIR = os.path.expanduser("~/Library/LaunchAgents")


def _launchctl(*args):
    try:
        r = subprocess.run(("launchctl",) + args, capture_output=True,
                           text=True, timeout=30)
        return r.returncode == 0, (r.stderr or r.stdout).strip()
    except Exception as e:
        return False, str(e)


def job_loaded(label):
    ok, _ = _launchctl("print", f"gui/{os.getuid()}/{label}")
    return ok


def load_job(label):
    plist = os.path.join(PLIST_DIR, f"{label}.plist")
    if not os.path.exists(plist):
        return False, f"{label}: plist missing at {plist}"
    ok, err = _launchctl("bootstrap", f"gui/{os.getuid()}", plist)
    return ok, "" if ok else f"{label}: {err[:120]}"


def poll_once(attempts=5, wait=15):
    """Force one real cycle. Retried because at login the network is often
    not up yet, and a false alarm on every boot trains you to ignore it."""
    env = dict(os.environ, SENTRY_NOW="1")
    last = ""
    for i in range(attempts):
        try:
            r = subprocess.run([sys.executable, os.path.join(sentry.BASE,
                                                             "sentry.py")],
                               capture_output=True, text=True, timeout=180,
                               env=env)
            if r.returncode == 0:
                st = json.load(open(sentry.STATE_PATH))
                lf = st.get("lastFetch") or {}
                if lf.get("ok"):
                    return True, lf
                last = f"fetch failed: {lf.get('error', 'unknown')[:100]}"
            else:
                last = (r.stderr or r.stdout or "nonzero exit").strip()[:150]
        except Exception as e:
            last = str(e)[:150]
        if i < attempts - 1:
            time.sleep(wait)
    return False, last


def check():
    problems, repaired = [], []
    for label in JOBS:
        if job_loaded(label):
            continue
        ok, err = load_job(label)
        (repaired if ok else problems).append(err or label)

    polled, detail = poll_once()
    if not polled:
        problems.append(f"live poll failed — {detail}")

    targets = sentry.CFG.get("target_dates") or []
    venue = ", ".join(v["name"] for v in sentry.CFG["venues"].values())
    if problems:
        sentry.push(
            "SENTRY DID NOT COME BACK",
            "Startup check found problems after login:\n- "
            + "\n- ".join(problems)
            + f"\nYou are NOT being watched for {', '.join(targets) or 'drops'}.",
            priority="urgent")
        return 1

    bits = [f"Watching {venue} for {', '.join(targets)}" if targets
            else f"Watching {venue} for new dates"]
    if repaired:
        bits.append("relaunched: " + ", ".join(repaired))
    bits.append(f"live poll OK ({detail.get('bytes')}B, "
                f"{detail.get('movies')} movies).")
    sentry.push("Sentry armed at startup", " · ".join(bits), priority="low")
    return 0


if __name__ == "__main__":
    sys.exit(check())
