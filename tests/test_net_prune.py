"""QA N1: destroy removes the app's own empty networks; the reaper prunes empty, old, unowned aloof-* networks only."""
import time, json
from paas import engine
from paas.db import q
from test_parity import Fake, fake, user, deploy   # noqa: F401
class Nets(Fake):
    def __init__(self): super().__init__(); self.nets = {}   # name -> (created, n_containers)
    def remove_network_if_empty(self, n):
        if n in self.nets and self.nets[n][1] == 0: del self.nets[n]; return True
        return False
    def aloof_networks(self): return [(n, c) for n, (c, _) in self.nets.items()]
def _fx(monkeypatch):
    from paas.drivers import REGISTRY, get_drivers
    get_drivers(); f = Nets(); monkeypatch.setitem(REGISTRY, "local-docker", f); return f
def _acct(user): return user.get("/v1/account").json()["id"] if user.get("/v1/account").status_code == 200 else None
def test_destroy_removes_own_network_keeps_shared(monkeypatch, user):
    f = _fx(monkeypatch); deploy(user, "np-a", "good:v1"); deploy(user, "np-b", "good:v1")
    aid = q("select account_id from apps where name='np-a'", one=True)["account_id"]; acct = f"aloof-acct-{aid}"
    sp = engine.load_spec(q("select spec from apps where name='np-a'", one=True)["spec"]); assert acct in sp["networks"]
    old = time.time() - 3600
    f.nets = {acct: (old, 0), "aloof-own-np-a": (old, 0)}
    engine.update_spec("np-a", lambda s: s.__setitem__("networks", s["networks"] + ["aloof-own-np-a"]))
    assert user.delete("/v1/apps/np-a").status_code == 200
    assert "aloof-own-np-a" not in f.nets and acct in f.nets          # np-b still live on the account network
    assert user.delete("/v1/apps/np-b").status_code == 200
    assert acct not in f.nets                                        # last live app gone -> removed
def test_reaper_prunes_only_safe(monkeypatch, user):
    f = _fx(monkeypatch); deploy(user, "np-c", "good:v1")
    aid = q("select account_id from apps where name='np-c'", one=True)["account_id"]
    now = time.time(); old = now - 3600
    f.nets = {f"aloof-acct-{aid}": (old, 0),            # live owner -> keep
              "aloof-acct-999999999": (old, 0),         # unknown account -> prune
              "aloof-acct-999999998": (now - 60, 0),    # too young -> keep
              "aloof-pg-ghost": (old, 0),               # unknown db -> prune
              "aloof-pg-busy": (old, 2),                # has containers -> keep
              "aloof-acct-999999997": (None, 0)}        # unknown age -> keep
    gone = engine.prune_networks(now)
    assert sorted(gone) == ["aloof-acct-999999999", "aloof-pg-ghost"]
    assert set(f.nets) == {f"aloof-acct-{aid}", "aloof-acct-999999998", "aloof-pg-busy", "aloof-acct-999999997"}
def test_docker_ts():
    from paas.drivers.local_docker import _docker_ts
    assert abs(_docker_ts("2026-10-27T03:43:35.123456789Z") - 1793072615) < 1 and _docker_ts("2026-10-27T09:13:35+05:30") == _docker_ts("2026-10-27T03:43:35Z")
    assert _docker_ts("") is None
def test_mcp_delete_client_revokes_tagged_tokens():
    import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp_server"))
    import oauth
    a = engine.create_account(f"np-{time.time_ns()}@real.dev")
    q("insert into deploy_tokens(id,account_id,app,label,token_hash,prefix,created,can_destroy,oauth_client) values(?,?,?,?,?,?,?,?,?)",
      ("dt_np1", a["id"], None, "x", "h-np1", "dt_np1", time.time(), 0, "cid-np"))
    q("insert into deploy_tokens(id,account_id,app,label,token_hash,prefix,created,can_destroy,oauth_client) values(?,?,?,?,?,?,?,?,?)",
      ("dt_np2", a["id"], None, "x", "h-np2", "dt_np2", time.time(), 0, None))
    n = oauth.delete_client("cid-np"); assert n["deploy_tokens"] == 1
    assert {r["id"] for r in q("select id from deploy_tokens where account_id=?", (a["id"],))} == {"dt_np2"}
