"""Support bugs 61/62/65 (Nov 4): past-due pause has its own status + message, sticks through month reset / plan changes /
upgrades, and resume actively restarts apps at their old size (workers/cron included). Test fixtures only."""
import json, time, uuid, pytest
from fastapi.testclient import TestClient
from paas import api, billing, engine, stripe_billing as S, promo_policy as P
from paas.db import q
from test_unit import _acct_client, _stripe_ev
w = TestClient(api.app)

@pytest.fixture
def fake_engine(monkeypatch):
    def start(name, drv, image, spec):
        q("insert into instances(id,app,provider,host,port,status,started) values(?,?,?,?,?,?,?)", ("i-" + uuid.uuid4().hex[:8], name, "local-docker", "x", 1, "running", time.time()))
    monkeypatch.setattr(engine, "_start", start)
    monkeypatch.setattr(engine, "_stop", lambda o: q("update instances set status='stopped' where id=?", (o["id"],)))
    monkeypatch.setattr(engine, "get_drivers", lambda: {"local-docker": object()})
    monkeypatch.setenv("PAAS_PAST_DUE_ACTION", "limit")
    yield
    for t in ("instances", "apps"): q(f"delete from {t} where " + ("app" if t == "instances" else "name") + " like 'pd6%'")

def _setup(tag):
    c = _acct_client(f"{tag}@example.com"); aid = c.get("/v1/auth/me").json()["id"]
    q("update accounts set stripe_customer=?, tier='hobby', stripe_subscription=? where id=?", ("cus_" + tag, "sub_" + tag, aid))
    now = time.time()
    apps = {f"{tag}-web": ({"autoscale": {"min": 0, "max": 3}}, 1, "idle"), f"{tag}-worker": ({}, 2, "running"), f"{tag}-auto": ({"autoscale": {"min": 1, "max": 4}}, 2, "running")}
    for n, (extra, k, st) in apps.items():
        sp = {"name": n, "plan": "shared-1x-256mb", "instances": k, "_image": "img", "source": {"image": "img"}} | extra
        q("insert into apps(name,account_id,spec,status,provider,created,updated) values(?,?,?,?,?,?,?)", (n, aid, json.dumps(sp), st, "local-docker", now, now))
        for i in range(k if st == "running" else 0):
            q("insert into instances(id,app,provider,host,port,status,started) values(?,?,?,?,?,?,?)", (f"{n}-{i}", n, "local-docker", "x", 1, "running", now))
    return c, aid
st = lambda n: q("select status from apps where name=?", (n,), one=True)["status"]
running = lambda n: len(q("select id from instances where app=? and status='running'", (n,)))
def _post(evid, typ, o): raw, h = _stripe_ev(evid, typ, o); return w.post("/v1/billing/stripe/webhook", content=raw, headers=h).json()

def test_past_due_pause_message_sticks_and_resumes(fake_engine):
    c, aid = _setup("pd61")
    assert _post("evt_pd61a", "invoice.payment_failed", {"id": "in_pd61", "customer": "cus_pd61", "subscription": "sub_pd61", "attempt_count": 1, "metadata": {}})["past_due"] == "limit"
    for n in ("pd61-web", "pd61-worker", "pd61-auto"): assert st(n) == "past_due" and running(n) == 0
    msgs = [e["msg"] for e in q("select msg from events where app='pd61-worker'")]
    assert any("payment for your plan didn't go through" in m for m in msgs) and not any("free tier" in m for m in msgs[-2:])   # bug 61
    # bug 62: month reset, plan change and upgrade don't unpause
    billing.uncap_account(aid, "new month"); S.set_tier(aid, "pro", "sub_pd61", why="test plan change"); S.set_tier(aid, "hobby", "sub_pd61", why="test")
    q("update accounts set month_key='1999-01' where id=?", (aid,)); billing.meter_tick(time.time())
    assert all(st(n) == "past_due" for n in ("pd61-web", "pd61-worker", "pd61-auto"))
    with pytest.raises(engine.PastDue): engine.scale("pd61-worker", 1)
    assert c.post("/v1/apps/pd61-worker/scale", json={"instances": 1}).status_code == 402
    # bug 65: payment succeeds -> apps actively restarted at their old size
    _post("evt_pd61b", "invoice.paid", {"id": "in_pd61b", "customer": "cus_pd61", "subscription": "sub_pd61", "amount_paid": 500, "metadata": {},
                                        "lines": {"data": [{"price": {"id": "price_hobby"}}]}})
    assert running("pd61-worker") == 2 and st("pd61-worker") == "running"
    assert running("pd61-auto") == 2 and st("pd61-auto") == "running"
    assert running("pd61-web") == 1 and st("pd61-web") == "running"   # it was serving 1 before; scales to zero again on its own
    assert not q("select past_due_since from accounts where id=?", (aid,), one=True)["past_due_since"]
    assert "_resume_instances" not in json.loads(q("select spec from apps where name='pd61-worker'", one=True)["spec"])

def test_status_active_also_resumes_and_diagnose(fake_engine):
    c, aid = _setup("pd62")
    sub = {"id": "sub_pd62", "status": "past_due", "metadata": {"tier": "hobby"}, "items": {"data": [{"price": {"id": "price_hobby"}}]}}
    _post("evt_pd62a", "customer.subscription.updated", sub); assert st("pd62-worker") == "past_due"
    from paas import diagnose
    assert any("payment failed" in x["cause"] for x in diagnose._rules(q("select * from apps where name='pd62-worker'", one=True), {}, [], [], [], None, time.time()))
    assert _post("evt_pd62b", "customer.subscription.updated", dict(sub, status="active"))["past_due"] == "cleared"
    assert running("pd62-worker") == 2

def test_free_cap_message_unchanged(fake_engine):
    _, aid = _setup("pd63"); billing.cap_account(aid)
    assert st("pd63-worker") == "capped"; billing.uncap_account(aid, "new month"); assert st("pd63-worker") == "idle"
