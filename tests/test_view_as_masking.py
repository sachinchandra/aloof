"""Admin "view as user" masks customer secrets on the server and can't reveal them; the customer's own view is unchanged."""
import time
from test_admin_stages import owner2fa, _real, CS
from paas import api, crypto
MASK = "••••••••"
from paas.db import q, kv_put, kv_get

def _imp():
    o = owner2fa(); cl, u = _real(); n = f"pm{time.time_ns() % 10**9}"
    q("insert into pg_dbs(name,account_id,app,password_enc,plan,disk_gb,created,attached) values(?,?,?,?,?,?,?,?)",
      (n, u["id"], f"pg-{n}", crypto.enc("SuperSecretPw123"), "pg-dev", 1, time.time(), "[]"))
    r = o.post(f"/v1/admin/accounts/{u['id']}/impersonate", json={"confirm": u["email"], "reason": "support ticket"}, headers=CS); assert r.status_code == 200
    return o, cl, u, n

def test_view_as_user_masks_db_url_and_cannot_reveal():
    o, cl, u, n = _imp()
    assert o.get(f"/v1/pg/{n}?reveal=true").status_code == 403   # reveal is refused outright
    for path in (f"/v1/pg/{n}", "/v1/pg"):
        r = o.get(path); assert r.status_code == 200 and "SuperSecretPw123" not in r.text, path
    x = o.get(f"/v1/pg/{n}").json(); assert x["masked"] and x["database_url"] and "SuperSecretPw123" not in x["database_url"]
    assert not q("select 1 from audit where action='pg.reveal_url' and app=?", (f"pg-{n}",))
    own = cl.get(f"/v1/pg/{n}?reveal=true").json(); assert "SuperSecretPw123" in own["database_url"]   # the customer still can

def test_view_as_user_never_takes_new_api_key():
    o, cl, u, n = _imp(); k = "pk_" + "Z" * 30
    kv_put("key_reveal", str(u["id"]), k, ttl=600)
    me = o.get("/v1/auth/me"); assert me.status_code == 200 and me.json()["new_api_key"] is None and k not in me.text
    assert kv_get("key_reveal", str(u["id"])) == k   # still waiting for the customer
    assert all(k not in o.get(p).text for p in ("/v1/auth/keys", "/v1/apps", "/v1/tokens", "/v1/auth/connected-apps"))

def test_bug53_delete_card_matches_docs():
    CONTACT_EMAIL = "privacy@aloof.si"   # shown as written (b43336d); bug 53: card and docs must match
    cl, u = _real(); card = cl.get("/dashboard").text
    i = card.index("<b>Delete account</b>"); card = card[i:i + 1200]
    assert f"mailto:{CONTACT_EMAIL}" in card and "delete apps, databases and volumes first" not in card
    doc = __import__("fastapi.testclient", fromlist=["x"]).TestClient(api.app).get("/docs/billing").text
    d = doc[doc.index("id=delete"):][:1500]
    assert CONTACT_EMAIL in d
    for step in ("cancel any card subscription", "export anything you want to keep", "from the address on"):
        assert step in card and step in d, step
