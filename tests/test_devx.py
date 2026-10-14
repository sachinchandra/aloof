"""flyctl-parity developer features: aloof.toml, launch detection, deploy tokens, staged secrets, process groups, release_command,
cron, private networks, previews, GitHub App (manifest, OAuth-verified install, signed webhooks), registry/git credential storage."""
import os, json, time, hmac, hashlib, base64, calendar, pytest, httpx
from fastapi.testclient import TestClient
from paas import api, engine, appconfig, launch, cron, creds, github_app, crypto
from paas.db import q
from test_parity import Fake, fake, user, wait, deploy   # noqa: F401 (fixtures)
TOML = {"app": "cfg-app", "primary_region": "eu-de", "build": {"dockerfile": "Dockerfile.prod", "build_args": {"V": 1}},
        "env": {"LOG": "info"}, "processes": {"web": "gunicorn app:app", "worker": "celery -A app worker"},
        "http_service": {"internal_port": 8000, "force_https": True, "auto_stop_machines": "stop", "min_machines_running": 1,
                         "concurrency": {"soft_limit": 25}, "checks": [{"path": "/health", "interval": "15s"}]},
        "deploy": {"release_command": "python manage.py migrate"}, "mounts": [{"source": "cfgdata", "destination": "/data", "initial_size": "3gb"}],
        "vm": [{"cpus": 2, "memory": "2gb"}], "scale": {"worker": 2}, "cron": [{"name": "nightly", "schedule": "0 3 * * *", "command": "python cleanup.py"}],
        "regions": ["eu-central", "us-east"]}
def test_toml_to_spec():
    spec, mounts, w = appconfig.to_spec(TOML)
    assert spec["port"] == 8000 and spec["env"]["PORT"] == "8000" and spec["source"] == {"dockerfile": "Dockerfile.prod", "build_args": {"V": "1"}}
    assert spec["health"]["path"] == "/health" and spec["health"]["interval"] == 15 and spec["release_command"].startswith("python manage.py")
    assert spec["processes"]["worker"].startswith("celery") and spec["process_counts"] == {"web": 1, "worker": 2}
    assert spec["autoscale"]["min"] == 1 and spec["autoscale"]["target_concurrency"] == 25 and spec["plan"] == "shared-2x-2gb"
    assert mounts == [{"name": "cfgdata", "path": "/data", "size_gb": 3}] and spec["cron"][0]["name"] == "nightly"
    assert any("multi-region" in x for x in w)
    assert not appconfig.validate({"app": "x"})["ok"]   # bad name
    assert "cron" in appconfig.validate({"app": "abc", "cron": [{"schedule": "61 * * * *", "command": "x"}]})["error"]
    assert appconfig.validate({"app": "abc", "vm": {"size": "shared-cpu-1x"}})["spec"]["plan"] == "shared-1x-256mb"
@pytest.mark.parametrize("files,fw", [
    ({"package.json": '{"dependencies":{"next":"14"},"scripts":{"build":"next build","start":"next start"}}', "pnpm-lock.yaml": None}, "nextjs"),
    ({"package.json": '{"main":"server.js"}'}, "node"),
    ({"requirements.txt": "flask\n", "app.py": "app = Flask(__name__)"}, "flask"),
    ({"requirements.txt": "fastapi\nuvicorn", "main.py": ""}, "fastapi"),
    ({"manage.py": "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mysite.settings')", "requirements.txt": "django"}, "django"),
    ({"go.mod": "module x"}, "go"), ({"Cargo.toml": '[package]\nname = "svc"'}, "rust"),
    ({"Gemfile": "gem 'rails'"}, "rails"), ({"composer.json": "{}", "artisan": None}, "laravel"), ({"index.php": None}, "php"),
    ({"pom.xml": "<project/>"}, "java"), ({"build.gradle.kts": ""}, "java"), ({"mix.exs": "{:phoenix, \"~> 1.7\"}"}, "phoenix"),
    ({"index.html": None}, "static"), ({"Dockerfile": "FROM x\nEXPOSE 3000"}, "dockerfile")])
