"""Self-healing deploys: thresholds, guards, auto-rollback with recorded reason; includes QA's Oct 14 repros (H1, H2, M1, M2, L2)."""
import time, itertools, pytest
from fastapi.testclient import TestClient
from paas import api, selfheal, engine
from paas.db import q
from tests.test_parity import fake, user, wait, deploy   # noqa: F401

C = {"window_s": 300, "min_requests": 100, "min_bad": 10, "min_ips": 3, "err_pp": 5, "err_x": 2, "p95_x": 2, "p95_ms": 500}
def S(n, e=0, p=50, ips=5): return {"requests": n, "errors_5xx": e, "counted_5xx": e, "error_rate": e / n if n else 0, "error_ips": ips, "p95_ms": p}
_ip = itertools.cycle([f"198.51.100.{k}" for k in range(1, 8)])
def _t(app, n, status=200, ms=10, path="/", ip=None):
    for _ in range(n): selfheal.record(app, status, ms, ip or next(_ip), path)
def _act(name): return q("select version,image,reason from releases where app=? and status='active' order by version desc", (name,), one=True)

def test_verdict_thresholds():
    assert selfheal.verdict(S(200, 20), S(200, 1), C)
    assert not selfheal.verdict(S(200, 10), S(200, 0), C)          # exactly +5pp: strict >
    assert not selfheal.verdict(S(100, 30), S(100, 20), C)         # 1.5x only
    assert not selfheal.verdict(S(100, 9), S(100, 0), C)           # 9% but under 10 errors
    assert not selfheal.verdict(S(200, 20, ips=2), S(200, 0), C)   # errors from only 2 client IPs
    assert not selfheal.verdict(S(99, 99), S(200, 0), C) and not selfheal.verdict(S(200, 99), S(50, 0), C)
    assert selfheal.verdict(S(200, 0, 1200), S(200, 0, 200), C, slow=lambda t: (20, 5))
    assert not selfheal.verdict(S(200, 0, 1200), S(200, 0, 200), C, slow=lambda t: (9, 5))   # too few slow requests
    assert not selfheal.verdict(S(200, 0, 300), S(200, 0, 100), C)

def test_auto_rollback_reason_audit_and_no_loop(fake, user):
    deploy(user, "sh-app", "good:v1"); _t("sh-app", 150)
    deploy(user, "sh-app", "good:v2"); assert selfheal._watch["sh-app"]["version"] == 2
    _t("sh-app", 100); _t("sh-app", 50, 500, path="/api/x"); selfheal.tick()
    for _ in range(60):
        top = _act("sh-app")
        if top["version"] == 3: break
        time.sleep(0.1)
    assert top["image"] == "good:v1" and top["reason"].startswith("auto-rollback to v1: v2 5xx rate")
    v2 = next(r for r in user.get("/v1/apps/sh-app/releases").json() if r["version"] == 2)
    assert v2["auto_rollback"]["rolled_back_to"] == 1 and v2["auto_rollback"]["baseline"]["requests"] == 150
    assert "sh-app" not in selfheal._watch
    _t("sh-app", 200, 500); selfheal.tick(); time.sleep(0.2); assert _act("sh-app")["version"] == 3
    assert [a for a in q("select * from audit where app='sh-app' and action='auto-rollback'")]   # L2

def test_m1_single_error_or_slow_request_does_not_trigger(fake, user, monkeypatch):
    calls = []; monkeypatch.setattr(engine, "rollback", lambda *a, **k: calls.append(a))
    deploy(user, "sh-one", "good:v1"); _t("sh-one", 120, ms=20)
    deploy(user, "sh-one", "good:v2"); _t("sh-one", 119, ms=20); _t("sh-one", 1, 500); _t("sh-one", 3, ms=30000); selfheal.tick()
    assert not calls

