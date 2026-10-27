"""Owner dashboard stages 3-6: pulse strip, revenue/MRR without double counting, account detail + actions, admin 2FA."""
import json, time, pytest
from test_admin_cockpit import owner, _reg, acct, real_and_test, html, c, CS
from fastapi.testclient import TestClient
from paas import api, www, mfa
from paas.db import q

@pytest.fixture(autouse=True)
def _fresh_login_limits():
    yield; api._login_hits.clear()   # these tests sign in a lot from one test-client IP
def _real():
    n = time.time_ns(); e = f"cust{n}@realco.io"; cl = _reg(e); www.mark_flags(); return cl, acct(e)

def test_revenue_counts_each_source_once():
    _, a = _real(); _, b = _real(); _, cc = _real(); _, t = _real()
    q("update accounts set is_test=1 where id=?", (t["id"],))
    now = time.time(); k = str(time.time_ns())
    rows = [("tp" + k, a["id"], "topup", 2000, "inv_tp" + k), ("inv_tp" + k, a["id"], "invoice", 2360, "inv_tp" + k),   # top-up and its own invoice: once
            ("sub" + k, b["id"], "subscription", None, None), ("in_sub" + k, b["id"], "invoice", 1900, "in_sub" + k),  # subscription money arrives as an invoice
            ("tpt" + k, t["id"], "topup", 99900, None)]                                                                 # test account: ignored
    for i, ac, kind, amt, inv in rows:
        q("insert into stripe_sessions(id,account_id,kind,amount_cents,status,invoice_id,created,updated) values(?,?,?,?,?,?,?,?)", (i, ac, kind, amt, "paid", inv, now, now))
    q("insert into payments(order_id,account_id,amount_paise,credit_inr,status,created,updated) values(?,?,?,?,?,?,?)", ("ord" + k, cc["id"], 50000, 500, "paid", now, now))
    q("update accounts set tier='pro', stripe_subscription=? where id=?", ("sub_" + k, b["id"]))
    q("update accounts set tier='hobby' where id=?", (cc["id"],))
    q("insert into ledger(account_id,ts,amount_cents,kind,ref) values(?,?,?,?,?)", (cc["id"], now, -500, "plan", "hobby plan"))
    q("update accounts set tier='pro' where id=?", (a["id"],))   # paid tier, but no subscription or plan charge: not MRR
    try:
        real = {r["id"] for r in q("select id from accounts where coalesce(is_test,0)=0")}
        r = www.revenue(real, now, now - 60)
        fee = lambda t: www._tier_fee(t)
        assert r["topups_usd"] == 20.0 and r["subscription_invoices_usd"] == 19.0 and r["revenue_this_month_usd"] == 39.0
        assert r["razorpay_inr"] == 500.0 and r["plan_from_balance_usd"] == 5.0
        assert r["mrr_card_usd"] == fee("pro") and r["mrr_balance_usd"] == fee("hobby") and r["mrr_usd"] == fee("pro") + fee("hobby")
        assert r["paid_tier_unfunded"] >= 1
        q("insert into admin_refunds(id,session_id,account_id,amount_cents,reason,created) values(?,?,?,?,?,?)", ("re" + k, "tp" + k, a["id"], 500, "x", now))
        assert www.revenue(real, now, now - 60)["revenue_this_month_usd"] == 34.0
    finally:
        q("delete from stripe_sessions where id like ?", (f"%{k}",)); q("delete from payments where order_id=?", ("ord" + k,)); q("delete from admin_refunds where id=?", ("re" + k,))
        q("delete from ledger where account_id=?", (cc["id"],)); q("update accounts set tier='free', stripe_subscription=null where id in (?,?,?)", (a["id"], b["id"], cc["id"]))

