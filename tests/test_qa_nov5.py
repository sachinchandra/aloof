"""QA regress Nov 5: owner-scoped build recovery (N5-2), edge Caddy upstreams A+B (N5-1), manual scale to 0 while paused honoured
on resume (N5-4), DB-backed login rate limit shared across processes (N5-3), free-tier clamp warning."""
import json, os, time, re, pytest
from paas import engine, billing, api
from paas.db import q
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

@pytest.fixture
def n5():
    yield
    q("delete from apps where name like 'n5-%'"); q("delete from releases where app like 'n5-%'"); q("delete from events where app like 'n5-%'")
def _app(name, owner, status="building"):
    now = time.time()
    q("insert into apps(name,account_id,spec,status,provider,created,updated,build_owner) values(?,?,?,?,?,?,?,?)",
      (name, 1, json.dumps({"name": name, "instances": 1}), status, "local-docker", now, now, owner))
    q("insert into releases(app,version,image,spec,status,created,owner) values(?,?,?,?,?,?,?)", (name, 1, None, "{}", "deploying", now, owner))

@pytest.mark.parametrize("me,bg", [("api8000", "1"), ("api8010", "0")])
def test_n52_recover_only_own(monkeypatch, tmp_path, n5, me, bg):
    monkeypatch.setattr(engine, "OWNER", me); monkeypatch.setenv("PAAS_BACKGROUND", bg)
    other = "api8010" if me == "api8000" else "api8000"
    _app("n5-mine", me); _app("n5-theirs", other); _app("n5-legacy", None)
    for d in (f"aloof-build-{me}-abc", f"aloof-build-{other}-def", "aloof-build-x1y2z3_q"): (tmp_path / d).mkdir()
    engine.recover_interrupted(build_tmp=str(tmp_path))
    st = lambda n: q("select status from apps where name=?", (n,), one=True)["status"]
    rs = lambda n: q("select status from releases where app=?", (n,), one=True)["status"]
    assert st("n5-mine") == "failed" and rs("n5-mine") == "failed"
    assert st("n5-theirs") == "building" and rs("n5-theirs") == "deploying"
    left = sorted(p.name for p in tmp_path.iterdir())
    assert f"aloof-build-{me}-abc" not in left and f"aloof-build-{other}-def" in left
    if bg == "1": assert st("n5-legacy") == "failed" and "aloof-build-x1y2z3_q" not in left   # unstamped -> primary only
    else: assert st("n5-legacy") == "building" and "aloof-build-x1y2z3_q" in left

def test_n52_stamps_and_startup_in_every_process():
    import inspect
    from paas import main
    from paas.drivers import local_docker
    src = inspect.getsource(main.main); assert src.index("engine.recover_interrupted()") < src.index('PAAS_BACKGROUND", "1") == "1"')
    assert 'prefix=f"aloof-build-{OWNER}-"' in inspect.getsource(local_docker)
    assert "OWNER" in inspect.getsource(engine._new_release) and "build_owner" in inspect.getsource(engine.deploy)
    assert "recover_interrupted" not in inspect.getsource(__import__("paas.controller", fromlist=["x"]))

def test_n51_caddy_edge_both_upstreams():
    for f in ("deploy/Caddyfile.prod", "deploy/Caddyfile"):
        s = open(os.path.join(ROOT, f)).read(); blk = s[s.index("https:// {"):]
        blk = blk[:blk.index("\n}\n")]
        assert "reverse_proxy 127.0.0.1:8080 127.0.0.1:8090" in blk and "lb_policy first" in blk and "lb_try_duration 10s" in blk and "health_uri /__aloof_edge_healthz" in blk
    s = open(os.path.join(ROOT, "deploy/Caddyfile.prod")).read(); h = s[s.index("http:// {"):]
    assert "reverse_proxy 127.0.0.1:8000 127.0.0.1:8010" in h and "lb_policy first" in h
    from fastapi.testclient import TestClient
    assert TestClient(api.proxy).get("/__aloof_edge_healthz").text == "ok"

def test_n54_scale_to_zero_while_paused_honoured(monkeypatch, n5):
    now = time.time(); spec = {"name": "n5-pd", "plan": "shared-1x-256mb", "instances": 0, "_image": "img", "_resume_instances": 3, "source": {"image": "img"}}
    q("insert into apps(name,account_id,spec,status,provider,created,updated) values(?,?,?,?,?,?,?)", ("n5-pd", 424242, json.dumps(spec), "past_due", "local-docker", now, now))
    with pytest.raises(engine.PastDue): engine.scale("n5-pd", 2)
    engine.scale("n5-pd", 0); sp = json.loads(q("select spec from apps where name='n5-pd'", one=True)["spec"])
    assert sp["_resume_instances"] == 0 and sp["_resume_set"]
    called = []; monkeypatch.setattr(engine, "scale", lambda n, k, reason="": called.append(k))
    assert billing.resume_past_due(424242, "test") == ["n5-pd"] and called == []   # stays at 0
    sp = json.loads(q("select spec from apps where name='n5-pd'", one=True)["spec"]); assert "_resume_set" not in sp and sp["instances"] == 0

def test_n53_login_limit_in_db_shared():
    api._login_hits.clear()
    for _ in range(api.LOGIN_LIMIT): api._rate_check(("ip:n5",), now=5000.0)
    dict.clear(api._login_hits)   # a different process / after a roll: no memory, same DB
    with pytest.raises(Exception) as e: api._rate_check(("ip:n5",), now=5010.0)
    assert getattr(e.value, "status_code", None) == 429
    assert not q("select 1 from login_hits where k like '%n5%'", one=True)   # keys hashed
    api._login_hits.clear(); api._rate_check(("ip:n5",), now=5011.0)

def test_free_clamp_warns(n5):
    spec = {"name": "n5-free", "plan": "shared-1x-256mb", "instances": 3}
    t = engine.TIERS["free"] if hasattr(engine, "TIERS") else None
    from paas import plans
    import unittest.mock as m
    with m.patch.object(engine, "account_tier", return_value=("free", plans.TIERS["free"])):
        out = engine.apply_tier_limits(1, dict(spec), redeploy=True)
    assert out["instances"] == plans.TIERS["free"]["max_instances"]
    assert any("WARNING" in e["msg"] and "free tier" in e["msg"] for e in q("select msg from events where app='n5-free'"))