def test_m2_single_ip_and_baseline_route_errors_ignored(fake, user, monkeypatch):
    calls = []; monkeypatch.setattr(engine, "rollback", lambda *a, **k: calls.append(a))
    deploy(user, "sh-atk", "good:v1"); _t("sh-atk", 140); _t("sh-atk", 10, 500, path="/boom")
    deploy(user, "sh-atk", "good:v2"); _t("sh-atk", 100)
    _t("sh-atk", 60, 500, path="/boom/x")                    # route already failing under the baseline
    _t("sh-atk", 60, 500, path="/api", ip="203.0.113.9")     # one attacker IP
    selfheal.tick(); assert not calls and "sh-atk" in selfheal._watch

def test_h1_destroy_during_window_never_resurrects(fake, user):
    deploy(user, "sh-dead", "good:v1"); _t("sh-dead", 150)
    deploy(user, "sh-dead", "good:v2"); assert selfheal._watch.get("sh-dead")
    assert user.delete("/v1/apps/sh-dead").status_code in (200, 204); time.sleep(0.3)
    assert "sh-dead" not in selfheal._watch
    selfheal._watch["sh-dead"] = {"version": 2, "prev": 1, "start": time.time(), "baseline": S(150), "baseline_err_routes": set()}  # stale watch
    _t("sh-dead", 200, 500); selfheal.tick(); time.sleep(1)
    assert q("select status from apps where name='sh-dead'", one=True)["status"] == "destroyed"
    assert q("select count(*) n from instances where app='sh-dead' and status='running'", one=True)["n"] == 0
    with pytest.raises(ValueError): engine.rollback("sh-dead", 1)

def test_h2_user_deploy_during_window_wins(fake, user):
    deploy(user, "sh-race", "good:v1"); _t("sh-race", 150)
    deploy(user, "sh-race", "good:v2"); _t("sh-race", 100, 500)
    user.post("/v1/apps", json={"name": "sh-race", "source": {"image": "good:v3"}, "plan": "shared-1x-256mb", "provider": "local-docker"})
    selfheal.tick(); time.sleep(1.5); wait(user, "sh-race"); time.sleep(0.5)
    a = _act("sh-race"); assert a["image"] == "good:v3" and not (a["reason"] or "").startswith("auto-rollback")
    assert not q("select 1 from releases where app='sh-race' and reason like 'auto-rollback%'")

def test_pass_window_and_opt_out_and_no_baseline(fake, user, monkeypatch):
    deploy(user, "sh-ok", "good:v1"); _t("sh-ok", 150, ms=100)
    deploy(user, "sh-ok", "good:v2"); _t("sh-ok", 120, ms=120)
    monkeypatch.setenv("PAAS_SELFHEAL_WINDOW", "0"); selfheal.tick(); monkeypatch.delenv("PAAS_SELFHEAL_WINDOW")
    assert "sh-ok" not in selfheal._watch and any("passed its" in e["msg"] for e in q("select msg from events where app='sh-ok'"))
    deploy(user, "sh-nb", "good:v1"); _t("sh-nb", 30); deploy(user, "sh-nb", "good:v2"); assert "sh-nb" not in selfheal._watch
    deploy(user, "sh-off", "good:v1"); _t("sh-off", 150)
    assert user.put("/v1/apps/sh-off/self-heal", json={"enabled": False}).json()["enabled"] is False
    deploy(user, "sh-off", "good:v2"); assert "sh-off" not in selfheal._watch
    assert user.get("/v1/apps/sh-off/self-heal").json()["thresholds"]["min_requests"] == 100
    assert TestClient(api.app, base_url="http://api.aloof.test").get("/v1/apps/sh-off/self-heal").status_code == 401

def test_edge_records_per_release(fake, user):
    deploy(user, "sh-edge", "good:v1"); px = TestClient(api.proxy)
    for _ in range(3): assert px.get("/_app/sh-edge/").status_code == 200
    s = selfheal._stats[("sh-edge", 1)]; assert s.n == 3 and s.e5 == 0 and s.p95() > 0
