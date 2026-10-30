"""Developer-experience API (flyctl parity): private registries + git credentials, local-dir deploys (upload) with aloof.toml,
`aloof launch` detection, deploy tokens, staged secrets, scale count/vm, certs, ips, port-forward proxy, previews, cron, GitHub App."""
import os, re, ssl, json, time, socket, asyncio, secrets, hashlib, datetime
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from .db import q, kv_get, kv_put, kv_del, audit as _audit, event
from . import engine, creds, launch, appconfig, crypto, github_app, cron as cronmod
from .crypto import load_spec
from .drivers import get_drivers
R = APIRouter()
def _api():
    from . import api; return api
def acct(request: Request): return _api().acct(request, request.headers.get("authorization"))
def own(name, a): return _api().own(name, a)
def _ip(r): return r.client.host if r.client else None
def _no_token(a, what="this"):
    if a.get("_token"): raise HTTPException(403, f"deploy tokens can't manage {what}")
# ---------------- private registries ----------------
class RegReq(BaseModel): server: str="docker.io"; username: str; password: str; app: str|None=None
@R.post("/v1/registry")
def registry_login(b: RegReq, request: Request, a=Depends(acct)):
    if a.get("_scope_app") and b.app != a["_scope_app"]: raise HTTPException(403, "app-scoped deploy tokens may only set registry credentials for their app")
    if b.app: own(b.app, a)
    try: r = creds.registry_set(a["id"], b.server, b.username, b.password, b.app)
    except ValueError as e: raise HTTPException(400, str(e))
    _audit(a["id"], b.app, "registry.login", f"{r['server']} as {r['username']}", _ip(request)); return r
@R.get("/v1/registry")
def registry_list(app: str|None=None, a=Depends(acct)): return creds.registry_list(a["id"], app)
@R.delete("/v1/registry/{server:path}")
def registry_logout(server: str, request: Request, app: str|None=None, a=Depends(acct)):
    if app: own(app, a)
    elif a.get("_scope_app"): raise HTTPException(403, "scoped token")
    creds.registry_delete(a["id"], server, app); _audit(a["id"], app, "registry.logout", server, _ip(request)); return {"ok": True}
# ---------------- private git ----------------
class GitReq(BaseModel): host: str; kind: str; token: str|None=None; username: str|None=None; private_key: str|None=None; app: str|None=None
@R.post("/v1/git/credentials")
def git_cred(b: GitReq, request: Request, a=Depends(acct)):
    if a.get("_scope_app") and b.app != a["_scope_app"]: raise HTTPException(403, "scoped token")
    if b.app: own(b.app, a)
    try: r = creds.git_set(a["id"], b.host, b.kind, b.app, b.token, b.username, b.private_key)
    except ValueError as e: raise HTTPException(400, str(e))
    _audit(a["id"], b.app, "git.credential", f"{r['kind']} for {r['host']}", _ip(request))
    if r["kind"] == "ssh": r["next"] = f"add this public key as a read-only deploy key on the repo (GitHub: Settings > Deploy keys), then deploy with git@{r['host']}:<owner>/<repo>.git"
    return r
@R.get("/v1/git/credentials")
def git_list(a=Depends(acct)): return [c for c in creds.git_list(a["id"]) if not a.get("_scope_app") or c["app"] == a["_scope_app"]]
@R.delete("/v1/git/credentials/{cid}")
def git_del(cid: str, a=Depends(acct)): _no_token(a, "git credentials"); creds.git_delete(a["id"], cid); return {"ok": True}
# ---------------- local-dir deploy: upload + aloof.toml ----------------
def _claim_name(name, a):
    if not engine.NAME_RE.match(name) or name.startswith("pg-"): raise HTTPException(400, "bad app name")
    if not _api().scope_ok(name, a): raise HTTPException(403, "scoped token")
    ex = q("select account_id, status from apps where name=?", (name,), one=True)
    if ex and ex["account_id"] != a["id"] and ex["status"] != "destroyed": raise HTTPException(409, "app name taken")
