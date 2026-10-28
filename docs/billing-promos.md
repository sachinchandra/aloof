# Promo codes (billing)

Promo codes are Stripe promotion codes. Stripe is the source of truth; admins can read the live state at
`GET /v1/admin/promos` (redemptions used/max, expiry, restrictions) and check one account at
`GET /v1/admin/accounts/{id}/promo-eligibility`. Both make read-only Stripe calls.

| Code | Discount | Applies to | First-time only | Cap | Expires |
|---|---|---|---|---|---|
| FIRST1 | 100% off for 1 month | Hobby plan only | Yes | 50 redemptions total | 31 Dec 2026, 23:59 (UTC+4) = 1 Jan 2027 01:29 IST |
| EARLY3 | 100% off for 3 months | Hobby plan only | Yes | 50 redemptions total | 31 Dec 2026, 23:59 (UTC+4) = 1 Jan 2027 01:29 IST |

- **First-time only** is Stripe's rule: the code is rejected if the Stripe customer has any earlier successful payment
  (a top-up, a subscription invoice, anything). Razorpay payments (legacy) are not seen by Stripe.
- **Hobby only**: the coupon is limited to the Hobby product, so it gives nothing on Pro or on credit top-ups.
- After the free month(s) the subscription renews at the normal Hobby price unless cancelled.
- One code per subscription; codes don't stack. Customers enter them on the Stripe checkout page ("Add promotion code").
