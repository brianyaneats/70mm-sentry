# app (Dropwatch)

The product: campsite-cancellation alerts for any reservable
recreation.gov campground. Wires `sources`, `engine`, `notify`, and
`billing` into one process. stdlib only.

| File | Job |
|---|---|
| `serve.py` | composition root: config, then API + poller thread + runner thread. SIGTERM stops them cleanly. `python3 -m app.serve` |
| `config.py` | every env var, validated at boot (exits 2 with one line per problem) |
| `api.py` | HTTP API + static UI, per-IP POST rate limit, Stripe webhook endpoint |
| `probe.py` | one real recreation.gov poll pass from this box, without touching the db: `python3 -m app.probe 232447` |
| `static/` | `index.html`, `how.html`, `terms.html`, `privacy.html` |
| `campgrounds.json` | 4,955 reservable campgrounds (picker index) |
| `build_campgrounds.py` | rebuilds the index from a RIDB export |

Flow: pick a campground and dates, then `POST /api/subscribe`. A 6-digit
code goes over the chosen channel (ntfy, Discord, or signed webhook).
`POST /api/verify` makes the watch live. The poller watches only
campgrounds and dates with live subscriptions, and an opening produces an
alert with a booking link. Paid destinations (Stripe) get it at once;
free ones get it 60s later.

Local demo (fake channels, codes printed to the console):

    python3 -m app.api --demo    # http://127.0.0.1:8787

Production: see `deploy/README.md`.

## Tests

From the repo root:

    python3 -m unittest discover -s app/tests -t .
