"""Fly-parity features against a fake driver: releases + zero-downtime + rollback, managed Postgres, snapshots, exec/console audit, drains."""
import os, io, json, time, socket, threading, http.server, tarfile, pytest
os.environ["PAAS_READY_TIMEOUT"] = "1.5"
from fastapi.testclient import TestClient
from paas import api, engine, parity, billing, plans
from paas.db import q
from paas.drivers import get_drivers, REGISTRY
from paas.drivers.base import Instance

class _H(http.server.BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a): pass
def _server():
    s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H); threading.Thread(target=s.serve_forever, daemon=True).start(); return s.server_address[1]
def _dead_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

class Fake:
    """Images named 'bad*' never become ready. exec_run echoes; exec_socket is a socketpair running a tiny echo 'shell'."""
    name = "local-docker"; supports_gpu = False
    def __init__(self): self.n = 0; self.started = {}; self.stopped = []; self.nets = {}; self.execs = []; self.files = {}
    def available(self): return True, "ok"
    def build(self, app, source, log): self.n += 1; return source.get("image") or f"paas/{app}:r{self.n}"
    def start_instance(self, app, image, spec, index):
        self.n += 1; iid = f"ct{time.time_ns()}"; port = _dead_port() if "bad" in image else _server()
        self.started[iid] = (app, image, dict(spec.get("env") or {}), list(spec.get("networks") or [])); return Instance(id=iid, host="127.0.0.1", port=port)
    def stop_instance(self, iid): self.stopped.append(iid)
    def logs(self, iid, tail=200): return ""
    def inspect(self, iid): return "running"
    def exec_run(self, iid, cmd, timeout=60, stdout_only=False):
        self.execs.append((iid, cmd))
        if cmd[0] == "pg_dump": return 0, b"PGDMP-fake-" + str(len(self.execs)).encode()
        return 0, ("ran: " + " ".join(cmd)).encode()
    def exec_socket(self, iid, cmd=None):
        a, b = socket.socketpair()
        def shell():
            while True:
                d = b.recv(4096)
                if not d: break
                b.sendall(b"echo:" + d)
        threading.Thread(target=shell, daemon=True).start(); return "exec1", a
    def exec_resize(self, *a): pass
    def snapshot_volume(self, vol, path):
        open(path, "wb").write(self.files.get(vol, b"empty")); return os.path.getsize(path)
    def restore_volume(self, vol, path): self.files[vol] = open(path, "rb").read()
    def remove_network(self, n): pass
    class _C:   # docker-client stand-in for pg_restore's put_archive
        class containers:
            @staticmethod
            def get(iid):
                class Ct:
                    def put_archive(self, path, data): return True
                    def logs(self, **k): return b""
                return Ct()
    c = _C()
@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.setattr(parity, "BACKUP_DIR", str(tmp_path / "bk")); get_drivers(); f = Fake(); monkeypatch.setitem(REGISTRY, "local-docker", f); return f
@pytest.fixture
def user():
    c = TestClient(api.app, base_url="http://api.aloof.test"); e = f"p{time.time_ns()}@example.com"
    k = c.post("/v1/signup", json={"email": e}).json()["api_key"]; q("update accounts set tier='hobby' where email=?", (e,))
    c.headers["Authorization"] = f"Bearer {k}"; c.key = k; c.acct = q("select id from accounts where email=?", (e,), one=True)["id"]
    yield c
    time.sleep(0.2)   # let deploy threads settle, then leave nothing running for other test modules
    q("update instances set status='stopped' where app in (select name from apps where account_id=?)", (c.acct,))
    q("update apps set status='destroyed' where account_id=?", (c.acct,)); q("delete from archives where account_id=?", (c.acct,))
def wait(c, name, path=None, until=("running", "failed", "stopped", "idle"), t=20):
    end = time.time() + t
    while time.time() < end:
        r = c.get(path or f"/v1/apps/{name}").json()
        if r.get("status") in until: return r
        time.sleep(0.1)
    raise AssertionError(f"timeout: {r}")
def deploy(c, name, image, **kw):
    r = c.post("/v1/apps", json={"name": name, "source": {"image": image}, "plan": "shared-1x-256mb", "provider": "local-docker", **kw}); assert r.status_code == 200, r.text
    time.sleep(0.05); return wait(c, name)

