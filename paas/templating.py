"""One-click templates. A template is paas/templates/<id>.toml (format: aloof-template v1, see SCHEMA / docs below).

  id, name, description, category?, website?
  [source]   image = "..."  |  repo = "https://github.com/<owner>/<repo>" (+ ref?, subdir?);  command? = [...]
  [app]      port, plan (default plan), region (only live regions; today nbg1)
  [[env]]    name + one of: value (supports ${APP_URL} / ${APP_NAME}) | generate = "secret:<n>" | "hex:<n>" | required = true (user supplies)
  [[volumes]] name (suffix; volume is <app>-<name>), path, size_gb
  [postgres] plan (pg-*), var (env var for the URL, default DATABASE_URL); optional [postgres.env] VAR = "url|host|port|database|user|password"
             for apps that take the connection parts separately (then var is only set if given explicitly)

Single service per template for now (a `services` table is rejected). Deploy-button URLs: https://www.aloof.si/deploy?template=<id>
or ?repo=<https github/gitlab url>; they go through login/signup and an explicit confirm click (no deploy on GET)."""
import os, re, json, secrets, tomllib, functools, html, urllib.parse
from fastapi import APIRouter, Request, HTTPException, Header
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from pydantic import BaseModel
from .db import q
from .plans import PLANS, PG_PLANS

DIR = os.path.join(os.path.dirname(__file__), "templates")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
LIVE_REGIONS = {"nbg1": "nbg1", "eu-de": "nbg1", "eu-central": "nbg1"}
REPO_HOSTS = {"github.com", "gitlab.com"}
SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "https://api.aloof.si/v1/templates/schema", "title": "aloof-template v1",
    "type": "object", "required": ["id", "name", "description", "source", "app"], "additionalProperties": False,
    "properties": {
        "id": {"type": "string", "pattern": ID_RE.pattern}, "name": {"type": "string"}, "description": {"type": "string"},
        "category": {"type": "string"}, "website": {"type": "string", "format": "uri"},
        "source": {"type": "object", "additionalProperties": False, "properties": {"image": {"type": "string"}, "repo": {"type": "string"},
                   "ref": {"type": "string"}, "subdir": {"type": "string"}, "command": {"type": "array", "items": {"type": "string"}}},
                   "oneOf": [{"required": ["image"]}, {"required": ["repo"]}]},
        "app": {"type": "object", "required": ["port"], "properties": {"port": {"type": "integer"}, "plan": {"type": "string"}, "region": {"type": "string"}}},
        "env": {"type": "array", "items": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}, "value": {"type": "string"},
                "generate": {"type": "string", "pattern": "^(secret|hex):[0-9]+$"}, "required": {"type": "boolean"}, "description": {"type": "string"}}}},
        "volumes": {"type": "array", "items": {"type": "object", "required": ["name", "path"], "properties": {"name": {"type": "string"}, "path": {"type": "string"}, "size_gb": {"type": "integer"}}}},
        "postgres": {"type": "object", "properties": {"plan": {"type": "string"}, "var": {"type": "string"},
                     "env": {"type": "object", "additionalProperties": {"enum": ["url", "host", "port", "database", "user", "password"]}}}},
    }}

class TemplateError(ValueError): pass
PG_FIELDS = ("url", "host", "port", "database", "user", "password")
def pg_vars(pgt):
    """{env var: field} for a template's [postgres] table."""
    m = dict(pgt.get("env") or {})
    if "var" in pgt or not m: m[pgt.get("var", "DATABASE_URL")] = "url"
    return m

def check_repo(url):
    """SSRF-safe repo URL check: https only, host exactly github.com or gitlab.com, no credentials/port/query/fragment,
    a plain <owner>/<repo>[.git] path (GitLab: up to 3 group levels). Purely syntactic: no request is made here."""
    if not isinstance(url, str) or len(url) > 300 or any(c in url for c in " \t\r\n\\\x00"): raise TemplateError("repo must be an https URL to a public GitHub or GitLab repository")
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https": raise TemplateError("repo URL must use https://")
    if u.username or u.password or "@" in u.netloc: raise TemplateError("repo URL must not contain credentials")
    if u.port is not None or u.netloc.lower() != (u.hostname or "") or u.hostname not in REPO_HOSTS:
        raise TemplateError("repo must be on github.com or gitlab.com (no other hosts, IPs or ports)")
    if u.query or u.fragment: raise TemplateError("repo URL must not have a query or fragment")
    parts = [p for p in u.path.split("/") if p]
    seg = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$")   # no leading '-' (never an option-looking segment)
    maxp = 2 if u.hostname == "github.com" else 4
    if not (2 <= len(parts) <= maxp) or not all(seg.match(p) and p not in (".", "..") for p in parts): raise TemplateError("repo URL must look like https://github.com/<owner>/<repo>")
    return f"https://{u.hostname}/" + "/".join(parts)

