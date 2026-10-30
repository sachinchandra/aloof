"""QA oct30 account-delete regressions D1-D8."""
import time, hashlib, pytest
from fastapi import HTTPException
from paas import engine, account_delete as ad, billing, stripe_billing as sb, parity
from paas.db import q, kv_put
from test_account_delete import calls, _victim   # noqa: F401 (fixture)

def test_d1_suspend_and_revoke_committed_before_infra(calls, monkeypatch):
    a = _victim(); i = a["id"]; seen = {}
    q("insert into sessions(token_hash,account_id,created,expires) values(?,?,?,?)", (f"s{i}", i, time.time(), time.time() + 99))
    def fd(name, with_volumes=False):
        r = q("select suspended, deleting from accounts where id=?", (i,), one=True)
        seen.update(r=dict(r), sess=q("select count(*) n from sessions where account_id=?", (i,), one=True)["n"],
                    keys=q("select count(*) n from api_keys where account_id=?", (i,), one=True)["n"])
        q("update apps set status='destroyed' where name=?", (name,))
    monkeypatch.setattr(engine, "destroy_app", fd)
    ad.delete_account(i)
    assert seen["r"]["suspended"] and seen["r"]["deleting"] and seen["sess"] == 0 and seen["keys"] == 0
    assert calls.get("acct_sweep") == [i]

def test_d1_deploy_volume_pg_refused_while_deleting():
    a = _victim(); i = a["id"]; q("update accounts set deleting=1 where id=?", (i,))
    with pytest.raises(PermissionError): engine.deploy(i, {"name": f"zz{i}", "image": "nginx"})
    with pytest.raises(PermissionError): engine.create_volume(i, f"vz{i}", 1)
    with pytest.raises(PermissionError): parity.pg_create(i, f"pz{i}")
    q("update accounts set deleting=0, suspended=1 where id=?", (i,))
    with pytest.raises(PermissionError): engine.deploy(i, {"name": f"zz{i}", "image": "nginx"})

def test_d1_rescan_catches_app_created_mid_delete(calls, monkeypatch):
    a = _victim(); i = a["id"]; done = []
    def fd(name, with_volumes=False):
        done.append(name); q("update apps set status='destroyed' where name=?", (name,))
        if len(done) == 1:
            now = time.time(); q("insert into apps(name,account_id,status,created,updated) values(?,?,?,?,?)", (f"late{i}", i, "queued", now, now))
    monkeypatch.setattr(engine, "destroy_app", fd)
    ad.delete_account(i)
    assert f"late{i}" in done and not q("select 1 from apps where account_id=?", (i,), one=True)

def test_d3_stripe_and_oauth_untouched_when_infra_fails_and_resumable(calls, monkeypatch):
    a = _victim(); i = a["id"]; q("update accounts set stripe_subscription='sub_x' where id=?", (i,))
    stripe = []; monkeypatch.setattr(sb, "_call", lambda *x, **k: stripe.append(x))
    def boom(name, with_volumes=False): raise RuntimeError("docker down")
    monkeypatch.setattr(engine, "destroy_app", boom)
    with pytest.raises(HTTPException) as e: ad.delete_account(i)
    assert e.value.status_code == 502 and not stripe
    r = q("select suspended, deleting from accounts where id=?", (i,), one=True); assert r["suspended"] and r["deleting"]
    monkeypatch.setattr(engine, "destroy_app", lambda n, with_volumes=False: q("update apps set status='destroyed' where name=?", (n,)))
    assert ad.delete_account(i)["ok"] and stripe and not q("select 1 from accounts where id=?", (i,), one=True)

def test_d4_split_options_and_ui_never_sends_ignore(calls, monkeypatch):
    a = _victim(); i = a["id"]
    q("insert into payments(order_id,account_id,status,created) values(?,?,?,?)", (f"o{i}", i, "paid", time.time()))
    monkeypatch.setattr(engine, "destroy_app", lambda n, with_volumes=False: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(HTTPException) as e: ad.delete_account(i, allow_payments=True)
    assert e.value.status_code == 502   # allow_payments does not ignore cleanup errors
    assert ad.delete_account(i, allow_payments=True, ignore_cleanup_errors=True)["ok"]
    assert set(ad.Delete.model_fields) >= {"allow_payments", "ignore_cleanup_errors"} and "force" not in ad.Delete.model_fields
    html = open("dashboard/admin.html").read(); assert "ignore_cleanup_errors" not in html and "allow_payments:" in html

def test_d5_ledger_refused_and_late_webhook_flagged():
    a = _victim(); i = a["id"]; q("update accounts set deleting=1 where id=?", (i,))
    with pytest.raises(billing.AccountGone): billing.credit_rows(i, 100, "topup", "x")
    with pytest.raises(billing.AccountGone): billing.credit_rows(987654321, 100, "topup", "x")
    sid = f"cs_late{i}"
    q("insert into stripe_sessions(id,account_id,kind,credit_cents,amount_cents,status,created) values(?,?,?,?,?,?,?)", (sid, i, "topup", 500, 500, "open", time.time()))
    r = sb.fulfill_topup({"id": sid, "payment_status": "paid", "amount_subtotal": 500, "payment_intent": "pi_x", "metadata": {"account_id": str(i)}})
    assert r.get("refund_review") and q("select status from stripe_sessions where id=?", (sid,), one=True)["status"] == "refund_review"
    assert q("select count(*) n from ledger where account_id=? and kind='topup'", (i,), one=True)["n"] == 0

def test_d6_d7_string_and_huge_ids(calls):
    a = _victim(); assert ad.delete_account(str(a["id"]))["account_id"] == a["id"]
    for bad in (2**63, 2**64 + 5, -1, 0, "abc"):
        with pytest.raises(HTTPException) as e: ad.delete_account(bad)
        assert e.value.status_code == 404
    from test_admin_stages import owner2fa
    from test_account_delete import CS
    o = owner2fa()
    assert o.get(f"/v1/admin/accounts/{2**63}/delete-check").status_code == 404
    assert o.post(f"/v1/admin/accounts/{2**63}/delete", json={"confirm": "x"}, headers=CS).status_code == 404

def test_d8_key_reveal_waitlist_removed_email_hashed(calls):
    a = _victim(); i = a["id"]; e = a["email"]
    kv_put("key_reveal", str(i), "x")
    q("insert into waitlist(kind,email,created) values(?,?,?)", ("gpu", e.upper(), time.time()))
    ad.delete_account(i)
    assert not q("select 1 from kv where kind='key_reveal' and k=?", (str(i),), one=True)
    assert not q("select 1 from waitlist where lower(email)=?", (e.lower(),), one=True)
    d = q("select detail from audit where action='admin.delete_account' and detail like ?", (f"account={i} %",), one=True)["detail"]
    assert e not in d and hashlib.sha256(e.lower().encode()).hexdigest() in d

def test_d2_driver_removes_anonymous_volumes():
    src = open("paas/drivers/local_docker.py").read()
    import re; assert not re.search(r"\.remove\(force=True\)", src.replace("volumes.get(self.volume_ref(name)).remove(force=True)", ""))
    from paas.drivers.local_docker import _acct_label
    assert _acct_label({"networks": ["aloof-acct-42", "x"]}) == {"paas.account": "42"} and _acct_label({}) == {}