def test_releases_zero_downtime_and_rollback(fake, user):
    a = deploy(user, "rel-app", "good:v1"); assert a["status"] == "running"; first = a["instances"][0]["id"]
    rel = user.get("/v1/apps/rel-app/releases").json(); assert [r["version"] for r in rel] == [1] and rel[0]["status"] == "active"
    # a broken release never takes traffic: new instance removed, v1 still serving
    a = deploy(user, "rel-app", "bad:v2"); assert a["status"] == "running" and "still serving v1" in a["error"]
    assert [i["id"] for i in a["instances"]] == [first] and first not in fake.stopped
    rel = {r["version"]: r["status"] for r in user.get("/v1/apps/rel-app/releases").json()}; assert rel == {1: "active", 2: "failed"}
    # good v3 rolls new-before-old; then roll back to v1 without a build
    a = deploy(user, "rel-app", "good:v3"); assert a["status"] == "running" and not a["error"] and first in fake.stopped
    builds = fake.n
    user.post("/v1/apps/rel-app/env", json={"env": {"X": "1"}}); wait(user, "rel-app")   # config change = release v4
    r = user.post("/v1/apps/rel-app/rollback", json={}); assert r.status_code == 200, r.text; a = wait(user, "rel-app")
    rel = user.get("/v1/apps/rel-app/releases").json(); top = rel[0]
    assert top["version"] == 5 and top["status"] == "active" and top["image"] == "good:v3" and top["reason"] == "rollback to v3"
    assert "X" not in fake.started[a["instances"][0]["id"]][2]                      # previous config restored
    r = user.post("/v1/apps/rel-app/rollback", json={"version": 1}); wait(user, "rel-app")
    assert user.get("/v1/apps/rel-app/releases").json()[0]["image"] == "good:v1"
    assert user.post("/v1/apps/rel-app/rollback", json={"version": 99}).status_code == 400
    assert any(e["action"] == "rollback" for e in user.get("/v1/audit").json())

def test_pg_plans_margin_floor():
    for k, p in plans.PG_PLANS.items():
        assert plans.pg_margin(k) >= plans.MIN_MARGIN, k
        assert p["price_usd_month"] >= plans.PLANS[p["compute"]]["price_usd_month"] + p["disk_gb"] * plans.RATES["volume_usd_gb_month"]
    assert plans.net_margin(plans.RATES["backup_usd_gb_month"], plans.RATE_COST["volume_usd_gb_month"]) >= plans.MIN_MARGIN

def test_managed_postgres_attach_backup_retention(fake, user, monkeypatch):
    monkeypatch.setattr(parity, "PG_KEEP", 2)
    r = user.post("/v1/pg", json={"name": "db1", "plan": "pg-basic"}); assert r.status_code == 200, r.text
    d = wait(user, None, "/v1/pg/db1"); assert d["status"] == "running", d["error"]; assert d["host"] == "pg-db1"
    assert user.post("/v1/apps", json={"name": "pg-evil", "source": {"image": "x"}}).status_code == 400   # reserved prefix
    assert all(not x["name"].startswith("pg-") for x in user.get("/v1/apps").json())                        # hidden from apps
    deploy(user, "web1", "good:web")
    r = user.post("/v1/pg/db1/attach", json={"app": "web1"}); assert r.status_code == 200, r.text; a = wait(user, "web1")
    app_, img, env, nets = fake.started[a["instances"][0]["id"]]
    url = user.get("/v1/pg/db1?reveal=true").json()["database_url"]
    assert env["DATABASE_URL"] == url and url.startswith("postgres://aloof:") and "@pg-db1:5432/db1" in url and "aloof-pg-db1" in nets
    assert "DATABASE_URL" in a["env_keys"] and url not in json.dumps(user.get("/v1/pg/db1").json())       # not shown unless revealed
    row = q("select spec from apps where name='web1'", one=True)["spec"]; assert url.split("@")[0].split(":")[-1] not in row   # encrypted at rest
    for _ in range(3): assert user.post("/v1/pg/db1/backups").status_code == 200
    bk = user.get("/v1/pg/db1/backups").json(); assert len(bk) == 2                                         # retention
    assert all(oct(os.stat(r["path"]).st_mode & 0o777) == "0o600" for r in q("select path from archives where owner='db1'"))
    q("update archives set created=created-90000 where owner='db1'"); q("update pg_dbs set created=created-900 where name='db1'"); parity.pg_backup_tick()
    assert any(b["note"] == "daily" for b in user.get("/v1/pg/db1/backups").json())
    # billing: pg compute at the pg plan rate (minus disk, metered as a volume) + backups
    for i in q("select id from instances where app='pg-db1' and status='running'"):
        q("insert into meter(instance_id,last_billed) values(?,?) on conflict(instance_id) do update set last_billed=excluded.last_billed", (i["id"], time.time() - 3600))
    billing.meter_tick(); m = billing.month_summary(user.acct)["usage"]
    p = plans.PG_PLANS["pg-basic"]; exp = (p["price_usd_month"] - p["disk_gb"] * plans.RATES["volume_usd_gb_month"]) / 720 * 100
    assert abs(m["postgres"]["cost_cents"] - exp) / exp < 0.05 and "backup" in m
    user.delete("/v1/pg/db1/attach/web1"); a = wait(user, "web1"); assert "DATABASE_URL" not in a["env_keys"]
    assert user.delete("/v1/pg/db1").status_code == 200 and user.get("/v1/pg").json() == []
    assert not q("select 1 from volumes where name='pgdata-db1'") and not q("select 1 from archives where owner='db1'")

