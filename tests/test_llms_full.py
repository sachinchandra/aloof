from fastapi.testclient import TestClient
from paas import api, llms_full
c = TestClient(api.app)
def test_llms_full_is_plain_markdown_and_complete():
    r = c.get("/llms-full.txt"); assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    t = r.text
    for s in ("## Quickstart", "Custom domains", "Postgres", "Billing", "## Regions", "nbg1", "/mcp-tools.md", "/api/openapi.json"): assert s in t, s
    assert "<div" not in t and "<script" not in t and "<a href" not in t
def test_mcp_tools_reference_matches_server():
    r = c.get("/mcp-tools.md"); assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    names = [t["name"] for t in llms_full.mcp_tools()]
    assert len(names) == len(api.mcp_tool_names()) and set(names) == set(api.mcp_tool_names())
    for n in names: assert f"## `{n}`" in r.text
    assert "| `name` | `str` | yes |" in r.text
    j = c.get("/mcp-tools.json").json(); assert [t["name"] for t in j["tools"]] == names
def test_llms_txt_links():
    t = c.get("/llms.txt").text
    for s in ("/llms-full.txt", "/mcp-tools.md", "/api/openapi.json"): assert s in t
def test_connected_apps_list_and_revoke():
    import json, time
    from paas import engine, crypto
    from paas.db import kv_put, q
    a = engine.create_account("conn-apps@real.dev"); key = a["api_key"]
    other = engine.create_account("conn-other@real.dev")
    kv_put("oauth:client", "cid-1", json.dumps({"client_id": "cid-1", "client_name": "Claude", "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]}))
    for kind, k, ak in (("access", "h1", key), ("refresh", "h2", key), ("access", "h3", other["api_key"])):
        kv_put("oauth:" + kind, k, json.dumps({"client_id": "cid-1", "api_key": "enc:" + crypto.enc(ak), "expires_at": time.time() + 3600}), 3600)
    for k, ak, cid in (("c1", key, "cid-1"), ("c2", other["api_key"], "cid-1"), ("c3", key, "cid-1x")):
        kv_put("oauth:code", k, json.dumps({"code": {"client_id": cid}, "api_key": "enc:" + crypto.enc(ak)}), 300)
    h = {"Authorization": f"Bearer {key}"}
    r = c.get("/v1/auth/connected-apps", headers=h).json()
    assert len(r) == 1 and r[0]["name"] == "Claude" and r[0]["redirect_host"] == "claude.ai" and r[0]["access_tokens"] == 1 and r[0]["refresh_tokens"] == 1
    r = c.delete("/v1/auth/connected-apps/cid-1", headers=h).json(); assert r["tokens_revoked"] == 2 and r["codes_revoked"] == 1
    assert c.get("/v1/auth/connected-apps", headers=h).json() == []
    assert q("select count(*) n from kv where kind='oauth:access' and k='h3'", one=True)["n"] == 1   # other account untouched
    codes = {r["k"] for r in q("select k from kv where kind='oauth:code'")}
    assert "c1" not in codes and {"c2", "c3"} <= codes   # this account's unused code burned; other account / other client kept
    assert c.delete("/v1/auth/connected-apps/cid-1", headers=h).status_code == 404
def test_cli_login_exists():
    import subprocess, sys, os
    r = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "..", "cli", "aloof"), "login", "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "--key" in r.stdout
def test_docs_deploy_button_and_revoke():
    assert "needs a signed-in browser session" in c.get("/docs/api").text
    assert "logged-in session" in c.get("/llms.txt").text and "/v1/auth/connected-apps" in c.get("/docs/mcp").text

def test_revoke_codes_only_client_returns_200():
    import json
    from paas import engine, crypto
    from paas.db import kv_put
    a = engine.create_account("codes-only@real.dev"); h = {"Authorization": f"Bearer {a['api_key']}"}
    kv_put("oauth:code", "co1", json.dumps({"code": {"client_id": "cid-codes"}, "api_key": "enc:" + crypto.enc(a["api_key"])}), 300)
    r = c.delete("/v1/auth/connected-apps/cid-codes", headers=h)
    assert r.status_code == 200 and r.json() == {"ok": True, "tokens_revoked": 0, "codes_revoked": 1}
def test_default_plan_by_tier_and_errors():
    from paas.db import q
    a = engine_acct("plan-free@real.dev"); h = {"Authorization": f"Bearer {a['api_key']}"}
    r = c.post("/v1/apps", headers=h, json={"name": "pf-default-1", "source": {"image": "nginx:alpine"}, "port": 80})
    assert r.status_code == 200, r.text
    from paas import engine
    assert engine.default_plan(a["id"]) == "shared-1x-512mb"
    assert '"plan": "shared-1x-512mb"' in q("select spec from apps where name='pf-default-1'", one=True)["spec"] or engine.get_app("pf-default-1")["plan"]["id"] == "shared-1x-512mb"
    r = c.post("/v1/apps", headers=h, json={"name": "pf-big-1", "source": {"image": "nginx:alpine"}, "plan": "shared-1x-1gb", "port": 80})
    assert r.status_code == 400 and "Allowed plans: shared-1x-256mb, shared-1x-512mb" in r.json()["detail"]
    q("update accounts set tier='hobby' where id=?", (a["id"],)); assert engine.default_plan(a["id"]) == "shared-1x-1gb"
    b = engine_acct("plan-other@real.dev")
    r = c.post("/v1/apps", headers={"Authorization": f"Bearer {b['api_key']}"}, json={"name": "pf-default-1", "source": {"image": "nginx:alpine"}, "port": 80})
    assert r.status_code == 409 and "Pick another name" in r.json()["detail"]
def engine_acct(email):
    from paas import engine; return engine.create_account(email)
def test_quickstart_uses_unique_name():
    t = c.get("/docs/quickstart").text
    assert "deploy hello" not in t and "your-app-name" in t and "my-api-1234" in t
    l = c.get("/llms.txt").text; assert "my-api-1234" in l and "app name taken" in l
