#!/usr/bin/env python3
"""Turn history.jsonl into a self-contained availability-replay page.

The watcher answers "is there a seat right now"; this answers the question you
can only get from time: how often do sold-out showtimes reopen, at what hour,
and how long does a returned ticket survive before someone takes it.

    python3 report.py [-o out.html]

Reads history.jsonl (written by sentry.record_history) and emits one HTML file
with no external requests — safe to publish as an artifact.
"""

import argparse
import collections
import datetime as dt
import json
import os
import statistics
import sys
import zoneinfo

import sentry

OPEN = "available"
GONE = ("soldout", "restricted")
BUCKETS = 120


def load_events(path=None):
    path = path or sentry.HISTORY_PATH
    rows = []
    for p in (path + ".1", path):  # rotated first: chronological order
        if not os.path.exists(p):
            continue
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a torn final line must not kill the report
    rows.sort(key=lambda r: r.get("at", ""))
    return rows


def theater_tz():
    try:
        return zoneinfo.ZoneInfo(sentry.CFG.get("schedule_tz")
                                 or "America/New_York")
    except Exception:
        return None


def to_theater(iso, tz):
    """Timestamps are written in host-local time; refund *behaviour* is only
    meaningful on the theater's clock."""
    t = dt.datetime.fromisoformat(iso)
    if t.tzinfo is None:
        t = t.astimezone()
    return t.astimezone(tz) if tz else t


def analyze(rows):
    tz = theater_tz()
    events = []
    for r in rows:
        if not r.get("at") or not r.get("date"):
            continue
        events.append({**r, "t": to_theater(r["at"], tz)})
    if not events:
        return None

    start, end = events[0]["t"], events[-1]["t"]
    span = max((end - start).total_seconds(), 1)
    slots = sorted({(e["date"], e["time"]) for e in events},
                   key=lambda s: (s[0], sentry.slot_hour(s[1]) or 0))
    index = {s: i for i, s in enumerate(slots)}

    # per-slot status timeline, sampled onto a fixed grid so the page can
    # scrub without shipping every raw event to the browser
    grid = [["u"] * BUCKETS for _ in slots]
    opened = [0] * BUCKETS
    taken = [0] * BUCKETS
    by_hour = [0] * 24
    survival = []          # seconds a reopened slot stayed available
    open_since = {}
    first_seen_at = {}

    for e in events:
        i = index[(e["date"], e["time"])]
        b = min(BUCKETS - 1, int((e["t"] - start).total_seconds() / span
                                 * (BUCKETS - 1)))
        code = {OPEN: "a", "soldout": "s", "restricted": "r"}.get(e["to"], "u")
        for k in range(b, BUCKETS):
            grid[i][k] = code
        first_seen_at.setdefault(i, e["t"])
        if e["from"] is None:
            if e["to"] == OPEN:
                open_since[i] = e["t"]
            continue  # a first sighting is a baseline, not churn
        if e["to"] == OPEN and e["from"] in GONE:
            opened[b] += 1
            by_hour[e["t"].hour] += 1
            open_since[i] = e["t"]
        elif e["to"] in GONE and e["from"] == OPEN:
            taken[b] += 1
            if i in open_since:
                survival.append((e["t"] - open_since.pop(i)).total_seconds())

    labels = [(start + dt.timedelta(seconds=span * k / (BUCKETS - 1)))
              .isoformat(timespec="minutes") for k in range(BUCKETS)]
    dates = sorted({s[0] for s in slots})
    return {
        "start": start.isoformat(timespec="minutes"),
        "end": end.isoformat(timespec="minutes"),
        "startLabel": start.strftime("%b %-d, %-I:%M %p"),
        "endLabel": end.strftime("%b %-d, %-I:%M %p"),
        "hours": round(span / 3600, 1),
        "tz": sentry.CFG.get("schedule_tz", "local"),
        "slots": [{"date": d, "time": t, "label": sentry.pretty_date(d),
                   "timeLabel": sentry.pretty_time(t)} for d, t in slots],
        "dates": dates, "grid": grid, "labels": labels,
        "opened": opened, "taken": taken, "byHour": by_hour,
        "totals": {
            "events": len(events),
            "opened": sum(opened), "taken": sum(taken),
            "slots": len(slots), "dates": len(dates),
            "statuses": dict(collections.Counter(
                e["to"] for e in events)),
            "everSoldout": any(e["to"] == "soldout" for e in events),
            "medianSurvivalMin": (round(statistics.median(survival) / 60, 1)
                                  if survival else None),
            "fastestSurvivalMin": (round(min(survival) / 60, 1)
                                   if survival else None),
            "survivalSamples": len(survival),
        },
    }


