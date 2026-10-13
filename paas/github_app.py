"""GitHub App integration: one-click creation via the manifest flow, per-account installation (verified with the installing user's
OAuth token, so nobody can claim someone else's installation_id), repo listing, clone with short-lived installation tokens,
push-to-deploy and per-PR preview apps from signed webhooks, and check runs + commit statuses reporting each deploy.
Config comes from the manifest conversion (stored encrypted in kv) or env: GITHUB_APP_ID, GITHUB_APP_SLUG, GITHUB_APP_PRIVATE_KEY,
GITHUB_WEBHOOK_SECRET, GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET."""
import os, json, time, hmac, hashlib, base64, secrets, threading, logging, httpx
from .db import q, kv_get, kv_put, kv_del, event
from . import crypto
log = logging.getLogger("paas.github")
API = os.environ.get("GITHUB_API_URL", "https://api.github.com"); WEB = os.environ.get("GITHUB_WEB_URL", "https://github.com")
def _client(): return httpx.Client(timeout=30)
def config():
    raw = kv_get("github_app", "config")
    c = crypto.dec_json(raw) if raw else {}
    env = {"id": os.environ.get("GITHUB_APP_ID"), "slug": os.environ.get("GITHUB_APP_SLUG"), "pem": os.environ.get("GITHUB_APP_PRIVATE_KEY"),
           "webhook_secret": os.environ.get("GITHUB_WEBHOOK_SECRET"), "client_id": os.environ.get("GITHUB_CLIENT_ID"), "client_secret": os.environ.get("GITHUB_CLIENT_SECRET")}
    return {**c, **{k: v for k, v in env.items() if v}}
def configured(): c = config(); return bool(c.get("id") and c.get("pem") and c.get("webhook_secret"))
def manifest(public_api, site, name="Aloof Deploy"):
    return {"name": name, "url": site, "description": "Deploy GitHub repos to Aloof: push-to-deploy, PR previews, deploy checks.",
            "hook_attributes": {"url": f"{public_api}/v1/github/webhook", "active": True},
            "redirect_url": f"{public_api}/github/manifest-callback", "callback_urls": [f"{public_api}/github/installed"],
            "setup_url": f"{public_api}/github/installed", "setup_on_update": True, "request_oauth_on_install": True, "public": True,
            "default_permissions": {"contents": "read", "metadata": "read", "statuses": "write", "checks": "write", "pull_requests": "write", "deployments": "write"},
            "default_events": ["push", "pull_request"]}
def new_setup_state(): s = secrets.token_urlsafe(24); kv_put("gh_setup", s, "1", ttl=3600); return s
def finish_manifest(code, state):
    if not state or not kv_get("gh_setup", state): raise PermissionError("setup link expired or invalid; create a new one")
    kv_del("gh_setup", state)
    with _client() as c: r = c.post(f"{API}/app-manifests/{code}/conversions", headers={"Accept": "application/vnd.github+json"})
    if r.status_code >= 300: raise RuntimeError(f"GitHub manifest conversion failed: {r.status_code} {r.text[:200]}")
    d = r.json()
    cfg = {"id": str(d["id"]), "slug": d["slug"], "pem": d["pem"], "webhook_secret": d["webhook_secret"], "client_id": d["client_id"],
           "client_secret": d["client_secret"], "html_url": d.get("html_url"), "owner": (d.get("owner") or {}).get("login"), "created": time.time()}
    kv_put("github_app", "config", crypto.enc_json(cfg)); return cfg
def _b64(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def app_jwt():
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    c = config(); now = int(time.time())
    head = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()); body = _b64(json.dumps({"iat": now - 60, "exp": now + 540, "iss": c["id"]}).encode())
    key = serialization.load_pem_private_key(c["pem"].encode(), None)
    sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{_b64(sig)}"
_tok_cache = {}
def installation_token(iid):
    t = _tok_cache.get(iid)
    if t and t[1] > time.time() + 300: return t[0]
    with _client() as c:
        r = c.post(f"{API}/app/installations/{iid}/access_tokens", headers={"Authorization": f"Bearer {app_jwt()}", "Accept": "application/vnd.github+json"})
    if r.status_code >= 300: raise RuntimeError(f"installation token: {r.status_code} {r.text[:200]}")
    d = r.json(); exp = time.time() + 3000
    _tok_cache[iid] = (d["token"], exp); return d["token"]
def _gh(method, path, iid, **kw):
    with _client() as c:
        r = c.request(method, f"{API}{path}", headers={"Authorization": f"token {installation_token(iid)}", "Accept": "application/vnd.github+json"}, **kw)
    if r.status_code >= 300: raise RuntimeError(f"GitHub {method} {path}: {r.status_code} {r.text[:200]}")
    return r.json() if r.content else {}
