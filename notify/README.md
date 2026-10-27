# notify

Delivery channels plus an SSRF-safe HTTPS transport. Imports nothing else
from this repo; stdlib only. Usable on its own.

| Module | Job |
|---|---|
| `channels.py` | `DiscordWebhookChannel`, `NtfyChannel`, `make_default_channels(http_post)` |
| `webhook.py` | signed `WebhookChannel` (`X-Dropwatch-Signature: sha256=<hmac>`, schema `dropwatch.event.v1`), `is_safe_url`, `is_public_ip`, `generate_secret` |
| `transport.py` | `http_post`, `http_post_status`, `OPENER`: resolve once, refuse non-public IPs, dial the vetted IP (no DNS-rebinding gap), never follow redirects, verify TLS against the hostname |

Each channel takes an injected `http_post`, so tests never touch the
network. In production you pass `transport.http_post`.

Errors: `DeliveryError(status)`, `RateLimitedError` (has `retry_after`),
and `PermanentDeliveryError` / `WebhookGone` (both `permanent = True`).
Messages carry only a status code, never an address, URL, or secret, so
they are safe to log.

```python
from notify import channels, transport
ntfy = channels.NtfyChannel(transport.http_post)
ntfy.send("my-topic", "Site 42 opened for Nov 14")
```

## Tests

From the repo root:

    python3 -m unittest discover -s notify/tests -t .
