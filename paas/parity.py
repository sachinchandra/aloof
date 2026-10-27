"""Fly-parity features: managed Postgres, volume snapshots/restore, Postgres backups, exec, log retention + drains.
Everything runs on the local-docker driver (the edge host) in phase 1; remote nodes report a clear 'not supported yet'."""
import os, io, json, time, socket, ssl, secrets, threading, tarfile, ipaddress, urllib.parse, logging, base64
import httpx
from .db import q, event, audit
from . import engine, crypto
from .crypto import load_spec
from .drivers import get_drivers
from .plans import PG_PLANS, TIERS
log = logging.getLogger("paas.parity")
ROOT = os.path.join(os.path.dirname(__file__), "..")
BACKUP_DIR = os.environ.get("PAAS_BACKUP_DIR", os.path.join(ROOT, "data", "backups"))
LOG_DIR = os.environ.get("PAAS_LOG_DIR", os.path.join(ROOT, "data", "logs"))
PG_KEEP = int(os.environ.get("PAAS_PG_BACKUP_KEEP", "7")); SNAP_KEEP = int(os.environ.get("PAAS_SNAPSHOT_KEEP", "10"))
PG_IMAGE = os.environ.get("PAAS_PG_IMAGE", "postgres:16-alpine")
def _local(): return get_drivers()["local-docker"]
def _inst(app):
    i = q("select * from instances where app=? and status='running' order by started", (app,), one=True)
    if not i: raise ValueError(f"{app} has no running instance")
    d = get_drivers()[i["provider"]]
    if not hasattr(d, "exec_run"): raise NotImplementedError(f"exec isn't supported on {i['provider']} nodes yet")
    return i, d
def _arc_path(kind, owner, aid, ext):
    d = os.path.join(BACKUP_DIR, kind, owner); os.makedirs(d, mode=0o700, exist_ok=True); return os.path.join(d, f"{aid}.{ext}")
def _arc_view(r): return {"id": r["id"], "kind": r["kind"], "owner": r["owner"], "size_bytes": r["size_bytes"], "created": r["created"], "note": r["note"]}
def _prune(kind, owner, keep):
    for r in q("select * from archives where kind=? and owner=? order by created desc", (kind, owner))[keep:]: delete_archive(r)
def delete_archive(r):
    try: os.remove(r["path"])
    except FileNotFoundError: pass
    q("delete from archives where id=?", (r["id"],))
# ---------------- managed Postgres ----------------
PG_NAME = __import__("re").compile(r"^[a-z][a-z0-9-]{1,24}[a-z0-9]$")
def pg_app(name): return f"pg-{name}"
def pg_net(name): return f"aloof-pg-{name}"
def _dbname(name): return name.replace("-", "_")
def pg_url(db, host=None):
    pw = crypto.dec(db["password_enc"]); return f"postgres://aloof:{pw}@{host or pg_app(db['name'])}:5432/{_dbname(db['name'])}?sslmode=disable"
def pg_create(account_id, name, plan="pg-dev", region=None):
    from . import regions as R
    if not PG_NAME.match(name or ""): raise ValueError("name must be 3-26 chars, lowercase letters/digits/hyphens")
    region = R.require_live(region or R.home())
    R.check_allowed(region, engine.account_tier(account_id)[0])
    if plan not in PG_PLANS: raise ValueError(f"unknown plan; choose from {list(PG_PLANS)}")
    if q("select 1 from pg_dbs where name=?", (name,), one=True) or q("select 1 from apps where name=? and status!='destroyed'", (pg_app(name),), one=True):
        raise PermissionError("database name taken")
    p = PG_PLANS[plan]; pw = secrets.token_urlsafe(24); vol = f"pgdata-{name}"
    engine.create_volume(account_id, vol, p["disk_gb"], region); q("update volumes set app=?, mount_path=? where name=?", (pg_app(name), "/var/lib/postgresql/data", vol))
    q("insert into pg_dbs(name,account_id,app,password_enc,plan,disk_gb,created,attached,region) values(?,?,?,?,?,?,?,?,?)",
      (name, account_id, pg_app(name), crypto.enc(pw), plan, p["disk_gb"], time.time(), "[]", region))
    spec = {"name": pg_app(name), "kind": "postgres", "pg_plan": plan, "source": {"image": PG_IMAGE}, "plan": p["compute"], "port": 5432,
            "env": {"POSTGRES_USER": "aloof", "POSTGRES_PASSWORD": pw, "POSTGRES_DB": _dbname(name), "PGDATA": "/var/lib/postgresql/data/pgdata"},
            "volumes": [{"name": vol, "path": "/var/lib/postgresql/data"}], "networks": [pg_net(name)], "net_alias": pg_app(name),
            "health": {"path": None, "tcp": True, "interval": 10, "failures": 3}, "instances": 1, "region": region,
            **({"provider": "local-docker"} if region == R.home() and R.local_capacity() else {})}
    try: engine.deploy(account_id, spec)
    except Exception:
        q("delete from pg_dbs where name=?", (name,)); q("update volumes set app=null where name=?", (vol,)); engine.delete_volume(vol); raise
    event(pg_app(name), f"postgres {name} ({plan}) created")
    return pg_view(q("select * from pg_dbs where name=?", (name,), one=True))