def validate(t, fname=None):
    for k in ("id", "name", "description", "source", "app"):
        if k not in t: raise TemplateError(f"{fname or 'template'}: missing {k}")
    extra = set(t) - set(SCHEMA["properties"])
    if extra: raise TemplateError(f"{t['id']}: unknown keys {sorted(extra)}" + (" (multi-service templates are not supported yet)" if "services" in extra else ""))
    if not ID_RE.match(t["id"]): raise TemplateError(f"bad id {t['id']}")
    if fname and fname != t["id"]: raise TemplateError(f"{fname}.toml must have id = {fname}")
    s = t["source"]
    if bool(s.get("image")) == bool(s.get("repo")): raise TemplateError(f"{t['id']}: source needs exactly one of image or repo")
    if s.get("repo"): check_repo(s["repo"])
    a = t["app"]
    if not isinstance(a.get("port"), int) or not 1 <= a["port"] <= 65535: raise TemplateError(f"{t['id']}: app.port")
    if a.get("plan", "shared-1x-512mb") not in PLANS: raise TemplateError(f"{t['id']}: unknown plan {a.get('plan')}")
    if a.get("region", "nbg1") not in LIVE_REGIONS: raise TemplateError(f"{t['id']}: region {a.get('region')} is not live")
    names = set()
    for e in t.get("env", []):
        if not ENV_RE.match(e.get("name", "")) or e["name"] in names: raise TemplateError(f"{t['id']}: bad/duplicate env {e.get('name')}")
        names.add(e["name"])
        if sum(k in e for k in ("value", "generate")) > 1: raise TemplateError(f"{t['id']}: env {e['name']} has both value and generate")
        if "generate" in e and not re.fullmatch(r"(secret|hex):([1-9][0-9]?)", e["generate"]): raise TemplateError(f"{t['id']}: env {e['name']} generate")
        if not ("value" in e or "generate" in e) and e.get("required") is False: raise TemplateError(f"{t['id']}: env {e['name']} needs value, generate or required")
    for v in t.get("volumes", []):
        if not re.fullmatch(r"[a-z0-9-]{1,20}", v.get("name", "")) or not str(v.get("path", "")).startswith("/"): raise TemplateError(f"{t['id']}: bad volume")
    if "postgres" in t:
        if t["postgres"].get("plan", "pg-dev") not in PG_PLANS: raise TemplateError(f"{t['id']}: postgres plan")
        if not ENV_RE.match(t["postgres"].get("var", "DATABASE_URL")): raise TemplateError(f"{t['id']}: postgres var")
        for k, v in (t["postgres"].get("env") or {}).items():
            if not ENV_RE.match(k) or v not in PG_FIELDS: raise TemplateError(f"{t['id']}: postgres.env {k}={v}")
    return t

@functools.lru_cache(maxsize=1)
def registry():
    out = {}
    for f in sorted(os.listdir(DIR)):
        if f.endswith(".toml"):
            with open(os.path.join(DIR, f), "rb") as fh: t = tomllib.load(fh)
            out[t["id"]] = validate(t, f[:-5])
    return out

def get(tid):
    t = registry().get(tid)
    if not t: raise KeyError(tid)
    return t

