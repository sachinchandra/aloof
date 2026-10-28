"""Waitlists (kind=gpu | region | design_partner): storage, duplicate handling, confirmation + admin emails,
spam protection (per-IP rate limit + honeypot), tokenized unsubscribe, test-entry flagging, admin list/export/delete."""
import os, re, time, json, hmac, hashlib, html, csv, io, logging, threading
from urllib.parse import urlparse
from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from .db import q, kv_put, kv_get
from . import auth, site
log = logging.getLogger("paas.waitlist")
R = APIRouter()
KINDS = ("gpu", "region", "design_partner", "byo_server")
BYO_PROVIDERS = {"hetzner": "Hetzner", "digitalocean": "DigitalOcean", "aws": "AWS", "other": "Other"}
LIMIT = int(os.environ.get("PAAS_WAITLIST_LIMIT", "5")); WINDOW = float(os.environ.get("PAAS_WAITLIST_WINDOW", "600"))
TEST_RE = re.compile(os.environ.get("PAAS_WAITLIST_TEST_RE", r"^ux-test|\+test@|@aloof\.si$|@example\.(com|org|net)$"), re.I)
_hits = {}; _hl = threading.Lock()

def is_test_email(e): return bool(TEST_RE.search(e or ""))
def notify_recipients():
    """PAAS_WAITLIST_NOTIFY (comma list), else the owner email that gates /admin (www.ADMIN_EMAIL: PAAS_ADMIN_EMAIL, default sachin@aloof.si)."""
    from . import www
    raw = os.environ.get("PAAS_WAITLIST_NOTIFY") or www.ADMIN_EMAIL or ""
    return [x.strip().lower() for x in raw.split(",") if x.strip()]
def _secret():
    from . import crypto
    k = os.environ.get("PAAS_SECRET_KEY") or (open(crypto._KF).read().strip() if os.path.exists(crypto._KF) else "")
    return hashlib.sha256(b"aloof-waitlist-unsub:" + k.encode()).digest()
def token(entry_id, email): return hmac.new(_secret(), f"{entry_id}:{email}".encode(), hashlib.sha256).hexdigest()[:32]
def unsub_url(entry_id, email): return f"{site.SITE}/waitlist/unsubscribe?id={entry_id}&t={token(entry_id, email)}"

def rate_check(ip, now=None):
    now = now or time.time()
    with _hl:
        if len(_hits) > 50000: _hits.clear()
        h = [t for t in _hits.get(ip, []) if now - t < WINDOW]
        if len(h) >= LIMIT: raise HTTPException(429, "Too many signups from your network. Try again in a few minutes.", headers={"Retry-After": str(int(WINDOW - (now - h[0])) + 1)})
        h.append(now); _hits[ip] = h

def _source(src, referer):
    s = (src or "").strip() or (urlparse(referer or "").path if referer else "")
    return s[:200] or None

def upsert(kind, email, *, gpu=None, region=None, company=None, note=None, source=None, ip=None):
    """Insert, or update the choice of an existing entry while keeping its first signup date. Returns (row, created)."""
    item = region if kind == "region" else ""
    now = time.time()
    old = q("select * from waitlist where kind=? and email=? and item=?", (kind, email, item), one=True)
    if old:
        q("update waitlist set gpu=?, region=?, company=coalesce(?,company), note=coalesce(?,note), source=coalesce(?,source), consent_ts=?, updated=?, ip=? where id=?",
          (gpu, region, company, note, source, now, now, ip, old["id"]))
        return q("select * from waitlist where id=?", (old["id"],), one=True), False
    q("insert into waitlist(kind,email,item,gpu,region,company,note,source,consent_ts,created,updated,ip,is_test) values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
      (kind, email, item, gpu, region, company, note, source, now, now, now, ip, int(is_test_email(email))))
    return q("select * from waitlist where kind=? and email=? and item=?", (kind, email, item), one=True), True

def byo_status(email): return q("select id, note, created from waitlist where kind='byo_server' and email=? and item=''", (email,), one=True)