def test_launch_detect(files, fw):
    p = launch.detect(files); assert p["framework"] == fw
    if fw != "dockerfile": assert p["dockerfile"].startswith("FROM ") and "8080" in p["dockerfile"]
    else: assert p["port"] == 3000
    t = launch.toml_for("demo-app", p, fw); import tomllib; cfg = tomllib.loads(t); assert appconfig.validate(cfg)["ok"], t
    if fw == "rails": assert p["release_command"] == "bin/rails db:migrate"
    if fw == "rust": assert "target/release/svc" in p["dockerfile"]
    if fw == "django": assert "mysite.wsgi" in p["dockerfile"]
def test_cron_parse():
    t = calendar.timegm((2026, 10, 13, 3, 0, 0))   # Tuesday 03:00 UTC
    assert cron.due("0 3 * * *", t) and cron.due("*/15 * * * 2", t) and not cron.due("0 4 * * *", t) and cron.due("@daily", t - 3 * 3600)
    with pytest.raises(ValueError): cron.parse("* * *")
def test_cli_dockerignore(tmp_path):
    src = open(os.path.join(os.path.dirname(__file__), "..", "cli", "aloof")).read(); src = src[:src.index("p = argparse.ArgumentParser")]
    ns = {}; exec(compile(src, "aloof", "exec"), ns)
    (tmp_path / ".dockerignore").write_text("node_modules\n*.log\nsecrets/\n!secrets/public.txt\n**/__pycache__\n")
    for f in ("app.py", "node_modules/x/i.js", "a.log", "secrets/k.pem", "secrets/public.txt", "pkg/__pycache__/m.pyc", "pkg/m.py"):
        p = tmp_path / f; p.parent.mkdir(parents=True, exist_ok=True); p.write_text("x")
    import tarfile, io
    data, n = ns["tar_context"](str(tmp_path)); names = set(tarfile.open(fileobj=io.BytesIO(data)).getnames())
    assert names == {".dockerignore", "app.py", "secrets/public.txt", "pkg/m.py"}, names
def test_registry_and_git_creds_storage(user):
    r = user.post("/v1/registry", json={"server": "https://index.docker.io/v1/", "username": "me", "password": "dckr_pat_x"}); assert r.json()["server"] == "docker.io"
    assert user.post("/v1/registry", json={"server": "123456789012.dkr.ecr.eu-central-1.amazonaws.com", "username": "AKIA", "password": "s"}).json()["kind"] == "ecr"
    assert user.post("/v1/registry", json={"server": "europe-docker.pkg.dev", "username": "_json_key", "password": "notjson"}).status_code == 400
    assert user.post("/v1/registry", json={"server": "ghcr.io", "username": "me", "password": "ghp_x"}).json()["kind"] == "ghcr"
    assert {x["server"] for x in user.get("/v1/registry").json()} >= {"docker.io", "ghcr.io"}
    assert "dckr_pat_x" not in json.dumps(q("select * from registry_creds where account_id=?", (user.acct,)))
    with creds.docker_config(None, user.acct) as (env, has):
        cfg = json.load(open(os.path.join(env["DOCKER_CONFIG"], "config.json")))
        assert has and base64.b64decode(cfg["auths"]["https://index.docker.io/v1/"]["auth"]).decode() == "me:dckr_pat_x"
        d = env["DOCKER_CONFIG"]; assert oct(os.stat(d).st_mode & 0o777) == "0o700"
    assert not os.path.exists(d)
    k = user.post("/v1/git/credentials", json={"host": "github.com", "kind": "ssh"}).json()
    assert k["public_key"].startswith("ssh-ed25519 ") and "PRIVATE" not in json.dumps(k)
    assert "BEGIN OPENSSH" not in json.dumps(q("select * from git_creds where account_id=?", (user.acct,)))
    t = user.post("/v1/git/credentials", json={"host": "gitlab.com", "kind": "token", "token": "glpat-x"}).json(); assert t["username"] == "oauth2"
    assert creds.registry_of("ghcr.io/o/r:1") == "ghcr.io" and creds.registry_of("library/nginx") == "docker.io" and creds.registry_of("localhost:5000/x") == "localhost:5000"
    with pytest.raises(ValueError): creds._check_url("file:///etc")
    with pytest.raises(ValueError): creds._check_url("https://user:pw@github.com/o/r.git")
    os.environ.pop("PAAS_ALLOW_PRIVATE_GIT", None)
    with pytest.raises(ValueError, match="private address"): creds._check_url("http://127.0.0.1/x.git")