def pg_view(db, reveal=False):
    ap = q("select status, error from apps where name=?", (db["app"],), one=True) or {}
    v = {"name": db["name"], "region": db.get("region") or "nbg1", "plan": db["plan"], "disk_gb": db["disk_gb"], "status": ap.get("status"), "error": ap.get("error"), "created": db["created"],
         "attached": json.loads(db["attached"] or "[]"), "host": pg_app(db["name"]), "port": 5432, "database": _dbname(db["name"]), "user": "aloof",
         "price_usd_month": PG_PLANS[db["plan"]]["price_usd_month"],
         "backups": [_arc_view(r) for r in q("select * from archives where kind='pg' and owner=? order by created desc", (db["name"],))]}
    if reveal: v["database_url"] = pg_url(db)
    return v
def pg_attach(name, app, var="DATABASE_URL"):
    db = q("select * from pg_dbs where name=?", (name,), one=True); ap = q("select * from apps where name=?", (app,), one=True)
    from . import regions as R
    R.same_region("database", name, db.get("region"), app, ap.get("region"))
    pg = q("select provider from apps where name=?", (db["app"],), one=True) or {}
    if ap["provider"] != pg.get("provider"): raise ValueError(f"{app} runs on {ap['provider']} but database {name} runs on {pg.get('provider')}; they must share a node pool")
    if not __import__("re").fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", var): raise ValueError("bad env var name")
    att = [x for x in json.loads(db["attached"] or "[]") if x["app"] != app] + [{"app": app, "var": var}]
    q("update pg_dbs set attached=? where name=?", (json.dumps(att), name)); event(app, f"postgres {name} attached as ${var}")
    def f(spec):
        spec["networks"] = sorted(set(spec.get("networks") or []) | {pg_net(name)}); spec["env"][var] = pg_url(db)
    return engine.update_spec(app, f)
def pg_detach(name, app):
    db = q("select * from pg_dbs where name=?", (name,), one=True); att = json.loads(db["attached"] or "[]")
    var = next((x["var"] for x in att if x["app"] == app), "DATABASE_URL")
    q("update pg_dbs set attached=? where name=?", (json.dumps([x for x in att if x["app"] != app]), name)); event(app, f"postgres {name} detached")
    def f(spec):
        spec["networks"] = [n for n in spec.get("networks") or [] if n != pg_net(name)]; spec["env"].pop(var, None)
    return engine.update_spec(app, f)
def pg_destroy(name, keep_backups=False):
    db = q("select * from pg_dbs where name=?", (name,), one=True)
    for x in json.loads(db["attached"] or "[]"):
        if q("select 1 from apps where name=? and status!='destroyed'", (x["app"],), one=True): pg_detach(name, x["app"])
    engine.destroy(db["app"]); vol = f"pgdata-{name}"
    if q("select 1 from volumes where name=?", (vol,), one=True): engine.delete_volume(vol)
    if not keep_backups:
        for r in q("select * from archives where kind='pg' and owner=?", (name,)): delete_archive(r)
    q("delete from pg_dbs where name=?", (name,))   # first, so the network no longer counts as owned
    for d in engine.get_drivers().values():   # the pg container is gone now; drop its network if nothing else is on it
        f = getattr(d, "remove_network_if_empty", None)
        if f:
            try: f(pg_net(name))
            except Exception: pass
    q("delete from pg_dbs where name=?", (name,))
