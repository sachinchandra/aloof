"""Regression tests for QA findings F1-F7 on feature/templates."""
import json, re, subprocess, shutil, pytest
from fastapi.testclient import TestClient
from paas import api, templating as T, engine, parity, auth, site
from paas.db import q
from tests.test_templates import _client, fake  # noqa: F401

BAD_NEXT = ["//evil.com", "/\t/evil.com", "/%09/evil.com", "/\n/evil.com", "/\r/evil.com", "/\\evil.com", "https://evil.com", "/ /evil.com", "javascript:alert(1)", ""]
@pytest.mark.parametrize("n", BAD_NEXT)
def test_f1_safe_next_rejects(n):
    import urllib.parse as u
    assert auth.safe_next(u.unquote(n)) == "/dashboard"
def test_f1_safe_next_accepts():
    assert auth.safe_next("/deploy?template=n8n") == "/deploy?template=n8n" and auth.safe_next("/dashboard#apps") == "/dashboard#apps"
@pytest.mark.parametrize("raw", ["%2F%09%2Fevil.com", "%0A/evil.com", "%0D/evil.com", "//evil.com", "/%5Cevil.com"])
def test_f1_login_drops_unsafe_next(raw):
    r = TestClient(api.app).get("/login?next=" + raw, follow_redirects=False); assert r.status_code == 303 and r.headers["location"] == "/login"
    assert TestClient(api.app).get("/login?next=%2Fdeploy%3Ftemplate%3Dn8n").status_code == 200
@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
@pytest.mark.parametrize("n,want", [("/\t/evil.com", "/dashboard"), ("/\n/evil.com", "/dashboard"), ("/\r/evil.com", "/dashboard"), ("//evil.com", "/dashboard"),
    ("/\\evil.com", "/dashboard"), ("/a/..//evil.com", "/dashboard"), ("https://evil.com", "/dashboard"), ("/deploy?template=n8n", "/deploy?template=n8n")])
def test_f1_login_js(n, want):
    js = re.search(r"var n=.*?location=d", site.login("Aloof")).group(0).replace("location=d", "out=d")
    prog = ("var location={origin:'https://www.aloof.si',search:'?next='+encodeURIComponent(%s)};var out;" % json.dumps(n)) + js + ";console.log(out)"
    assert subprocess.run(["node", "-e", prog], capture_output=True, text=True).stdout.strip() == want

def test_f2_deploy_tokens_refused(fake):
    c = _client("f2@qa.dev")
    for app in (None,):
        tok = c.post("/v1/tokens", json={"label": "ci", **({"app": app} if app else {})}).json()["token"]
        d = TestClient(api.app, base_url="http://api.aloof.test"); d.headers["Authorization"] = f"Bearer {tok}"
        assert d.post("/v1/templates/uptime-kuma/deploy", json={"name": "ftwo"}).status_code == 403
        assert d.post("/v1/deploy/repo", json={"name": "ftwob", "repo": "https://github.com/a/b"}).status_code == 403
    assert c.post("/v1/templates/uptime-kuma/deploy", json={"name": "ftwoc"}).status_code == 200

def test_f3_mcp_template_traversal(monkeypatch):
    import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp_server"))
    from mcp_server import server
    calls = []; monkeypatch.setattr(server, "_r", lambda *a, **k: calls.append(a) or {"ok": True})
    for bad in ("../apps/abc", "n8n/../../apps/x", "a%2F..", "UP", "x"):
        assert "error" in server.deploy_template(bad, "abc")
    assert not calls
    server.deploy_template("uptime-kuma", "abc"); assert calls[-1][1] == "/v1/templates/uptime-kuma/deploy"

def test_f4_precheck_and_rollback(fake, monkeypatch):
    c = _client("f4@qa.dev")
    r = c.post("/v1/templates/umami/deploy", json={"name": "forphan", "plan": "shared-4x-8gb"}); assert r.status_code in (400, 402, 403)
    assert not q("select 1 from pg_dbs where name='forphan-db'", one=True) and not q("select 1 from volumes where name like 'forphan-%'", one=True)
    def boom(acct, spec): raise ValueError("late failure")
    monkeypatch.setattr(engine, "deploy", boom)
    monkeypatch.setattr(parity, "pg_destroy", lambda n, keep_backups=False: q("delete from pg_dbs where name=?", (n,)))
    assert c.post("/v1/templates/umami/deploy", json={"name": "flate"}).status_code == 400
    assert not q("select 1 from pg_dbs where name='flate-db'", one=True)
    assert c.post("/v1/templates/uptime-kuma/deploy", json={"name": "flatev"}).status_code == 400
    assert not q("select 1 from volumes where name like 'flatev-%'", one=True)