# ---------- emails ----------
def _what(r):
    from .plans import PLANS
    if r["kind"] == "gpu": return "the Aloof GPU waitlist" + (f" ({PLANS[r['gpu']]['gpu']})" if r.get("gpu") in PLANS else "")
    if r["kind"] == "region":
        from .www_content import SOON_REGIONS
        names = {c: l for c, l, _, _ in SOON_REGIONS}; return f"the Aloof waitlist for {names.get(r['region'], r['region'])}"
    if r["kind"] == "byo_server": return "the Aloof waitlist for deploying to your own server"
    return "the Aloof design partner program"
def _html(title, paras, link_text, link):
    ps = "".join(f'<p style="margin:0 0 14px;font-size:15px;line-height:1.55;color:#3a2e24">{p}</p>' for p in paras)
    return f"""<!doctype html><html><body style="margin:0;background:#f6f1ea;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">
<table role=presentation width="100%" cellpadding=0 cellspacing=0 style="background:#f6f1ea;padding:32px 12px"><tr><td align=center>
<table role=presentation width="100%" cellpadding=0 cellspacing=0 style="max-width:520px;background:#fcfaf7;border:1px solid #e8ddd0;border-radius:12px">
<tr><td style="padding:22px 28px;border-bottom:3px solid #ff7a1a"><span style="font-size:18px;font-weight:700;color:#1a120b">Aloof</span></td></tr>
<tr><td style="padding:26px 28px 10px"><h1 style="margin:0 0 16px;font-size:20px;color:#1a120b">{html.escape(title)}</h1>{ps}</td></tr>
<tr><td style="padding:6px 28px 26px;border-top:1px solid #efe6db;font-size:12px;color:#7a6a5c">You're receiving this because this address was entered at aloof.si. <a href="{link}" style="color:#c2410c">{link_text}</a></td></tr>
</table></td></tr></table></body></html>"""
def confirmation(r):
    what = _what(r); link = unsub_url(r["id"], r["email"])
    if r["kind"] == "design_partner":
        subj = "We received your Aloof design partner application"; body = "Thanks for applying. We read every application and will reply by email."
    elif r["kind"] == "byo_server":
        subj = "You're on the Aloof own-server waitlist"; body = "Deploying to your own server is in early access and isn't available yet. We'll email you when we can let you in. We won't use your address for anything else."
    elif r["kind"] == "region":
        subj = "You're on the Aloof region waitlist"; body = "We'll email you once, when the region is live. We won't use your address for anything else."
    else:
        subj = "You're on the Aloof GPU waitlist"; body = "GPUs launch in our second phase. We'll email you once, when they're available to deploy. We won't use your address for anything else."
    from . import emailkit; text, htm = emailkit.waitlist(r["kind"], what, body, link)
    return subj, text, htm, link
def _admin_mail(r):
    fields = {k: r.get(k) for k in ("kind", "email", "gpu", "region", "company", "note", "source", "ip")}
    subj = f"[Aloof] New {r['kind']} waitlist signup: {r['email']}" + (" (test)" if r.get("is_test") else "")
    return subj, "\n".join(f"{k}: {v}" for k, v in fields.items() if v) + f"\n\nAll entries: {site.SITE}/admin#waitlist\n"
def _record(entry_id, col, status):
    q(f"update waitlist set {col}=? where id=?", (status[:300], entry_id))
    if status.startswith("failed"): kv_put("mail_status", "last_error", json.dumps({"ts": time.time(), "where": f"waitlist {col}", "error": status}))
def send_emails(entry_id):
    """Runs after the response (BackgroundTasks). Each outcome is stored on the row and shown in /admin."""
    r = q("select * from waitlist where id=?", (entry_id,), one=True)
    if not r: return
    subj, text, htm, link = confirmation(r)
    try: _record(entry_id, "mail_user", "sent:" + auth.send_mail(r["email"], subj, text, html=htm, headers={"List-Unsubscribe": f"<{link}>"}))
    except Exception as e: _record(entry_id, "mail_user", f"failed: {e}")
    to = notify_recipients()
    if not to:
        log.warning("waitlist signup %s: no admin recipients (set PAAS_WAITLIST_NOTIFY or PAAS_ADMIN_EMAIL)", entry_id)
        _record(entry_id, "mail_admin", "skipped: no admin recipients configured"); return
    s2, t2 = _admin_mail(r); res = []
    for a in to:
        try: res.append(f"{a}=" + auth.send_mail(a, s2, t2))
        except Exception as e: res.append(f"{a}=failed ({e})")
    _record(entry_id, "mail_admin", ("failed: " if any("failed" in x for x in res) else "sent: ") + ", ".join(res))