def test_pulse_today_vs_week_real_accounts_only():
    _, a = _real(); now = time.time(); t0 = www._day0(now); k = str(time.time_ns())
    q("update accounts set created=? where id=?", (t0 + 60, a["id"]))
    q("insert into apps(name,account_id,spec,status,created) values(?,?,?,?,?)", ("pa" + k, a["id"], json.dumps({"plan": "nano"}), "running", t0 + 60))
    q("insert into releases(app,version,status,created) values(?,?,?,?)", ("pa" + k, 1, "active", t0 + 120))
    q("insert into releases(app,version,status,created) values(?,?,?,?)", ("pa" + k, 2, "failed", t0 - 2 * 86400))
    q("insert into instances(id,app,status,started) values(?,?,?,?)", ("pi" + k, "pa" + k, "running", now - 60))
    q("insert into ledger(account_id,ts,amount_cents,kind,ref) values(?,?,?,?,?)", (a["id"], t0 - 86400, -700, "usage", "m"))
    q("insert into status_checks(ts,component,ok) values(?,?,?)", (t0 - 86400, "zz" + k, 0))
    q("insert into status_checks(ts,component,ok) values(?,?,?)", (t0 - 86400 + 60, "zz" + k, 0))
    try:
        p = www.metrics()["pulse"]
        assert p["signups"]["today"] >= 1 and p["activated"]["today"] >= 1 and p["activated"]["of_today"] >= 1
        assert p["active_apps"]["now"] >= 1 and p["usage_usd"]["week"] >= 7.0 and p["failed_deploys"]["week"] >= 1 and p["incidents"]["week"] >= 1
        before = www.pulse(set(), now)
        assert before["signups"]["today"] == 0 and before["active_apps"]["now"] == 0   # nothing from excluded accounts
        assert www.snapshot(now) in (True, False) and not www.snapshot(now)
    finally:
        q("delete from apps where name=?", ("pa" + k,)); q("delete from releases where app=?", ("pa" + k,)); q("delete from instances where id=?", ("pi" + k,))
        q("delete from ledger where account_id=?", (a["id"],)); q("delete from status_checks where component=?", ("zz" + k,))
    h = html(); assert "id=pulse" in h and "function pulse(" in h

def test_account_detail_and_actions_need_admin_typed_confirm_and_audit():
    o = owner(); ucl, u = _real(); other, _ = _real(); path = f"/v1/admin/accounts/{u['id']}"
    for cl in (c, other):
        assert cl.get(path).status_code == 401
        assert cl.post(path + "/suspend", json={"suspended": True, "confirm": u["email"], "reason": "x"}, headers=CS).status_code == 401
    assert other.get("/admin").status_code == 404
    d = o.get(path).json(); assert d["profile"]["email"] == u["email"] and "apps" in d and "audit" in d
    assert o.post(path + "/suspend", json={"suspended": True, "confirm": "nope", "reason": "abuse"}, headers=CS).status_code == 400
    assert o.post(path + "/suspend", json={"suspended": True, "confirm": u["email"], "reason": ""}, headers=CS).status_code == 400
    assert o.post(path + "/suspend", json={"suspended": True, "confirm": u["email"], "reason": "test suspend"}, headers=CS).status_code == 200
    assert ucl.get("/v1/apps").status_code == 401   # sessions dropped
    k = ucl.post("/v1/auth/login", json={"email": u["email"], "password": "correct horse 1"}); assert ucl.get("/v1/apps").status_code == 403
    assert o.post(path + "/suspend", json={"suspended": False, "confirm": u["email"], "reason": "done"}, headers=CS).status_code == 200
    assert ucl.get("/v1/apps").status_code == 200
    r = o.post(path + "/credit", json={"amount_usd": 2.5, "confirm": u["email"], "reason": "goodwill"}, headers=CS); assert r.status_code == 200
    assert r.json()["credit_usd"] == round((u["credit_cents"] or 0) / 100 + 2.5, 2)
    assert o.post(path + "/credit", json={"amount_usd": 1, "confirm": u["email"]}, headers=CS).status_code == 400   # reason required
    acts = {x["action"] for x in o.get(path).json()["audit"]}
    assert {"admin.suspend", "admin.unsuspend", "admin.credit"} <= acts
    own = q("select id from accounts where lower(email)=?", (www.ADMIN_EMAIL,), one=True)["id"]
    assert o.post(f"/v1/admin/accounts/{own}/suspend", json={"suspended": True, "confirm": www.ADMIN_EMAIL, "reason": "no"}, headers=CS).status_code == 400

def test_refund_capped_at_original_and_goes_through_stripe(monkeypatch):
    o = owner(); _, u = _real(); k = str(time.time_ns()); calls = []
    from paas import stripe_billing as sb
    monkeypatch.setattr(sb, "_call", lambda m, p, d=None, idem=None, client=None: calls.append((m, p, d)) or {"id": "re_" + str(len(calls)) + k})
    q("insert into stripe_sessions(id,account_id,kind,amount_cents,status,payment_intent,created,updated) values(?,?,?,?,?,?,?,?)", ("cs" + k, u["id"], "topup", 1000, "paid", "pi_" + k, time.time(), time.time()))
    path = f"/v1/admin/accounts/{u['id']}/refund"; body = {"session_id": "cs" + k, "confirm": u["email"], "reason": "billing error"}
    try:
        assert o.post(path, json=body | {"amount_usd": 10.01}, headers=CS).status_code == 400 and not calls
        r = o.post(path, json=body | {"amount_usd": 6}, headers=CS); assert r.status_code == 200 and r.json()["refundable_left_usd"] == 4.0
        assert calls[-1][1] == "/refunds" and calls[-1][2]["amount"] == 600 and calls[-1][2]["payment_intent"] == "pi_" + k
        assert o.post(path, json=body | {"amount_usd": 5}, headers=CS).status_code == 400
        assert o.post(path, json=body | {"amount_usd": 4}, headers=CS).status_code == 200
        assert q("select 1 from ledger where account_id=? and kind='refund'", (u["id"],), one=True)
        assert len([x for x in o.get(f"/v1/admin/accounts/{u['id']}").json()["audit"] if x["action"] == "admin.refund"]) == 2
    finally:
        q("delete from stripe_sessions where id=?", ("cs" + k,)); q("delete from admin_refunds where session_id=?", ("cs" + k,))