def pg_backup(name, note="manual"):
    db = q("select * from pg_dbs where name=?", (name,), one=True); i, d = _inst(db["app"])
    code, out = d.exec_run(i["id"], ["pg_dump", "-U", "aloof", "-Fc", _dbname(name)], stdout_only=True)
    if code: raise RuntimeError(f"pg_dump failed: {out[-500:].decode(errors='replace')}")
    aid = "pgb_" + time.strftime("%Y%m%d%H%M%S") + "_" + secrets.token_hex(3); path = _arc_path("pg", name, aid, "dump")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f: f.write(out)
    q("insert into archives(id,kind,owner,account_id,path,size_bytes,created,note) values(?,?,?,?,?,?,?,?)", (aid, "pg", name, db["account_id"], path, len(out), time.time(), note))
    event(db["app"], f"backup {aid} ({len(out)} bytes, {note})"); _prune("pg", name, PG_KEEP)
    return _arc_view(q("select * from archives where id=?", (aid,), one=True))
def pg_restore(name, backup_id):
    db = q("select * from pg_dbs where name=?", (name,), one=True); r = q("select * from archives where id=? and kind='pg' and owner=?", (backup_id, name), one=True)
    if not r: raise ValueError("backup not found")
    i, d = _inst(db["app"]); ct = d.c.containers.get(i["id"]); data = open(r["path"], "rb").read()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        ti = tarfile.TarInfo("aloof-restore.dump"); ti.size = len(data); ti.mode = 0o644; t.addfile(ti, io.BytesIO(data))
    ct.put_archive("/tmp", buf.getvalue())
    code, out = d.exec_run(i["id"], ["pg_restore", "-U", "aloof", "-d", _dbname(name), "--clean", "--if-exists", "--no-owner", "/tmp/aloof-restore.dump"])
    d.exec_run(i["id"], ["rm", "-f", "/tmp/aloof-restore.dump"])
    if code: raise RuntimeError(f"pg_restore failed: {out[-500:].decode(errors='replace')}")
    event(db["app"], f"restored backup {backup_id}"); return {"ok": True, "restored": backup_id}
def pg_backup_tick():
    """Daily automatic backups (checked hourly), PG_KEEP kept per database."""
    for db in q("select * from pg_dbs"):
        last = q("select max(created) t from archives where kind='pg' and owner=?", (db["name"],), one=True)["t"] or 0
        if time.time() - last > 86400 and time.time() - db["created"] > 300:
            try: pg_backup(db["name"], "daily")
            except Exception as e: log.warning("pg backup %s: %s", db["name"], e)
# ---------------- volume snapshots ----------------
def snapshot_create(vol, note="manual"):
    v = q("select * from volumes where name=?", (vol,), one=True)
    if v.get("node_id"): raise NotImplementedError("snapshots for volumes on remote nodes aren't supported yet")
    aid = "snap_" + time.strftime("%Y%m%d%H%M%S") + "_" + secrets.token_hex(3); path = _arc_path("snap", vol, aid, "tar.gz")
    size = _local().snapshot_volume(vol, path)
    q("insert into archives(id,kind,owner,account_id,path,size_bytes,created,note) values(?,?,?,?,?,?,?,?)", (aid, "snap", vol, v["account_id"], path, size, time.time(), note))
    if v["app"]: event(v["app"], f"volume {vol} snapshot {aid} ({size} bytes)")
    _prune("snap", vol, SNAP_KEEP); return _arc_view(q("select * from archives where id=?", (aid,), one=True))
def snapshot_restore(vol, sid):
    v = q("select * from volumes where name=?", (vol,), one=True); r = q("select * from archives where id=? and kind='snap' and owner=?", (sid, vol), one=True)
    if not r: raise ValueError("snapshot not found")
    app = v["app"]; n = len(q("select 1 from instances where app=? and status='running'", (app,))) if app else 0
    if app and n: engine.scale(app, 0, reason=f"restoring volume {vol}")   # single writer: stop, restore, start
    try: _local().restore_volume(vol, r["path"])
    finally:
        if app and n: engine.scale(app, n, reason=f"restored volume {vol}")
    if app: event(app, f"volume {vol} restored from {sid}")
    return {"ok": True, "volume": vol, "restored": sid, "restarted_instances": n}