# ---------- unsubscribe ----------
def _page(h1, inner):
    return HTMLResponse(site.page(f"{h1} · Aloof", f"<section class=s><div class=wrap style='max-width:560px;padding:64px 0'><h1>{h1}</h1>{inner}</div></section>"), headers={"X-Robots-Tag": "noindex"})
def _check(id, t):
    r = q("select * from waitlist where id=?", (id,), one=True)
    ok = r and hmac.compare_digest(token(r["id"], r["email"]), t or "")
    return r if ok else None
@R.get("/waitlist/unsubscribe", response_class=HTMLResponse, include_in_schema=False)
def unsub_page(id: int = 0, t: str = ""):
    r = _check(id, t)
    if not r: return _page("Link not valid", "<p class=muted>This removal link is invalid, or the entry was already removed. Nothing else to do.</p><p><a href='/'>Back to Aloof</a></p>")
    return _page("Remove me from the waitlist", f"""<p>This removes <b>{html.escape(r['email'])}</b> from {html.escape(_what(r))}. We won't email you about it again.</p>
<form method=post action="/waitlist/unsubscribe"><input type=hidden name=id value="{r['id']}"><input type=hidden name=t value="{html.escape(t)}"><button class="btn pri">Remove me</button></form>""")
@R.post("/waitlist/unsubscribe", response_class=HTMLResponse, include_in_schema=False)
async def unsub_do(request: Request):
    f = await request.form()
    try: r = _check(int(f.get("id") or 0), f.get("t") or "")
    except ValueError: r = None
    if r: q("delete from waitlist where id=?", (r["id"],)); log.info("waitlist unsubscribe id=%s kind=%s", r["id"], r["kind"])
    return _page("You've been removed", "<p>Your address is off the list and we've deleted the entry. You won't hear from us about it.</p><p><a href='/'>Back to Aloof</a></p>")

# ---------- admin ----------
COLS = ["id", "kind", "email", "gpu", "region", "company", "note", "source", "created", "updated", "consent_ts", "is_test", "mail_user", "mail_admin"]
def rows(kind=None, gpu=None, include_test=True):
    sql = "select " + ",".join(COLS) + " from waitlist where 1=1"; a = []
    if kind: sql += " and kind=?"; a.append(kind)
    if gpu: sql += " and gpu=?"; a.append(gpu)
    if not include_test: sql += " and is_test=0"
    return q(sql + " order by created desc", tuple(a))
def counts():
    c = {k: 0 for k in KINDS}; reg = {}
    for r in q("select kind, region, count(*) n from waitlist where is_test=0 group by kind, region"):
        c[r["kind"]] = c.get(r["kind"], 0) + r["n"]
        if r["kind"] == "region": reg[r["region"]] = r["n"]
    t = q("select count(*) n from waitlist where is_test=1", one=True)["n"]
    return {"gpu": c["gpu"], "regions": c["region"], "by_region": reg, "design_partners": c["design_partner"], "byo_server": c["byo_server"], "test_entries": t}
def mail_health():
    out = {"smtp_configured": bool(os.environ.get("SMTP_HOST")), "admin_recipients": notify_recipients(), "last_error": None}
    v = kv_get("mail_status", "last_error")
    if v: out["last_error"] = json.loads(v)
    return out
from .www import require_admin
@R.get("/v1/admin/waitlist")
def admin_list(kind: str = None, gpu: str = None, include_test: bool = True, _=Depends(require_admin)):
    return {"entries": rows(kind, gpu, include_test), "counts": counts(), "mail": mail_health()}
@R.get("/v1/admin/waitlist.csv")
def admin_csv(kind: str = None, gpu: str = None, include_test: bool = False, _=Depends(require_admin)):
    buf = io.StringIO(); w = csv.writer(buf); w.writerow(COLS)
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) if t else ""
    for r in rows(kind, gpu, include_test): w.writerow([iso(r[c]) if c in ("created", "updated", "consent_ts") else ("" if r[c] is None else r[c]) for c in COLS])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="aloof-waitlist-{kind or "all"}-{time.strftime("%Y%m%d")}.csv"'})