def public(t):
    """Machine-readable view. Never includes generated values."""
    a = t["app"]
    return {"id": t["id"], "name": t["name"], "description": t["description"], "category": t.get("category"), "website": t.get("website"),
            "source": {k: v for k, v in t["source"].items() if k in ("image", "repo", "ref", "subdir")}, "port": a["port"],
            "default_plan": a.get("plan", "shared-1x-512mb"), "region": LIVE_REGIONS[a.get("region", "nbg1")],
            "env": [{"name": e["name"], "kind": "generated" if "generate" in e else ("default" if "value" in e else "required"),
                     **({"default": e["value"]} if "value" in e else {}), "description": e.get("description", "")} for e in t.get("env", [])],
            "required_env": [e["name"] for e in t.get("env", []) if not ("value" in e or "generate" in e)],
            "volumes": [{"name": v["name"], "path": v["path"], "size_gb": v.get("size_gb", 1)} for v in t.get("volumes", [])],
            "postgres": ({"plan": t["postgres"].get("plan", "pg-dev"), "env": pg_vars(t["postgres"])} if "postgres" in t else None),
            "page": f"https://www.aloof.si/templates/{t['id']}", "deploy_button": f"https://www.aloof.si/deploy?template={t['id']}",
            "api": f"POST /v1/templates/{t['id']}/deploy"}

def _gen(g):
    kind, n = g.split(":"); n = int(n)
    return secrets.token_hex(n) if kind == "hex" else secrets.token_urlsafe(n)

REF_RE = re.compile(r"^[\w./@^~+-]+$")   # same rule as creds.clone
def repo_template(repo, ref=None, port=8080, plan=None):
    if ref is not None and (not REF_RE.fullmatch(ref) or ref.startswith("-") or ".." in ref or len(ref) > 200): raise TemplateError("bad git ref")
    return validate({"id": "from-repo", "name": "Repository", "description": repo, "source": {"repo": check_repo(repo), **({"ref": ref} if ref else {})},
                     "app": {"port": int(port or 8080), "plan": plan or "shared-1x-512mb"}})

def deploy(account_id, t, name, env=None, plan=None, url_domain=None):
    """Create volumes + Postgres the template needs, then deploy the app. Returns app record plus the generated secrets (once)."""
    from . import engine, parity
    env = dict(env or {}); plan = plan or t["app"].get("plan", "shared-1x-512mb")
    if plan not in PLANS: raise TemplateError(f"unknown plan {plan}")
    if not engine.NAME_RE.match(name or "") or name.startswith("pg-") or len(name) > 23:
        raise TemplateError("app name must be 3-23 chars: lowercase letters, digits, hyphens; starts with a letter")
    if q("select 1 from apps where name=? and status!='destroyed'", (name,), one=True): raise PermissionError("app name taken")
    for k in env:
        if not ENV_RE.match(k): raise TemplateError(f"bad env var name {k}")
    app_url = f"https://{name}.{url_domain}/" if url_domain else ""
    out_env, generated = {}, {}
    for e in t.get("env", []):
        k = e["name"]
        if k in env: out_env[k] = env.pop(k)
        elif "generate" in e: out_env[k] = generated[k] = _gen(e["generate"])
        elif "value" in e: out_env[k] = e["value"].replace("${APP_URL}", app_url.rstrip("/")).replace("${APP_NAME}", name)
        elif e.get("required", True): raise TemplateError(f"missing required env {k}" + (f" ({e['description']})" if e.get("description") else ""))
    out_env.update(env)
    s = t["source"]
    src = {"image": s["image"]} if s.get("image") else {"git": s["repo"], **({"ref": s["ref"]} if s.get("ref") else {}), **({"subdir": s["subdir"]} if s.get("subdir") else {})}
    spec = {"name": name, "source": src, "plan": plan, "port": t["app"]["port"], "env": out_env, "instances": 1, "template": t["id"]}
    if s.get("command"): spec["command"] = s["command"]
    # F4: plan/tier/quota/image checks before anything billable exists; roll back whatever we created if the deploy still fails
    import copy
    pre = copy.deepcopy(spec); pre_vols = [{"name": f"{name}-{v['name']}"[:32].rstrip("-"), "size_gb": v.get("size_gb", 1)} for v in t.get("volumes", [])]
    pre = engine.validate(pre); engine._check_platform_image(account_id, pre); engine.apply_tier_limits(account_id, pre, redeploy=False)
    for v in pre_vols:
        if q("select 1 from volumes where name=?", (v["name"],), one=True): raise PermissionError("volume exists")
    if "postgres" in t and q("select 1 from pg_dbs where name=?", (f"{name}-db",), one=True): raise PermissionError("postgres name taken")
    created_vols, created_pg = [], None
    try:
        vols = []
        for v in t.get("volumes", []):
            vn = f"{name}-{v['name']}"[:32].rstrip("-"); engine.create_volume(account_id, vn, v.get("size_gb", 1)); created_vols.append(vn)
            q("update volumes set app=?, mount_path=? where name=?", (name, v["path"], vn)); vols.append({"name": vn, "path": v["path"]})
        if vols: spec["volumes"] = vols
        pg = None
        if "postgres" in t:
            pgn = f"{name}-db"; vmap = pg_vars(t["postgres"]); var = next(iter(vmap))
            pg = parity.pg_create(account_id, pgn, t["postgres"].get("plan", "pg-dev")); created_pg = pgn
            row = q("select * from pg_dbs where name=?", (pgn,), one=True)
            url = parity.pg_url(row); u = urllib.parse.urlsplit(url)
            parts = {"url": url, "host": u.hostname, "port": str(u.port or 5432), "database": u.path.lstrip("/"),
                     "user": urllib.parse.unquote(u.username or ""), "password": urllib.parse.unquote(u.password or "")}
            spec["networks"] = [parity.pg_net(pgn)]
            for k, f in vmap.items(): spec["env"][k] = parts[f]
            q("update pg_dbs set attached=? where name=?", (json.dumps([{"app": name, "var": var}]), pgn))
        r = engine.deploy(account_id, spec)
    except Exception:
        if created_pg:
            try: parity.pg_destroy(created_pg)
            except Exception: q("delete from pg_dbs where name=?", (created_pg,))
        for vn in created_vols:
            try: q("update volumes set app=null, mount_path=null where name=?", (vn,)); engine.delete_volume(vn)
            except Exception: pass
        raise
    from .db import audit as _audit   # F5
    _audit(account_id, name, "template.deploy" if t["id"] != "from-repo" else "repo.deploy", json.dumps({"template": t["id"], "source": src, "plan": plan}))
    return {"app": r, "template": t["id"], "generated_secrets": generated, "volumes": vols, "postgres": pg and pg["name"],
            "note": "generated secrets are shown once here and stored in the app's env"}