def test_f5_audit(fake):
    c = _client("f5@qa.dev"); assert c.post("/v1/templates/uptime-kuma/deploy", json={"name": "ffive"}).status_code == 200
    row = q("select * from audit where app='ffive' and action='template.deploy'", one=True); assert row and "uptime-kuma" in row["detail"]
    assert c.post("/v1/deploy/repo", json={"name": "ffiveb", "repo": "https://github.com/a/b", "env": {"SECRET": "s3cr3t"}}).status_code == 200
    row = q("select * from audit where app='ffiveb' and action='repo.deploy'", one=True); assert row and "s3cr3t" not in row["detail"]

def test_f6_form_pattern_matches_server():
    pat = re.compile(r"^[a-z][a-z0-9\-]{1,21}[a-z0-9]$")
    for n, ok in (("abc", True), ("1abc", False), ("ab", False), ("a" * 23, True), ("a" * 24, False), ("ab-", False)):
        assert bool(pat.match(n)) == ok == (bool(engine.NAME_RE.match(n)) and len(n) <= 23 and not n.startswith("pg-"))
    src = open(T.__file__).read(); assert "{{1,21}}" in src and "maxlength=23" in src

@pytest.mark.parametrize("ref,ok", [("main", True), ("v1.2.3", True), ("feature/x", True), ("-x", False), ("a b", False), ("a;rm", False), ("$(id)", False)])
def test_f7_ref(ref, ok):
    if ok: T.repo_template("https://github.com/a/b", ref)
    else:
        with pytest.raises(T.TemplateError): T.repo_template("https://github.com/a/b", ref)

# ---- optional hardening (QA a072f38 items 1-5) ----
def test_safe_next_tightened():
    from paas.auth import safe_next
    for bad in ("/.//evil.com", "/@evil.com", "/\u2215evil.com", "/%2F%2Fevil.com", "/a/../..//x", "/\uff0fevil", "/a//b", "/%2e/%2e/"):
        assert safe_next(bad) == "/dashboard", bad
    for ok in ("/dashboard", "/deploy?template=n8n", "/deploy?repo=https%3A%2F%2Fgithub.com%2Fa%2Fb", "/templates/uptime-kuma"):
        assert safe_next(ok) == ok

def test_repo_owner_and_ref_hardening():
    import pytest as _p
    from paas import templating as T
    for bad in ("https://github.com/-x/b", "https://github.com/a/-b"):
        with _p.raises(T.TemplateError): T.check_repo(bad)
    for bad in ("..", "a..b", "main/../x", "main\n"):
        with _p.raises(T.TemplateError): T.repo_template("https://github.com/a/b", ref=bad)
    T.repo_template("https://github.com/a/b", ref="v1.2.3")

def test_mcp_template_id_fullmatch():
    src = open(__import__("os").path.join(__import__("os").path.dirname(__file__), "..", "mcp_server", "server.py")).read()
    assert "_re.fullmatch(r\"[a-z0-9][a-z0-9-]{1,38}[a-z0-9]\"" in src
    import re; assert not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,38}[a-z0-9]", "n8n\n")

def test_confirm_form_blocks_pg_prefix():
    from fastapi.testclient import TestClient
    from paas import api
    c = TestClient(api.app, base_url="http://api.aloof.test", follow_redirects=False)
    c.post("/v1/auth/register", json={"email": "pgform@example.in", "password": "correct horse 9"})
    assert "(?!pg-)" in c.get("/deploy?template=uptime-kuma").text

def test_unexpected_deploy_error_is_clean_json(monkeypatch):
    from fastapi.testclient import TestClient
    from paas import api, engine
    c = TestClient(api.app, base_url="http://api.aloof.test", raise_server_exceptions=False)
    k = c.post("/v1/signup", json={"email": "boom@example.in"}).json()["api_key"]
    monkeypatch.setattr(engine, "deploy", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("docker down")))
    r = c.post("/v1/templates/uptime-kuma/deploy", json={"name": "boomapp"}, headers={"Authorization": f"Bearer {k}"})
    assert r.status_code == 503 and "nothing was created" in r.json()["detail"] and "docker" not in r.text
    from paas.db import q
    assert not q("select 1 from volumes where name like 'boomapp%'", one=True)