@R.delete("/v1/admin/waitlist/{entry_id}")
def admin_delete(entry_id: int, ctx=Depends(require_admin)):
    r = q("select kind,email from waitlist where id=?", (entry_id,), one=True)
    if not r: raise HTTPException(404, "no such entry")
    q("delete from waitlist where id=?", (entry_id,))
    from .db import audit; audit((ctx.get("account") or {}).get("id"), None, "admin.waitlist_delete", f"id={entry_id} kind={r['kind']} email={r['email']}")
    return {"ok": True}
class TestFlag(BaseModel): is_test: bool
@R.post("/v1/admin/waitlist/{entry_id}/test")
def admin_flag(entry_id: int, b: TestFlag, ctx=Depends(require_admin)):
    r = q("select email from waitlist where id=?", (entry_id,), one=True)
    if not r: raise HTTPException(404, "no such entry")
    q("update waitlist set is_test=? where id=?", (int(b.is_test), entry_id))
    from .db import audit; audit(((ctx or {}).get("account") or {}).get("id"), None, "admin.waitlist_test", f"entry={entry_id} email={r['email']} is_test={int(b.is_test)}")
    return {"ok": True}

class SendReq(BaseModel): confirm: str
@R.post("/v1/admin/waitlist/{entry_id}/send")
def admin_send(entry_id: int, b: SendReq, ctx=Depends(require_admin)):
    """Owner-triggered (typed confirmation = the entry's email): send the confirmation + admin note for an entry that never got one."""
    r = q("select * from waitlist where id=?", (entry_id,), one=True)
    if not r: raise HTTPException(404, "no such entry")
    if (b.confirm or "").strip().lower() != r["email"].lower(): raise HTTPException(400, "type the entry's email address to confirm")
    if (r["mail_user"] or "").startswith("sent"): raise HTTPException(409, "the confirmation was already sent")
    send_emails(entry_id)
    from .db import audit; audit((ctx.get("account") or {}).get("id"), None, "admin.waitlist_send", f"id={entry_id} kind={r['kind']} email={r['email']}")
    return {"ok": True, "mail_user": q("select mail_user from waitlist where id=?", (entry_id,), one=True)["mail_user"]}

RETRY_AFTER, RETRY_WINDOW = 10 * 60, 2 * 86400
def retry_unsent(now=None):
    """Root cause of rows stuck at 'pending': send_emails runs as a FastAPI background task, so a restart between the response and
    the task loses it and nothing ever retries. This sweep sends for rows that are 10 min to 2 days old with no outcome recorded.
    Rows migrated from the old kv store (source='legacy') are never auto-sent: they signed up before confirmations existed."""
    now = now or time.time(); n = 0
    for r in q("select id from waitlist where mail_user is null and coalesce(source,'')!='legacy' and created<? and created>?", (now - RETRY_AFTER, now - RETRY_WINDOW)):
        _record(r["id"], "mail_user", "sending: retry"); send_emails(r["id"]); n += 1
    return n

def migrate_kv():
    """One-time move of the old kv-stored entries (gpu_waitlist, region_waitlist, design_partner) into the waitlist table."""
    for kind, kk in (("gpu", "gpu_waitlist"), ("region", "region_waitlist"), ("design_partner", "design_partner")):
        for r in q("select k, v from kv where kind=?", (kk,)):
            try: v = json.loads(r["v"])
            except Exception: v = {}
            email = r["k"].split("|")[0]; ts = v.get("ts") or time.time()
            item = v.get("region") or "" if kind == "region" else ""
            if not q("select 1 from waitlist where kind=? and email=? and item=?", (kind, email, item), one=True):
                q("insert into waitlist(kind,email,item,gpu,region,company,note,source,consent_ts,created,updated,is_test) values(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (kind, email, item, v.get("gpu"), v.get("region"), v.get("company"), v.get("note"), "legacy", None, ts, ts, int(is_test_email(email))))
            q("delete from kv where kind=? and k=?", (kk, r["k"]))
