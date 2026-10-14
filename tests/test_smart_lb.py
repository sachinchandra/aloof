"""Smart LB: least-connections, passive ejection with backoff, never the last instance, health-check ejection."""
import time, threading, http.server, pytest
from fastapi.testclient import TestClient
from paas import lb, api, controller
from paas.db import q
from tests.test_parity import fake, user, wait, deploy   # noqa: F401

@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for d in (lb.inflight, lb._fails, lb._ejected, lb._strikes): d.clear()
    monkeypatch.setenv("PAAS_LB_FAILS", "3"); monkeypatch.setenv("PAAS_LB_COOLDOWN", "10")
I = [{"id": "a"}, {"id": "b"}, {"id": "c"}]

def test_least_connections_and_ties():
    lb.begin("a"); lb.begin("a"); lb.begin("b")
    assert lb.pick("x", I)["id"] == "c"
    lb.begin("c"); lb.begin("c")
    assert lb.pick("x", I)["id"] == "b"
    lb.end("a"); lb.end("a"); lb.end("b"); lb.end("c"); lb.end("c")
    assert {lb.pick("x", I)["id"] for _ in range(6)} == {"a", "b", "c"}   # round-robin on ties

def test_passive_ejection_backoff_and_last_instance():
    assert not lb.failure("x", "a", I, "HTTP 500") and not lb.failure("x", "a", I, "HTTP 500")
    assert lb.failure("x", "a", I, "HTTP 500") == 10
    assert all(lb.pick("x", I)["id"] != "a" for _ in range(10))
    lb._ejected["a"] = (time.time() - 1, "x")                       # cooldown over
    for _ in range(3): cd = lb.failure("x", "a", I, "HTTP 500")
    assert cd == 20                                                 # doubled
    lb.success("a"); lb._ejected.clear()
    for _ in range(3): cd = lb.failure("x", "a", I, "HTTP 500")
    assert cd == 10                                                 # success resets backoff
    two = I[:2]; lb._ejected.clear(); lb._ejected["a"] = (time.time() + 60, "x")
    for _ in range(5): assert not lb.failure("x", "b", two, "HTTP 500")   # b is the last routable one
    assert "b" not in lb._ejected and lb.pick("x", two)["id"] == "b"
    lb._ejected["b"] = (time.time() + 60, "x")
    assert lb.pick("x", two)["id"] in ("a", "b")                    # all ejected -> still route

def test_health_failure_ejects_immediately_and_recovers():
    assert lb.health_failed("x", "a", I, "HTTP 503") == 10 and lb._ejected["a"][1].startswith("health check")
    lb.health_ok("a"); assert "a" not in lb._ejected
    assert not lb.health_failed("x", "a", [{"id": "a"}], "HTTP 503")   # single instance: never ejected

class _Bad(http.server.BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(500); self.end_headers(); self.wfile.write(b"no")
    def log_message(self, *a): pass

def test_edge_ejects_failing_instance(fake, user, monkeypatch):
    deploy(user, "lb-app", "good:v1", instances=2)
    insts = q("select * from instances where app='lb-app' and status='running' order by started")
    assert len(insts) == 2
    s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Bad); threading.Thread(target=s.serve_forever, daemon=True).start()
    q("update instances set port=? where id=?", (s.server_address[1], insts[0]["id"]))
    px = TestClient(api.proxy); codes = [px.get("/_app/lb-app/").status_code for _ in range(12)]
    assert codes.count(500) == 3 and codes[-6:] == [200] * 6
    assert insts[0]["id"] in lb._ejected
    ev = [e["msg"] for e in q("select msg from events where app='lb-app'")]; assert any("lb: ejected" in m and "3 consecutive failures" in m for m in ev)

def test_health_pass_ejects(fake, user, monkeypatch):
    deploy(user, "lb-hc", "good:v1", instances=2)
    insts = q("select * from instances where app='lb-hc' and status='running' order by started")
    monkeypatch.setattr(fake, "inspect", lambda iid: "exited" if iid == insts[0]["id"] else "running")
    monkeypatch.setattr(controller.engine, "replace_instance", lambda *a: None)
    controller._health_pass()
    assert insts[0]["id"] in lb._ejected and insts[1]["id"] not in lb._ejected
