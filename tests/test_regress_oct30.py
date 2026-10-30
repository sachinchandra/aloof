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

# ---- round 2: N1-N4 + D2 remote ----
import asyncio
from paas import api, ws_registry, admin_ops

class _WS:
    def __init__(self, key): self.headers = {"authorization": f"Bearer {key}"}; self.cookies = {}; self.closed = None
    async def close(self, code=1000, reason=""): self.closed = (code, reason)

def _key_for(i):
    import secrets as s; k = "ak_" + s.token_hex(16); q("update accounts set api_key_hash=? where id=?", (engine.sha256(k), i)); return k

def test_n1_api_key_ws_rejects_suspended_and_deleting():
    a = _victim(); i = a["id"]; k = _key_for(i)
    assert api._ws_account(_WS(k))["id"] == i
    q("update accounts set suspended=1 where id=?", (i,)); assert api._ws_account(_WS(k)) is None
    q("update accounts set suspended=0, deleting=1 where id=?", (i,)); assert api._ws_account(_WS(k)) is None

def test_n2_registry_close_and_watchdog():
    a = _victim(); i = a["id"]
    async def run():
        ws = _WS("x"); killed = []
        tok = ws_registry.register(i, ws, lambda: killed.append(1)); assert ws_registry.count(i) == 1
        await asyncio.to_thread(ws_registry.close_account, i, "account suspended"); await asyncio.sleep(0.05)
        assert ws.closed and ws.closed[0] == 4403 and killed; ws_registry.unregister(i, tok); assert ws_registry.count(i) == 0
        ws2 = _WS("y"); q("update accounts set suspended=1 where id=?", (i,))
        await asyncio.wait_for(ws_registry.watchdog(i, ws2, every=0.01), 2); assert ws2.closed
    asyncio.run(run())

def test_n2_suspend_route_closes_sockets(monkeypatch):
    a = _victim(); i = a["id"]; seen = []
    monkeypatch.setattr(ws_registry, "close_account", lambda acc, reason="": seen.append(acc) or 0)
    from test_admin_stages import owner2fa
    from test_account_delete import CS
    o = owner2fa(); r = o.post(f"/v1/admin/accounts/{i}/suspend", json={"suspended": True, "confirm": a["email"], "reason": "test n2"}, headers=CS)
    assert r.status_code == 200, r.text; assert seen == [i]

def test_n3_start_refused_without_row_or_deleting_label():
    a = _victim(); i = a["id"]
    with pytest.raises(PermissionError): engine._start_allowed("no-such-app-xyz", {})
    now = time.time(); q("insert into apps(name,account_id,status,created,updated) values(?,?,?,?,?)", (f"n3{i}", None, "queued", now, now))
    engine._start_allowed(f"n3{i}", {})   # platform app, no owner: allowed
    q("update accounts set deleting=1 where id=?", (i,))
    with pytest.raises(PermissionError): engine._start_allowed(f"n3{i}", {"networks": [f"aloof-acct-{i}"]})

def test_n4_profile_flags_and_ui_warning():
    a = _victim(); i = a["id"]; q("update accounts set deleting=1 where id=?", (i,))
    p = admin_ops.detail(i)["profile"]; assert p["deleting"] == 1 and p["destroyed_apps"] == 1
    assert "deleting=1" in open("dashboard/admin.html").read()

def test_d2_remote_sweep_same_targets(monkeypatch):
    from paas.drivers import remote, local_docker
    assert local_docker.account_sweep_targets(7) == {"label": "paas.account=7", "network": "aloof-acct-7", "id": "7"}
    calls_ = []
    monkeypatch.setattr(remote, "agent", lambda node, m, path, **k: calls_.append((m, path)) or {"removed": 2})
    d = remote.RemoteDriver.__new__(remote.RemoteDriver); d.name = "selfhosted-t"
    now = time.time()
    q("insert into nodes(id,provider,status,created) values(?,?,?,?)", ("nd-t1", "selfhosted-t", "ready", now))
    assert d.remove_account_containers(7) == 2 and calls_ == [("DELETE", "/v1/accounts/7/containers")]
    monkeypatch.setattr(remote, "agent", lambda *x, **k: (_ for _ in ()).throw(RuntimeError("404")))
    with pytest.raises(RuntimeError): d.remove_account_containers(7)
    src = open("paas/agent/agent.py").read()
    assert '"rm", "-f", "-v"' in src and "label=paas.account=" in src and "network={net}" in src