@R.post("/v1/apps/{name}/source")
async def upload_source(name: str, request: Request, a=Depends(acct)):
    """Body: the build context as .tar.gz (aloof deploy respects .dockerignore before uploading). Returns upload_id."""
    _claim_name(name, a)
    import tempfile
    os.makedirs(creds.UPLOAD_DIR, exist_ok=True); fd, tmp = tempfile.mkstemp(dir=creds.UPLOAD_DIR, suffix=".part"); n = 0
    try:
        with os.fdopen(fd, "wb") as f:
            async for c in request.stream():
                n += len(c)
                if n > creds.MAX_UPLOAD: raise HTTPException(413, f"build context over {creds.MAX_UPLOAD >> 20} MB (check .dockerignore)")
                f.write(c)
        def _save():
            with open(tmp, "rb") as f: return creds.save_upload(a["id"], name, iter(lambda: f.read(1 << 20), b""))
        try: r = await asyncio.to_thread(_save)
        except ValueError as e: raise HTTPException(400, str(e))
    finally:
        try: os.remove(tmp)
        except OSError: pass
    _audit(a["id"], name, "source.upload", f"{r['size_bytes']} bytes", _ip(request)); return r
class ConfigReq(BaseModel): config: dict; name: str|None=None
@R.post("/v1/config/validate")
def config_validate(b: ConfigReq): return appconfig.validate(b.config, b.name)
class DeployCfg(BaseModel):
    config: dict = {}; upload_id: str|None=None; image: str|None=None; git: str|None=None; ref: str|None=None; env: dict[str,str]={}; strategy: str|None=None
@R.post("/v1/apps/{name}/deploy")
def deploy_config(name: str, b: DeployCfg, request: Request, a=Depends(acct)):
    """Deploy from aloof.toml (parsed) + an uploaded context / image / git. Mounts are created (initial_size) and attached on first deploy."""
    _claim_name(name, a)
    v = appconfig.validate(b.config | {"app": name})
    if not v["ok"]: raise HTTPException(400, v["error"])
    spec = v["spec"]; src = spec["source"]
    if b.image: src = {"image": b.image}
    elif b.git: src = {"git": b.git, **({"ref": b.ref} if b.ref else {})} | {k: x for k, x in src.items() if k in ("subdir", "dockerfile", "build_args")}
    elif b.upload_id: src = {"upload": b.upload_id} | {k: x for k, x in src.items() if k in ("subdir", "dockerfile", "build_args")}
    if not (src.get("image") or src.get("git") or src.get("upload")): raise HTTPException(400, "nothing to deploy: set [build] image/git in aloof.toml or upload the directory (aloof deploy)")
    spec["source"] = src; spec["env"] = {**spec.get("env", {}), **b.env}
    ex = engine.get_app(name)
    for m in v["mounts"]:
        vol = q("select * from volumes where name=?", (m["name"],), one=True)
        if vol and vol["account_id"] != a["id"]: raise HTTPException(409, f"volume {m['name']} belongs to another account")
        if not vol:
            try: engine.create_volume(a["id"], m["name"], m["size_gb"])
            except (ValueError, PermissionError) as e: raise HTTPException(400, str(e))
        if (vol or {}).get("app") not in (None, name): raise HTTPException(409, f"volume {m['name']} is attached to {vol['app']}")
        q("update volumes set app=?, mount_path=? where name=?", (name, m["path"], m["name"]))
    if v["mounts"]: spec["volumes"] = [{"name": m["name"], "path": m["path"]} for m in v["mounts"]]
    if ex and ex["status"] != "destroyed" and "env" not in (b.config or {}): pass
    try: r = engine.deploy(a["id"], spec)
    except PermissionError as e: raise HTTPException(409, str(e))
    except ValueError as e: raise HTTPException(400, str(e))
    _audit(a["id"], name, "deploy", json.dumps({"source": {k: x for k, x in src.items() if k != "build_args"}})[:500], _ip(request))
    return _api().with_url(r) | {"warnings": v["warnings"]}
class DetectReq(BaseModel): files: dict[str, str|None]; name: str|None=None
@R.post("/v1/launch/detect")
def launch_detect(b: DetectReq):
    plan = launch.detect(b.files); name = b.name or "my-app"
    return plan | {"toml": launch.toml_for(name, plan, plan["framework"])}
