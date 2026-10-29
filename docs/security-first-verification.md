# First verification of a pre-existing account (F8, pre-account takeover)

Password and API signups don't prove the address, so an attacker could sign up with a victim's email first.
When an **unverified** account is verified for the first time (magic link; OAuth never adopts an unverified
account, it refuses with `email_linked_other`), `auth.secure_first_verification` runs before `email_verified=1`:

- the primary API key is rotated; every extra API key (incl. MCP OAuth-issued keys) and every deploy token is deleted;
- every session for the account is ended (the magic-link response then starts one fresh session);
- **unless** the link is opened in the same browser session that signed up, the password is cleared
  (sign in by email link or social, then set a new password in Account);
- the new API key is shown once on the dashboard (`/v1/auth/me` → `new_api_key`, kept 24h, deleted when read). It is never emailed.

The normal "sign up with a password, then click the link in the same browser" flow keeps the password; only the key changes.
Accounts that are already verified are unaffected.

## Round 3 (QA retest of e3fb213)
- **No same-session exception (F8a).** Cookie tossing from a tenant app on `<app>.aloof.si` could plant the attacker's session in the victim's browser, so the password is now always cleared at first verification. `same_session` is ignored.
- **`__Host-aloof_session` cookie (F8a).** When cookies are Secure (prod), the session cookie is `__Host-aloof_session` (Secure, Path=/, no Domain), which no subdomain can set or overwrite. The old `aloof_session` name is **not** accepted, because a tossed cookie can't be told apart from a real one. Everyone is logged out once. Any request that still carries `aloof_session` gets it expired both host-only and at `Domain=aloof.si`, which also removes a copy a tenant subdomain set.
- **Edge filter (F8a).** The edge proxy (`api.edge`, :8080, which serves every tenant app) drops tenant `Set-Cookie` headers that use the session name or `Domain=aloof.si`. This is done in Python rather than Caddy, so there's no Caddyfile change and `supervisord.conf` is unchanged. A side fix: multiple tenant Set-Cookie headers are no longer collapsed into one.
- **Identities (F8b).** Social identities linked while the account was unverified are deleted. Linking (`/auth/{p}/connect` and `oauth_login.link`) is refused until the email is verified.
- **Ordering (F8c).** Everything runs in one DB transaction, in this order: clear the password, rotate the key, delete extra keys, deploy tokens and identities, mark verified, then end sessions last.
- **Recommendation (needs sachin's decision; infra spend):** serve tenant apps from a separate registrable domain (e.g. `aloofapp.net`) and add it to the Public Suffix List. That ends cookie tossing at its root.