# ---- install / link (OAuth-verified) ----
def install_url(account_id):
    c = config(); st = secrets.token_urlsafe(24); kv_put("gh_install", st, str(account_id), ttl=3600)
    return f"{WEB}/apps/{c['slug']}/installations/new?state={st}"
def finish_install(installation_id, code, state):
    account_id = kv_get("gh_install", state or "")
    if not account_id: raise PermissionError("install link expired; run `aloof github install` again")
    c = config()
    with _client() as h:
        r = h.post(f"{WEB}/login/oauth/access_token", data={"client_id": c["client_id"], "client_secret": c["client_secret"], "code": code}, headers={"Accept": "application/json"})
        ut = (r.json() if r.status_code < 300 else {}).get("access_token")
        if not ut: raise PermissionError("GitHub did not confirm the installing user")
        r = h.get(f"{API}/user/installations", headers={"Authorization": f"token {ut}", "Accept": "application/vnd.github+json"})
    insts = {int(i["id"]): i for i in (r.json().get("installations") or [])} if r.status_code < 300 else {}
    iid = int(installation_id)
    if iid not in insts: raise PermissionError("that installation is not accessible to the GitHub user who completed the flow")
    kv_del("gh_install", state)
    q("insert into github_installs(installation_id,account_id,login,created) values(?,?,?,?) on conflict(installation_id) do update set account_id=excluded.account_id, login=excluded.login",
      (iid, int(account_id), insts[iid]["account"]["login"], time.time()))
    return {"installation_id": iid, "account": insts[iid]["account"]["login"]}
def installs(account_id): return q("select installation_id, login, created from github_installs where account_id=?", (account_id,))
def owns_installation(account_id, iid): return bool(q("select 1 from github_installs where installation_id=? and account_id=?", (int(iid), account_id), one=True))
def repos(account_id):
    out = []
    for i in installs(account_id):
        page = 1
        while True:
            d = _gh("GET", f"/installation/repositories?per_page=100&page={page}", i["installation_id"])
            out += [{"repo": r["full_name"], "private": r["private"], "default_branch": r.get("default_branch"), "installation_id": i["installation_id"]} for r in d.get("repositories", [])]
            if len(d.get("repositories", [])) < 100: break
            page += 1
    return out
def link(account_id, app, repo, branch=None, previews=True):
    for r in repos(account_id):
        if r["repo"].lower() == repo.lower():
            q("insert into github_links(app,account_id,installation_id,repo,branch,previews,created) values(?,?,?,?,?,?,?) on conflict(app) do update set "
              "installation_id=excluded.installation_id, repo=excluded.repo, branch=excluded.branch, previews=excluded.previews",
              (app, account_id, r["installation_id"], r["repo"], branch or r["default_branch"] or "main", int(bool(previews)), time.time()))
            return q("select * from github_links where app=?", (app,), one=True)
    raise ValueError(f"{repo} is not in any of your GitHub App installations; grant the app access to it on GitHub")
