# Lifecycle (onboarding) emails — PENDING sachin's approval
Off unless `LIFECYCLE_EMAILS_ENABLED=1`. `lifecycle.tick` runs every 10 min: welcome_onboarding (0–2d), no_deploy_24h (1–3d, no apps), day4_tip (4–6d).
A row in `lifecycle_mail` is claimed before sending (idempotent), max 1 lifecycle email per account per 24h, test and opted-out accounts skipped.
Signed one-click unsubscribe (`/email/unsubscribe`, RFC 8058 headers) + dashboard toggle (`PUT /v1/account/email-prefs`).
With the flag off, the old password-only welcome is sent; with it on, every signup method gets exactly one onboarding welcome (password signups include the API key block).
Copy: /workspace/aloof-onboarding-emails.md (Sales). UTM: email/onboarding/onboarding_v1/e1–e3.
