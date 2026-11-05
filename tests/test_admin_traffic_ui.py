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
    assert "Unique visitors" in sec and "API calls" in sec and "test accounts are not counted" in sec
    assert "aria-live=polite" in sec and "violet" not in HTML.lower()

def test_mapping_uses_only_api_numbers():
    m = run("trfMap({range:'30d',visitors:'1203',clicks:87.4,series:[{day:'2026-11-01',visitors:5,clicks:-2},{bad:1}],top_clicks:[{label:'a',clicks:1},{label:'b',clicks:9}]},'7d')")
    assert m["range"] == "30d" and m["visitors"] == 1203 and m["clicks"] == 87 and not m["empty"]
    assert m["series"] == [{"day": "2026-11-01", "visitors": 5, "clicks": 0}] and [t["label"] for t in m["top"]] == ["b", "a"]
    assert run("trfMap({visitors:3},'7d')") is None and run("trfMap(null,'7d')") is None   # missing clicks -> error, never a made-up 0
    assert run("trfMap({range:'bogus',visitors:0,clicks:0},'90d')")["range"] == "90d"
    assert run("trfMap({visitors:0,clicks:0},'7d')")["empty"] is True

def test_states():
    assert "Loading website traffic" in run("trfView(null,'loading')")
    e = run("trfView(null,'error','The server answered 500')")
    assert "role=alert" in e and "Try again" in e and "trfLoad()" in e and "500" in e
    assert "aren't connected yet" in run("trfView(null,'unavailable')")
    assert "No visits or clicks" in run("trfView(trfMap({range:'1d',visitors:0,clicks:0}),'ok')")
    ok = run("trfView(trfMap({range:'7d',visitors:1500,clicks:12,series:[{day:'a',visitors:1,clicks:0},{day:'b',visitors:2,clicks:1}],top_clicks:[{label:'<x>',clicks:3}]}),'ok')")
    assert "1,500" in ok and 'role=img aria-label="Visitors per day, last 7 days"' in ok and "&lt;x&gt;" in ok and "<x>" not in ok and "scope=col" in ok

def test_independent_lazy_load():
    assert "const TRF_API=null;" in JS and "if(!TRF_API)" in JS   # pending Backend contract: no request, no guessed endpoint
    assert "/v1/admin/traffic" not in HTML and "requestIdleCallback" in JS and "seq!=TRF_SEQ" in JS
    assert "'trfb'" not in HTML.split("const PANELS=")[1].split(";")[0]   # main dashboard failure doesn't wipe it