def test_read_only_view_blocks_writes_and_exits():
    o = owner(); _, u = _real(); path = f"/v1/admin/accounts/{u['id']}/impersonate"
    assert o.post(path, json={"confirm": "x", "reason": "support"}, headers=CS).status_code == 400
    r = o.post(path, json={"confirm": u["email"], "reason": "support ticket"}, headers=CS); assert r.status_code == 200
    assert o.get("/v1/apps").status_code == 200
    assert o.post("/v1/apps", json={"name": "x" + str(time.time_ns())[-6:], "image": "nginx"}, headers=CS).status_code == 403
    h = o.get("/dashboard"); assert "Read-only view of" in h.text and u["email"] in h.text
    assert o.get("/admin").status_code == 404   # the admin dashboard isn't reachable from inside the view
    e = o.post("/v1/admin/impersonate/exit", headers=CS); assert e.status_code == 200 and e.json()["url"] == "/admin"
    assert o.get("/v1/admin/dashboard").status_code == 200
    assert {"admin.impersonate", "admin.impersonate_exit"} <= {x["action"] for x in q("select action from audit where detail like ?", (f"account={u['id']} %",))}

def test_totp_and_2fa_never_locks_owner_out(monkeypatch):
    s = mfa.new_secret(); assert mfa.check(s, mfa.code(s)) and not mfa.check(s, "000000" if mfa.code(s) != "000000" else "111111")
    assert mfa.code("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", 59) == "287082"   # RFC 6238 SHA-1 vector (last 6 digits)
    o = owner(); oid = q("select id from accounts where lower(email)=?", (www.ADMIN_EMAIL,), one=True)["id"]
    q("update accounts set totp_enabled=0, totp_secret_enc=null, totp_recovery=null where id=?", (oid,))
    monkeypatch.setenv("PAAS_ADMIN_REQUIRE_2FA", "1")
    try:
        assert o.get("/v1/admin/dashboard").status_code == 200   # flag on, not enrolled: still in
        _, u = _real(); assert TestClient(api.app, base_url="http://api.aloof.test").get("/v1/admin/2fa").status_code == 404
        st = o.post("/v1/admin/2fa/start", headers=CS).json(); assert st["uri"].startswith("otpauth://totp/")
        assert o.post("/v1/admin/2fa/confirm", json={"code": "123"}, headers=CS).status_code == 400
        codes = o.post("/v1/admin/2fa/confirm", json={"code": mfa.code(st["secret"])}, headers=CS).json()["recovery_codes"]; assert len(codes) == 8
        assert o.post("/v1/admin/2fa/start", headers=CS).status_code == 409   # can't swap the secret once on
        assert o.get("/v1/admin/dashboard").status_code == 200   # this session was verified by enrolling
        o2 = TestClient(api.app, base_url="http://api.aloof.test"); o2.post("/v1/auth/login", json={"email": www.ADMIN_EMAIL, "password": "correct horse 1"})
        assert o2.get("/v1/admin/dashboard").status_code == 401
        r = o2.get("/admin", follow_redirects=False); assert r.status_code == 303 and r.headers["location"] == "/admin/2fa"
        assert o2.get("/admin/2fa").status_code == 200
        assert o2.post("/v1/admin/2fa/verify", json={"code": codes[0]}, headers=CS).status_code == 200   # recovery code works once
        assert o2.get("/v1/admin/dashboard").status_code == 200
        o3 = TestClient(api.app, base_url="http://api.aloof.test"); o3.post("/v1/auth/login", json={"email": www.ADMIN_EMAIL, "password": "correct horse 1"})
        assert o3.post("/v1/admin/2fa/verify", json={"code": codes[0]}, headers=CS).status_code == 400
        monkeypatch.delenv("PAAS_ADMIN_REQUIRE_2FA"); assert o3.get("/v1/admin/dashboard").status_code == 200   # flag off: as before
    finally:
        q("update accounts set totp_enabled=0, totp_secret_enc=null, totp_recovery=null where id=?", (oid,))
