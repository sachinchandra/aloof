# Support note: FIRST1 / EARLY3

**What they are:** FIRST1 = Hobby free for 1 month; EARLY3 = Hobby free for 3 months. Each: 50 uses max, first-time
customers only, Hobby plan only, expires 1 Jan 2027 01:29 IST (31 Dec 23:59 UTC+4). Full table: `docs/billing-promos.md`.

**"My code doesn't work":**
1. `GET /v1/admin/promos`: is the code still active, with uses remaining and not expired?
2. `GET /v1/admin/accounts/{id}/promo-eligibility`: `eligible_first_time_codes: false` means the account has paid
   through Stripe before (`stripe_paid_charges` / `stripe_paid_invoices` / `local_paid_sessions`), so Stripe rejects it. That's by design.
3. Is the customer subscribing to Hobby? The codes give nothing on Pro or on top-ups.

**Don't:** create, edit or extend codes in Stripe from support. Ask the owner. Don't hand out a code to an existing payer
as a workaround. Offer a credit adjustment through the owner dashboard instead, if the owner approves.
