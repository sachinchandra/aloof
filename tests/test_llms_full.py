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