CSS = """
*,*::before,*::after{box-sizing:border-box}
:root{
 --font:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
 --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,monospace;
 --ink:#131517;--ink64:rgba(19,21,23,.64);--ink44:rgba(19,21,23,.44);
 --hair:rgba(19,21,23,.08);--bg:#F7F8F9;--surface:#FFF;--inset:#F0F1F2;
 --open:#15803D;--gone:#B91C1C;--idle:rgba(19,21,23,.10);--r:8px}
@media (prefers-color-scheme:dark){:root{
 --ink:#F2F3F4;--ink64:rgba(242,243,244,.64);--ink44:rgba(242,243,244,.44);
 --hair:rgba(242,243,244,.10);--bg:#0E0F10;--surface:#161819;--inset:#101112;
 --open:#4ADE80;--gone:#F87171;--idle:rgba(242,243,244,.12)}}
:root[data-theme=dark]{
 --ink:#F2F3F4;--ink64:rgba(242,243,244,.64);--ink44:rgba(242,243,244,.44);
 --hair:rgba(242,243,244,.10);--bg:#0E0F10;--surface:#161819;--inset:#101112;
 --open:#4ADE80;--gone:#F87171;--idle:rgba(242,243,244,.12)}
:root[data-theme=light]{
 --ink:#131517;--ink64:rgba(19,21,23,.64);--ink44:rgba(19,21,23,.44);
 --hair:rgba(19,21,23,.08);--bg:#F7F8F9;--surface:#FFF;--inset:#F0F1F2;
 --open:#15803D;--gone:#B91C1C;--idle:rgba(19,21,23,.10)}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--font);
 font-size:15px;line-height:28px;-webkit-font-smoothing:antialiased}
.wrap{max-width:880px;margin:0 auto;padding:0 24px 96px}
header{border-bottom:1px solid var(--hair);margin-bottom:56px}
header .wrap{padding:18px 24px;display:flex;align-items:center;
 justify-content:space-between;gap:16px}
.brand{font-size:14px;font-weight:600;letter-spacing:-.2px}
.meta{font-size:12px;color:var(--ink44)}
.eyebrow{font-size:12px;font-weight:500;letter-spacing:.08em;
 text-transform:uppercase;color:var(--ink44);margin:0 0 10px}
h1{font-size:30px;line-height:36px;font-weight:600;letter-spacing:-.75px;
 margin:0 0 14px;text-wrap:balance}
:focus-visible{outline:2px solid var(--ink);outline-offset:2px;
 border-radius:3px}
h2{font-size:16px;font-weight:600;margin:0 0 6px}
.lede{font-size:15px;color:var(--ink64);max-width:560px;margin:0 0 8px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
 gap:12px;margin:40px 0}
.stat{background:var(--surface);border:1px solid var(--hair);
 border-radius:var(--r);padding:16px 18px}
.stat .n{font-size:24px;font-weight:600;letter-spacing:-.5px;line-height:32px;
 font-variant-numeric:tabular-nums}
.stat .k{font-size:12px;color:var(--ink44);line-height:18px}
.card{background:var(--surface);border:1px solid var(--hair);
 border-radius:var(--r);padding:20px;margin:0 0 24px}
.card-head{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;
 margin-bottom:16px}
.card-head h2{margin:0}
.card-head .sub{font-size:13px;color:var(--ink44)}
.panel{background:var(--inset);border-radius:var(--r);padding:16px}
.scrub{display:flex;align-items:center;gap:12px;margin-bottom:10px}
/* the utility face carries every reading of the instrument — timestamps,
   axis ticks, counts — so measured values never read as prose */
.scrub .now{font-family:var(--mono);font-size:13px;font-weight:500;
 font-variant-numeric:tabular-nums}
.ends,.hr span,.cell{font-family:var(--mono)}
.bars{display:flex;align-items:center;gap:1px;height:64px;width:100%}
.bar{flex:1;display:flex;flex-direction:column;justify-content:center;
 gap:1px;min-width:0;cursor:pointer}
.bar i{display:block;border-radius:1px;min-height:0}
.bar .up{background:var(--open);align-self:stretch}
.bar .dn{background:var(--gone);align-self:stretch}
.bar.cursor{outline:1px solid var(--ink44);outline-offset:1px;border-radius:2px}
input[type=range]{width:100%;accent-color:var(--ink);margin:12px 0 0}
.ends{display:flex;justify-content:space-between;font-size:12px;
 color:var(--ink44);margin-top:2px}
.legend{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;
 color:var(--ink64);margin-top:14px;align-items:center}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;
 margin-right:6px;vertical-align:middle}
.map{margin-top:18px;display:flex;flex-direction:column;gap:10px}
.daygroup{display:flex;gap:14px;align-items:flex-start}
.dayname{font-size:12px;color:var(--ink44);width:88px;flex:none;
 padding-top:3px;line-height:18px}
.cells{display:flex;flex-wrap:wrap;gap:6px}
.cell{display:flex;align-items:center;gap:6px;font-size:12px;
 line-height:18px;color:var(--ink64);border:1px solid var(--hair);
 border-radius:var(--r);padding:3px 8px;background:var(--bg);
 font-variant-numeric:tabular-nums}
.cell .sw{width:8px;height:8px;border-radius:2px;background:var(--idle)}
.cell.a .sw{background:var(--open)}
.cell.a{color:var(--ink);border-color:color-mix(in srgb,var(--open) 40%,transparent)}
.cell.s .sw{background:var(--gone)}
/* restricted is its own state, not a shade of sold out: it is the only
   transition this feed actually shows, so it must be readable as distinct */
.cell.r .sw{background:transparent;border:2px solid var(--gone)}
.hours{display:flex;align-items:flex-end;gap:3px;height:88px;margin-top:6px}
.hr{flex:1;display:flex;flex-direction:column;justify-content:flex-end;
 align-items:center;gap:4px;min-width:0}
.hr i{display:block;width:100%;background:var(--open);border-radius:2px;
 min-height:2px}
.hr span{font-size:10px;color:var(--ink44);line-height:12px}
.empty{background:var(--surface);border:1px solid var(--hair);
 border-radius:var(--r);padding:28px 22px}
footer{margin-top:56px;padding-top:20px;border-top:1px solid var(--hair);
 font-size:12px;line-height:20px;color:var(--ink44)}
code{font-family:var(--mono);font-size:12px}
@media (max-width:640px){.daygroup{flex-direction:column;gap:4px}
 .dayname{width:auto}h1{font-size:26px;line-height:32px}}
/* opt-in: content is visible unless JS is alive to animate it, so a script
   failure can never leave the page blank */
@media (prefers-reduced-motion:no-preference){
 .js .fade{opacity:0;transform:translateY(8px);
  transition:opacity .4s ease-out,transform .4s ease-out}
 .js .fade.in{opacity:1;transform:none}}
"""