# ---------------- exec (non-interactive), audited ----------------
def exec_cmd(account_id, app, cmd, ip=None, instance=None):
    if isinstance(cmd, str): cmd = ["/bin/sh", "-c", cmd]
    i, d = _inst(app) if not instance else (q("select * from instances where id=? and app=? and status='running'", (instance, app), one=True), None)
    if not i: raise ValueError("instance not found")
    d = d or get_drivers()[i["provider"]]
    audit(account_id, app, "exec", json.dumps(cmd)[:1000], ip)
    t = time.time()
    try: code, out = d.exec_run(i["id"], cmd)
    except Exception as e:
        if "is not running" in str(e) or "No such container" in str(e): raise ValueError("instance is stopping or asleep; send a request to wake it and retry")
        raise
    audit(account_id, app, "exec.result", f"exit {code} in {time.time()-t:.2f}s, {len(out)} bytes", ip)
    return {"instance": i["id"], "exit_code": code, "output": out[-200000:].decode(errors="replace")}
# ---------------- log retention + drains ----------------
_last = {}       # instance id -> last docker timestamp seen
_last_ev = {}    # app -> last event id
def _day(ts): return time.strftime("%Y-%m-%d", time.gmtime(ts))
def _append(app, lines):
    if not lines: return
    d = os.path.join(LOG_DIR, app); os.makedirs(d, mode=0o700, exist_ok=True)
    by = {}
    for l in lines: by.setdefault(_day(l["ts"]), []).append(l)
    for day, ls in by.items():
        with open(os.path.join(d, f"{day}.jsonl"), "a") as f:
            for l in ls: f.write(json.dumps(l) + "\n")
def _parse_ts(s):
    try:
        base, frac = (s.rstrip("Z").split(".") + ["0"])[:2]
        return time.mktime(time.strptime(base, "%Y-%m-%dT%H:%M:%S")) - time.timezone + float("0." + frac[:6])
    except Exception: return time.time()
def collect_tick():
    """Every few seconds: new stdout/stderr lines of every instance + platform events -> retained JSONL per app/day -> drains."""
    for a in q("select name, account_id from apps where status!='destroyed'"):
        app = a["name"]; out = []
        lid = _last_ev.get(app)
        if lid is None: lid = _last_ev[app] = (q("select max(id) m from events where app=?", (app,), one=True)["m"] or 0)
        for e in q("select * from events where app=? and id>? order by id", (app, lid)):
            out.append({"ts": e["ts"], "app": app, "instance": "platform", "stream": "platform", "message": e["msg"]}); _last_ev[app] = e["id"]
        for i in q("select * from instances where app=? and status='running'", (app,)):
            d = get_drivers().get(i["provider"])
            if not hasattr(d, "c"): continue
            try:
                since = _last.get(i["id"])
                raw = d.c.containers.get(i["id"]).logs(timestamps=True, since=int(_parse_ts(since)) if since else int(i["started"]) - 1).decode(errors="replace")
            except Exception: continue
            for line in raw.splitlines():
                ts, _, msg = line.partition(" ")
                if since and ts <= since: continue
                out.append({"ts": _parse_ts(ts), "app": app, "instance": i["id"], "stream": "app", "message": msg[:8000]}); since = ts
            if since: _last[i["id"]] = since
        if out:
            _append(app, out)
            for dr in q("select * from drains where app=?", (app,)): _ship(dr, out)
def prune_logs_tick():
    for a in q("select a.name, coalesce(c.tier,'free') tier from apps a left join accounts c on c.id=a.account_id"):
        keep = TIERS.get(a["tier"], TIERS["free"]).get("log_retention_days", 1); d = os.path.join(LOG_DIR, a["name"])
        if not os.path.isdir(d): continue
        cutoff = _day(time.time() - keep * 86400)
        for f in os.listdir(d):
            if f.endswith(".jsonl") and f[:10] < cutoff: os.remove(os.path.join(d, f))
def history(app, since=0, limit=1000, contains=None):
    d = os.path.join(LOG_DIR, app); out = []
    if not os.path.isdir(d): return out
    for f in sorted(os.listdir(d)):
        if f[:10] < _day(since or 0): continue
        for line in open(os.path.join(d, f)):
            try: l = json.loads(line)
            except Exception: continue
            if l["ts"] >= since and (not contains or contains in l["message"]): out.append(l)
    return out[-limit:]
