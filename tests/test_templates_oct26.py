"""QA templates Oct 26: destroy cascades template-created volumes/Postgres; secure defaults; n8n needs Hobby; key hint."""
import os, json
from fastapi.testclient import TestClient
from paas import api, templating as T, engine, parity
from paas.db import q
from test_templates import _client, fake   # noqa: F401 (fixture)

def test_destroy_removes_template_resources_keeps_user_volumes(fake, monkeypatch):
    c = _client("casc@example.in"); q("update accounts set tier='hobby' where email='casc@example.in'")
    assert c.post("/v1/templates/n8n/deploy", json={"name": "flowc"}).status_code == 200
    aid = c.get("/v1/auth/me").json()["id"]
    q("insert into apps(name,account_id,spec,status,created) values(?,?,?,?,?) on conflict(name) do update set status='running'", ("flowc", aid, json.dumps({"name": "flowc", "plan": "shared-1x-1gb"}), "running", 0))
    engine.create_volume(aid, "flowc-mine", 1); q("update volumes set app='flowc', mount_path='/x' where name='flowc-mine'")
    assert q("select template_app from volumes where name='flowc-data'", one=True)["template_app"] == "flowc"
    assert q("select template_app from pg_dbs where name='flowc-db'", one=True)["template_app"] == "flowc"
    destroyed = []
    monkeypatch.setattr(parity, "pg_destroy", lambda n, keep_backups=False: (destroyed.append(n), q("delete from pg_dbs where name=?", (n,))))
    r = c.delete("/v1/apps/flowc").json()
    assert sorted(r["deleted"]["volumes"]) == ["flowc-data", "pgdata-flowc-db"] and r["deleted"]["postgres"] == ["flowc-db"] and r["kept"]["volumes"] == ["flowc-mine"] and destroyed == ["flowc-db"]
    assert not q("select 1 from volumes where name='flowc-data'") and q("select app from volumes where name='flowc-mine'", one=True)["app"] is None
    used = q("select coalesce(sum(size_gb),0) s from volumes where account_id=?", (aid,), one=True)["s"]; assert used == 1   # only the kept one counts
    o = engine.template_orphans(); assert not [x for x in o["volumes"] + o["postgres"] if x.get("template_app") == "flowc"]

def test_destroy_with_volumes_deletes_user_volumes(fake):
    c = _client("casc2@example.in"); aid = c.get("/v1/auth/me").json()["id"]
    q("insert into apps(name,account_id,spec,status,created) values(?,?,?,?,?)", ("plainx", aid, json.dumps({"name": "plainx", "plan": "shared-1x-256mb"}), "running", 0))
    engine.create_volume(aid, "plainx-v", 1); q("update volumes set app='plainx' where name='plainx-v'")
    r = c.delete("/v1/apps/plainx?with_volumes=true").json(); assert r["deleted"]["volumes"] == ["plainx-v"] and r["kept"]["volumes"] == []

def test_backfill_and_orphans_from_audit():
    q("insert into audit(ts,account_id,app,action,detail) values(?,?,?,?,?)", (0, 9901, "oldvw", "template.deploy", json.dumps({"template": "vaultwarden"})))
    for n in ("oldvw-data", "oldvw-extra"): q("insert into volumes(name,account_id,size_gb,created) values(?,?,?,?)", (n, 9901, 1, 0))
    assert engine.backfill_template_owned() >= 1
    assert q("select template_app from volumes where name='oldvw-data'", one=True)["template_app"] == "oldvw"
    assert q("select template_app from volumes where name='oldvw-extra'", one=True)["template_app"] is None   # not a template volume name
    assert "oldvw-data" in [v["name"] for v in engine.template_orphans()["volumes"]]

def test_n8n_requires_hobby_early(fake):
    c = _client("n8nfree@example.in")
    r = c.post("/v1/templates/n8n/deploy", json={"name": "flowfree"}); assert r.status_code == 400 and "requires the Hobby plan" in r.text
    assert "spec" not in fake and not q("select 1 from volumes where name='flowfree-data'") and not q("select 1 from pg_dbs where name='flowfree-db'")
    j = TestClient(api.app).get("/v1/templates/n8n").json(); assert j["min_tier"] == "hobby" and j["requires"] == "Requires Hobby or Pro"
    assert "Requires Hobby or Pro" in TestClient(api.app).get("/templates/n8n").text

def test_vaultwarden_secure_defaults(fake):
    c = _client("vw@example.in"); r = c.post("/v1/templates/vaultwarden/deploy", json={"name": "vaultx"}).json()
    env = fake["spec"]["env"]; assert env["SIGNUPS_ALLOWED"] == "false" and env["INVITATIONS_ALLOWED"] == "true"
    assert len(r["generated_secrets"]["ADMIN_TOKEN"]) >= 48 and "HSTS" in TestClient(api.app).get("/templates/vaultwarden").text

