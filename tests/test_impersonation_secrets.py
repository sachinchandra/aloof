"""Admin 'view as user' hides customer secrets: masked JSON, refused reveal paths, one-time key not consumed, banner copy."""
import time, secrets as S
from fastapi.testclient import TestClient
from paas.api import app
from paas import auth, imp_mask, admin_ops
from paas.crypto import sha256, dump_spec
from paas.db import q, kv_put, kv_get
from test_admin_cockpit import _reg, acct

def _imp():
    e = f"imp{time.time_ns()}@example.com"; _reg(e); a = acct(e); i = a["id"]; now = time.time()
    tok = S.token_urlsafe(24)
    q("insert into sessions(token_hash,account_id,expires,created,impersonated_by) values(?,?,?,?,?)", (sha256(tok), i, now + 600, now, 1))
    c = TestClient(app); c.cookies.set(auth.COOKIE, tok); return c, a

def test_mask_unit():
    m = imp_mask.mask({"name": "web", "env": {"DATABASE_URL": "postgres://u:p4ss@h/db", "PORT": "80"}, "api_key": "pk_live_abcdefghijkl",
                       "url": "https://web.aloof.si", "database_url": "postgres://u:pw@h/d", "note": "use sk_test_abcdefgh1234 now",
                       "api_key_prefix": "pk_ab", "has_password": True, "git": "https://bob:ghp_abcdefghijklmnop@github.com/x"})
    assert m["env"] == {"DATABASE_URL": imp_mask.HIDDEN, "PORT": imp_mask.HIDDEN} and m["api_key"] == imp_mask.HIDDEN
    assert m["url"] == "https://web.aloof.si" and m["database_url"] == imp_mask.HIDDEN and "sk_test" not in m["note"]
    assert m["api_key_prefix"] == "pk_ab" and m["has_password"] is True and "ghp_" not in m["git"] and "bob:" in m["git"]

def test_api_responses_masked_and_header():
    c, a = _imp(); i = a["id"]; now = time.time(); n = f"im{time.time_ns()}"
    q("insert into apps(name,account_id,status,spec,created,updated) values(?,?,?,?,?,?)",
      (n, i, "running", dump_spec({"name": n, "image": "nginx", "plan": "shared-1x-256mb", "port": 80, "instances": 1, "env": {"SECRET_KEY": "supersecretvalue123"}}), now, now))
    r = c.get(f"/v1/apps/{n}/secrets"); assert r.status_code == 200 and r.headers.get(imp_mask.HEADER) == "1"
    assert all(x["digest"] == imp_mask.HIDDEN for x in r.json()) and r.json()[0]["name"] == "SECRET_KEY"
    body = c.get(f"/v1/apps/{n}").text; assert "supersecretvalue123" not in body
    assert c.get("/v1/auth/keys").status_code == 200 and c.get("/v1/tokens").headers.get(imp_mask.HEADER) == "1"

def test_reveal_blocked_and_writes_refused():
    c, a = _imp()
    assert c.get("/v1/pg/anything?reveal=true").status_code == 403
    assert c.get("/v1/pg/anything?reveal=1").json()["detail"].startswith("secrets are hidden")
    assert c.post("/v1/auth/api-key", headers={"X-Aloof-CSRF": "1"}).status_code == 403

def test_one_time_key_not_consumed_by_admin_view():
    c, a = _imp(); i = a["id"]; kv_put("key_reveal", str(i), "pk_zzzzzzzzzzzzzzzz", ttl=600)
    j = c.get("/v1/auth/me").json(); assert not j.get("new_api_key")
    assert kv_get("key_reveal", str(i))   # still there for the customer

def test_banner_copy():
    assert "Secrets hidden while viewing as user" in admin_ops.imp_banner({"email": "x@example.com"})

def test_normal_session_unmasked():
    e = f"nm{time.time_ns()}@example.com"; _reg(e); a = acct(e); tok = S.token_urlsafe(24); now = time.time()
    q("insert into sessions(token_hash,account_id,expires,created) values(?,?,?,?)", (sha256(tok), a["id"], now + 600, now))
    c = TestClient(app); c.cookies.set(auth.COOKIE, tok)
    r = c.get("/v1/auth/me"); assert r.status_code == 200 and imp_mask.HEADER not in r.headers

# --- QA retest 4 (Oct 30): M1 free text, L1 logout sockets, L2 deny-by-default, L3 no-store ---
def test_text_masking_unit():
    t = imp_mask._s("run --password QACANARY_cmd --token=QAC2 -p 80 pw=hunter2xyz DB_PASSWORD: 's3cr3t'\nAuthorization: Bearer abc.def\nport=80")
    for c in ("QACANARY_cmd", "QAC2", "hunter2xyz", "s3cr3t", "abc.def"): assert c not in t
    assert "port=80" in t and "-p 80" in t and "hidden while viewing as user while" not in t
    assert imp_mask._s("command: run --x y\n") == f"command: {imp_mask.HIDDEN}\n"
    assert imp_mask.mask({"spec": {"command": "run --password X", "args": ["--password", "X"], "port": 80}})["spec"] == {"command": imp_mask.HIDDEN, "args": [imp_mask.HIDDEN, imp_mask.HIDDEN], "port": 80}