def test_volume_snapshot_restore(fake, user, monkeypatch):
    monkeypatch.setattr(parity, "SNAP_KEEP", 2)
    assert user.post("/v1/volumes", json={"name": "data1", "size_gb": 1}).status_code == 200
    deploy(user, "volapp", "good:vol"); user.post("/v1/apps/volapp/volumes", json={"volume": "data1", "path": "/data"}); wait(user, "volapp")
    fake.files["data1"] = b"version-A"; s1 = user.post("/v1/volumes/data1/snapshots").json()
    fake.files["data1"] = b"version-B"; user.post("/v1/volumes/data1/snapshots"); user.post("/v1/volumes/data1/snapshots")
    snaps = user.get("/v1/volumes/data1/snapshots").json(); assert len(snaps) == 2 and s1["id"] not in [s["id"] for s in snaps]
    fake.files["data1"] = b"version-A"; s1 = user.post("/v1/volumes/data1/snapshots").json(); fake.files["data1"] = b"corrupted"
    before = len(fake.stopped); r = user.post("/v1/volumes/data1/restore", json={"snapshot_id": s1["id"]}).json()
    assert r["restarted_instances"] == 1 and fake.files["data1"] == b"version-A" and len(fake.stopped) > before
    assert wait(user, "volapp")["status"] == "running" and len(wait(user, "volapp")["instances"]) == 1
    other = TestClient(api.app); k = other.post("/v1/signup", json={"email": f"o{time.time_ns()}@x.com"}).json()["api_key"]
    assert other.get("/v1/volumes/data1/snapshots", headers={"Authorization": f"Bearer {k}"}).status_code == 404

def test_exec_and_console_are_authenticated_and_audited(fake, user):
    deploy(user, "shell-app", "good:sh")
    r = user.post("/v1/apps/shell-app/exec", json={"command": "ls /"}).json(); assert r["exit_code"] == 0 and "ls /" in r["output"]
    with user.websocket_connect("/v1/apps/shell-app/console", headers={"Authorization": f"Bearer {user.key}"}) as ws:
        ws.send_text(json.dumps({"resize": [40, 120]})); ws.send_bytes(b"whoami\r"); assert ws.receive_bytes().startswith(b"echo:whoami")
    time.sleep(0.2); acts = [(e["action"], e["detail"]) for e in user.get("/v1/audit?app_name=shell-app").json()]
    assert ("console.input", "whoami") in acts and any(a == "console.open" for a, _ in acts) and any(a == "console.close" for a, _ in acts)
    assert ("exec", '["/bin/sh", "-c", "ls /"]') in acts
    anon = TestClient(api.app)
    with pytest.raises(Exception):
        with anon.websocket_connect("/v1/apps/shell-app/console") as ws: ws.receive_bytes()
    o = TestClient(api.app); k = o.post("/v1/signup", json={"email": f"x{time.time_ns()}@x.com"}).json()["api_key"]
    with pytest.raises(Exception):
        with o.websocket_connect("/v1/apps/shell-app/console", headers={"Authorization": f"Bearer {k}"}) as ws: ws.receive_bytes()
    assert o.post("/v1/apps/shell-app/exec", json={"command": "id"}, headers={"Authorization": f"Bearer {k}"}).status_code == 404
    # session cookie from a foreign Origin is refused (cross-site WebSocket hijacking)
    c = TestClient(api.app, base_url="http://api.aloof.test"); e = f"w{time.time_ns()}@example.com"
    c.post("/v1/auth/register", json={"email": e, "password": "correct horse 1"})
    with pytest.raises(Exception):
        with c.websocket_connect("/v1/apps/shell-app/console", headers={"Origin": "https://evil.example"}) as ws: ws.receive_bytes()

