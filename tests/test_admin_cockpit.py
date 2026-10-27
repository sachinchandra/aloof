"""Owner dashboard (admin cockpit) rework, Oct 27: stage 1 quick wins and later stages."""
import os, time, json
os.environ.setdefault("PAAS_STATUS_PROBES", "0")
from fastapi.testclient import TestClient
from paas import api, www
from paas.db import q
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
c = TestClient(api.app, base_url="http://api.aloof.test")
CS = {"X-Aloof-CSRF": "1"}
def _reg(email):
    cl = TestClient(api.app, base_url="http://api.aloof.test")
    assert cl.post("/v1/auth/register", json={"email": email, "password": "correct horse 1"}).status_code == 200; return cl
def owner():
    e = www.ADMIN_EMAIL
    if not q("select 1 from accounts where lower(email)=?", (e,), one=True): o = _reg(e)
    else:
        o = TestClient(api.app, base_url="http://api.aloof.test"); assert o.post("/v1/auth/login", json={"email": e, "password": "correct horse 1"}).status_code == 200
    www.mark_flags(); return o
def acct(email): return q("select * from accounts where email=?", (email,), one=True)
def real_and_test():
    n = time.time_ns(); re_, te = f"real{n}@realco.io", f"t{n}@example.com"
    r = _reg(re_); t = _reg(te); www.mark_flags()
    assert acct(te)["is_test"] == 1 and not acct(re_)["is_test"]
    return r, t, acct(re_), acct(te)
def html(): return open(os.path.join(ROOT, "dashboard/admin.html")).read()

def test_stage1_filters_test_accounts_and_deleted_apps():
    o = owner(); _, _, ra, ta = real_and_test(); n = time.time_ns(); now = time.time()
    for name, aid, st, err in ((f"live{n}", ra["id"], "running", "real live error"), (f"gone{n}", ra["id"], "destroyed", "deleted app error"), (f"tst{n}", ta["id"], "running", "test acct error")):
        q("insert into apps(name,account_id,spec,status,created,updated,error) values(?,?,?,?,?,?,?)", (name, aid, "{}", st, now, now, err))
    q("insert into volumes(name,account_id,size_gb,created) values(?,?,?,?)", (f"vt{n}", ta["id"], 5, now))
    q("insert into volumes(name,account_id,size_gb,created) values(?,?,?,?)", (f"vr{n}", ra["id"], 2, now))
    q("insert into pg_dbs(name,account_id,created) values(?,?,?)", (f"pt{n}", ta["id"], now))
    q("insert into releases(app,version,status,reason,created) values(?,?,?,?,?)", (f"live{n}", 3, "deploying", "deploy", now - 20 * 60))
    q("insert into releases(app,version,status,reason,created) values(?,?,?,?,?)", (f"live{n}", 4, "deploying", "deploy", now - 60))
    q("insert into releases(app,version,status,reason,created) values(?,?,?,?,?)", (f"tst{n}", 1, "deploying", "deploy", now - 3600))
    try: m = o.get("/v1/admin/dashboard").json()
    finally:
        for t in ("apps", "volumes", "pg_dbs"): q(f"delete from {t} where name like ?", (f"%{n}",))
        q("delete from releases where app like ?", (f"%{n}",))
    errs = {e["app"] for e in m["errors"]["app_errors"]}
    assert f"live{n}" in errs and f"gone{n}" not in errs and f"tst{n}" not in errs
    q("insert into volumes(name,account_id,size_gb,created) values(?,?,?,?)", (f"vr{n}", ra["id"], 2, now)); q("insert into volumes(name,account_id,size_gb,created) values(?,?,?,?)", (f"vt{n}", ta["id"], 5, now)); q("insert into pg_dbs(name,account_id,created) values(?,?,?)", (f"pt{n}", ta["id"], now))
    try: m2 = o.get("/v1/admin/dashboard").json(); vols = q("select account_id,size_gb from volumes"); pgn = q("select count(*) n from pg_dbs where account_id not in (select id from accounts where is_test=1)", one=True)["n"]
    finally:
        for t in ("volumes", "pg_dbs"): q(f"delete from {t} where name like ?", (f"%{n}",))
    m["data"] = m2["data"]; test = {r["id"] for r in q("select id from accounts where is_test=1")}
    assert m["data"]["volumes"] == sum(1 for v in vols if v["account_id"] not in test)
    assert m["data"]["postgres_dbs"] == pgn
    stuck = {(s["app"], s["version"]) for s in m["errors"]["stuck_deploys"]}
    assert (f"live{n}", 3) in stuck and (f"live{n}", 4) not in stuck and not any(a == f"tst{n}" for a, _ in stuck)
    assert m["deployments"]["apps_total"] >= 1   # the live real app counts, the destroyed one does not (checked via errors)
    assert m["real_accounts"] == m["users"]["total"] and "certs" not in m and m["certs_hosts"]

