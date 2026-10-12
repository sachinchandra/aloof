"""One-shot, idempotent import of the pre-v0.3 SQLite DB (data/paas.db) into the default DB (Postgres).
API keys become SHA-256 hashes (same key keeps working); app env is encrypted into env_enc; domains get TXT tokens
(previously verified domains are grandfathered as owned+routed)."""
import os, sys, sqlite3, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from paas import db
from paas.crypto import sha256, dump_spec
from paas.domains import new_token
src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "data", "paas.db")
print("target:", "postgres" if db.PG else "sqlite"); s = sqlite3.connect(src); s.row_factory = sqlite3.Row
rows = lambda t: [dict(r) for r in s.execute(f"select * from {t}")]
n = {}
for a in rows("accounts"):
    db.q("insert into accounts(id,email,api_key_hash,api_key_prefix,created,credit_inr,credit_cents,email_verified) values(?,?,?,?,?,?,?,0) on conflict(id) do nothing",
         (a["id"], a["email"], sha256(a["api_key"]), a["api_key"][:10], a["created"], a["credit_inr"], a["credit_inr"] * 100.0 / 88.0)); n["accounts"] = n.get("accounts", 0) + 1
for a in rows("apps"):
    spec = json.loads(a["spec"]); spec.setdefault("env", {})
    db.q("insert into apps(name,account_id,spec,status,provider,created,updated,error) values(?,?,?,?,?,?,?,?) on conflict(name) do nothing",
         (a["name"], a["account_id"], dump_spec(spec), a["status"], a["provider"], a["created"], a["updated"], a["error"])); n["apps"] = n.get("apps", 0) + 1
for i in rows("instances"):
    db.q("insert into instances(id,app,provider,host,port,status,started,stopped) values(?,?,?,?,?,?,?,?) on conflict(id) do nothing",
         (i["id"], i["app"], i["provider"], i["host"], i["port"], i["status"], i["started"], i["stopped"])); n["instances"] = n.get("instances", 0) + 1
if not db.q("select 1 from events limit 1"):
    for e in rows("events"): db.q("insert into events(app,ts,msg) values(?,?,?)", (e["app"], e["ts"], e["msg"]))
    n["events"] = len(rows("events"))
for v in rows("volumes"):
    db.q("insert into volumes(name,account_id,size_gb,app,mount_path,created) values(?,?,?,?,?,?) on conflict(name) do nothing",
         (v["name"], v["account_id"], v["size_gb"], v["app"], v["mount_path"], v["created"])); n["volumes"] = n.get("volumes", 0) + 1
for d in rows("domains"):
    db.q("insert into domains(domain,app,account_id,verified,created,txt_token,owned,routed) values(?,?,?,?,?,?,?,?) on conflict(domain) do nothing",
         (d["domain"], d["app"], d["account_id"], d["verified"], d["created"], new_token(), d["verified"], d["verified"])); n["domains"] = n.get("domains", 0) + 1
if db.PG: db.q("select setval(pg_get_serial_sequence('accounts','id'), (select coalesce(max(id),1) from accounts))")
print("imported:", n)
