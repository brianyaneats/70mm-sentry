# billing

Stripe without the SDK or any outbound call: a Payment Link to start
checkout, the customer portal to cancel, and one signed webhook that
grants or revokes access. Imports nothing else from this repo; stdlib
only.

- `stripe.checkout_url(payment_link, subscription_id)` appends `client_reference_id=w<id>`.
- `stripe.verify_signature(raw_body, header, whsec, now)` checks `Stripe-Signature` (HMAC-SHA256 over `t.body`, 300s window, any `v1` may match).
- `stripe.handle_event(store, event, now)` is idempotent and safe under redelivery and reordering:
  - `checkout.session.completed` calls `store.grant(...)`.
  - `customer.subscription.updated` / `.deleted` calls `store.set_by_stripe_subscription(...)`. `active`, `trialing`, and `past_due` stay live.
  - Anything else is acknowledged and ignored.

`store` is any object with those two methods. Dropwatch passes
`engine.core.Entitlements(conn)`. Call `handle_event` only after
`verify_signature` passes.

## Tests

From the repo root:

    python3 -m unittest discover -s billing/tests -t .
