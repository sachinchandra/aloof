# Social sign-in setup (Google, GitHub, X)

Each provider is enabled when **both** of its env vars are set in `deploy/golive.env`. The buttons stay hidden until then
(`GET /v1/auth/providers` reports `enabled:false`). After editing golive.env, redeploy:
`cd /workspace/paas && umask 022 && bash deploy/golive.sh`.

All flows start and end on the site host **https://www.aloof.si**. That is `PAAS_SITE_URL`; you can override it with
`PAAS_OAUTH_BASE_URL`. A start on api.aloof.si is redirected there first. Register exactly these redirect URIs:

| Provider | Redirect / callback URI (prod) |
|---|---|
| Google | `https://www.aloof.si/auth/google/callback` |
| GitHub | `https://www.aloof.si/auth/github/callback` |
| X | `https://www.aloof.si/auth/x/callback` |

## Google (OIDC)
1. Go to https://console.cloud.google.com/ and create or pick a project (e.g. "Aloof").
2. **APIs & Services → OAuth consent screen** (Google Auth Platform → Branding):
   - App name **Aloof**, support email, logo optional.
   - App domain `https://www.aloof.si`, privacy `https://www.aloof.si/privacy`, terms `https://www.aloof.si/terms`.
   - Authorized domain `aloof.si`. Audience: **External**.
   - Data access / scopes: `openid`, `.../auth/userinfo.email`, `.../auth/userinfo.profile`. These are non-sensitive, so no verification review is needed.
   - **Publish** the app (testing mode only allows listed test users).
3. **Credentials → Create credentials → OAuth client ID**:
   - Type **Web application**, name "Aloof web login".
   - Authorized redirect URI `https://www.aloof.si/auth/google/callback`. No JavaScript origins are needed.
4. Put the values in golive.env:
   ```
   GOOGLE_OAUTH_CLIENT_ID=<...apps.googleusercontent.com>
   GOOGLE_OAUTH_CLIENT_SECRET=<GOCSPX-...>
   ```
Scopes requested: `openid email profile`. PKCE is S256 plus a nonce. The id_token is verified against Google's JWKS
(signature, iss, aud, exp, nonce). The email is trusted only when `email_verified` is true.

## GitHub (separate OAuth App, not the aloof-deploy GitHub App)
Checked: the existing **aloof-deploy** GitHub App has client credentials, but its permissions are only checks, contents,
deployments, metadata, pull_requests and statuses. It has no *Email addresses* permission, so `/user/emails` would fail.
Reusing it would mean adding a user permission and a second callback URL to the deploy app. Every installing org would
then be asked to re-approve, and login would be coupled to the deploy integration. So login uses its own OAuth App.
1. Go to https://github.com/settings/developers (or the org's settings) → **OAuth Apps → New OAuth App**:
   - Application name **Aloof**.
   - Homepage URL `https://www.aloof.si`.
   - Authorization callback URL `https://www.aloof.si/auth/github/callback`.
   - Leave "Enable Device Flow" off.
2. **Generate a new client secret**.
3. Put the values in golive.env:
   ```
   GITHUB_OAUTH_CLIENT_ID=<Ov23...>
   GITHUB_OAUTH_CLIENT_SECRET=<...>
   ```
   These names are deliberately different from the deploy app's `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET`.

Scopes requested: `read:user user:email`, with PKCE S256. Only the **primary + verified** email from `/user/emails` is used.

## X (OAuth 2.0 + PKCE)
1. Go to https://developer.x.com/en/portal/dashboard and open the project/app (or create app "Aloof").
2. **User authentication settings → Set up**:
   - App permissions: **Read**.
   - "Request email from users": **on**. This needs the privacy and terms URLs below.
   - Type of App: **Web App, Automated App or Bot** (a confidential client).
   - Callback URI `https://www.aloof.si/auth/x/callback`.
   - Website `https://www.aloof.si`, Terms `https://www.aloof.si/terms`, Privacy `https://www.aloof.si/privacy`.
3. **Keys and tokens → OAuth 2.0 Client ID and Client Secret**. Copy both.
4. Put the values in golive.env:
   ```
   X_OAUTH_CLIENT_ID=<...>
   X_OAUTH_CLIENT_SECRET=<...>
   ```
Scopes requested: `users.read tweet.read users.email`. The token endpoint is called with HTTP Basic client auth.

If X returns `confirmed_email`, that address is treated as verified. If it returns no email (setting off, or no confirmed
address), the user lands on `/auth/x/email`, enters an address and clicks the magic link. Only then is the account
created or the X identity attached.

## Behaviour summary
- Account linking happens only by a provider-**verified** email that matches an Aloof account whose own email is verified.
  If the Aloof account is unverified, or the provider email is unverified but an account exists, the result is
  `auth_error=email_linked_other`.
- A signed-in user who starts a flow **connects** that provider to their account (Settings → Sign-in methods,
  `GET/DELETE /v1/auth/identities[/{provider}]`).
- New accounts go through the same `engine.create_account` as magic-link signups (free tier, same limits), and fire
  `signup_completed` with `method` = google, github or x. Starts are rate-limited per IP like logins.
- Error redirect: `/login?auth_error=<code>&provider=<p>&next=<path>`, where `<code>` is one of: cancelled,
  email_unverified, email_linked_other, no_email (reserved, unused: X without email goes straight to the email page),
  state_invalid, provider_error, rate_limited, provider_disabled, already_linked.