def test_umami_admin_password_set_synchronously(fake, monkeypatch, umami_enabled):
    calls = []
    class D:
        def exec_run(self, iid, cmd, **k):
            calls.append(cmd); return (0, b"1\n")
        def exec_stdin(self, iid, cmd, data, **k):
            calls.append(cmd + [data.decode()]); return (0, b"1\n")
    monkeypatch.setattr(parity, "_inst", lambda app: ({"id": "i1"}, D()))
    c = _client("um@example.in"); r = c.post("/v1/templates/umami/deploy", json={"name": "umx"}); assert r.status_code == 200, r.text
    pw = r.json()["generated_secrets"]["ADMIN_LOGIN"].split(" / ")[1]; assert len(pw) >= 24 and pw not in json.dumps(fake["spec"])
    assert not any(pw in a for c_ in calls for a in c_[:-1]) and calls[-1][-3:-1] == ["-f", "-"]   # pw only on stdin
    sql = calls[-1][-1]; assert sql.startswith("set log_statement = 'none'") and f"crypt('{pw}', gen_salt('bf', 10))" in sql and "drop extension" not in sql and "crypt('umami', password)" in sql

def test_umami_init_failure_fails_deploy_and_tears_down(fake, monkeypatch, umami_enabled, caplog):
    monkeypatch.setattr(T, "UMAMI_TIMEOUT", 0.05)
    class D:
        def exec_run(self, iid, cmd, **k): return (0, b"1\n")
        def exec_stdin(self, iid, cmd, data, **k): return (1, b"ERROR: function gen_salt does not exist")
    monkeypatch.setattr(parity, "_inst", lambda app: ({"id": "i1"}, D()))
    torn = []; monkeypatch.setattr(engine, "destroy_app", lambda n, with_volumes=False: torn.append(n) or {"ok": True, "deleted": {"volumes": ["pgdata-umy-db"], "postgres": ["umy-db"]}})
    c = _client("um2@example.in"); r = c.post("/v1/templates/umami/deploy", json={"name": "umy"})
    assert r.status_code == 503 and "couldn't be set" in r.text and "gen_salt" in r.text and torn == ["umy"] and "Removed: umy, umy-db, pgdata-umy-db" in r.text
    assert any(x.levelname == "WARNING" and "umy" in x.getMessage() for x in caplog.records)

def test_cli_signup_and_key_hint():
    cli = open(os.path.join(os.path.dirname(__file__), "..", "cli", "aloof")).read()
    assert 'r["api_key"][:7] + "…' in cli and "[:10]" not in cli.split('if o.cmd == "signup":')[1].split("elif")[0]
    assert api.key_hint("pk_abcdefghij") == "pk_abcd"
    assert "--with-volumes" in cli and "deleted volume" in cli and "kept volume" in cli

def test_disabled_template_hidden_and_rejected(fake):
    c = TestClient(api.app)
    if not T.registry()["umami"].get("disabled"): return
    assert "umami" not in [t["id"] for t in c.get("/v1/templates").json()["templates"]] and "umami" not in c.get("/llms-templates.md").text
    k = _client("dis@example.in"); r = k.post("/v1/templates/umami/deploy", json={"name": "umdis"})
    assert r.status_code == 503 and "temporarily disabled" in r.text and "spec" not in fake and not q("select 1 from pg_dbs where name='umdis-db'")

def test_teardown_reports_partial_and_failed(monkeypatch):
    monkeypatch.setattr(engine, "destroy_app", lambda n, with_volumes=False: {"ok": True, "deleted": {"volumes": [], "postgres": []}, "errors": ["postgres a-db: boom"]})
    m = T._teardown("a"); assert m.startswith("Partial cleanup") and "postgres a-db: boom" in m and "aloof destroy a" in m
    monkeypatch.setattr(engine, "destroy_app", lambda n, with_volumes=False: (_ for _ in ()).throw(RuntimeError("x")))
    m = T._teardown("a"); assert "Cleanup FAILED" in m and "Removed" not in m

def test_umami_exception_text_redacted(monkeypatch, caplog):
    pw = "A" * 24
    monkeypatch.setattr(parity, "_inst", lambda app: (_ for _ in ()).throw(RuntimeError(f"exec failed: {pw}")))
    try: T._umami_admin("u", "u-db", pw, timeout=0.05, sleep=0.01); assert False
    except T.InitError as e: assert pw not in str(e) and "<pw>" in str(e)
    assert all(pw not in r.getMessage() for r in caplog.records)

def test_cli_template_wait_uses_pre_request_time():
    cli = open(os.path.join(os.path.dirname(__file__), "..", "cli", "aloof")).read()
    seg = cli.split("if o.template:")[1].split("cfg = load_toml")[0]
    assert seg.index("t0 = time.time()") < seg.index('call("POST"') and "wait_deploy(name, t0)" in seg and "time.time() - 1" not in seg
    assert 'VERSION = "0.4.3"' in cli and "def wait_deploy(name, t0, timeout=600)" in cli and "stopped waiting" in cli
