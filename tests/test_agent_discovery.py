"""llms.txt / llms-full.txt / /compare pages: served, and their Aloof numbers still match plans.py and the MCP server."""
import re
from fastapi.testclient import TestClient
from paas import api, site
from paas.plans import PLANS, PG_PLANS, RATES, TIERS
c = TestClient(api.app)
def test_llms_full_has_summary_and_docs():
    t = c.get("/llms-full.txt").text; assert t.startswith("# Aloof") and "### Tools (25)" in t and "## Regions" in t
def test_tool_count_and_names_match_server():
    names = site.mcp_tool_names(); full = c.get("/llms-full.txt").text
    assert len(names) == 25
    listed = re.search(r"### Tools \(25\)\n(.+)\n", full)[1].rstrip(".").split(", "); assert sorted(listed) == sorted(names)
def test_prices_match_plans():
    full = c.get("/llms-full.txt").text
    for k, p in PLANS.items():
        if p["kind"] == "cpu" and not p["legacy"]: assert f"{k} ${p['price_usd_month']:g}" in full
    for k, p in PG_PLANS.items(): assert f"{k} ${p['price_usd_month']:g}" in full
    assert RATES["egress_usd_gb"] == 0.06 and RATES["volume_usd_gb_month"] == 0.22 and [TIERS[t]["fee_usd"] for t in ("free", "hobby", "pro")] == [0, 5, 20]
def test_compare_pages():
    for k in ("fly", "render"):
        r = c.get(f"/compare/{k}"); assert r.status_code == 200 and 'class="tbl cmp"' in r.text and "$1.99/mo" in r.text and "FIRST1" in r.text
        md = c.get(f"/compare/{k}.md").text; assert md.startswith("# Aloof vs")
        import re as _re
        from paas.mcp_tools import mcp_tool_names
        assert f"{len(mcp_tool_names())} tools" in md and "__MCP" not in md
        # no competitor pricing: the only dollar figures are Aloof's own prices
        assert set(_re.findall(r"\$[0-9.]+", md)) <= {"$0", "$1", "$1.99", "$2.99", "$5", "$20"}, _re.findall(r"\$[0-9.]+", md)
        assert not any(x in md for x in ("✅", "❌", "TL;DR", "elf-driving", "fetched", "docs warn"))
        assert 'href="/login#register"' in r.text and 'data-label=' in r.text
    from paas.www_compare import _inline
    assert _inline("see https://x.io/a.") == 'see <a href="https://x.io/a">https://x.io/a</a>.'
    for row in c.get("/v1/plans").json()["plans"]: assert not ({"compare_usd_month", "compare_usd_hr", "note"} & set(row))
    assert "Fly.io</th>" not in c.get("/pricing").text and "$2.19" not in c.get("/blog/aloof-vs-fly-render-railway").text
    assert c.get("/compare/railway").status_code == 404 and c.get("/compare/x").status_code == 404
