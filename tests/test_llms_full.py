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
    assert c.delete("/v1/auth/connected-apps/cid-1", headers=h).json()["tokens_revoked"] == 2
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