def test_deploy_tokens_scope_and_expiry(fake, user):
    deploy(user, "tok-app", "good:v1"); deploy(user, "tok-other", "good:v1")
    t = user.post("/v1/tokens", json={"app": "tok-app", "expires_in": "1h"}).json(); assert t["token"].startswith("dt_")
    c = TestClient(api.app, base_url="http://api.aloof.test"); c.headers["Authorization"] = f"Bearer {t['token']}"
    assert c.get("/v1/apps/tok-app").status_code == 200 and c.get("/v1/apps/tok-other").status_code == 403
    assert [x["name"] for x in c.get("/v1/apps").json()] == ["tok-app"]
    assert c.post("/v1/apps", json={"name": "tok-other", "source": {"image": "good:v2"}}).status_code == 403
    assert c.post("/v1/tokens", json={}).status_code == 403 and c.get("/v1/billing").status_code == 403 and c.post("/v1/auth/api-key").status_code == 403
    assert c.post("/v1/apps", json={"name": "tok-app", "source": {"image": "good:v2"}, "provider": "local-docker"}).status_code == 200
    org = user.post("/v1/tokens", json={"expires_in": "never"}).json(); c.headers["Authorization"] = f"Bearer {org['token']}"
    assert c.get("/v1/apps/tok-other").status_code == 200 and c.get("/v1/tokens").status_code == 403
    q("update deploy_tokens set expires=? where id=?", (time.time() - 1, t["id"])); c.headers["Authorization"] = f"Bearer {t['token']}"
    assert c.get("/v1/apps/tok-app").status_code == 401
    assert user.delete(f"/v1/tokens/{org['id']}").status_code == 200; c.headers["Authorization"] = f"Bearer {org['token']}"
    assert c.get("/v1/apps").status_code == 401
def test_staged_secrets(fake, user):
    deploy(user, "sec-app", "good:v1", env={"A": "1"})
    assert user.post("/v1/apps/sec-app/secrets", json={"secrets": {"B": "2"}, "unset": ["A"], "stage": True}).json()["staged"]
    st = {x["name"]: x["status"] for x in user.get("/v1/apps/sec-app/secrets").json()}; assert st == {"A": "deployed, staged unset", "B": "staged"}
    user.post("/v1/apps/sec-app/secrets/deploy"); time.sleep(0.3); wait(user, "sec-app")
    assert [x["name"] for x in user.get("/v1/apps/sec-app/secrets").json()] == ["B"]
    assert user.post("/v1/apps/sec-app/secrets/deploy").status_code == 400
class FakeRun(Fake):
    def __init__(self): super().__init__(); self.runs = []; self.fail_release = False
    def run_once(self, app, image, spec, command, timeout=900):
        self.runs.append((app, image, command, list(spec.get("networks") or [])))
        return (1, "migration failed") if self.fail_release and "migrate" in str(command) else (0, "done")
@pytest.fixture
def fakerun(monkeypatch, tmp_path):
    from paas import parity
    from paas.drivers import get_drivers, REGISTRY
    monkeypatch.setattr(parity, "BACKUP_DIR", str(tmp_path / "bk")); get_drivers(); f = FakeRun(); monkeypatch.setitem(REGISTRY, "local-docker", f); return f