# ---- webhooks ----
def verify_signature(body: bytes, sig: str):
    secret = config().get("webhook_secret")
    if not secret or not sig or not sig.startswith("sha256="): return False
    return hmac.compare_digest("sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(), sig)
def _report(iid, repo, sha, state, app, url, desc, check_id=None):
    """Commit status + check run (both visible on the commit / PR)."""
    try: _gh("POST", f"/repos/{repo}/statuses/{sha}", iid, json={"state": state, "target_url": url, "description": desc[:140], "context": f"aloof/{app}"})
    except Exception as e: log.warning("status: %s", e)
    try:
        if check_id is None:
            return _gh("POST", f"/repos/{repo}/check-runs", iid, json={"name": f"aloof deploy ({app})", "head_sha": sha, "status": "in_progress", "details_url": url,
                                                                       "output": {"title": desc, "summary": desc}}).get("id")
        _gh("PATCH", f"/repos/{repo}/check-runs/{check_id}", iid, json={"status": "completed", "conclusion": "success" if state == "success" else "failure",
                                                                       "output": {"title": desc, "summary": desc}})
    except Exception as e: log.warning("check run: %s", e)
def _deploy_and_report(account_id, spec, iid, repo, sha, url_of, pr=None):
    from . import engine
    name = spec["name"]; url = url_of(name)
    check = _report(iid, repo, sha, "pending", name, url, f"deploying {sha[:7]}")
    try: engine.deploy(account_id, spec)
    except Exception as e: _report(iid, repo, sha, "failure", name, url, f"deploy rejected: {e}", check); return
    def watch():
        end = time.time() + 3600
        while time.time() < end:
            time.sleep(3); a = q("select status, error from apps where name=?", (name,), one=True) or {}
            rel = q("select status from releases where app=? order by version desc", (name,), one=True) or {}
            if rel.get("status") == "active" and a.get("status") in ("running", "idle"):
                _report(iid, repo, sha, "success", name, url, f"live at {url}", check)
                if pr and not kv_get("gh_pr_comment", f"{repo}#{pr}"):
                    try: _gh("POST", f"/repos/{repo}/issues/{pr}/comments", iid, json={"body": f"Preview deployed: {url} (app `{name}`, commit {sha[:7]})"}); kv_put("gh_pr_comment", f"{repo}#{pr}", "1")
                    except Exception as e: log.warning("pr comment: %s", e)
                return
            if rel.get("status") == "failed" or a.get("status") == "failed":
                _report(iid, repo, sha, "failure", name, url, (a.get("error") or "deploy failed")[:140], check); return
        _report(iid, repo, sha, "failure", name, url, "deploy timed out", check)
    threading.Thread(target=watch, daemon=True).start()
def preview_name(app, pr):
    n = f"{app}-pr-{pr}"
    if len(n) > 32: n = f"{app[:32 - len(str(pr)) - 4].rstrip('-')}-pr-{pr}"
    return n
def preview_spec(app, n, repo_url, ref, iid=None, copy_env=True):
    from .crypto import load_spec
    base = load_spec(q("select spec from apps where name=?", (app,), one=True)["spec"])
    src = {"git": repo_url, "ref": ref, **({"github_installation": iid} if iid else {}), **{k: v for k, v in (base.get("source") or {}).items() if k in ("subdir", "dockerfile", "build_args")}}
    return {"name": preview_name(app, n), "source": src, "plan": base["plan"], "port": base.get("port", 8080), "env": dict(base.get("env") or {}) if copy_env else {},
            "_replace_env": True, "instances": 1, "health": base.get("health"), "release_command": base.get("release_command"),
            "autoscale": {"min": 0, "max": 1, "target_concurrency": 10, "idle_seconds": 300}, "_preview_of": app}
def handle(event_name, payload, url_of):
    """Returns a short description of what was done (for the webhook response / tests)."""
    from . import engine
    from .crypto import load_spec
    repo = (payload.get("repository") or {}).get("full_name"); iid = (payload.get("installation") or {}).get("id")
    if event_name == "installation" and payload.get("action") == "deleted":
        q("delete from github_installs where installation_id=?", (iid,)); q("delete from github_links where installation_id=?", (iid,)); return "installation removed"
    if not repo or not iid: return "ignored"
    links = [l for l in q("select * from github_links where installation_id=?", (iid,)) if l["repo"].lower() == repo.lower()]
    done = []
    if event_name == "push":
        ref = payload.get("ref", ""); sha = payload.get("after")
        if payload.get("deleted") or not sha or set(sha) == {"0"}: return "branch deleted"
        for l in links:
            if ref != f"refs/heads/{l['branch']}": continue
            row = q("select spec, status from apps where name=?", (l["app"],), one=True)
            base = load_spec(row["spec"]) if row and row["status"] != "destroyed" else {}
            src = {"git": f"{WEB}/{repo}.git", "ref": sha, "github_installation": iid, **{k: v for k, v in (base.get("source") or {}).items() if k in ("subdir", "dockerfile", "build_args")}}
            _deploy_and_report(l["account_id"], {"name": l["app"], "source": src}, iid, repo, sha, url_of); done.append(f"deploy {l['app']}@{sha[:7]}")
    elif event_name == "pull_request":
        pr = payload["pull_request"]; n = payload.get("number") or pr["number"]; act = payload.get("action")
        for l in links:
            if not l["previews"]: continue
            pn = preview_name(l["app"], n)
            if act == "closed":
                if q("select 1 from apps where name=? and status!='destroyed'", (pn,), one=True): engine.destroy(pn); done.append(f"destroyed {pn}")
                continue
            if act not in ("opened", "synchronize", "reopened"): continue
            head = pr["head"]; same = ((head.get("repo") or {}).get("full_name") or "").lower() == repo.lower()
            spec = preview_spec(l["app"], n, f"{WEB}/{head['repo']['full_name']}.git", head["sha"], iid if same else None, copy_env=same)
            _deploy_and_report(l["account_id"], spec, iid, repo, head["sha"], url_of, pr=n); done.append(f"preview {pn}@{head['sha'][:7]}" + ("" if same else " (fork: no secrets)"))
    return "; ".join(done) or "no linked app for this event"
