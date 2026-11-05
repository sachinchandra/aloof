"""Admin 'Website traffic' section: real-data mapping, ranges, loading/empty/error/unavailable states, a11y."""
import json, os, re, shutil, subprocess, pytest
ROOT = os.path.join(os.path.dirname(__file__), "..")
HTML = open(os.path.join(ROOT, "dashboard/admin.html")).read()
JS = HTML.split("/* traffic:start */")[1].split("/* traffic:end */")[0]

def run(expr):
    if not shutil.which("node"): pytest.skip("node not installed")
    out = subprocess.run(["node", "-e", JS + f"\nconsole.log(JSON.stringify({expr}))"], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)

def test_markup_ranges_and_definitions():
    sec = HTML.split("<section class=trf")[1].split("</section>")[0]
    assert 'aria-labelledby=trfh' in HTML and "Website traffic" in sec
    assert re.findall(r"data-r=(\w+)", sec) == ["1d", "7d", "30d", "90d"] and sec.count("type=button") == 4
    assert 'role=group aria-label="Date range"' in sec and sec.count("aria-pressed=true") == 1
    assert "UTC" in sec and "visitor-days" in sec and "API calls" in sec and "test or admin accounts" in sec
    assert "aria-live=polite" in sec and "violet" not in HTML.lower()

OK = {"status":"ok","range_key":"7d","range":{"from":"2026-10-30","to":"2026-11-05","days":7,"timezone":"UTC"},"collecting_since":"2026-11-04",
      "visitors":"12","clicks":3.4,"series":[{"day":"2026-11-03","visitors":None,"clicks":None},{"day":"2026-11-04","visitors":0,"clicks":0},{"day":"2026-11-05","visitors":12,"clicks":3},{"bad":1}],
      "top_clicks":[{"label":"github.com","kind":"outbound","clicks":1},{"label":"deploy_app","kind":"cta","clicks":2}],"generated":1793000000}
def J(o): return json.dumps(o)

def test_live_contract_mapping_keeps_nulls():
    m = run(f"trfMap({J(OK)},'30d')")
    assert m["range"] == "7d" and m["visitors"] == 12 and m["clicks"] == 3 and not m["noData"] and not m["empty"]
    assert m["series"][0] == {"day": "2026-11-03", "visitors": None, "clicks": None}     # unmeasured stays null
    assert m["series"][1] == {"day": "2026-11-04", "visitors": 0, "clicks": 0} and len(m["series"]) == 3
    assert [(t["label"], t["kind"]) for t in m["top"]] == [("deploy_app", "cta"), ("github.com", "outbound")]

def test_no_data_vs_zero_vs_bad_shape():
    nd = run("trfMap({status:'no_data',range_key:'90d',visitors:null,clicks:null,series:[{day:'d',visitors:null,clicks:null}],collecting_since:null},'7d')")
    assert nd["noData"] is True and nd["range"] == "90d" and "visitors" not in nd
    z = run("trfMap({status:'ok',range_key:'1d',visitors:0,clicks:0,series:[]},'1d')")
    assert z["empty"] is True and z["visitors"] == 0
    assert run("trfMap({status:'ok',visitors:null,clicks:1},'7d')") is None       # null total on ok -> error, never 0
    assert run("trfMap({visitors:3,clicks:1},'7d')") is None and run("trfMap(null,'7d')") is None

def test_states_render():
    assert "Loading website traffic" in run("trfView(null,'loading')")
    nd = run("trfView(trfMap({status:'no_data',range_key:'7d',collecting_since:'2026-11-05'}),'ok')")
    assert "No traffic data" in nd and "2026-11-05" in nd and "role=status" in nd and ">0<" not in nd
    z = run("trfView(trfMap({status:'ok',range_key:'1d',visitors:0,clicks:0}),'ok')")
    assert "Measured, and nobody visited" in z and "Unique visitors" in z
    ok = run(f"trfView(trfMap({J(OK)}),'ok')")
    assert "Visitor-days" in ok and "not measured" in ok and "left blank, not counted as zero" in ok and "Link out" in ok and "scope=col" in ok
    assert 'role=img aria-label="Visitors per day, last 7 days"' in ok
    e = run("trfView(null,'error',trfErr(503,{detail:'site analytics unavailable'}))")
    assert "role=alert" in e and "Try again" in e and "not shown as zero" in e
    assert "sign in again" in run("trfErr(403,null)") and "range" in run("trfErr(400,{detail:'range must be'})")
    assert "&lt;x&gt;" in run("trfView(trfMap({status:'ok',visitors:1,clicks:1,top_clicks:[{label:'<x>',clicks:1}]},'7d'),'ok')")

def test_real_endpoint_output_maps(monkeypatch):
    from fastapi.testclient import TestClient
    from paas import api, site_stats
    api.app.dependency_overrides[site_stats._admin] = lambda: None
    try:
        r = TestClient(api.app).get("/v1/admin/site-analytics?range=7d")
        assert r.status_code == 200, r.text
        m = run(f"trfMap({r.text},'7d')")
        assert m is not None and m["range"] == "7d" and (m["noData"] or isinstance(m["visitors"], int))
        assert TestClient(api.app).get("/v1/admin/site-analytics?range=2d").status_code == 400
    finally: api.app.dependency_overrides.pop(site_stats._admin, None)

def test_independent_lazy_load():
    assert "const TRF_API='/v1/admin/site-analytics';" in JS and "requestIdleCallback" in JS and "seq!=TRF_SEQ" in JS
    assert "setInterval" not in JS and "'trfb'" not in HTML.split("const PANELS=")[1].split(";")[0]
