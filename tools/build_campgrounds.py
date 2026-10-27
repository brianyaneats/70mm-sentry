"""Build deploy/campgrounds.json -- the reservable-campground index the
watch picker searches. Run by hand when the list feels stale:

    python3 tools/build_campgrounds.py

~10 requests to recreation.gov's site search (500 per page), 2s apart.
Committed output means the server never calls search at request time.
"""

import json
import os
import time
import urllib.request

URL = ("https://www.recreation.gov/api/search?fq=entity_type%3Acampground"
       "&fq=reservable%3A1&size=500&start={start}")
UA = "Dropwatch/0.1 (availability alerts)"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "deploy", "campgrounds.json")


def fetch(start):
    req = urllib.request.Request(URL.format(start=start),
                                 headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def main():
    rows, start, total = {}, 0, None
    while total is None or start < total:
        page = fetch(start)
        total = page["total"]
        for r in page["results"]:
            if r.get("reservable") and r.get("entity_id"):
                rows[r["entity_id"]] = {
                    "id": r["entity_id"],
                    "name": " ".join(r["name"].split()).title(),
                    "parent": r.get("parent_name") or "",
                    "state": r.get("state_code") or "",
                }
        start += len(page["results"]) or total
        time.sleep(2)
    out = sorted(rows.values(), key=lambda r: r["name"])
    with open(OUT, "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
        fh.write("\n")
    print("wrote %d campgrounds to %s" % (len(out), OUT))


if __name__ == "__main__":
    main()
