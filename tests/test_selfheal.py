"""Self-healing deploys: per-release 5xx/p95 tracking, auto-rollback with recorded reason, guards (baseline, min requests,
no rollback of a rollback, one per deploy, opt-out)."""
import time, pytest
from fastapi.testclient import TestClient
from paas import api, selfheal, engine
from paas.db import q
from tests.test_parity import fake, user, wait, deploy   # noqa: F401  (fixtures)

C = {"window_s": 300, "min_requests": 20, "err_pp": 5, "err_x": 2, "p95_x": 2, "p95_ms": 500}
def S(n, e=0, p=50): return {"requests": n, "errors_5xx": e, "error_rate": e / n if n else 0, "p95_ms": p}

def test_verdict_thresholds():
    assert selfheal.verdict(S(100, 10), S(100, 1), C)              # 10% vs 1%: +9pp and 10x
    assert not selfheal.verdict(S(100, 4), S(100, 0), C)           # +4pp: under 5pp
    assert not selfheal.verdict(S(100, 12), S(100, 8), C)          # +4pp
    assert not selfheal.verdict(S(100, 30), S(100, 20), C)         # +10pp but only 1.5x
    assert selfheal.verdict(S(100, 0, 1200), S(100, 0, 200), C)    # p95 6x and +1000ms
    assert not selfheal.verdict(S(100, 0, 300), S(100, 0, 100), C) # 3x but only +200ms
    assert not selfheal.verdict(S(100, 0, 2400), S(100, 0, 2000), C)  # +400ms only 1.2x
    assert not selfheal.verdict(S(19, 19), S(100, 0), C)           # too few requests on the new release
    assert not selfheal.verdict(S(100, 50), S(5, 0), C)            # too few on the baseline

def _traffic(app, n, status=200, ms=10):
    for _ in range(n): selfheal.record(app, status, ms)
def _active(c, name): return next(r for r in c.get(f"/v1/apps/{name}/releases").json() if r["status"] == "active")

def test_auto_rollback_on_5xx_and_no_loop(fake, user):
    deploy(user, "sh-app", "good:v1"); _traffic("sh-app", 40)
    deploy(user, "sh-app", "good:v2"); assert selfheal._watch["sh-app"]["version"] == 2
    _traffic("sh-app", 30, 500); selfheal.tick(); time.sleep(0.1); wait(user, "sh-app")
    for _ in range(50):
        top = _active(user, "sh-app")
        if top["version"] == 3: break
        time.sleep(0.1)
    assert top["image"] == "good:v1" and top["reason"].startswith("auto-rollback to v1: v2 5xx rate 100.0%")
    v2 = next(r for r in user.get("/v1/apps/sh-app/releases").json() if r["version"] == 2)
    assert v2["auto_rollback"]["rolled_back_to"] == 1 and "5xx rate" in v2["auto_rollback"]["reason"] and v2["auto_rollback"]["baseline"]["requests"] == 40
    assert "sh-app" not in selfheal._watch                       # a rollback is never watched -> never auto-rolled back
    _traffic("sh-app", 50, 500); selfheal.tick(); time.sleep(0.2)
    assert _active(user, "sh-app")["version"] == 3
    ev = [e["msg"] for e in q("select msg from events where app='sh-app'")]
    assert any(m.startswith("self-heal: rolling back v2 to v1") for m in ev)

def test_one_rollback_per_deploy(fake, user, monkeypatch):
    deploy(user, "sh-one", "good:v1"); _traffic("sh-one", 40)
    deploy(user, "sh-one", "good:v2"); calls = []
    monkeypatch.setattr(engine, "rollback", lambda *a, **k: calls.append(a))
    _traffic("sh-one", 30, 500); selfheal.tick(); selfheal.tick(); selfheal.tick()
    assert len(calls) == 1

def test_latency_trigger_and_pass(fake, user, monkeypatch):
    calls = []; monkeypatch.setattr(engine, "rollback", lambda *a, **k: calls.append((a, k)))
    deploy(user, "sh-lat", "good:v1"); _traffic("sh-lat", 40, ms=100)
    deploy(user, "sh-lat", "good:v2"); _traffic("sh-lat", 30, ms=900); selfheal.tick()
    assert calls and calls[0][0] == ("sh-lat", 1) and "p95 latency 900ms vs 100ms" in calls[0][1]["reason"]
    deploy(user, "sh-ok", "good:v1"); _traffic("sh-ok", 40, ms=100)
    deploy(user, "sh-ok", "good:v2"); _traffic("sh-ok", 30, ms=120)
    monkeypatch.setenv("PAAS_SELFHEAL_WINDOW", "0"); selfheal.tick()
    assert "sh-ok" not in selfheal._watch and len(calls) == 1
    assert any("passed its" in e["msg"] for e in q("select msg from events where app='sh-ok'"))

def test_skips_without_baseline_and_when_disabled(fake, user):
    deploy(user, "sh-nb", "good:v1"); _traffic("sh-nb", 5)
    deploy(user, "sh-nb", "good:v2"); assert "sh-nb" not in selfheal._watch
    assert any("no baseline" in e["msg"] for e in q("select msg from events where app='sh-nb'"))
    deploy(user, "sh-off", "good:v1"); _traffic("sh-off", 40)
    r = user.put("/v1/apps/sh-off/self-heal", json={"enabled": False}); assert r.status_code == 200 and r.json()["enabled"] is False
    deploy(user, "sh-off", "good:v2"); assert "sh-off" not in selfheal._watch
    assert user.get("/v1/apps/sh-off/self-heal").json()["thresholds"]["err_pp"] == 5
    assert user.put("/v1/apps/sh-off/self-heal", json={"enabled": True}).json()["enabled"] is True
    other = TestClient(api.app, base_url="http://api.aloof.test")
    assert other.get("/v1/apps/sh-off/self-heal").status_code in (401, 403)

def test_edge_records_per_release(fake, user):
    deploy(user, "sh-edge", "good:v1"); px = TestClient(api.proxy)
    for _ in range(3): assert px.get("/_app/sh-edge/").status_code == 200
    s = selfheal._stats[("sh-edge", 1)]; assert s.n == 3 and s.e5 == 0 and s.p95() > 0