def test_release_command_groups_cron_network(fakerun, user):
    r = user.post("/v1/apps", json={"name": "grp-app", "source": {"image": "good:v1"}, "plan": "shared-1x-256mb", "provider": "local-docker",
                                    "processes": {"web": "serve", "worker": "work --queue q"}, "process_counts": {"worker": 2}, "release_command": "migrate",
                                    "cron": [{"schedule": "* * * * *", "command": "tick"}]})
    assert r.status_code == 200, r.text; wait(user, "grp-app"); time.sleep(0.5); wait(user, "grp-app-worker")
    assert fakerun.runs[0][2] == "migrate" and f"aloof-acct-{user.acct}" in fakerun.runs[0][3]
    st = user.get("/v1/apps/grp-app/status").json(); groups = {g["group"]: g["count"] for g in st["process_groups"]}
    assert groups == {"web": 1, "worker": 2} and st["internal_host"] == "grp-app.internal"
    w = [v for v in fakerun.started.values() if v[0] == "grp-app-worker"]; assert w and all(f"aloof-acct-{user.acct}" in v[3] for v in w)
    spec = json.loads(q("select spec from apps where name='grp-app-worker'", one=True)["spec"]); assert spec["command"] == ["sh", "-c", "work --queue q"] and spec["_parent"] == "grp-app"
    assert spec["net_aliases"][f"aloof-acct-{user.acct}"] == ["grp-app-worker", "grp-app-worker.internal"]
    assert user.post("/v1/apps/grp-app/scale/count", json={"counts": {"worker": 1}}).json()["counts"] == {"worker": 1}
    assert len(engine.get_app("grp-app-worker")["instances"]) == 1
    assert user.post("/v1/apps/grp-app/scale/count", json={"counts": {"nope": 1}}).status_code == 400
    cron.tick(); time.sleep(0.3); assert any(x[2] == "tick" for x in fakerun.runs)
    assert user.post("/v1/apps/grp-app/cron/job0/run").json()["exit_code"] == 0
    assert user.get("/v1/apps/grp-app/cron").json()["runs"]
    fakerun.fail_release = True   # failed release_command: new release never starts, old keeps serving
    before = engine.get_app("grp-app")["instances"][0]["id"]
    user.post("/v1/apps", json={"name": "grp-app", "source": {"image": "good:v2"}, "provider": "local-docker"}); time.sleep(0.5); wait(user, "grp-app")
    a = engine.get_app("grp-app"); assert a["instances"][0]["id"] == before and "release_command exited 1" in (a["error"] or "")
    user.delete("/v1/apps/grp-app"); assert q("select status from apps where name='grp-app-worker'", one=True)["status"] == "destroyed"
def test_platform_image_is_owner_only(fake, user):
    deploy(user, "img-owner", "good:v1")
    c = TestClient(api.app, base_url="http://api.aloof.test"); k = c.post("/v1/signup", json={"email": f"o{time.time_ns()}@example.com"}).json()["api_key"]
    c.headers["Authorization"] = f"Bearer {k}"
    r = c.post("/v1/apps", json={"name": "thief-app", "source": {"image": "paas/img-owner:r1"}, "plan": "shared-1x-256mb"}); assert r.status_code == 409
    r = c.post("/v1/apps", json={"name": "thief-app2", "source": {"image": "127.0.0.1:5000/img-owner:1"}, "plan": "shared-1x-256mb"}); assert r.status_code == 409
    r = c.post("/v1/apps", json={"name": "thief-app3", "source": {"git": "https://github.com/o/r", "github_installation": 42}, "plan": "shared-1x-256mb"}); assert r.status_code == 409
def test_previews_api(fake, user):
    user.post("/v1/apps", json={"name": "prv-app", "source": {"image": "good:v1"}, "plan": "shared-1x-256mb", "provider": "local-docker"}); wait(user, "prv-app")
    assert user.post("/v1/apps/prv-app/previews", json={"pr": 7, "ref": "feature"}).status_code == 400   # not a git app
    sp = json.loads(q("select spec from apps where name='prv-app'", one=True)["spec"]); sp["source"] = {"git": "https://example.com/o/r.git"}
    q("update apps set spec=? where name='prv-app'", (json.dumps(sp),))
    r = user.post("/v1/apps/prv-app/previews", json={"pr": 7, "ref": "feature"}); assert r.status_code == 200 and r.json()["name"] == "prv-app-pr-7"
    assert [p["name"] for p in user.get("/v1/apps/prv-app/previews").json()] == ["prv-app-pr-7"]
    assert user.delete("/v1/apps/prv-app/previews/7").status_code == 200
