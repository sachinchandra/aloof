"""Owner-only account deletion: protected ids, payment guard, cleanup across tables, money rows kept, audit, admin endpoint."""
import time, pytest
from test_admin_cockpit import _reg, acct, CS
from test_admin_stages import owner2fa
from fastapi import HTTPException
from paas import engine, account_delete as ad, www
from paas.db import q

@pytest.fixture
def calls(monkeypatch):
    c = {"destroy": [], "sweep": []}
    def fake_destroy(name, with_volumes=False):
        c["destroy"].append((name, with_volumes)); q("update apps set status='destroyed' where name=?", (name,)); q("delete from domains where app=?", (name,))
    monkeypatch.setattr(engine, "destroy_app", fake_destroy); monkeypatch.setattr(engine, "_sweep_containers", lambda n: c["sweep"].append(n))
    monkeypatch.setattr(engine, "sweep_account_containers", lambda i: c.setdefault("acct_sweep", []).append(i) or 0)
    monkeypatch.setattr(engine, "delete_volume", lambda v: q("delete from volumes where name=?", (v,))); return c
def _victim():
    while True:   # a fresh test DB hands out ids 1 and 2 first, and those are protected
        n = time.time_ns(); e = f"del{n}@example.com"; _reg(e); a = acct(e); i = a["id"]; now = time.time()
        if i > 2: break
    q("insert into apps(name,account_id,status,created,updated) values(?,?,?,?,?)", (f"dl{n}", i, "running", now, now))
    q("insert into apps(name,account_id,status,created,updated) values(?,?,?,?,?)", (f"dd{n}", i, "destroyed", now, now))
    q("insert into releases(app,version,status,created) values(?,?,?,?)", (f"dl{n}", 1, "live", now))
    q("insert into domains(domain,app,account_id,created) values(?,?,?,?)", (f"x{n}.example.com", f"dl{n}", i, now))
    q("insert into volumes(name,account_id,size_gb,created) values(?,?,?,?)", (f"v{n}", i, 1, now))
    q("insert into api_keys(id,account_id,key_hash,created) values(?,?,?,?)", (f"k{n}", i, f"h{n}", now))
    q("insert into kv(kind,k,v,acct) values(?,?,?,?)", ("oauth:access", f"t{n}", "{}", f"h{n}"))
    q("insert into ledger(account_id,ts,amount_cents,kind,ref) values(?,?,?,?,?)", (i, now, 500, "grant", "start"))
    return a

def test_protected_ids_and_owner_never_deleted(calls):
    for i in (1, 2):
        if q("select 1 from accounts where id=?", (i,), one=True):
            with pytest.raises(HTTPException) as e: ad.delete_account(i, allow_payments=True)
            assert e.value.status_code == 400
    o = q("select id from accounts where lower(email)=?", (www.ADMIN_EMAIL,), one=True)
    if o:
        with pytest.raises(HTTPException): ad.delete_account(o["id"], allow_payments=True)

def test_delete_cleans_everything_and_keeps_money(calls):
    a = _victim(); i = a["id"]
    out = ad.delete_account(i, actor=None, reason="cleanup")
    assert out["ok"] and len(out["apps"]) == 2
    assert [d for d in calls["destroy"] if d[1]] and calls["sweep"]
    assert ad.leftovers(i) == {}
    assert not q("select 1 from kv where acct=?", (f"h{a['email'][3:-12]}",), one=True)
    assert q("select count(*) n from ledger where account_id=?", (i,), one=True)["n"] == 1
    assert not q("select 1 from releases where app like ?", (f"dl{a['email'][3:-12]}",), one=True)
    assert q("select 1 from audit where action='admin.delete_account' and detail like ?", (f"account={i} %",), one=True)

def test_payments_need_force_and_require_empty(calls):
    a = _victim(); i = a["id"]; now = time.time()
    with pytest.raises(HTTPException) as e: ad.delete_account(i, require_empty=True)
    assert e.value.status_code == 409
    q("insert into stripe_sessions(id,account_id,kind,amount_cents,status,created,updated) values(?,?,?,?,?,?,?)", (f"s{time.time_ns()}", i, "topup", 500, "paid", now, now))
    with pytest.raises(HTTPException) as e: ad.delete_account(i)
    assert "allow_payments" in e.value.detail
    assert ad.delete_account(i, allow_payments=True)["ok"]
    assert q("select count(*) n from stripe_sessions where account_id=?", (i,), one=True)["n"] == 1

def test_admin_endpoint_needs_typed_email(calls):
    o = owner2fa(); a = _victim()
    assert o.get(f"/v1/admin/accounts/{a['id']}/delete-check").json()["blockers"]["live_apps"] == 1
    r = o.post(f"/v1/admin/accounts/{a['id']}/delete", json={"confirm": "nope", "reason": "test"}, headers=CS); assert r.status_code == 400
    r = o.post(f"/v1/admin/accounts/{a['id']}/delete", json={"confirm": a["email"], "reason": "test"}, headers=CS); assert r.status_code == 200, r.text
    assert not acct(a["email"])

def test_manual_test_flag_survives_mark_flags():
    from test_admin_cockpit import owner
    o = owner(); e = f"pin{time.time_ns()}@aloof.si"; _reg(e); a = acct(e)
    q("update accounts set email_verified=1 where id=?", (a["id"],)); www.mark_flags(); assert acct(e)["is_test"] == 1
    assert o.post(f"/v1/admin/accounts/{a['id']}/test", json={"is_test": False}, headers=CS).status_code == 200
    www.mark_flags(); engine.recompute_test(a["id"]); assert acct(e)["is_test"] == 0
