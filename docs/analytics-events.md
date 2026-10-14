# Analytics events (PostHog EU)

Server side: `paas/analytics.py`. Host `POSTHOG_HOST` (default `https://eu.i.posthog.com`), key `POSTHOG_API_KEY`; no-op when unset.
Frontend gets both from `GET /v1/config` (`posthog_key`, `posthog_host`; the `phc_` project key is public by design).

**distinct_id** = the account's numeric id as a string, e.g. `"42"` (`id` from `GET /v1/auth/me`). The frontend must call `posthog.identify(String(me.id))`.

Rules: properties are allow-listed per event, strings are stripped of control chars, truncated (UTM 100 chars, others 200) and any email or
key-looking string is replaced with `[redacted]`. Referrers keep only scheme://host/path. Accounts with `is_test=1` are never sent.
Sending is a background queue + one thread, 2 s timeout; failures are dropped and never affect requests.

| Event | When | Properties |
|---|---|---|
| `signup_completed` | account created via `POST /v1/signup`, `POST /v1/auth/register`, or first magic-link login (`GET /auth/magic`) | `method` (`api`\|`password`\|`magic_link`), `utm_source`, `utm_medium`, `utm_campaign`, `referrer` (all optional strings) |
| `first_deploy_succeeded` | first successful deploy of the account (once per account) | `app_id` (app name), `region`, `source_type` |
| `deploy_succeeded` | every successful release (not scale-only) | `app_id`, `region`, `source_type` (`github`\|`git`\|`upload`\|`path`\|`image`\|`postgres`\|`other`), `version` (int) |
| `mcp_connected` | OAuth authorization code exchanged in mcp_server | `client_name`, `client_host` (redirect URI host) |
| `plan_upgraded` | Stripe webhook: subscription checkout completed, or subscription moved to a higher tier | `plan` (`hobby`\|`pro`), `amount_usd` (number), `interval` (`month`) |
| `gpu_waitlist_joined` | new GPU waitlist entry whose email matches an account | `gpu` (plan id or absent) |

Also accepted: the site's nested `"attribution": {...}` object (same keys). `/v1/config` returns `posthog_key`, `posthog_host` (and the older `posthog: {key, host}`).
Frontend passes UTM/referrer as optional JSON fields on the signup bodies: `{"email", "password", "utm_source", "utm_medium", "utm_campaign", "referrer"}`
(`/v1/auth/register`), `{"email", ...utm}` (`/v1/auth/magic`, `/v1/signup`). Send the first-touch values stored at landing.