# ---------------- deploy tokens ----------------
class TokReq(BaseModel): app: str|None=None; label: str="ci"; expires_in: str|int|None="90d"; can_destroy: bool=False
def _secs(v):
    if v in (None, "", "never", 0): return None
    if isinstance(v, int): return v
    m = re.fullmatch(r"(\d+)\s*([smhdwy]?)", str(v).strip().lower())
    if not m: raise HTTPException(400, "expires_in like 3600, 12h, 30d, 1y or never")
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}[m.group(2)]
@R.post("/v1/tokens")
def token_create(b: TokReq, request: Request, a=Depends(acct)):
    if b.app: own(b.app, a)
    if b.can_destroy and a.get("_level", 3) < 3: raise HTTPException(403, "only a full-access (paas:admin) caller can create a deploy token that can destroy")
    if not b.app and a.get("_level", 3) < 3:
        raise HTTPException(403, "only a full-access (paas:admin) caller can create an org-wide deploy token; pass an app to create an app-scoped one")
    oc = (request.headers.get("x-aloof-client") or "").strip()[:300] or None   # set by the MCP server for OAuth connections
    t = "dt_" + secrets.token_urlsafe(32); tid = "tok_" + secrets.token_hex(5); exp = _secs(b.expires_in)
    q("insert into deploy_tokens(id,account_id,app,label,token_hash,prefix,expires,created,can_destroy,oauth_client) values(?,?,?,?,?,?,?,?,?,?)",
      (tid, a["id"], b.app, b.label[:60], crypto.sha256(t), t[:10], time.time() + exp if exp else None, time.time(), 1 if b.can_destroy else 0, oc))
    _audit(a["id"], b.app, "token.create", f"{tid} {b.label} scope={'app:' + b.app if b.app else 'org'} can_destroy={bool(b.can_destroy)}", _ip(request))
    return {"id": tid, "token": t, "scope": f"app:{b.app}" if b.app else "org (all apps in this account)", "expires": time.time() + exp if exp else None,
            "can_destroy": bool(b.can_destroy), "access": "paas:admin (can destroy and restore)" if b.can_destroy else "paas:write (deploy, scale, config; can't destroy apps or delete/restore data)",
            "detail": "shown once; use as ALOOF_API_TOKEN (Authorization: Bearer)"}
# When new deploy tokens became no-destroy by default (migration v16 deploy, 2026-10-26 17:39 UTC). Tokens created before
# then were migrated to can_destroy=1 so existing pipelines kept working; flag them so users can recreate them.
NO_DESTROY_DEFAULT_SINCE = 1793036340
LEGACY_DESTROY_NOTE = "created before Oct 26 2026, so it kept destroy rights; revoke it and create a new token (no destroy by default) unless this pipeline needs to delete apps or data"
def _legacy_note(r):
    old = bool(r.get("can_destroy")) and (r.get("created") or 0) < NO_DESTROY_DEFAULT_SINCE
    return {"legacy_destroy": old} | ({"note": LEGACY_DESTROY_NOTE} if old else {})
@R.get("/v1/tokens")
def token_list(a=Depends(acct)):
    return [{k: r[k] for k in ("id", "app", "label", "prefix", "expires", "created", "last_used")} | {"can_destroy": bool(r.get("can_destroy")), "expired": bool(r["expires"] and r["expires"] < time.time())}
            | _legacy_note(r)
            for r in q("select * from deploy_tokens where account_id=? order by created desc", (a["id"],))]
@R.delete("/v1/tokens/{tid}")
def token_revoke(tid: str, request: Request, a=Depends(acct)):
    q("delete from deploy_tokens where id=? and account_id=?", (tid, a["id"])); _audit(a["id"], None, "token.revoke", tid, _ip(request))
    from . import ws_registry; ws_registry.revalidate(a["id"]); return {"ok": True}
# ---------------- secrets (flyctl-style, optionally staged) ----------------
def _staged(app):
    raw = kv_get("staged_secrets", app); return crypto.dec_json(raw) if raw else {"set": {}, "unset": []}