def test_drains_ssrf_http_syslog_and_retention(fake, user, monkeypatch, tmp_path):
    monkeypatch.setattr(parity, "LOG_DIR", str(tmp_path)); deploy(user, "logapp", "good:log")
    for bad in ("http://127.0.0.1:8000/x", "https://10.0.0.5/x", "ftp://example.com", "syslog+tcp://example.com"):
        assert user.post("/v1/apps/logapp/drains", json={"url": bad}).status_code == 400, bad
    got = []
    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got.append((self.headers.get("Authorization"), json.loads(self.rfile.read(int(self.headers["Content-Length"]))))); self.send_response(204); self.end_headers()
        def log_message(self, *a): pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); udp.bind(("127.0.0.1", 0)); udp.settimeout(3)
    monkeypatch.setenv("PAAS_DRAIN_ALLOW_PRIVATE", "1")
    d = user.post("/v1/apps/logapp/drains", json={"url": f"http://user:s3cret@127.0.0.1:{srv.server_address[1]}/ingest"}).json()
    assert "s3cret" not in json.dumps(d) and "s3cret" not in json.dumps(q("select * from drains where id=?", (d["id"],), one=True))
    user.post("/v1/apps/logapp/drains", json={"url": f"syslog+udp://127.0.0.1:{udp.getsockname()[1]}"})
    parity._last_ev["logapp"] = 0; parity.collect_tick()
    assert got and got[0][0].startswith("Basic ") and any("deploy complete" in l["message"] for l in got[0][1])
    assert udp.recv(9000).startswith(b"<14>1 ") 
    assert all(x["sent"] > 0 and not x["last_error"] for x in user.get("/v1/apps/logapp/drains").json())
    h = user.get("/v1/apps/logapp/logs/history?contains=deploy").json(); assert h["retention_days"] == 7 and h["lines"]
    old = os.path.join(str(tmp_path), "logapp", "2000-01-01.jsonl"); open(old, "w").write("{}\n"); parity.prune_logs_tick(); assert not os.path.exists(old)

def test_docs_pricing_llms_list_new_features():
    c = TestClient(api.app)
    for d in ("releases", "postgres", "console", "logs"): assert c.get(f"/docs/{d}").status_code == 200, d
    assert "pg-basic" in c.get("/docs/postgres").text and "Managed Postgres" in c.get("/pricing").text
    t = c.get("/llms.txt").text; assert "/rollback" in t and "/v1/pg" in t and "/drains" in t and "/console" in t
    tools = set(c.get("/.well-known/mcp.json").json()["tools"]); assert {"rollback_app", "create_postgres", "exec_command", "add_log_drain", "snapshot_volume"} <= tools

def test_destroy_during_redeploy_leaves_nothing_running(fake, user, monkeypatch):
    deploy(user, "racer", "good:r1"); orig = fake.start_instance
    def slow(*a, **k): time.sleep(0.6); return orig(*a, **k)
    monkeypatch.setattr(fake, "start_instance", slow)
    user.post("/v1/apps/racer/env", json={"env": {"A": "1"}}); time.sleep(0.2)
    assert user.delete("/v1/apps/racer").status_code == 200; time.sleep(1.5)
    assert q("select status from apps where name='racer'", one=True)["status"] == "destroyed"
    assert not q("select 1 from instances where app='racer' and status='running'")