def test_free_text_hidden_routes():
    c, a = _imp(); i = a["id"]; now = time.time(); n = f"ft{time.time_ns()}"
    q("insert into apps(name,account_id,status,spec,created,updated) values(?,?,?,?,?,?)",
      (n, i, "running", dump_spec({"name": n, "image": "nginx", "plan": "shared-1x-256mb", "port": 80, "instances": 1, "command": "run --password QACANARY_cmd"}), now, now))
    q("insert into events(app,ts,msg) values(?,?,?)", (n, now, "build said secretword QACANARY_evt"))
    for u in ("/v1/apps", f"/v1/apps/{n}", f"/v1/apps/{n}?format=yaml"):
        r = c.get(u); assert r.status_code == 200 and "QACANARY_cmd" not in r.text, u
    r = c.get(f"/v1/apps/{n}/events"); assert r.status_code == 200 and "QACANARY_evt" not in r.text and r.json()[0]["msg"] == imp_mask.HIDDEN
    for u in ("releases", "deploy", "diagnose", "logs/history"):
        r = c.get(f"/v1/apps/{n}/{u}"); assert r.status_code in (200, 404) and "QACANARY" not in r.text, u
    r = c.get(f"/v1/apps/{n}/logs")
    assert r.status_code != 200 or r.text == imp_mask.HIDDEN

def test_deny_by_default_and_no_store():
    c, a = _imp()
    for u in ("/v1/admin/export.csv", "/v1/admin/waitlist.csv", "/v1/nodes/agent.py", "/v1/github/repos", "/mcp", "/cli/aloof", "/admin", "/v1/some/future"):
        r = c.get(u); assert r.status_code == 403, u
    assert c.post("/v1/apps", json={}, headers={"X-Aloof-CSRF": "1"}).status_code == 403
    r = c.get("/v1/auth/me"); assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert c.get("/dashboard").status_code == 200

def test_no_store_on_normal_authenticated_v1():
    from test_admin_cockpit import _reg
    e = f"ns{time.time_ns()}@example.com"; _reg(e); a = acct(e); tok = S.token_urlsafe(24); now = time.time()
    q("insert into sessions(token_hash,account_id,expires,created) values(?,?,?,?)", (sha256(tok), a["id"], now + 600, now))
    c = TestClient(app); c.cookies.set(auth.COOKIE, tok)
    r = c.get("/v1/auth/me"); assert r.status_code == 200 and r.headers["cache-control"] == "no-store" and "Cookie" in r.headers["vary"]

def test_logout_and_exit_revalidate_sockets(monkeypatch):
    from paas import ws_registry
    calls = []; monkeypatch.setattr(ws_registry, "revalidate", lambda i, reason="": calls.append((i, reason)) or 0)
    e = f"lo{time.time_ns()}@example.com"; _reg(e); a = acct(e); tok = S.token_urlsafe(24); now = time.time()
    q("insert into sessions(token_hash,account_id,expires,created) values(?,?,?,?)", (sha256(tok), a["id"], now + 600, now))
    c = TestClient(app); c.cookies.set(auth.COOKIE, tok)
    assert c.post("/v1/auth/logout", headers={"X-Aloof-CSRF": "1"}).status_code == 200 and calls == [(a["id"], "signed out")]
    c2, b = _imp(); calls.clear()
    assert c2.post("/v1/admin/impersonate/exit", headers={"X-Aloof-CSRF": "1"}).status_code == 200 and calls[0][0] == b["id"]

# --- QA retest 5 (Oct 30): N-M1 app/status free text, N-L1 pattern gaps ---
def test_masking_gaps_unit():
    t = imp_mask._s('db postgres://u:pa/ss@h/db cfg {"pass":"QAX1"} hdr Bearer QAX2tokenvalue mysql -p QAX3 -p 8080:80 -port 80')
    for c in ("pa/ss", "QAX1", "QAX2", "QAX3"): assert c not in t, t
    assert "-p 8080:80" in t and "-port 80" in t and "u:" in t
    m = imp_mask.mask({"processes": {"web": "run --x QAX4", "worker": "w"}, "health_check": "curl -H x QAX5", "cron": [{"schedule": "* * * * *", "command": "job QAX6"}]})
    assert "QAX" not in str(m) and set(m["processes"]) == {"web", "worker"}

def test_app_and_status_free_text_hidden():
    c, a = _imp(); i = a["id"]; now = time.time(); n = f"nm{time.time_ns()}"
    q("insert into apps(name,account_id,status,spec,created,updated,error) values(?,?,?,?,?,?,?)",
      (n, i, "failed", dump_spec({"name": n, "image": "nginx", "plan": "shared-1x-256mb", "port": 80, "instances": 1, "processes": {"web": "run QANM_proc"}}), now, now, "build failed: QANM_err"))
    q("insert into releases(app,version,created,status,reason) values(?,?,?,?,?)", (n, 1, now, "failed", "deploy said QANM_rel"))
    for u in ("/v1/apps", f"/v1/apps/{n}", f"/v1/apps/{n}/status"):
        r = c.get(u); assert r.status_code in (200, 404) and "QANM" not in r.text, (u, r.text[:300])
    assert imp_mask.hide_text({"x": [{"last_release": {"reason": "r", "log": ["l"], "msg": "m"}, "error": "e"}]}) == \
        {"x": [{"last_release": {"reason": imp_mask.HIDDEN, "log": imp_mask.HIDDEN, "msg": imp_mask.HIDDEN}, "error": imp_mask.HIDDEN}]}