def _put_staged(app, st): kv_put("staged_secrets", app, crypto.enc_json(st))
@R.get("/v1/apps/{name}/secrets")
def secrets_list(name: str, a=Depends(acct)):
    own(name, a); spec = load_spec(q("select spec from apps where name=?", (name,), one=True)["spec"]); st = _staged(name)
    out = [{"name": k, "digest": hashlib.sha256(str(v).encode()).hexdigest()[:16], "status": "deployed" + (", staged change" if k in st["set"] else ", staged unset" if k in st["unset"] else "")}
           for k, v in sorted(spec.get("env", {}).items())]
    out += [{"name": k, "digest": hashlib.sha256(str(v).encode()).hexdigest()[:16], "status": "staged"} for k, v in sorted(st["set"].items()) if k not in spec.get("env", {})]
    return out
class SecReq(BaseModel): secrets: dict[str, str] = {}; unset: list[str] = []; stage: bool = False
@R.post("/v1/apps/{name}/secrets")
def secrets_set(name: str, b: SecReq, request: Request, a=Depends(acct)):
    own(name, a)
    for k in list(b.secrets) + b.unset:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k): raise HTTPException(400, f"bad secret name {k!r}")
    _audit(a["id"], name, "secrets." + ("stage" if b.stage else "set"), "set " + ",".join(sorted(b.secrets)) + (" unset " + ",".join(b.unset) if b.unset else ""), _ip(request))
    if b.stage:
        st = _staged(name); st["set"].update(b.secrets); st["unset"] = sorted(set(st["unset"]) - set(b.secrets) | set(b.unset))
        for k in b.unset: st["set"].pop(k, None)
        _put_staged(name, st); return {"staged": True, "detail": f"run `aloof secrets deploy {name}` to apply"}
    env = dict(b.secrets) | {k: None for k in b.unset}
    return _api().with_url(engine.set_env(name, env)) | {"staged": False}
@R.post("/v1/apps/{name}/secrets/deploy")
def secrets_deploy(name: str, request: Request, a=Depends(acct)):
    own(name, a); st = _staged(name)
    if not st["set"] and not st["unset"]: raise HTTPException(400, "no staged secrets")
    kv_del("staged_secrets", name); _audit(a["id"], name, "secrets.deploy", f"{len(st['set'])} set, {len(st['unset'])} unset", _ip(request))
    return _api().with_url(engine.set_env(name, dict(st["set"]) | {k: None for k in st["unset"]}))
# ---------------- status / scale ----------------
@R.get("/v1/apps/{name}/status")
def app_status(name: str, a=Depends(acct)):
    ap = _api().with_url(own(name, a)); spec = load_spec(q("select spec from apps where name=?", (name,), one=True)["spec"])
    groups = [{"group": "web", "count": len(ap["instances"]), "instances": ap["instances"], "status": ap["status"]}]
    for g in (spec.get("processes") or {}):
        if g == "web": continue
        ch = engine.get_app(engine.group_app(name, g))
        if ch: groups.append({"group": g, "app": ch["name"], "count": len(ch["instances"]), "instances": ch["instances"], "status": ch["status"]})
    rel = q("select version, status, image, created, reason from releases where app=? order by version desc", (name,), one=True)
    previews = [p for p in engine.children(name) if load_spec(q("select spec from apps where name=?", (p,), one=True)["spec"]).get("_preview_of") == name]
    return {"name": name, "status": ap["status"], "error": ap.get("error"), "url": ap["url"], "plan": ap["plan"]["id"], "provider": ap["provider"],
            "release": rel, "process_groups": groups, "cron": spec.get("cron") or [], "release_command": spec.get("release_command"),
            "internal_host": f"{name}.internal", "domains": ap["domains"], "previews": previews, "autoscale": spec.get("autoscale")} | (engine.release_state(name) or {})
