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
