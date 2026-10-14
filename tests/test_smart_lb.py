"""Smart LB: least-connections, passive ejection with backoff, never the last instance, health-check ejection."""
import time, threading, http.server, pytest
from fastapi.testclient import TestClient
from paas import lb, api, controller
from paas.db import q
from tests.test_parity import fake, user, wait, deploy   # noqa: F401

@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for d in (lb.inflight, lb._fails, lb._ejected, lb._strikes, lb._hfails): d.clear()
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
    assert not lb.failure("x", "a", I, "HTTP 503") and not lb.failure("x", "a", I, "HTTP 503")
    assert lb.failure("x", "a", I, "HTTP 503") == 10
    assert all(lb.pick("x", I)["id"] != "a" for _ in range(10))
    lb._ejected["a"] = (time.time() - 1, "x")                       # cooldown over
    for _ in range(3): cd = lb.failure("x", "a", I, "HTTP 503")
    assert cd == 20                                                 # doubled
    lb.success("a"); lb._ejected.clear()
    for _ in range(3): cd = lb.failure("x", "a", I, "HTTP 503")
    assert cd == 10                                                 # success resets backoff
    two = I[:2]; lb._ejected.clear(); lb._ejected["a"] = (time.time() + 60, "x")
    for _ in range(5): assert not lb.failure("x", "b", two, "HTTP 503")   # b is the last routable one
    assert "b" not in lb._ejected and lb.pick("x", two)["id"] == "b"
    lb._ejected["b"] = (time.time() + 60, "x")
    assert lb.pick("x", two)["id"] in ("a", "b")                    # all ejected -> still route

def test_health_failure_needs_two_and_recovers():
    assert not lb.health_failed("x", "a", I, "HTTP 503")
    assert lb.health_failed("x", "a", I, "HTTP 503") == 10 and lb._ejected["a"][1].startswith("health check")
    lb.health_ok("a"); assert "a" not in lb._ejected
    for _ in range(3): assert not lb.health_failed("x", "a", [{"id": "a"}], "HTTP 503")   # single instance: never ejected

class _Bad(http.server.BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(503); self.end_headers(); self.wfile.write(b"no")
    def log_message(self, *a): pass

def test_edge_ejects_failing_instance(fake, user, monkeypatch):
    deploy(user, "lb-app", "good:v1", instances=2)
    insts = q("select * from instances where app='lb-app' and status='running' order by started")
    assert len(insts) == 2
    s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Bad); threading.Thread(target=s.serve_forever, daemon=True).start()
    q("update instances set port=? where id=?", (s.server_address[1], insts[0]["id"]))
    px = TestClient(api.proxy); codes = [px.get("/_app/lb-app/").status_code for _ in range(12)]
    assert codes.count(503) == 3 and codes[-6:] == [200] * 6
    assert insts[0]["id"] in lb._ejected
    ev = [e["msg"] for e in q("select msg from events where app='lb-app'")]; assert any("lb: ejected" in m and "3 consecutive failures" in m for m in ev)

def test_health_pass_ejects(fake, user, monkeypatch):
    deploy(user, "lb-hc", "good:v1", instances=2)
    insts = q("select * from instances where app='lb-hc' and status='running' order by started")
    monkeypatch.setattr(fake, "inspect", lambda iid: "exited" if iid == insts[0]["id"] else "running")
    monkeypatch.setattr(controller.engine, "replace_instance", lambda *a: None)
    controller._health_pass(); assert insts[0]["id"] not in lb._ejected   # one failure: not yet
    controller._health_pass()
    assert insts[0]["id"] in lb._ejected and insts[1]["id"] not in lb._ejected

def test_m4_plain_500_never_ejects_and_half_cap(fake, user):
    deploy(user, "lb-500", "good:v1", instances=3)
    insts = q("select * from instances where app='lb-500' and status='running'")
    class B(http.server.BaseHTTPRequestHandler):
        def do_GET(self): self.send_response(500); self.end_headers(); self.wfile.write(b"boom")
        def log_message(self, *a): pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), B); threading.Thread(target=srv.serve_forever, daemon=True).start()
    q("update instances set port=? where app='lb-500'", (srv.server_address[1],))
    px = TestClient(api.proxy)
    for _ in range(30): assert px.get("/_app/lb-500/boom").status_code == 500
    assert not lb._ejected
    three = [{"id": x} for x in "pqr"]
    for x in "pqr":
        for _ in range(10): lb.failure("x", x, three, "HTTP 503")
    assert len([k for k in lb._ejected if k in "pqr"]) == 1          # at most half of 3 instances out

def test_m5_no_inflight_leak_on_cancel(fake, user, monkeypatch):
    import asyncio
    from starlette.requests import Request
    deploy(user, "lb-cx", "good:v1")
    scope = {"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": [(b"host", b"lb-cx.aloof.test")], "client": ("1.2.3.4", 1), "scheme": "http"}
    async def rcv(): return {"type": "http.request", "body": b"", "more_body": False}
    async def boom(*a, **k): raise asyncio.CancelledError()
    monkeypatch.setattr(api.client, "send", boom)
    m0 = controller.M.inflight.get("lb-cx", 0)
    with pytest.raises(BaseException): asyncio.run(api.edge("", Request(scope, rcv)))
    assert sum(lb.inflight.values()) == 0 and controller.M.inflight.get("lb-cx", 0) == m0
    class R:
        status_code = 200; headers = {}
        async def aiter_raw(self):
            yield b"x"
        async def aclose(self): pass
    async def ok(*a, **k): return R()
    monkeypatch.setattr(api.client, "send", ok)
    resp = asyncio.run(api.edge("", Request(scope, rcv)))
    assert sum(lb.inflight.values()) == 1
    asyncio.run(resp.background()); assert sum(lb.inflight.values()) == 0     # response background releases it
    resp2 = asyncio.run(api.edge("", Request(scope, rcv))); del resp2
    import gc; gc.collect(); assert sum(lb.inflight.values()) == 0             # abandoned body released on GC
    assert controller.M.inflight.get("lb-cx", 0) == m0
