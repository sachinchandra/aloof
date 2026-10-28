# Analytics events (PostHog US)

Server side: `paas/analytics.py`. Host `POSTHOG_HOST` (default `https://us.i.posthog.com`), key `POSTHOG_API_KEY`; no-op when unset.
Frontend gets both from `GET /v1/config` (`posthog_key`, `posthog_host`; the `phc_` project key is public by design).

**distinct_id** = the account's numeric id as a string, e.g. `"42"` (`id` from `GET /v1/auth/me`). The frontend must call `posthog.identify(String(me.id))`.

Rules: properties are allow-listed per event, strings are stripped of control chars, truncated (UTM 100 chars, others 200) and any email or
key-looking string is replaced with `[redacted]`. Referrers keep only scheme://host/path. Accounts with `is_test=1` are never sent.
Sending is a background queue + one thread, 2 s timeout; failures are dropped and never affect requests.

| Event | When | Properties |
|---|---|---|
| `signup_started` | browser: Create account clicked, or sign-in link requested while on the Create account tab (`aloofTrack`, no account yet) | `method` (`password`\|`magic_link`) |
| `signup_completed` | account created via `POST /v1/signup`, `POST /v1/auth/register`, or first magic-link login (`GET /auth/magic`) | `method` (`api`\|`password`\|`magic_link`), `utm_source`, `utm_medium`, `utm_campaign`, `utm_term`, `utm_content`, `referrer` (all optional strings); also `$set_once` person props `initial_utm_*`, `initial_referrer` |
| `first_deploy_succeeded` | first successful deploy of the account (once per account) | `app_id` (app name), `region`, `source_type` |
| `deploy_succeeded` | every successful release (not scale-only) | `app_id`, `region`, `source_type` (`github`\|`git`\|`upload`\|`path`\|`image`\|`postgres`\|`other`), `version` (int) |
| `mcp_connected` | OAuth authorization code exchanged in mcp_server | `client_name`, `client_host` (redirect URI host) |
| `plan_upgraded` | Stripe webhook: subscription checkout completed, or subscription moved to a higher tier | `plan` (`hobby`\|`pro`), `amount_usd` (number), `interval` (`month`) |
| `gpu_waitlist_joined` | new GPU waitlist entry whose email matches an account | `gpu` (plan id or absent) |
| `byo_waitlist_join` | signed-in user joins the "deploy to your own server" early-access waitlist (`POST /v1/waitlist/byo-server`, dashboard). Sent once per account; a repeat join sends nothing | `provider` (`hetzner`\|`digitalocean`\|`aws`\|`other`\|`none`) |

Also accepted: the site's nested `"attribution": {...}` object (same keys). `/v1/config` returns `posthog_key`, `posthog_host` (and the older `posthog: {key, host}`).
Frontend passes UTM/referrer as optional JSON fields on the signup bodies: `{"email", "password", "utm_source", "utm_medium", "utm_campaign", "referrer"}`
(`/v1/auth/register`), `{"email", ...utm}` (`/v1/auth/magic`, `/v1/signup`). Send the first-touch values stored at landing.

## Browser events (paas/static/analytics.js, site + console)

| Event | When | Properties |
|---|---|---|
| `$pageview` | every page load | `utm_*` from the URL (if present), `referrer` (external, no query), and first-touch `first_utm_source/medium/campaign/term/content`, `first_referrer`, `first_landing` (kept in sessionStorage from the first page of the visit) |
| `cta_clicked` | click on any `[data-cta]` element | `cta` (`deploy_free` sign-up buttons, `login`, `pricing_<plan>`, `connect_mcp`, `github_signin`/`google_signin`/`x_signin`, `deploy_app`, `subscribe_card`, `<promo>_claim` e.g. `first1_claim` = Subscribe by card after landing with `?promo=FIRST1`), `location`, `href` (no query), `promo` (claim only) |
| `$autocapture` | click on any link/button/summary/[data-cta] (own cookieless autocapture; posthog-js is not used) | `$event_type`=`click`, `$elements_chain` (tag, classes, id, href without query, button/link text ≤60 chars, data-cta; never input values), `$el_text` |
| `signup_started` | Create account clicked / sign-in link requested on the Create tab | `method` |
| `$identify` | console load after login (distinct_id = account id) | `$set_once`: `initial_utm_*`, `initial_referrer`, `initial_landing` |

Key: `POSTHOG_API_KEY` (env, deploy/golive.env); never hardcoded. `POSTHOG_KEY` / `data/posthog_key` are legacy fallbacks.


All browser events carry first-touch `first_utm_*`, `first_referrer`, `first_landing`, `first_promo`. Visiting with `?aloof_test=1` (or `is_test=1`) marks the tab: every event gets `is_test=true` and so does the person on identify.
Social sign-up (GitHub/Google/X): the login page appends first-touch UTM to `/auth/{p}/start`; it is kept in an HttpOnly `aloof_attr` cookie (path /auth/, same TTL as the OAuth state) and attached to `signup_completed`.