def test_stage1_certs_endpoint_is_admin_only(monkeypatch):
    o = owner(); other = _reg(f"z{time.time_ns()}@x.com")
    monkeypatch.setattr(www, "cert_expiry", lambda h: {"host": h, "expires": 1, "days_left": 50})
    assert c.get("/v1/admin/certs").status_code == 401 and other.get("/v1/admin/certs").status_code == 401
    j = o.get("/v1/admin/certs").json(); assert j and all(x["days_left"] == 50 for x in j)

def test_stage1_page_states_and_safety():
    h = html()
    assert "No real accounts yet" in h and "test account${m.excluded_test_accounts==1?'':'s'} excluded" in h
    assert "class=errcard" in h and "Try again" in h and "PANELS.forEach(id=>$(id).innerHTML='')" in h and "$('gpub').style.width='0'" in h
    assert "class=skel" in h and "aria-busy=true" in h and "Updated '+ago(GEN)" in h and "setInterval(()=>{if(!document.hidden)load()},60000)" in h
    assert "loadCerts()" in h and "/v1/admin/certs" in h
    assert h.index("Needs attention") < h.index("<h2>Deployments</h2>") and "<details class=tacc" in h and "<details class=tacc id=taccd open" not in h
    assert "confirm(" not in h and "alert(" not in h and "showModal()" in h and "Undo" in h
    assert 'aria-live=polite' in h and "Switch to light theme" in h and "stuck in “deploying” for over 15 minutes" in h
    assert "onclick=\"wlDel(${r.id},'${E(r.email)}')" not in h   # email no longer interpolated into JS

def test_stage2_waitlist_retry_sweep_and_owner_send(monkeypatch):
    from paas import waitlist as W, auth
    sent = []; monkeypatch.setattr(auth, "send_mail", lambda to, *a, **k: sent.append(to) or "smtp")
    now = time.time(); n = time.time_ns(); ids = {}
    for tag, src, age in (("lost", "landing", 3600), ("fresh", "landing", 60), ("old", "landing", 5 * 86400), ("legacy", "legacy", 3600)):
        e = f"wl{tag}{n}@realco.io"
        q("insert into waitlist(kind,email,item,source,created,updated,is_test) values(?,?,?,?,?,?,0)", ("gpu", e, f"gpu-h100-{n}", src, now - age, now - age))
        ids[tag] = q("select id from waitlist where email=?", (e,), one=True)["id"]
    try:
        W.retry_unsent(now)
        st = {t: q("select mail_user from waitlist where id=?", (i,), one=True)["mail_user"] for t, i in ids.items()}
        assert st["lost"].startswith("sent") and st["fresh"] is None and st["old"] is None and st["legacy"] is None
        assert [x for x in sent if "wl" in x] == [f"wllost{n}@realco.io"]
        o = owner(); other = _reg(f"z{time.time_ns()}@x.com"); u = f"/v1/admin/waitlist/{ids['legacy']}/send"
        assert c.post(u, json={"confirm": "x"}).status_code == 401 and other.post(u, json={"confirm": "x"}, headers=CS).status_code == 401
        assert o.post(u, json={"confirm": "wrong@x.com"}, headers=CS).status_code == 400
        assert o.post(u, json={"confirm": f"wllegacy{n}@realco.io"}, headers=CS).json()["mail_user"].startswith("sent")
        assert o.post(u, json={"confirm": f"wllegacy{n}@realco.io"}, headers=CS).status_code == 409
        assert q("select 1 from audit where action='admin.waitlist_send' and detail like ?", (f"%wllegacy{n}%",), one=True)
    finally:
        for i in ids.values(): q("delete from waitlist where id=?", (i,))
    h = html(); assert "never sent" in h and "wlSend(" in h and "{type:e}" in h