# ---------------- HTTP ----------------
R = APIRouter()
class TplDeploy(BaseModel):
    name: str; env: dict[str, str] = {}; plan: str | None = None
class RepoDeploy(BaseModel):
    name: str; repo: str; ref: str | None = None; port: int = 8080; plan: str | None = None; env: dict[str, str] = {}

def _acct(request):
    """F2: these endpoints create new apps, volumes and billable Postgres, so deploy tokens (dt_..., app-scoped or account-wide) are refused."""
    from . import api
    authz = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    if authz.startswith("dt_"): raise HTTPException(403, "deploy tokens can't create apps from templates or repos")
    a = api.acct(request, request.headers.get("authorization"))
    if a.get("_token") or a.get("_scope_app"): raise HTTPException(403, "deploy tokens can't create apps from templates or repos")
    return a

def _run(a, t, b):
    from . import api
    try: r = deploy(a["id"], t, b.name, b.env, b.plan, api.URL_DOMAIN); api.with_url(r["app"]); return r
    except PermissionError as e: raise HTTPException(409, str(e))
    except ValueError as e: raise HTTPException(400, str(e))
    except HTTPException: raise
    except Exception:   # rollback already ran inside deploy(); return a clean JSON error, never a bare 500 page
        import logging; logging.getLogger("paas.templates").exception("template deploy %s failed", t.get("id"))
        raise HTTPException(503, "deploy could not be started; nothing was created. Try again, or contact support if it persists")

@R.get("/v1/templates")
def list_templates(): return {"version": 1, "schema": "/v1/templates/schema", "templates": [public(t) for t in registry().values()]}
@R.get("/v1/templates/schema")
def template_schema(): return SCHEMA
@R.get("/v1/templates/{tid}")
def get_template(tid: str):
    try: return public(get(tid))
    except KeyError: raise HTTPException(404, "template not found")
@R.post("/v1/templates/{tid}/deploy")
def deploy_template(tid: str, b: TplDeploy, request: Request):
    a = _acct(request)
    try: t = get(tid)
    except KeyError: raise HTTPException(404, "template not found")
    return _run(a, t, b)
