"""QA pre-launch Nov 3: P1 restart-orphaned builds, P2 destroy removes release images, P3 checkout amounts, P4 email caps, P5 unknown provider 404."""
import os, time, tempfile, pytest
from fastapi.testclient import TestClient
from paas.api import app
from paas import engine, stripe_billing, auth
from paas.crypto import dump_spec
from paas.db import q

def _app(name, acct_id, status="building", prev=False, alive=False):
    now = time.time(); sp = dump_spec({"name": name, "image": "nginx", "plan": "shared-1x-256mb", "port": 80, "instances": 1})
    q("insert into apps(name,account_id,status,spec,created,updated,provider) values(?,?,?,?,?,?,?)", (name, acct_id, status, sp, now, now, "local-docker"))
    if prev: q("insert into releases(app,version,spec,status,reason,created) values(?,?,?,?,?,?)", (name, 1, sp, "active", "deploy", now))
    q("insert into releases(app,version,spec,status,reason,created) values(?,?,?,?,?,?)", (name, 2 if prev else 1, sp, "deploying", "deploy", now))
    if alive: q("insert into instances(id,app,provider,status,started) values(?,?,?,?,?)", (f"i{time.time_ns()}", name, "local-docker", "running", now))

def test_recover_interrupted(tmp_path):
    a, b = f"rb{time.time_ns()}", f"rc{time.time_ns()}"
    _app(a, None); _app(b, None, prev=True, alive=True)
    d = tmp_path / "aloof-build-xyz"; d.mkdir(); (d / "f").write_text("x")
    engine.recover_interrupted(str(tmp_path))
    ra = q("select status, error from apps where name=?", (a,), one=True)
    assert ra["status"] == "failed" and "interrupted by platform restart" in ra["error"]
    assert q("select status, error from releases where app=? and version=1", (a,), one=True)["status"] == "failed"
    rb = q("select status, error from apps where name=?", (b,), one=True)
    assert rb["status"] == "running" and "still serving v1" in rb["error"]
    assert q("select status from releases where app=? and version=1", (b,), one=True)["status"] == "active"
    assert not d.exists()

def test_recover_skips_live_deploy(tmp_path):
    a = f"rl{time.time_ns()}"; _app(a, None)
    with engine._lock(a): engine.recover_interrupted(str(tmp_path))
    assert q("select status from apps where name=?", (a,), one=True)["status"] == "building"

def test_retry_takes_over_stale_lock(monkeypatch):
    from test_impersonation_secrets import _imp
    c, ac = _imp(); q("update sessions set impersonated_by=null where account_id=?", (ac["id"],))
    n = f"rt{time.time_ns()}"; _app(n, ac["id"])
    seen = {}; monkeypatch.setattr(engine, "deploy", lambda aid, spec: seen.setdefault("spec", spec) and {"name": n, "status": "queued"})
    q("update apps set spec=? where name=?", (dump_spec({"name": n, "source": {"image": "nginx"}, "plan": "shared-1x-256mb"}), n))
    r = c.post(f"/v1/apps/{n}/retry", headers={"X-Aloof-CSRF": "1"}); assert r.status_code == 200, r.text
    with engine._lock(n):
        assert c.post(f"/v1/apps/{n}/retry", headers={"X-Aloof-CSRF": "1"}).status_code == 409

def test_build_timeout_configured():
    from paas import buildcfg, launch
    import paas.drivers.local_docker as ld
    assert buildcfg.BUILD_TIMEOUT_S > 0 and ld.BUILD_TIMEOUT_S == launch.BUILD_TIMEOUT_S == buildcfg.BUILD_TIMEOUT_S

def test_destroy_with_volumes_removes_images(monkeypatch):
    n = f"di{time.time_ns()}"; _app(n, None, status="running")
    class D:
        removed = []
        def remove_app_images(self, app): D.removed.append(app); return [f"paas/{app}:r1", f"paas/{app}:r2"]
    monkeypatch.setattr(engine, "get_drivers", lambda: {"local-docker": D()})
    monkeypatch.setattr(engine, "destroy", lambda name: None)
    out = engine.destroy_app(n, with_volumes=True)
    assert out["deleted"]["images"] == [f"paas/{n}:r1", f"paas/{n}:r2"]
    D.removed.clear(); out = engine.destroy_app(n, with_volumes=False)
    assert D.removed == [] and "images" not in out["deleted"]

@pytest.mark.parametrize("v", ["NaN", "nan", "inf", "-Infinity", "1e400", "10.005", "abc", "12.345"])
def test_topup_amount_rejected(v):
    with pytest.raises(ValueError): stripe_billing.topup_cents(v)

def test_topup_amount_ok():
    assert stripe_billing.topup_cents("10") == 1000 and stripe_billing.topup_cents(25.5) == 2550 and stripe_billing.topup_cents("10.01") == 1001

def test_checkout_nan_clean_400():
    from test_impersonation_secrets import _imp
    c, ac = _imp(); q("update sessions set impersonated_by=null where account_id=?", (ac["id"],))
    for v in ("NaN", "10.005", "Infinity"):
        r = c.post("/v1/billing/checkout", json={"credit_usd": v}, headers={"X-Aloof-CSRF": "1"})
        assert r.status_code == 400 and "convert" not in r.text and "credit_usd" in r.text, (v, r.text)

def test_email_caps():
    assert auth._check_email("a" * 64 + "@x.com")
    for e in ("a" * 65 + "@x.com", "a@" + ("b" * 60 + ".") * 5 + "com"):
        with pytest.raises(ValueError): auth._check_email(e)

def test_unknown_provider_404():
    c = TestClient(app)
    assert c.get("/auth/nosuch/start", follow_redirects=False).status_code == 404
    assert c.get("/auth/google/start", follow_redirects=False).status_code != 404
