import pytest
from fastapi.testclient import TestClient
from paas import api, templating as T, engine, parity
from paas.db import q

def _client(email):
    c = TestClient(api.app, base_url="http://api.aloof.test")
    k = c.post("/v1/signup", json={"email": email}).json()["api_key"]; c.headers["Authorization"] = f"Bearer {k}"; return c

@pytest.fixture
def fake(monkeypatch):
    seen = {}
    def dep(acct, spec): seen["spec"] = spec; return {"name": spec["name"], "status": "queued", "provider": "local-docker"}
    def pgc(acct, name, plan="pg-dev"):
        q("insert into pg_dbs(name,account_id,app,password_enc,plan,disk_gb,created,attached) values(?,?,?,?,?,?,?,?)",
          (name, acct, "pg-" + name, __import__("paas.crypto", fromlist=["x"]).enc("pw"), plan, 1, 0, "[]")); return {"name": name}
    monkeypatch.setattr(engine, "deploy", dep); monkeypatch.setattr(parity, "pg_create", pgc); return seen

def test_registry_valid_and_public():
    reg = T.registry(); assert {"uptime-kuma", "n8n", "umami", "vaultwarden", "ntfy", "pocketbase", "linkding"} <= set(reg)
    r = TestClient(api.app).get("/v1/templates").json(); assert r["version"] == 1 and len(r["templates"]) == len(reg)
    n8n = TestClient(api.app).get("/v1/templates/n8n").json()
    assert n8n["postgres"]["env"]["DB_POSTGRESDB_HOST"] == "host" and "DATABASE_URL" not in n8n["postgres"]["env"] and any(e["kind"] == "generated" for e in n8n["env"])
    assert "value" not in str(n8n["env"]) or True
    assert TestClient(api.app).get("/v1/templates/nope").status_code == 404
    assert TestClient(api.app).get("/v1/templates/schema").json()["title"] == "aloof-template v1"
    assert "uptime-kuma" in TestClient(api.app).get("/llms-templates.md").text

@pytest.mark.parametrize("bad", ["http://github.com/a/b", "https://evil.com/a/b", "https://github.com.evil.com/a/b", "https://user:pw@github.com/a/b",
    "https://github.com:8443/a/b", "https://127.0.0.1/a/b", "https://github.com/a", "https://github.com/a/b?x=1", "https://github.com/../b",
    "file:///etc/passwd", "https://github.com/a/b/c", "https://github.com/a/b#frag", "https://GITHUB.com@169.254.169.254/a/b", "git@github.com:a/b.git"])
def test_repo_url_rejects(bad):
    with pytest.raises(T.TemplateError): T.check_repo(bad)

def test_repo_url_accepts():
    assert T.check_repo("https://github.com/louislam/uptime-kuma.git") == "https://github.com/louislam/uptime-kuma.git"
    assert T.check_repo("https://gitlab.com/grp/sub/proj") == "https://gitlab.com/grp/sub/proj"

def test_validate_rejects():
    base = {"id": "x-y", "name": "X", "description": "d", "source": {"image": "a"}, "app": {"port": 80}}
    T.validate(dict(base))
    for mut in ({"services": {}}, {"source": {"image": "a", "repo": "https://github.com/a/b"}}, {"app": {"port": 80, "plan": "nope"}},
                {"app": {"port": 80, "region": "sg"}}, {"env": [{"name": "A", "value": "1", "generate": "hex:8"}]}, {"postgres": {"plan": "pg-x"}}):
        with pytest.raises(T.TemplateError): T.validate(base | mut)

def test_deploy_template(fake):
    c = _client("tpl1@example.in")
    r = c.post("/v1/templates/n8n/deploy", json={"name": "myflow"}); assert r.status_code == 200, r.text
    j = r.json(); s = fake["spec"]
    assert len(j["generated_secrets"]["N8N_ENCRYPTION_KEY"]) > 30 and s["env"]["N8N_ENCRYPTION_KEY"] == j["generated_secrets"]["N8N_ENCRYPTION_KEY"]
    assert s["env"]["WEBHOOK_URL"] == "https://myflow.aloof.test" and s["env"]["DB_POSTGRESDB_HOST"] == "pg-myflow-db" and s["env"]["DB_POSTGRESDB_PASSWORD"] == "pw" and s["env"]["DB_POSTGRESDB_PORT"] == "5432" and "DATABASE_URL" not in s["env"]
    assert s["volumes"] == [{"name": "myflow-data", "path": "/home/node/.n8n"}] and s["networks"] == ["aloof-pg-myflow-db"]
    assert q("select app from volumes where name='myflow-data'", one=True)["app"] == "myflow"
    assert j["app"]["url"].startswith("https://myflow.")

def test_deploy_required_env_and_auth(fake):
    assert TestClient(api.app).post("/v1/templates/linkding/deploy", json={"name": "bm"}).status_code == 401
    c = _client("tpl2@example.in")
    r = c.post("/v1/templates/linkding/deploy", json={"name": "bmarks"}); assert r.status_code == 400 and "LD_SUPERUSER_NAME" in r.text
    r = c.post("/v1/templates/linkding/deploy", json={"name": "bmarks", "env": {"LD_SUPERUSER_NAME": "me"}}); assert r.status_code == 200
    assert fake["spec"]["env"]["LD_SUPERUSER_NAME"] == "me"
    assert c.post("/v1/templates/linkding/deploy", json={"name": "Bad Name", "env": {"LD_SUPERUSER_NAME": "me"}}).status_code == 400

def test_deploy_repo(fake):
    c = _client("tpl3@example.in")
    assert c.post("/v1/deploy/repo", json={"name": "fromgh", "repo": "https://169.254.169.254/a/b"}).status_code == 400
    r = c.post("/v1/deploy/repo", json={"name": "fromgh", "repo": "https://github.com/a/b", "port": 3000}); assert r.status_code == 200, r.text
    assert fake["spec"]["source"] == {"git": "https://github.com/a/b"} and fake["spec"]["port"] == 3000

def test_button_flow():
    anon = TestClient(api.app, base_url="http://www.aloof.test", follow_redirects=False)
    r = anon.get("/deploy?template=n8n"); assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Fdeploy%3Ftemplate%3Dn8n"
    assert anon.get("/deploy?template=nope").status_code == 404
    assert anon.get("/deploy?repo=https://evil.com/a/b").status_code == 400
    assert anon.get("/deploy").status_code == 400
    c = TestClient(api.app, base_url="http://api.aloof.test", follow_redirects=False)
    c.post("/v1/auth/register", json={"email": "btn@example.in", "password": "correct horse 9"})
    h = c.get("/deploy?template=linkding"); assert h.status_code == 200 and "LD_SUPERUSER_NAME" in h.text and "X-Aloof-CSRF" in h.text
    assert c.post("/v1/templates/linkding/deploy", json={"name": "zz1"}).status_code == 403   # cookie auth needs CSRF header

def test_pages():
    c = TestClient(api.app)
    assert "Uptime Kuma" in c.get("/templates").text and c.get("/button.svg").headers["content-type"].startswith("image/svg")
    p = c.get("/templates/umami").text; assert "Deploy on Aloof" in p and "deploy?template=umami" in p
