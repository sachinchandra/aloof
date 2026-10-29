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
