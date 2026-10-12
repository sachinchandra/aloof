"""Re-encrypt all at-rest secrets with the first key in PAAS_SECRET_KEY (format: "new,old[,older]")."""
import os, sys, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from paas import db, crypto
n = 0
for a in db.q("select name, spec from apps"):
    s = json.loads(a["spec"])
    if "env_enc" in s: s["env_enc"] = crypto.F.rotate(s["env_enc"].encode()).decode(); db.q("update apps set spec=? where name=?", (json.dumps(s), a["name"])); n += 1
for r in db.q("select id, agent_token_enc from nodes where agent_token_enc is not null"):
    db.q("update nodes set agent_token_enc=? where id=?", (crypto.F.rotate(r["agent_token_enc"].encode()).decode(), r["id"])); n += 1
for r in db.q("select kind, k, v from kv where v like ?", ('%"enc:%',)):
    v = json.loads(r["v"])
    for f, x in list(v.items()):
        if isinstance(x, str) and x.startswith("enc:"): v[f] = "enc:" + crypto.F.rotate(x[4:].encode()).decode()
    db.q("update kv set v=? where kind=? and k=?", (json.dumps(v), r["kind"], r["k"])); n += 1
print("re-encrypted", n, "records")