@R.post("/v1/deploy/repo")
def deploy_repo(b: RepoDeploy, request: Request):
    a = _acct(request)
    try: t = repo_template(b.repo, b.ref, b.port, b.plan)
    except ValueError as e: raise HTTPException(400, str(e))
    return _run(a, t, b)
@R.get("/llms-templates.md", response_class=PlainTextResponse)
def llms_templates():
    lines = ["# Aloof one-click templates", "", "Deploy: POST https://api.aloof.si/v1/templates/<id>/deploy {\"name\": \"<app>\"} or MCP deploy_template, or `aloof deploy --template <id>`.", ""]
    for t in registry().values(): lines.append(f"- [{t['name']}](https://www.aloof.si/templates/{t['id']}) (`{t['id']}`): {t['description']}")
    return "\n".join(lines) + "\n"

# Template logos: committed SVGs in paas/static/logos (sources and licenses in LICENSES.md), inlined so they stay crisp.
LOGO_DIR = os.path.join(os.path.dirname(__file__), "static", "logos")
LOGO_COLOR = {"n8n": "#ea4b71", "uptime-kuma": "#5cdd8b", "ntfy": "#3fa58f", "pocketbase": "#b8dbe4"}
@functools.lru_cache(maxsize=64)
def _logo_svg(f): return re.sub(r"<title>.*?</title>", "", open(f).read().strip())
def tpl_logo(tid, name=None, size=""):
    """Uniform Ember tile with the project's logo; amber monogram when no logo file exists."""
    f = os.path.join(LOGO_DIR, re.sub(r"[^a-z0-9-]", "", tid) + ".svg")
    cls = f"tlogo {size}".strip()
    if os.path.isfile(f):
        svg, c = _logo_svg(f), LOGO_COLOR.get(tid, "var(--fg)")
        return f"<span class='{cls}' style='color:{c}' aria-hidden=true>{svg}</span>"
    words = re.split(r"[\s_-]+", (name or tid).strip()) or ["?"]
    mono = (words[0][:1] + (words[1][:1] if len(words) > 1 else words[0][1:2])).upper() if words[0] else "?"
    return f"<span class='{cls} tlogo-mono' aria-hidden=true>{html.escape(mono)}</span>"

def _page(path, title, desc, inner):
    from . import www
    return www.render(path, title, desc, www.shell("Templates", html.escape(title), html.escape(desc), inner, narrow=True))

@R.get("/templates", response_class=HTMLResponse)
def templates_page():
    cards = "".join(f"<a class=tcard href='/templates/{t['id']}'>{tpl_logo(t['id'], t['name'])}<div><h3>{html.escape(t['name'])}</h3><p>{html.escape(t['description'])}</p></div></a>" for t in registry().values())
    return _page("/templates", "One-click templates", "Deploy open-source apps on Aloof in one click. Machine-readable: /v1/templates.", f"<div class=tgrid>{cards}</div>")

@R.get("/templates/{tid}", response_class=HTMLResponse)
def template_page(tid: str):
    try: p = public(get(tid))
    except KeyError: raise HTTPException(404, "template not found")
    e = lambda x: html.escape(str(x))
    envs = "".join(f"<li><code>{e(x['name'])}</code> · {e(x['kind'])}{' · ' + e(x['description']) if x['description'] else ''}</li>" for x in p["env"]) or "<li>none</li>"
    vols = "".join(f"<li>{e(v['size_gb'])} GB at <code>{e(v['path'])}</code></li>" for v in p["volumes"]) or "<li>none</li>"
    src = p["source"].get("image") or p["source"].get("repo")
    inner = (f"<p class=thead>{tpl_logo(p['id'], p['name'], 'lg')}<a class='btn' href='{e(p['deploy_button'])}'>Deploy on Aloof</a></p><h2>What gets created</h2><ul><li>App from <code>{e(src)}</code> on port {p['port']}, plan <code>{e(p['default_plan'])}</code>, region {e(p['region'])}</li>"
             f"{'<li>Managed Postgres (' + e(p['postgres']['plan']) + '), connection in <code>' + e(', '.join(p['postgres']['env'])) + '</code></li>' if p['postgres'] else ''}</ul>"
             f"<h3>Environment</h3><ul>{envs}</ul><h3>Volumes</h3><ul>{vols}</ul>"
             f"<h3>CLI / API / MCP</h3><pre>aloof deploy --template {e(p['id'])} &lt;app-name&gt;\ncurl -X POST https://api.aloof.si/v1/templates/{e(p['id'])}/deploy -H 'Authorization: Bearer $ALOOF_API_KEY' -d '{{\"name\":\"my-{e(p['id'])}\"}}'\nMCP: deploy_template(template=\"{e(p['id'])}\", name=...)</pre>"
             f"<p class=muted>Button markdown: <code>[![Deploy on Aloof](https://www.aloof.si/button.svg)]({e(p['deploy_button'])})</code></p>")
    return _page(f"/templates/{tid}", f"Deploy {p['name']} on Aloof", p["description"], inner)