# ---------------- GitHub App ----------------
@pytest.fixture
def gh(monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization as S
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(S.Encoding.PEM, S.PrivateFormat.PKCS8, S.NoEncryption()).decode()
    monkeypatch.setenv("GITHUB_APP_ID", "999"); monkeypatch.setenv("GITHUB_APP_SLUG", "aloof-test"); monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", pem)
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "whsec"); monkeypatch.setenv("GITHUB_CLIENT_ID", "cid"); monkeypatch.setenv("GITHUB_CLIENT_SECRET", "csec")
    calls = []
    def handler(req):
        calls.append((req.method, req.url.path, req.headers.get("authorization", "")))
        p = req.url.path
        if p == "/login/oauth/access_token": return httpx.Response(200, json={"access_token": "gho_user"})
        if p == "/user/installations": return httpx.Response(200, json={"installations": [{"id": 77, "account": {"login": "acme"}}]})
        if p.endswith("/access_tokens"): return httpx.Response(201, json={"token": "ghs_inst", "expires_at": "x"})
        if p == "/installation/repositories": return httpx.Response(200, json={"repositories": [{"full_name": "acme/web", "private": True, "default_branch": "main"}]})
        if p.endswith("/check-runs"): return httpx.Response(201, json={"id": 5})
        return httpx.Response(201, json={})
    monkeypatch.setattr(github_app, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    github_app._tok_cache.clear(); return key, calls
def test_github_jwt_and_install_verification(gh, user):
    key, calls = gh
    jwt = github_app.app_jwt(); h, b, sig = jwt.split(".")
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    key.public_key().verify(base64.urlsafe_b64decode(sig + "=="), f"{h}.{b}".encode(), padding.PKCS1v15(), hashes.SHA256())
    assert json.loads(base64.urlsafe_b64decode(b + "=="))["iss"] == "999"
    url = user.get("/v1/github/install").json()["url"]; st = url.split("state=")[1]; assert url.startswith("https://github.com/apps/aloof-test/installations/new")
    c = TestClient(api.app, base_url="http://api.aloof.test")
    assert "not accessible" in c.get(f"/github/installed?installation_id=12345&code=x&state={st}").text   # someone else's installation id
    assert "linked" in c.get(f"/github/installed?installation_id=77&code=x&state={st}").text
    assert "expired" in c.get(f"/github/installed?installation_id=77&code=x&state={st}").text   # state is single-use
    assert user.get("/v1/github/repos").json()[0]["repo"] == "acme/web"
def test_github_webhook_push_and_pr(gh, fake, user, monkeypatch):
    key, calls = gh
    q("insert into github_installs(installation_id,account_id,login,created) values(?,?,?,?) on conflict(installation_id) do update set account_id=excluded.account_id", (77, user.acct, "acme", time.time()))
    r = user.post("/v1/apps/gh-app/github", json={"repo": "acme/web", "deploy": False}); assert r.status_code == 200, r.text
    deployed = []
    monkeypatch.setattr(engine, "deploy", lambda acct, spec: deployed.append((acct, spec)) or {"name": spec["name"]})
    c = TestClient(api.app, base_url="http://api.aloof.test")
    def send(ev, payload, secret="whsec", did=None):
        body = json.dumps(payload).encode(); sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return c.post("/v1/github/webhook", content=body, headers={"X-GitHub-Event": ev, "X-Hub-Signature-256": sig, "X-GitHub-Delivery": did or str(time.time_ns())})
    push = {"ref": "refs/heads/main", "after": "a" * 40, "repository": {"full_name": "acme/web"}, "installation": {"id": 77}}
    assert send("push", push, secret="wrong").status_code == 401
    r = send("push", push, did="d1"); assert r.json()["result"] == f"deploy gh-app@{'a'*7}"
    assert send("push", push, did="d1").json().get("duplicate")   # replayed delivery ignored
    acct, spec = deployed[-1]; assert acct == user.acct and spec["source"] == {"git": "https://github.com/acme/web.git", "ref": "a" * 40, "github_installation": 77}
    assert any(p.endswith(f"/statuses/{'a'*40}") for _, p, _ in calls) and any(p.endswith("/check-runs") for _, p, _ in calls)
    assert send("push", push | {"ref": "refs/heads/other"}).json()["result"] == "no linked app for this event"
    q("insert into apps(name,account_id,spec,status,provider,created,updated) values(?,?,?,?,?,?,?) on conflict(name) do nothing",
      ("gh-app", user.acct, json.dumps({"name": "gh-app", "plan": "shared-1x-256mb", "env_enc": crypto.enc_json({"SECRET": "s"})}), "running", "local-docker", time.time(), time.time()))
    pr = {"action": "opened", "number": 12, "repository": {"full_name": "acme/web"}, "installation": {"id": 77},
          "pull_request": {"number": 12, "head": {"sha": "b" * 40, "repo": {"full_name": "evil/fork"}}}}
    r = send("pull_request", pr).json()["result"]; assert "preview gh-app-pr-12" in r and "fork: no secrets" in r
    spec = deployed[-1][1]; assert spec["env"] == {} and "github_installation" not in spec["source"] and spec["_preview_of"] == "gh-app"
    pr["pull_request"]["head"]["repo"]["full_name"] = "acme/web"; send("pull_request", pr)
    assert deployed[-1][1]["env"] == {"SECRET": "s"} and deployed[-1][1]["source"]["github_installation"] == 77
def test_github_manifest_setup(gh, monkeypatch):
    c = TestClient(api.app, base_url="http://api.aloof.test")
    assert c.post("/v1/admin/github/setup-link").status_code == 401
    st = github_app.new_setup_state(); page = c.get(f"/github/setup?state={st}").text
    assert "settings/apps/new" in page and "/v1/github/webhook" in page and "request_oauth_on_install" in page
    m = github_app.manifest("https://api.aloof.si", "https://www.aloof.si"); assert m["default_permissions"]["contents"] == "read" and set(m["default_events"]) == {"push", "pull_request"}
def test_named_api_keys():
    c = TestClient(api.app, base_url="http://api.aloof.test"); k = c.post("/v1/signup", json={"email": f"k{time.time_ns()}@example.com"}).json()["api_key"]
    c.headers["Authorization"] = f"Bearer {k}"
    ks = c.get("/v1/auth/keys").json(); assert [x["id"] for x in ks] == ["primary"] and ks[0]["prefix"] == k[:10]
    n = c.post("/v1/auth/keys", json={"label": "laptop"}).json(); assert n["api_key"].startswith("pk_")
    assert c.get("/v1/auth/me", headers={"Authorization": f"Bearer {n['api_key']}"}).status_code == 200
    row = [x for x in c.get("/v1/auth/keys").json() if x["label"] == "laptop"][0]; assert row["last_used"] and "api_key" not in row
    assert c.delete("/v1/auth/keys/primary").status_code == 400
    assert c.delete(f"/v1/auth/keys/{row['id']}").json()["ok"]
    assert c.get("/v1/auth/me", headers={"Authorization": f"Bearer {n['api_key']}"}).status_code == 401
def test_unknown_region_warns():
    from paas import appconfig
    w = appconfig.validate({"app": "reg-one", "primary_region": "ams", "build": {"image": "nginx"}})["warnings"]   # fly.io code: warn, don't 400
    assert any("unknown region 'ams'" in x and "nbg1" in x for x in w)
    for ok in ("eu-de", "eu-central", "nbg1"):   # aliases of nbg1
        assert not any("unknown region" in x for x in appconfig.validate({"app": "reg-two", "primary_region": ok, "build": {"image": "nginx"}})["warnings"])
    spec = appconfig.to_spec({"app": "reg-three", "primary_region": "fsn1", "regions": ["ams", "nbg1"]})[0]
    assert spec["region"] == "fsn1"   # primary_region wins over regions[0]
    spec = appconfig.to_spec({"app": "reg-four", "regions": ["iad"]})[0]; assert "region" not in spec