JS = """
document.documentElement.classList.add('js');
const D = window.__DATA__;
if (D) {
  const grid = D.grid, labels = D.labels, slots = D.slots;
  const range = document.getElementById('scrub');
  const nowEl = document.getElementById('now');
  const bars = [...document.querySelectorAll('.bar')];
  const cells = [...document.querySelectorAll('.cell')];
  function render(i) {
    nowEl.textContent = new Date(labels[i]).toLocaleString(undefined,
      {month:'short', day:'numeric', hour:'numeric', minute:'2-digit'});
    cells.forEach((c, n) => { c.className = 'cell ' + grid[n][i]; });
    bars.forEach((b, n) => b.classList.toggle('cursor', n === i));
    let open = 0;
    for (let n = 0; n < grid.length; n++) if (grid[n][i] === 'a') open++;
    document.getElementById('openNow').textContent =
      open + (open === 1 ? ' showtime open' : ' showtimes open');
  }
  range.addEventListener('input', e => render(+e.target.value));
  bars.forEach((b, n) => b.addEventListener('click', () => {
    range.value = n; render(n);
  }));
  render(grid.length ? labels.length - 1 : 0);
}
const io = new IntersectionObserver(es => es.forEach(e => {
  if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); }
}), {rootMargin: '-40px'});
document.querySelectorAll('.fade').forEach(el => io.observe(el));
"""


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def render(a, cfg):
    venue = ", ".join(v["name"] for v in cfg["venues"].values())
    gen = dt.datetime.now().strftime("%b %-d, %-I:%M %p")
    head = (f'<header><div class="wrap"><span class="brand">70mm Sentry</span>'
            f'<span class="meta">Updated {gen}</span></div></header>')

    if not a or not a["slots"]:
        return PAGE.format(css=ascii_safe(CSS), js=JS, data="null",
                           body=ascii_safe(
            f'{head}<div class="wrap"><p class="eyebrow">Availability study</p>'
            f'<h1>Collecting.</h1>'
            f'<p class="lede">The watcher is recording every availability '
            f'change it sees at {esc(venue)}. Nothing has changed yet — this '
            f'page fills in as showtimes flip between sold out and on sale. '
            f'Check back after a few hours.</p>'
            f'<div class="empty" style="margin-top:32px"><h2>Why it may be '
            f'empty</h2><p class="lede" style="margin:0">Transitions are only '
            f'written when a status actually changes, so a quiet file means a '
            f'quiet box office — not a broken watcher. Verify with '
            f'<code>python3 sentry.py --status</code>.</p></div>'
            f'<footer>Data: 70mm Sentry production polling · times in '
            f'{esc(a["tz"] if a else "America/New_York")}. Independent tool, '
            f'not affiliated with AMC Theatres, IMAX, or Fandango.</footer>'
            f'</div>'))

    t = a["totals"]
    # name what was actually measured: a flip to on-sale is not proof of a
    # refund, and one observation is not a median
    ns = t["survivalSamples"]
    surv = (f'{t["medianSurvivalMin"]} min' if ns >= 3
            else (f'{t["medianSurvivalMin"]} min' if ns else "—"))
    surv_label = ("median time back on sale" if ns >= 3
                  else f'time on sale ({ns} observation'
                       f'{"s" if ns != 1 else ""} — too few to average)'
                  if ns else "no on-sale spells observed")
    stats = "".join(
        f'<div class="stat"><div class="n">{esc(n)}</div>'
        f'<div class="k">{esc(k)}</div></div>'
        for n, k in [
            (t["opened"], "flips to on sale"),
            (t["taken"], "flips back off sale"),
            (surv, surv_label),
            (f'{a["hours"]}h', f'observed · {t["slots"]} showtimes'),
        ])

    peak = max(max(a["opened"]), max(a["taken"]), 1)
    bars = "".join(
        f'<div class="bar" title="{esc(a["labels"][i])} · '
        f'+{a["opened"][i]} / -{a["taken"][i]}">'
        f'<i class="up" style="height:{a["opened"][i] / peak * 26:.1f}px"></i>'
        f'<i class="dn" style="height:{a["taken"][i] / peak * 26:.1f}px"></i>'
        f'</div>' for i in range(len(a["labels"])))

    groups = []
    n = 0
    for d in a["dates"]:
        cells = []
        for s in a["slots"]:
            if s["date"] != d:
                continue
            cells.append(f'<div class="cell u" data-i="{n}">'
                         f'<span class="sw"></span>{esc(s["timeLabel"])}</div>')
            n += 1
        groups.append(f'<div class="daygroup"><div class="dayname">'
                      f'{esc(sentry.pretty_date(d))}</div>'
                      f'<div class="cells">{"".join(cells)}</div></div>')

    hpeak = max(max(a["byHour"]), 1)
    hours = "".join(
        f'<div class="hr" title="{h:02d}:00 — {a["byHour"][h]} reopened">'
        f'<i style="height:{a["byHour"][h] / hpeak * 60:.1f}px"></i>'
        f'<span>{h if h % 3 == 0 else ""}</span></div>' for h in range(24))

    # the finding is computed, never asserted: if this feed ever does show a
    # sellout, the page says so instead and the caveat disappears on its own
    if not t["everSoldout"] and t["slots"] >= 5:
        seen = ", ".join(f"{esc(k)} ({v})" for k, v in
                         sorted(t["statuses"].items(), key=lambda x: -x[1]))
        finding = (
            f'<div class="card fade"><div class="card-head">'
            f'<h2>What this feed can and cannot see</h2></div>'
            f'<p class="lede" style="max-width:640px">Across {t["slots"]} '
            f'showtimes over {a["hours"]} hours, not one has ever reported '
            f'<em>sold out</em>. Observed states: {seen}. Public listings mark '
            f'a showtime on sale, not whether a seat is left — so a house with '
            f'zero seats and a house with fifty look identical here.</p>'
            f'<p class="lede" style="max-width:640px;margin-top:12px">'
            f'Consequence worth being blunt about: refunds cannot be detected '
            f'from this data. Watching for <strong>new dates going on '
            f'sale</strong> — where a whole auditorium opens at once — is the '
            f'mechanism that actually works. Per-seat detection needs '
            f'seat-level access this feed does not carry.</p></div>')
    else:
        finding = ""

    best = max(range(24), key=lambda h: a["byHour"][h])
    hour_note = (f'Most reopenings seen around <strong>{best}:00 '
                 f'{esc(a["tz"].split("/")[-1].replace("_", " "))}</strong>.'
                 if sum(a["byHour"]) else 'No reopenings recorded yet.')

    body = f"""{head}<div class="wrap">
<p class="eyebrow">Availability study · {esc(venue)}</p>
<h1>What sold out really means.</h1>
<p class="lede">Every {esc(str(sentry.CFG.get("study_every_seconds", 600) // 60))}
minutes the watcher re-reads every showtime it can see and records only what
changed. This is {esc(a["hours"])} hours of that record — and the most useful
thing in it is what never moves.</p>
<div class="stats">{stats}</div>

<div class="card fade">
 <div class="card-head"><h2>Replay</h2>
  <span class="sub">drag to move through time · <span id="openNow"></span></span></div>
 <div class="panel">
  <div class="scrub"><span class="now" id="now"></span></div>
  <div class="bars">{bars}</div>
  <input type="range" id="scrub" min="0" max="{len(a["labels"]) - 1}"
   value="{len(a["labels"]) - 1}" aria-label="Move through time">
  <div class="ends"><span>{esc(a["startLabel"])}</span>
   <span>{esc(a["endLabel"])}</span></div>
 </div>
 <div class="legend">
  <span><span class="dot" style="background:var(--open)"></span>reopened</span>
  <span><span class="dot" style="background:var(--gone)"></span>sold out</span>
  <span><span class="dot" style="background:transparent;
   box-sizing:border-box;border:2px solid var(--gone)"></span>restricted</span>
  <span><span class="dot" style="background:var(--idle)"></span>not seen yet</span>
 </div>
 <div class="map">{"".join(groups)}</div>
</div>

{finding}
<div class="card fade">
 <div class="card-head"><h2>When refunds land</h2>
  <span class="sub">reopenings by hour, {esc(a["tz"])}</span></div>
 <div class="panel"><div class="hours">{hours}</div></div>
 <p class="lede" style="margin-top:14px">{hour_note}</p>
</div>

<footer>Recorded {esc(a["startLabel"])} — {esc(a["endLabel"])}
({esc(a["totals"]["events"])} transitions, {esc(t["slots"])} showtimes across
{esc(t["dates"])} dates). Showtime-level availability from the public feed:
a showtime counted as open is purchasable, which is not the same as a seat you
would want. Independent tool, not affiliated with AMC Theatres, IMAX, or
Fandango.</footer></div>"""
    return PAGE.format(css=ascii_safe(CSS), js=JS, data=json.dumps(
        {"grid": a["grid"], "labels": a["labels"], "slots": a["slots"]}),
        body=ascii_safe(body))


def ascii_safe(s):
    """Numeric entities for every non-ASCII char. The published page inherits a
    <head> we don't control, so never depend on a charset declaration —
    entities render correctly under any of them. Applied to markup only:
    entities are not decoded inside <script>, and json.dumps already escapes."""
    return s.encode("ascii", "xmlcharrefreplace").decode()


PAGE = """<style>{css}</style>
{body}
<script>window.__DATA__ = {data};
{js}</script>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default=os.path.join(sentry.DATA,
                                                        "report.html"))
    ap.add_argument("--history", default=None)
    args = ap.parse_args()
    a = analyze(load_events(args.history))
    html = render(a, sentry.CFG)
    with open(args.out, "w") as f:
        f.write(html)
    t = (a or {}).get("totals", {})
    print(f"wrote {args.out} "
          f"({t.get('events', 0)} transitions, {t.get('opened', 0)} reopenings, "
          f"{t.get('slots', 0)} showtimes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
