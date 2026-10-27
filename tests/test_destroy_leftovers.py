"""destroy_app must not leave containers behind: 'starting' instance rows (deploy interrupted mid-readiness) and
containers the DB never recorded are removed; the periodic reaper cleans up destroyed apps only."""
import time
from paas import engine
from paas.db import q
from test_parity import Fake, fake, user, deploy, wait   # noqa: F401 (fixtures)

class Labelled(Fake):
    def __init__(self): super().__init__(); self.extra = {}   # container id -> app label, not in the instances table
    def remove_app_containers(self, app):
        gone = [k for k, v in self.extra.items() if v == app]
        for k in gone: del self.extra[k]
        return len(gone)
    def app_labels(self): return list(self.extra.values())

def _fx(monkeypatch):
    from paas.drivers import REGISTRY, get_drivers
    get_drivers(); f = Labelled(); monkeypatch.setitem(REGISTRY, "local-docker", f); return f

def test_destroy_stops_starting_and_unrecorded(monkeypatch, user):
    f = _fx(monkeypatch); n = f"lo{time.time_ns()%10**8}"
    deploy(user, n, "img:1"); wait(user, n)
    iid = q("select id from instances where app=? and status='running'", (n,), one=True)["id"]
    q("update instances set status='starting' where id=?", (iid,))   # as if the deploy thread died during readiness
    f.extra["ghost"] = n; f.extra["other"] = n + "-x"              # unrecorded container + a different app's
    assert user.delete(f"/v1/apps/{n}").status_code == 200
    assert iid in f.stopped
    assert q("select count(*) c from instances where app=? and status!='stopped'", (n,), one=True)["c"] == 0
    assert f.extra == {"other": n + "-x"}                          # exact label match only

def test_reaper_only_touches_destroyed(monkeypatch, user):
    f = _fx(monkeypatch); live = f"lv{time.time_ns()%10**8}"; dead = f"dd{time.time_ns()%10**8}"
    for a in (live, dead): deploy(user, a, "img:1"); wait(user, a)
    q("update apps set status='destroyed' where name=?", (dead,))
    f.extra = {"a": live, "b": dead, "c": "not-an-app-xyz"}
    engine.reap_destroyed()
    assert f.extra == {"a": live, "c": "not-an-app-xyz"}
    assert q("select count(*) c from instances where app=? and status!='stopped'", (dead,), one=True)["c"] == 0
    assert q("select count(*) c from instances where app=? and status='running'", (live,), one=True)["c"] >= 1