class CountReq(BaseModel): counts: dict[str, int]
@R.post("/v1/apps/{name}/scale/count")
def scale_count(name: str, b: CountReq, request: Request, a=Depends(acct)):
    own(name, a); spec = load_spec(q("select spec from apps where name=?", (name,), one=True)["spec"]); groups = set(spec.get("processes") or {}) | {"web"}
    bad = set(b.counts) - groups
    if bad: raise HTTPException(400, f"unknown process group(s) {sorted(bad)}; groups: {sorted(groups)}")
    out = {}
    for g, n in b.counts.items():
        if g == "web": engine.scale(name, n); out[g] = n; continue
        pc = dict(spec.get("process_counts") or {}); pc[g] = max(0, min(int(n), 10)); spec["process_counts"] = pc
        ch = engine.group_app(name, g)
        if engine.get_app(ch): engine.scale(ch, pc[g])
        out[g] = pc[g]
    from .crypto import dump_spec
    cur = load_spec(q("select spec from apps where name=?", (name,), one=True)["spec"]); cur["process_counts"] = spec.get("process_counts")
    q("update apps set spec=? where name=?", (dump_spec(cur), name)); _audit(a["id"], name, "scale.count", json.dumps(out), _ip(request))
    return {"counts": out}
class VmReq(BaseModel): plan: str; group: str|None=None
@R.post("/v1/apps/{name}/scale/vm")
def scale_vm(name: str, b: VmReq, request: Request, a=Depends(acct)):
    own(name, a)
    from .plans import PLANS
    if b.plan not in PLANS: raise HTTPException(400, f"unknown plan; choose from {list(PLANS)}")
    target = engine.group_app(name, b.group) if b.group and b.group != "web" else name
    def f(spec): spec["plan"] = b.plan
    try: r = engine.update_spec(target, f)
    except (ValueError, PermissionError) as e: raise HTTPException(400, str(e))
    _audit(a["id"], name, "scale.vm", f"{target} -> {b.plan}", _ip(request)); return _api().with_url(r)
