"""MCP v2 tool surface: 25 tools, composite inspect/list, legacy names hidden unless opted in but still callable."""
import os, sys, json, anyio, httpx
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp_server"))
from mcp_server import server as S
from paas.mcp_tools import mcp_tool_names, legacy_map
def _calls(monkeypatch, body=None):
    seen = []
    def fake(method, url, **k): seen.append((method, url.replace(S.API, ""), k.get("json"), k.get("params"))); return httpx.Response(200, json=body if body is not None else {"ok": 1})
    monkeypatch.setattr(S.httpx, "request", fake)
    monkeypatch.setattr(S.httpx, "get", lambda url, **k: (seen.append(("GET", url.replace(S.API, ""), None, k.get("params"))), httpx.Response(200, text="log line"))[1])
    return seen
async def _names(legacy=False):
    from mcp.shared.memory import create_connected_server_and_client_session as conn
    async with conn(S.mcp._mcp_server) as s: return [t.name for t in (await s.list_tools()).tools]
def test_tool_count_and_static_list_match():
    names = anyio.run(_names)
    assert 20 <= len(names) <= 25 and names == mcp_tool_names()
    assert {"inspect", "list", "destroy_app", "restore", "deploy_app"} <= set(names)
    assert not set(names) & set(legacy_map())
def test_legacy_opt_in(monkeypatch):
    monkeypatch.setenv("ALOOF_MCP_TOOLS", "legacy")
    names = anyio.run(_names)
    assert set(legacy_map()) <= set(names) and len(names) == len(mcp_tool_names()) + len(legacy_map()) == 47
def test_legacy_names_still_callable(monkeypatch):
    seen = _calls(monkeypatch)
    async def go():
        from mcp.shared.memory import create_connected_server_and_client_session as conn
        async with conn(S.mcp._mcp_server) as s:
            return [await s.call_tool("get_status", {"name": "a1", "api_key": "pk_x"}), await s.call_tool("diagnose_app", {"name": "a1", "api_key": "pk_x"})]
    r = anyio.run(go); assert not any(x.isError for x in r)
    assert [u for _, u, _, _ in seen] == ["/v1/apps/a1", "/v1/apps/a1/diagnose"]
def test_inspect_routes(monkeypatch):
    seen = _calls(monkeypatch)
    for w, path in (("status", "/v1/apps/a1"), ("details", "/v1/apps/a1/status"), ("diagnose", "/v1/apps/a1/diagnose"), ("releases", "/v1/apps/a1/releases"),
                    ("metrics", "/v1/apps/a1/metrics"), ("events", "/v1/apps/a1/events"), ("domains", "/v1/apps/a1/domains"), ("logs", "/v1/apps/a1/logs")):
        seen.clear(); r = S.inspect_("a1", w, api_key="pk_x"); assert seen[0][1] == path, w
    assert r == {"name": "a1", "logs": "log line"}
    import pytest
    with pytest.raises(S.ToolError, match="unknown what"): S.inspect_("a1", "nope")
def test_list_routes(monkeypatch):
    seen = _calls(monkeypatch, body=[])
    for k, path in (("apps", "/v1/apps"), ("volumes", "/v1/volumes"), ("postgres", "/v1/pg"), ("templates", "/v1/templates"), ("regions", "/v1/regions"), ("usage", "/v1/usage"), ("domains", "/v1/domains")):
        seen.clear(); S.list_(k, api_key="pk_x"); assert seen[0][1] == path, k
    seen.clear(); S.list_("backups", name="db1", source="pg"); assert seen[0][1] == "/v1/pg/db1/offsite"
    seen.clear(); S.list_("snapshots", name="v1"); assert seen[0][1] == "/v1/volumes/v1/snapshots"
    import pytest
    with pytest.raises(S.ToolError): S.list_("backups", name="db1")
def test_mutating_tools(monkeypatch):
    seen = _calls(monkeypatch)
    S.scale_app("a1", autoscale={"min": 0}); assert seen[-1][:2] == ("PUT", "/v1/apps/a1/autoscale") and seen[-1][2]["max"] == 3
    S.scale_app("a1", autoscale={}); assert seen[-1][:2] == ("DELETE", "/v1/apps/a1/autoscale")
    S.scale_app("a1", instances=2); assert seen[-1][:3] == ("POST", "/v1/apps/a1/scale", {"instances": 2})
    S.backup("volume", "v1"); assert seen[-1][1] == "/v1/volumes/v1/snapshots"
    S.restore("offsite_pg", "db1", "b1"); assert seen[-1][1:3] == ("/v1/pg/db1/offsite-restore", {"backup_id": "b1"})
    S.add_credential("git_token", "github.com", secret="t"); assert seen[-1][1:3] == ("/v1/git/credentials", {"host": "github.com", "kind": "token", "token": "t", "app": None})
    S.deploy_app("a1", image="nginx", config={"processes": {}}); assert seen[-1][:2] == ("POST", "/v1/apps/a1/deploy")
def test_docs_have_legacy_mapping():
    from fastapi.testclient import TestClient
    from paas import api
    c = TestClient(api.app); md = c.get("/mcp-tools.md").text
    assert "## Legacy tool names" in md and "| `diagnose_app` | `inspect(what='diagnose')` |" in md and "?tools=legacy" in md
    j = c.get("/mcp-tools.json").json(); assert len(j["tools"]) == 25 and j["legacy"]["get_status"] == "inspect(what='status')"
    assert c.get("/.well-known/mcp.json").json()["tools"] == mcp_tool_names()
    assert "Tools (25," in c.get("/llms.txt").text
