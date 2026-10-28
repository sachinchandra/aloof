# Owner 2FA: turning on PAAS_ADMIN_REQUIRE_2FA (not on yet)

How it works now:
- Suspend, credit, refund and read-only view always need an owner session that has 2FA turned on and verified, whatever the flag says.
- The static admin Bearer token works only on infra routes (nodes, edge, backups) and the read-only `/v1/admin/metrics`.
- `PAAS_ADMIN_REQUIRE_2FA=1` also requires a verified 2FA session for the rest of the owner dashboard. It only applies once the owner has enrolled, so an owner who hasn't enrolled is never locked out, and `/admin/2fa` always stays reachable.
- Ten wrong codes in 15 min lock 2FA for 30 min and email sachin@aloof.si.

## Steps (do them in order)
1. Sign in as sachin@aloof.si and open https://www.aloof.si/admin. Click **Turn on 2FA** (top bar), or go to https://www.aloof.si/admin/2fa.
   Scan the QR code, enter the 6-digit code, and save the 8 recovery codes offline. The button then reads "2FA ✓".
2. On prod (`ssh root@178.104.200.75`):
   ```
   cd /workspace/paas && cp deploy/golive.env deploy/golive.env.bak.$(date +%s) && \
   (grep -q '^PAAS_ADMIN_REQUIRE_2FA=' deploy/golive.env && sed -i 's/^PAAS_ADMIN_REQUIRE_2FA=.*/PAAS_ADMIN_REQUIRE_2FA=1/' deploy/golive.env || echo PAAS_ADMIN_REQUIRE_2FA=1 >> deploy/golive.env) && \
   bash deploy/golive.sh
   ```
3. Verify: in a fresh private window, sign in as the owner. `/admin` should redirect to `/admin/2fa`. Enter the code and the dashboard should load.
   Rollback: set the value back to `0` (or delete the line) and run `bash deploy/golive.sh` again.
