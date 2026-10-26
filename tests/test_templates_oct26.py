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
    assert r["deleted"] == {"volumes": ["flowc-data"], "postgres": ["flowc-db"]} and r["kept"]["volumes"] == ["flowc-mine"] and destroyed == ["flowc-db"]
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

def test_umami_admin_password_generated_and_applied(fake, monkeypatch, umami_enabled):
    started = []
    monkeypatch.setattr(T.__dict__["__builtins__"]["__import__"]("threading"), "Thread", lambda target, args, daemon: type("X", (), {"start": lambda s: started.append(args)})())
    c = _client("um@example.in"); r = c.post("/v1/templates/umami/deploy", json={"name": "umx"}).json()
    login = r["generated_secrets"]["ADMIN_LOGIN"]; assert login.startswith("admin / ") and len(login) > 20 and "umami" not in login.split(" / ")[1]
    assert started and started[0][0] == "umx" and started[0][2] == login.split(" / ")[1]
    assert login.split(" / ")[1] not in json.dumps(fake["spec"])                       # not stored in the app env
    calls = []
    class D:
        def exec_run(self, iid, cmd, **k): calls.append(cmd); return 0, b"1\n"
    monkeypatch.setattr(parity, "_inst", lambda app: ({"id": "i1"}, D()))
    assert T._umami_admin("umx", "umx-db", "S3cretPw_", timeout=5, sleep=0)
    assert "crypt('S3cretPw_'" in calls[-1][-1] and "username = 'admin'" in calls[-1][-1]
    scaled = []
    monkeypatch.setattr(parity, "_inst", lambda app: (_ for _ in ()).throw(ValueError("not up")))
    monkeypatch.setattr(engine, "scale", lambda n, i, reason="": scaled.append((n, i)))
    assert not T._umami_admin("umx", "umx-db", "pw", timeout=0.05, sleep=0.01) and scaled == [("umx", 0)]

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
