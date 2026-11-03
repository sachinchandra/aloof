# FIRST1 + Stripe prep (finance, Nov 3 2026) — for sachin's review

Branch `finance/first1-stripe-prep`. **Nothing deployed, nothing merged, no Stripe writes.** There's no Stripe test key (prod has only a live `sk_live` key, length 107), so I checked live with read-only GETs, run on prod so the key never left the server.

## Read-only findings (live GETs, Nov 3)
| Object | Result |
|---|---|
| `promo_1UV39zA2Yc23CAveZMaVFLRA` (FIRST1) | active, **times_redeemed 0**, max 50, expires 1798747199 (Dec 31 2026 23:59:59 UTC+4), first_time_transaction=true, coupon GDLsTF74 |
| coupon `GDLsTF74` | 100% off, repeating 1 month, times_redeemed 0, **applies_to = {products: [prod_VQzeuLD1pWEmEI]} (Aloof Hobby)**, **not None** |
| EARLY3 `promo_1UUgeFA2Yc23CAveBBZzToEr` → coupon `her8ewZE` | 100% off, 3 months, 0 redemptions, applies_to = Hobby product already |
| Prices | Hobby $5/mo on prod_VQzeuLD1pWEmEI; Pro $20/mo on prod_VQzeDPSFqZ48jV; both tax_behavior=exclusive |
| Webhook endpoint (api.aloof.si/v1/billing/stripe/webhook) | events: checkout.session.completed, checkout.session.async_payment_succeeded, invoice.paid, customer.subscription.updated, customer.subscription.deleted. **invoice.payment_failed is not subscribed.** |
| Account | country AE, default currency aed, charges_enabled |
| Stripe Tax settings | status active, head office AE, default tax code txcd_10103000 (SaaS, business use), tax_behavior inferred_by_currency, **0 registrations** |
| prod env | STRIPE_TAX=0; the new flags are unset (so they're off) |

## 1. Hobby-only promo codes
Code: `allow_promotion_codes` is now sent only on Hobby subscription checkouts (`promo_policy.offer_promo_codes`). Pro checkouts and top-ups have no promo field.

**Stripe live changes:** the brief assumed GDLsTF74 had applies_to=None, but it is **already restricted to the Hobby product**, and so is EARLY3's coupon. Rebuilding the coupon and code would only add risk, so my recommendation is **no Stripe change for item 1**.
If you still want a fresh object (for example to add `max_redemptions`/`redeem_by` on the coupon itself), the exact calls would be the ones below. Order matters, because promotion codes must be unique among active codes:
1. `POST /v1/promotion_codes/promo_1UV39zA2Yc23CAveZMaVFLRA` `active=false` (frees the code "FIRST1"; 0 redemptions, so nothing is lost)
2. `POST /v1/coupons` `percent_off=100 duration=repeating duration_in_months=1 applies_to[products][0]=prod_VQzeuLD1pWEmEI name="First month Hobby free (FIRST1)" max_redemptions=50 redeem_by=1798747199` + Idempotency-Key
3. `POST /v1/promotion_codes` `coupon=<new> code=FIRST1 max_redemptions=50 expires_at=1798747199 restrictions[first_time_transaction]=true` + Idempotency-Key

FIRST1 is unavailable between steps 1 and 3, so run them back to back.

## 2. One promo per person: `PAAS_PROMO_ONE_PER_PERSON=off|flag|block` (default off)
- Each paid, discounted subscription checkout is stored in `promo_redemptions` (migration v23) with the account, promo, normalized email and card fingerprint. The email is normalized by lowercasing and stripping `+tag`; dots are removed only for gmail/googlemail. The card fingerprint is read with a GET on the subscription's default_payment_method, and only when the flag is on.
- If the same account, normalized email or card has redeemed before, the redemption is a repeat. A repeat writes the audit row `billing.promo_repeat`, including the matching accounts.
- `flag`: record and audit only. `block`: additionally, checkout stops offering the promo field to an account/email already known to have redeemed. This happens **before payment**, so there are no refunds or cancellations.
- Repeats seen only after payment (a new account and email but the same card) are flagged, never auto-cancelled. Auto-cancelling would mean a Stripe write from the webhook plus a support ticket, so a human decides.

Tradeoffs:
- Stripe's `first_time_transaction` already blocks reuse per *Stripe customer*. It doesn't catch new accounts, because each new account gets a new customer.
- Email normalization catches easy abuse. Dot-stripping on non-Gmail domains would cause false positives, so it's only done for Gmail.
- The card fingerprint is the strongest signal, but it's only known after checkout. Blocking on it would need a pre-checkout SetupIntent, which means more friction and more code.
- Shared family or company cards cause false positives, which is why repeats are flagged rather than refused.
- Privacy: a normalized email and fingerprint are stored per redemption, covered by the existing billing-data retention.

## 3. Failed payments: `PAAS_PAST_DUE_ACTION=none|limit|free` (default none)
- Handles `invoice.payment_failed` (subscription invoices only) and `customer.subscription.updated` with status=past_due. It sets `accounts.past_due_since` and writes the audit row `billing.past_due`.
- `limit`: pauses running apps (same as the Free cap) but keeps the plan. `free`: drops to Free but **keeps the subscription id**.
- Restore: `invoice.paid` for that subscription, or status=active, clears past_due and restores the tier from the price, or un-pauses the apps.
- `unpaid`/`canceled` still drop to Free (existing behaviour).
- Recommendation: `limit`. Stripe Smart Retries take about 1–3 weeks, and customers keep their plan settings while apps pause. `free` is harsher, but it also stops Hobby/Pro-only sizes.

**Stripe live change (planned, not done):** add `invoice.payment_failed` to the endpoint. Read the endpoint id (we_…) with GET /v1/webhook_endpoints, then
`POST /v1/webhook_endpoints/<we_id>` with `enabled_events[]` = checkout.session.async_payment_succeeded, checkout.session.completed, customer.subscription.deleted, customer.subscription.updated, invoice.paid, **invoice.payment_failed**. This replaces the whole list, so send all six.
Without this change, past_due still arrives through customer.subscription.updated, so the code works with the current subscription.
Also check Dashboard → Billing → Revenue recovery: Smart Retries on, and "if all retries fail → cancel or mark unpaid". Both already map to Free.

## 4. $5 starting credit stacking with the free month: `PAAS_STARTER_CREDIT_PROMO=keep|clawback` (default keep)
Today every account gets `credit_cents` default 500 (db.py column default).

Options:
- (a) Keep both. Simplest, but FIRST1 plus the credit gives $10 of free usage per throwaway account.
- (b) **Claw back the unspent starting credit when a promo makes the first Hobby month free (implemented).** It only applies when the account has never topped up, so paid money is never touched. It debits min($5, balance) as ledger kind `starter_clawback`.
- (c) Grant the credit only after email verification. This means changing the column default plus a grant hook in verification, and it affects every signup, not only promo users.
- (d) Grant the credit only after the first payment. It kills the free try-out, which is the reason the credit exists.

Recommendation: (b) now, and possibly (c) later as general anti-abuse.
Copy needs updating if (b) goes on: site.py says "The $5 starting credit every new account gets…". Add "(not combined with a free-month promo code)" to the FAQ/terms and the FIRST1 lifecycle email **before** enabling the flag.

## 5. Stripe Tax for Enigmatic IT Solutions FZE (UAE)
Not legal or tax advice. Confirm with the FZE's accountant, especially free-zone status for VAT.
- **UAE VAT (5%)**: registration with the FTA is mandatory above AED 375,000 of taxable supplies per 12 months and voluntary above AED 187,500. A free-zone FZE is *not* a "designated zone" for services, so SaaS is in scope. Exports of services to customers outside the GCC can be zero-rated if the conditions are met. Once the TRN is issued, add the AE registration in Stripe Tax.
- **Foreign obligations for B2C digital services** (B2B is generally reverse charge if a valid tax ID is collected):
  - EU: VAT from the first euro for non-EU sellers, via non-Union OSS (one registration, e.g. in Ireland).
  - UK: VAT from £0 for non-UK sellers.
  - Others: Norway, Switzerland (CHF 100k worldwide), Australia (AUD 75k), Singapore, India (OIDAR), Saudi Arabia, Canada, and US states (economic nexus, mostly $100k; many tax SaaS).
  - Practical start: AE only, monitor with Stripe's threshold alerts, and register in the EU (OSS) and UK once B2C revenue there is non-trivial, since both have a zero threshold.
- **Tax code**: account default txcd_10103000 (SaaS, business use) is already set. Consider txcd_10103001 (SaaS, personal use) if most users are individuals. Set it on both products explicitly (`tax_code` on prod_VQzeuLD1pWEmEI / prod_VQzeDPSFqZ48jV) so it doesn't depend on the default. Use the same for top-up line items (credit is prepaid usage, also SaaS).
- **Price tax behaviour**: both prices are already `exclusive` (tax added on top of $5/$20), matching the site copy "Prices are USD excl. sales tax/VAT". Keep exclusive. Prices can't change tax_behavior once set, so switching to inclusive would need new prices.
- **Customer address**: with `STRIPE_TAX=1`, the code already sends `automatic_tax.enabled`, `tax_id_collection.enabled` and `customer_update[address]=auto`. Checkout collects the billing address when needed. Also consider `billing_address_collection=required` for consistent evidence (location proof for the EU needs 2 non-conflicting pieces: billing country plus card BIN/IP).
- **Invoices**: subscription invoices are automatic; top-ups use invoice_creation. Settings needed:
  - Invoice template: legal name "Enigmatic IT Solutions FZE", address, TRN once issued.
  - UAE tax invoices need the words "Tax Invoice" and the TRN.
  - Show the customer tax ID, and turn on the reverse-charge note for B2B.
- **Cost of Stripe Tax**: Tax Basic is **0.5% per transaction where you're registered** on Checkout/Billing. That's $0.025 on a $5 Hobby invoice and $0.10 on Pro. Transactions in places where we're not registered aren't charged. API-only integrations cost AED 2 per transaction. Tax Complete (registrations and filings done by Stripe) starts at AED 330/month on a 1-year contract (2 registrations, 200 transactions/month). Basic is enough to start. Sources: stripe.com/ae/tax/pricing, checked Nov 3 2026.
- **Turning it on**: register in Stripe Tax (AE), set the product tax codes, then set `STRIPE_TAX=1` in golive.env and restart. That last step is a deploy, so it waits for your decision.

## Open decisions for sachin
1. Item 1: keep the existing FIRST1/GDLsTF74 (already Hobby-only, my recommendation), or recreate it anyway?
2. One-promo policy mode: off, flag (my recommendation for launch) or block?
3. Past-due action: limit (my recommendation), free or none. Add invoice.payment_failed to the webhook (live change above)?
4. Starter credit: enable clawback, and approve the copy change?
5. Tax: VAT registration timing with the FTA, EU OSS/UK registration, personal- or business-use tax code, Tax Basic vs Complete, and when to flip STRIPE_TAX=1.