# ---------------- certs / ips ----------------
def _probe_cert(host):
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((os.environ.get("PAAS_TLS_PROBE_HOST", "127.0.0.1"), 443), timeout=4) as s0:
            with ctx.wrap_socket(s0, server_hostname=host) as s1: c = s1.getpeercert()
        exp = datetime.datetime.strptime(c["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=datetime.timezone.utc)
        iss = dict(x[0] for x in c.get("issuer", ())).get("organizationName")
        return {"status": "issued", "issuer": iss, "expires": exp.isoformat(), "days_left": (exp - datetime.datetime.now(datetime.timezone.utc)).days}
    except ssl.SSLCertVerificationError as e: return {"status": "invalid", "error": e.verify_message}
    except Exception as e: return {"status": "not issued yet (issued on the first HTTPS request)" , "error": type(e).__name__}
@R.get("/v1/apps/{name}/certs")
def certs(name: str, probe: bool=True, a=Depends(acct)):
    """TLS certificates: Let's Encrypt via Caddy on-demand TLS, for <app>.<domain> and every verified custom domain (auto-renewed)."""
    own(name, a); api = _api(); out = []
    hosts = [(f"{name}.{api.URL_DOMAIN}", True, "platform")] + [(d["domain"], bool(d["verified"]), "custom") for d in q("select * from domains where app=?", (name,))]
    for h, ok, kind in hosts:
        r = {"hostname": h, "kind": kind, "dns_verified": ok}
        r |= (_probe_cert(h) if (probe and ok) else {"status": "awaiting DNS verification" if not ok else "unknown"})
        out.append(r)
    return out
@R.get("/v1/apps/{name}/ips")
def ips(name: str, a=Depends(acct)):
    own(name, a); api = _api()
    def res(h, fam):
        try: return sorted({x[4][0] for x in socket.getaddrinfo(h, 443, fam)})
        except Exception: return []
    edge = os.environ.get("PAAS_EDGE_HOST") or f"{name}.{api.URL_DOMAIN}"
    v4 = [x.strip() for x in os.environ.get("PAAS_PUBLIC_IPV4", "").split(",") if x.strip()] or res(edge, socket.AF_INET)
    v6 = [x.strip() for x in os.environ.get("PAAS_PUBLIC_IPV6", "").split(",") if x.strip()] or res(edge, socket.AF_INET6)
    priv = []
    for i in q("select * from instances where app=? and status='running'", (name,)):
        ip = _private_ip(i)
        if ip: priv.append({"instance": i["id"], "address": ip})
    return {"public": [{"type": "v4", "address": x, "shared": True} for x in v4] + [{"type": "v6", "address": x, "shared": True} for x in v6],
            "private": {"hostname": f"{name}.internal", "network": f"aloof-acct-{a['id']}", "instances": priv},
            "note": "public IPs are the shared edge (dedicated/anycast IPs are not offered); <app>.internal resolves only between your own apps"}
def _private_ip(i):
    drv = get_drivers().get(i["provider"])
    if not hasattr(drv, "c"): return None
    try:
        nets = drv.c.containers.get(i["id"]).attrs["NetworkSettings"]["Networks"]
        for n, v in nets.items():
            if n.startswith("aloof-acct-"): return v.get("IPAddress")
    except Exception: return None
# ---------------- proxy: port-forward to a private port of an instance ----------------
@R.websocket("/v1/apps/{name}/proxy")
@__import__("paas.ws_registry", fromlist=["tracked"]).tracked
async def proxy_ws(ws: WebSocket, name: str, port: int, instance: str|None=None):
    """Binary frames = raw TCP bytes to <instance>:<port> on the app's private network (any port, e.g. a database or admin UI)."""
    api = _api(); a = api._ws_account(ws); ap = engine.get_app(name)
    if not a or not ap or ap["account_id"] != a["id"] or ap["status"] == "destroyed" or not api.scope_ok(name, a): await ws.close(code=4401); return
    i = q("select * from instances where app=? and status='running'" + (" and id=?" if instance else "") + " order by started", (name, *([instance] if instance else [])), one=True)
    if not i: await ws.close(code=4409, reason="no running instance"); return
    host = _private_ip(i); tport = port
    if not host:   # remote node: only the published app port is reachable from the control plane
        if port != int(load_spec(q("select spec from apps where name=?", (name,), one=True)["spec"]).get("port", 8080)): await ws.close(code=4409, reason="on edge nodes only the app's internal_port can be proxied"); return
        host, tport = i["host"], i["port"]
    try: reader, writer = await asyncio.wait_for(asyncio.open_connection(host, tport), 5)
    except Exception as e: await ws.close(code=4502, reason=f"connect failed: {type(e).__name__}"); return
    await ws.accept(); _audit(a["id"], name, "proxy.open", f"{i['id']}:{port}", ws.client.host if ws.client else None)
    async def up():
        while True:
            d = await reader.read(65536)
            if not d: break
            await ws.send_bytes(d)
        await ws.close()
    t = asyncio.create_task(up())
    from . import ws_registry; ws_registry.set_kill(ws, writer.close)
    try:
        while True:
            m = await ws.receive()
            if m["type"] == "websocket.disconnect": break
            d = m.get("bytes") or (m.get("text") or "").encode()
            writer.write(d); await writer.drain()
    except Exception: pass
    finally:
        t.cancel(); writer.close()
# ---------------- previews (any git host; GitHub does it automatically per PR) ----------------
class PreviewReq(BaseModel): pr: int|str; ref: str; git: str|None=None
@R.post("/v1/apps/{name}/previews")
def preview_create(name: str, b: PreviewReq, request: Request, a=Depends(acct)):
    own(name, a); spec = load_spec(q("select spec from apps where name=?", (name,), one=True)["spec"])
    git = b.git or (spec.get("source") or {}).get("git")
    if not git: raise HTTPException(400, "app was not deployed from git; pass git=")
    ps = github_app.preview_spec(name, b.pr, git, b.ref, (spec.get("source") or {}).get("github_installation"))
    try: r = engine.deploy(a["id"], ps)
    except (ValueError, PermissionError) as e: raise HTTPException(400, str(e))
    _audit(a["id"], name, "preview.deploy", f"{ps['name']} @ {b.ref}", _ip(request)); return _api().with_url(r)
@R.get("/v1/apps/{name}/previews")
def preview_list(name: str, a=Depends(acct)):
    own(name, a); out = []
    for c in engine.children(name):
        ap = engine.get_app(c)
        if load_spec(q("select spec from apps where name=?", (c,), one=True)["spec"]).get("_preview_of") == name: out.append({"name": c, "status": ap["status"], "url": _api().with_url(ap)["url"]})
    return out
@R.delete("/v1/apps/{name}/previews/{pr}")
def preview_delete(name: str, pr: str, a=Depends(acct)):
    own(name, a); pn = github_app.preview_name(name, pr)
    if pn not in engine.children(name): raise HTTPException(404, "no such preview")
    engine.destroy(pn); return {"ok": True}
# ---------------- cron ----------------
@R.get("/v1/apps/{name}/cron")
def cron_list(name: str, a=Depends(acct)):
    own(name, a); spec = load_spec(q("select spec from apps where name=?", (name,), one=True)["spec"])
    return {"jobs": spec.get("cron") or [], "runs": q("select job, ts, exit_code, substr(output, 1, 2000) as output from cron_runs where app=? order by id desc limit 50", (name,))}
@R.post("/v1/apps/{name}/cron/{job}/run")
def cron_run(name: str, job: str, request: Request, a=Depends(acct)):
    ap = own(name, a); spec = load_spec(q("select spec from apps where name=?", (name,), one=True)["spec"])
    j = next((j for j in spec.get("cron") or [] if j["name"] == job), None)
    if not j or not spec.get("_image"): raise HTTPException(404, "no such job (or app not built yet)")
    _audit(a["id"], name, "cron.run", job, _ip(request))
    code = cronmod.run_job(name, j, spec, get_drivers()[ap["provider"]])
    return {"exit_code": code, "output": (q("select output from cron_runs where app=? and job=? order by id desc", (name, job), one=True) or {}).get("output", "")[-4000:]}
# ---------------- GitHub App ----------------
def _page(title, body):
    from html import escape
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>{escape(title)}</title><link rel=stylesheet href=/static/aloof.css>"
                        f"<main style='max-width:720px;margin:4rem auto;font-family:system-ui;padding:0 1rem'><h1>{escape(title)}</h1>{body}</main>")
@R.post("/v1/admin/github/setup-link")
def gh_setup_link(request: Request):
    _api().admin(request.headers.get("authorization")); st = github_app.new_setup_state()
    return {"url": f"{_api().PUBLIC_API_URL}/github/setup?state={st}", "expires_in": 3600, "detail": "open in a browser signed in to the GitHub account/org that should own the app"}
@R.get("/github/setup", response_class=HTMLResponse)
def gh_setup(state: str, org: str|None=None, name: str="Aloof Deploy"):
    """One-click GitHub App creation (manifest flow). The form posts the manifest to GitHub; GitHub redirects back with a code."""
    from html import escape
    if not kv_get("gh_setup", state): return _page("Setup link expired", "<p>Ask an admin for a new link: <code>POST /v1/admin/github/setup-link</code>.</p>")
    api = _api(); m = github_app.manifest(api.PUBLIC_API_URL, os.environ.get("PAAS_SITE_URL", api.PUBLIC_API_URL), name)
    target = f"{github_app.WEB}/organizations/{escape(org)}/settings/apps/new" if org else f"{github_app.WEB}/settings/apps/new"
    return _page("Create the Aloof GitHub App", f"""<p>This creates a GitHub App owned by {'the organization <b>' + escape(org) + '</b>' if org else 'your GitHub user'} with
read access to code, write access to commit statuses/checks/PR comments, and push + pull_request webhooks pointed at {escape(api.PUBLIC_API_URL)}.</p>
<form action="{target}?state={escape(state)}" method=post><input type=hidden name=manifest value='{escape(json.dumps(m), quote=True)}'>
<button type=submit style='font-size:1.1rem;padding:.6rem 1.2rem'>Create GitHub App on GitHub</button></form>
<p style='color:#666'>Name taken? <a href="?state={escape(state)}&name=Aloof+Deploy+{secrets.token_hex(2)}{'&org=' + escape(org) if org else ''}">use another name</a>. For an org: add <code>&org=&lt;org&gt;</code>.</p>""")
@R.get("/github/manifest-callback", response_class=HTMLResponse)
def gh_manifest_cb(code: str, state: str):
    try: c = github_app.finish_manifest(code, state)
    except (PermissionError, RuntimeError) as e: return _page("GitHub App setup failed", f"<p>{e}</p>")
    return _page("GitHub App created", f"<p>App <b>{c['slug']}</b> (id {c['id']}) is configured; its private key and webhook secret are stored encrypted.</p>"
                 f"<p>Users connect it with <code>aloof github install</code> or from the dashboard.</p>")
@R.get("/v1/github/status")
def gh_status(a=Depends(acct)):
    c = github_app.config()
    return {"configured": github_app.configured(), "app": c.get("slug"), "installations": github_app.installs(a["id"]),
            "links": q("select app, repo, branch, previews from github_links where account_id=?", (a["id"],))}
@R.get("/v1/github/install")
def gh_install(a=Depends(acct)):
    _no_token(a, "GitHub installations")
    if not github_app.configured(): raise HTTPException(503, "GitHub App not set up on this server yet (admin: POST /v1/admin/github/setup-link); deploy keys and HTTPS tokens work meanwhile")
    return {"url": github_app.install_url(a["id"])}
@R.get("/github/installed", response_class=HTMLResponse)
def gh_installed(installation_id: int|None=None, code: str|None=None, state: str|None=None, setup_action: str|None=None):
    if not installation_id or not code: return _page("GitHub", "<p>Nothing to link. Start from <code>aloof github install</code>.</p>")
    try: r = github_app.finish_install(installation_id, code, state)
    except (PermissionError, RuntimeError) as e: return _page("Could not link installation", f"<p>{e}</p>")
    return _page("GitHub connected", f"<p>Installation on <b>{r['account']}</b> linked. Next: <code>aloof github link &lt;app&gt; {r['account']}/&lt;repo&gt;</code></p>")
@R.get("/v1/github/repos")
def gh_repos(a=Depends(acct)):
    try: return github_app.repos(a["id"])
    except RuntimeError as e: raise HTTPException(502, str(e))
class LinkReq(BaseModel): repo: str; branch: str|None=None; previews: bool=True; deploy: bool=True
@R.post("/v1/apps/{name}/github")
def gh_link(name: str, b: LinkReq, request: Request, a=Depends(acct)):
    _claim_name(name, a)
    try: l = github_app.link(a["id"], name, b.repo, b.branch, b.previews)
    except ValueError as e: raise HTTPException(400, str(e))
    except RuntimeError as e: raise HTTPException(502, str(e))
    _audit(a["id"], name, "github.link", f"{l['repo']}@{l['branch']}", _ip(request)); out = {"linked": l}
    if b.deploy:
        ex = engine.get_app(name); spec = {"name": name, "source": {"git": f"{github_app.WEB}/{l['repo']}.git", "ref": l["branch"], "github_installation": l["installation_id"]}}
        try: out["app"] = _api().with_url(engine.deploy(a["id"], spec))
        except (ValueError, PermissionError) as e: raise HTTPException(400, str(e))
    return out
@R.delete("/v1/apps/{name}/github")
def gh_unlink(name: str, a=Depends(acct)): own(name, a); q("delete from github_links where app=? and account_id=?", (name, a["id"])); return {"ok": True}
@R.post("/v1/github/webhook")
async def gh_webhook(request: Request):
    body = await request.body()
    if not github_app.verify_signature(body, request.headers.get("x-hub-signature-256", "")): raise HTTPException(401, "bad signature")
    did = request.headers.get("x-github-delivery")
    if did:
        if kv_get("gh_delivery", did): return {"ok": True, "duplicate": True}
        kv_put("gh_delivery", did, "1", ttl=86400 * 3)
    ev = request.headers.get("x-github-event", ""); payload = json.loads(body or b"{}")
    if ev == "ping": return {"ok": True, "pong": True}
    url_of = lambda n: f"https://{n}.{_api().URL_DOMAIN}/"
    r = await asyncio.to_thread(github_app.handle, ev, payload, url_of)
    return {"ok": True, "result": r}