@R.get("/deploy", response_class=HTMLResponse)
def deploy_button(request: Request, template: str | None = None, repo: str | None = None):
    """Deploy-button landing. Logged-out visitors go to /login?next=<this URL> (signup is linked there). Logged in: a confirm form;
    the deploy only happens on an explicit click (POST with the CSRF header), never on GET."""
    from . import auth
    if bool(template) == bool(repo): raise HTTPException(400, "use ?template=<id> or ?repo=<https github/gitlab url>")
    try:
        if template: p = public(get(template))
        else: repo = check_repo(repo); p = None
    except KeyError: raise HTTPException(404, "template not found")
    except ValueError as x: raise HTTPException(400, str(x))
    if not auth.session_account(request.cookies.get(auth.COOKIE)):
        here = "/deploy?" + urllib.parse.urlencode({"template": template} if template else {"repo": repo})
        return RedirectResponse("/login?next=" + urllib.parse.quote(auth.safe_next(here, "/templates"), safe=""), 303)
    e = lambda x: html.escape(str(x), quote=True)
    req = [x for x in (p["env"] if p else []) if x["kind"] == "required"]
    fields = "".join(f"<label>{e(x['name'])}<input name='env_{e(x['name'])}' required></label>" for x in req)
    ep = f"/v1/templates/{e(template)}/deploy" if p else "/v1/deploy/repo"
    extra = "" if p else "<label>Port<input name=port type=number value=8080></label>"
    title = f"Deploy {p['name']}" if p else "Deploy repository"
    inner = f"""<p>{e(p['description']) if p else 'From <code>' + e(repo) + '</code>'}</p>
<form id=f class=card><label>App name<input name=name required pattern='(?!pg-)[a-z][a-z0-9\-]{{1,21}}[a-z0-9]' minlength=3 maxlength=23 title='3-23 chars: lowercase letters, digits, hyphens; starts with a letter'></label>{fields}{extra}<button class=btn>Deploy</button></form><pre id=o></pre>
<script>document.getElementById('f').onsubmit=async function(ev){{ev.preventDefault();var fd=new FormData(this),b={{name:fd.get('name'),env:{{}}}};
for(var [k,v] of fd.entries()){{if(k.indexOf('env_')==0)b.env[k.slice(4)]=v}}{'' if p else "b.repo=" + json.dumps(repo) + ";b.port=+fd.get('port');"}
var r=await fetch({json.dumps(ep)},{{method:'POST',credentials:'same-origin',headers:{{'Content-Type':'application/json','X-Aloof-CSRF':'1'}},body:JSON.stringify(b)}});
var j=await r.json();document.getElementById('o').textContent=r.ok?('Deploying '+j.app.name+' at '+j.app.url+(Object.keys(j.generated_secrets||{{}}).length?'\\nSave these generated secrets now: '+JSON.stringify(j.generated_secrets,null,1):'')):('Error: '+(j.detail||r.status));
if(r.ok)setTimeout(function(){{location='/dashboard'}},8000)}}</script>"""
    return _page("/deploy", title, "Confirm to deploy on Aloof.", inner)

BUTTON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="164" height="32" role="img" aria-label="Deploy on Aloof"><rect width="164" height="32" rx="6" fill="#111"/>'
              '<text x="82" y="21" fill="#fff" font-family="system-ui,sans-serif" font-size="14" font-weight="600" text-anchor="middle">Deploy on Aloof</text></svg>')
@R.get("/button.svg")
def button_svg():
    from fastapi import Response
    return Response(BUTTON_SVG, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})