# drains: https://... (JSON array POST, optional basic auth in URL) or syslog+tcp|udp|tls://host:port (RFC 5424)
def _check_target(url):
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("https", "http", "syslog+tcp", "syslog+udp", "syslog+tls"): raise ValueError("drain url must be https://, syslog+tls://, syslog+tcp:// or syslog+udp://")
    if not u.hostname: raise ValueError("drain url needs a host")
    if u.scheme.startswith("syslog") and not u.port: raise ValueError("syslog drains need a port")
    if os.environ.get("PAAS_DRAIN_ALLOW_PRIVATE") == "1": return u
    try: addrs = {x[4][0] for x in socket.getaddrinfo(u.hostname, u.port or 443)}
    except socket.gaierror: raise ValueError(f"can't resolve {u.hostname}")
    for ip in addrs:
        a = ipaddress.ip_address(ip)
        if a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_multicast or a.is_unspecified:
            raise ValueError("drain targets must be public addresses")
    return u
def _redact(url):
    u = urllib.parse.urlsplit(url); net = (u.hostname or "") + (f":{u.port}" if u.port else "")
    return urllib.parse.urlunsplit((u.scheme, ("***@" if u.username or u.password else "") + net, u.path, "***" if u.query else "", ""))
def drain_add(account_id, app, url):
    _check_target(url); did = "drn_" + secrets.token_hex(5)
    q("insert into drains(id,app,account_id,kind,url,created,sent) values(?,?,?,?,?,?,0)", (did, app, account_id, urllib.parse.urlsplit(url).scheme, crypto.enc(url), time.time()))
    return drain_view(q("select * from drains where id=?", (did,), one=True))
def drain_view(d): return {"id": d["id"], "app": d["app"], "kind": d["kind"], "url": _redact(crypto.dec(d["url"])), "created": d["created"],
                           "last_ok": d["last_ok"], "last_error": d["last_error"], "sent": d["sent"]}
def _syslog_line(l):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(l["ts"])) + f".{int((l['ts'] % 1) * 1e6):06d}Z"
    return f"<14>1 {ts} {l['app']} {l['app']} {l['instance'][:32]} {l['stream']} - {l['message']}"
def _ship(d, lines):
    url = crypto.dec(d["url"])
    try:
        u = _check_target(url)
        if u.scheme.startswith("http"):
            auth = (urllib.parse.unquote(u.username or ""), urllib.parse.unquote(u.password or "")) if u.username else None
            clean = urllib.parse.urlunsplit((u.scheme, u.hostname + (f":{u.port}" if u.port else ""), u.path or "/", u.query, ""))
            r = httpx.post(clean, json=lines, auth=auth, timeout=10, follow_redirects=False); r.raise_for_status()
        else:
            payload = [_syslog_line(l).encode() for l in lines]
            if u.scheme == "syslog+udp":
                s = socket.socket(socket.AF_INET6 if ":" in (u.hostname or "") else socket.AF_INET, socket.SOCK_DGRAM)
                for p in payload: s.sendto(p[:8000], (u.hostname, u.port))
                s.close()
            else:
                s = socket.create_connection((u.hostname, u.port), timeout=10)
                if u.scheme == "syslog+tls": s = ssl.create_default_context().wrap_socket(s, server_hostname=u.hostname)
                s.sendall(b"".join(f"{len(p)} ".encode() + p for p in payload)); s.close()   # RFC 6587 octet counting
        q("update drains set last_ok=?, last_error=null, sent=sent+? where id=?", (time.time(), len(lines), d["id"]))
    except Exception as e:
        q("update drains set last_error=? where id=?", (f"{type(e).__name__}: {e}"[:300], d["id"]))
def _loop(fn, every):
    while True:
        try: fn()
        except Exception: log.exception(fn.__name__)
        time.sleep(every)
def start():
    threading.Thread(target=_loop, args=(collect_tick, int(os.environ.get("PAAS_LOG_COLLECT_INTERVAL", "5"))), daemon=True).start()
    threading.Thread(target=_loop, args=(prune_logs_tick, 3600), daemon=True).start()
    threading.Thread(target=_loop, args=(pg_backup_tick, 3600), daemon=True).start()
