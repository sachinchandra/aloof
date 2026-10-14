"""Low-balance runway, alert emails (dedupe per threshold, reset after top-up), suspension mail, settings, dashboard/pricing copy."""
import time
from fastapi.testclient import TestClient
from paas import api, engine, billing, auth
from paas.db import q
c = TestClient(api.app)
def _acct(email, cents, tier="hobby"):
    a = engine.create_account(email); q("update accounts set credit_cents=?, tier=?, created=? where id=?", (cents, tier, time.time() - 7 * 3600, a["id"])); return a
def _burn(aid, cents_hr):   # 6 hours of metered usage debits at cents_hr
    now = time.time()
    for h in range(6): q("insert into ledger(account_id,ts,amount_cents,kind,ref) values(?,?,?,?,?)", (aid, now - h * 3600 - 60, -cents_hr, "usage", "test"))
def _mails(monkeypatch):
    sent = []; monkeypatch.setattr(auth, "send_mail", lambda to, subj, body, **k: sent.append((to, subj, body))); return sent
def test_runway_levels():
    a = _acct("lb-levels@realmail.dev", 1000); _burn(a["id"], 10)          # $10 at $0.10/h = 100h
    r = billing.runway(a["id"]); assert r["level"] == "ok" and 99 <= r["hours_left"] <= 101 and r["burn_usd_day"] == 2.4
    q("update accounts set credit_cents=500 where id=?", (a["id"],)); assert billing.runway(a["id"])["level"] == "warn"   # 50h
    q("update accounts set credit_cents=100 where id=?", (a["id"],)); assert billing.runway(a["id"])["level"] == "critical"
    f = _acct("lb-free@realmail.dev", 0, "free"); assert billing.runway(f["id"])["level"] == "free"
def test_alerts_dedupe_and_reset(monkeypatch):
    sent = _mails(monkeypatch); a = _acct("lb-dedupe@realmail.dev", 500); _burn(a["id"], 10)   # 50h -> 72h threshold
    billing.check_low_balance(); billing.check_low_balance()
    mine = [m for m in sent if m[0] == "lb-dedupe@realmail.dev"]; assert len(mine) == 1 and "about 3 days" in mine[0][1] and "/dashboard#billing" in mine[0][2]
    q("update accounts set credit_cents=100 where id=?", (a["id"],)); billing.check_low_balance(); billing.check_low_balance()
    mine = [m for m in sent if m[0] == "lb-dedupe@realmail.dev"]; assert len(mine) == 2 and "less than 24 hours" in mine[1][1]
    billing.credit(a["id"], 50, "topup", "t1"); assert (q("select lowbal_sent from accounts where id=?", (a["id"],), one=True)["lowbal_sent"] or "") == ""
    billing.check_low_balance(); assert len([m for m in sent if m[0] == "lb-dedupe@realmail.dev"]) == 3
def test_suspended_mail_and_opt_out(monkeypatch):
    sent = _mails(monkeypatch); a = _acct("lb-susp@realmail.dev", 0); _burn(a["id"], 10)
    billing.check_low_balance(); m = [x for x in sent if x[0] == "lb-susp@realmail.dev"]; assert len(m) == 1 and "suspended" in m[0][1]
    b = _acct("lb-off@realmail.dev", 100); _burn(b["id"], 10); q("update accounts set lowbal_alerts=0 where id=?", (b["id"],))
    billing.check_low_balance(); assert not [x for x in sent if x[0] == "lb-off@realmail.dev"]
def test_custom_threshold_and_api(monkeypatch):
    sent = _mails(monkeypatch); a = _acct("lb-api@realmail.dev", 900); _burn(a["id"], 1)   # 900h, but under a $10 threshold
    h = {"Authorization": f"Bearer {a['api_key']}"}
    assert c.put("/v1/billing/alerts", json={"enabled": True, "threshold_usd": -1}, headers=h).status_code == 400
    r = c.put("/v1/billing/alerts", json={"enabled": True, "threshold_usd": 10}, headers=h).json(); assert r["below_threshold"] and r["threshold_usd"] == 10
    billing.check_low_balance(); m = [x for x in sent if x[0] == "lb-api@realmail.dev"]; assert len(m) == 1 and "$10 alert" in m[0][1]
    assert c.get("/v1/billing/runway", headers=h).json()["level"] == "ok" and "runway" in c.get("/v1/billing", headers=h).json()
def test_copy():
    import os
    d = open(os.path.join(os.path.dirname(__file__), "..", "dashboard", "index.html")).read()
    assert "of credit left at your current usage" in d and "Your apps are suspended." in d and "Low-balance emails" in d and "Estimated runway" in d
    assert "about 3 days left and again under 24 hours" in c.get("/pricing").text
