"""Public site: landing, pricing, docs, login. Server-rendered, no JS framework."""
import html, json, os, re
from .contact import apply as _contact
from .plans import PLANS, TIERS, RATES, REGIONS, REGION_NOTE
from . import plans as _plans
ICON = {
 "bolt": '<path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z"/>', "globe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
 "lock": '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>', "scale": '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
 "disk": '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
 "git": '<circle cx="6" cy="6" r="2.5"/><circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="9" r="2.5"/><path d="M6 8.5v7M18 11.5c0 3-4 3.5-10 5"/>',
 "bot": '<rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 4v4M9 14h.01M15 14h.01"/>', "shield": '<path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6l-8-3Z"/><path d="m9 12 2 2 4-4"/>',
 "key": '<circle cx="8" cy="15" r="4"/><path d="m11 12 9-9M17 6l3 3"/>', "box": '<path d="M21 8 12 3 3 8v8l9 5 9-5V8Z"/><path d="m3 8 9 5 9-5M12 13v8"/>',
 "card": '<rect x="2" y="5" width="20" height="14" rx="2"/><path d="M2 10h20M6 15h4"/>', "eu": '<circle cx="12" cy="12" r="9"/><path d="M12 6v.01M16 7.5v.01M18 12v.01M16 16.5v.01M12 18v.01M8 16.5v.01M6 12v.01M8 7.5v.01"/>',
 "term": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3M13 15h4"/>', "apps": '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
 "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21c1.5-4 4.5-6 8-6s6.5 2 8 6"/>', "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
 "menu": '<path d="M4 7h16M4 12h16M4 17h16"/>', "logout": '<path d="M15 4h4v16h-4M10 17l5-5-5-5M15 12H3"/>', "book": '<path d="M4 4h10a4 4 0 0 1 4 4v12H8a4 4 0 0 1-4-4V4Z"/><path d="M18 20H8a4 4 0 0 1-4-4"/>',
}
def svg(name, cls=""): return f'<svg class="{cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{ICON[name]}</svg>'
# generic, non-trademark glyphs for the "works with" row
STACK = [("Docker images", '<path d="M3 13h18l-1 4a5 5 0 0 1-5 3H8a5 5 0 0 1-5-5v-2Z"/><path d="M6 13V9h3v4M9 13V9h3v4M12 13V9h3v4M9 9V6h3v3"/>'),
 ("Node.js", '<path d="m12 2 9 5v10l-9 5-9-5V7l9-5Z"/>'), ("Python", '<path d="M8 3h6a3 3 0 0 1 3 3v4H9a3 3 0 0 0-3 3v2H5a2 2 0 0 1-2-2V9a3 3 0 0 1 3-3h2V3Z"/><path d="M16 21h-6a3 3 0 0 1-3-3v-4h8a3 3 0 0 0 3-3V9h1a2 2 0 0 1 2 2v4a3 3 0 0 1-3 3h-2v3Z"/>'),
 ("Go", '<path d="M3 9h7M2 12h6M4 15h5"/><circle cx="16" cy="12" r="5"/>'), ("Rust", '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/>'),
 ("Ruby", '<path d="M6 3h12l4 6-10 12L2 9l4-6Z"/><path d="M2 9h20M12 21 8 9l4-6 4 6-4 12"/>'), ("Java / JVM", '<path d="M6 18c2 2 10 2 12 0M8 14h8v-3H8v3ZM16 12.5h2a2 2 0 0 1 0 4h-2M10 3c2 2-2 3 0 6M13 4c2 2-2 3 0 5"/>'),
 ("PHP", '<ellipse cx="12" cy="12" rx="10" ry="6"/><path d="M7 15l1-6h2a1.5 1.5 0 0 1 0 3H8M14 15l1-6h2a1.5 1.5 0 0 1 0 3h-2"/>'),
 ("Static sites", '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M7 6.5h.01M10 6.5h.01"/>')]
def stack_row(): return "".join(f'<span><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{p}</svg>{n}</span>' for n, p in STACK)
THEME_JS = """<script>(function(){var t=localStorage.getItem('theme');if(t)document.documentElement.dataset.theme=t})();
function toggleTheme(){var d=document.documentElement,cur=d.dataset.theme||'dark',n=cur=='dark'?'light':'dark';d.dataset.theme=n;localStorage.setItem('theme',n)}</script>"""
COMMON_JS = """<script>document.querySelectorAll('pre').forEach(function(p){if(p.closest('.term'))return;var b=document.createElement('button');b.className='copy';b.type='button';b.textContent='Copy';b.setAttribute('aria-label','Copy code');
b.onclick=function(){var t=p.cloneNode(true);t.querySelectorAll('.copy').forEach(function(x){x.remove()});navigator.clipboard.writeText(t.innerText.trim()).then(function(){b.textContent='Copied';setTimeout(function(){b.textContent='Copy'},1400)})};p.appendChild(b)});
var io=window.IntersectionObserver&&new IntersectionObserver(function(es){es.forEach(function(e){if(e.isIntersecting){e.target.classList.add('in');io.unobserve(e.target)}})},{threshold:.12});
document.querySelectorAll('.reveal').forEach(function(el){io?io.observe(el):el.classList.add('in')});</script>"""
SITE = os.environ.get("PAAS_PUBLIC_WWW", "https://www.aloof.si").rstrip("/")
CUR = {"path": "/", "og": "default", "type": "website"}
def head(title, desc):
    t, d = html.escape(title), html.escape(desc); u = SITE + CUR["path"]; og = f"{SITE}/static/og/{CUR['og']}.png"
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{t}</title><meta name=description content="{d}"><meta name=theme-color content="#0a0807"><link rel=canonical href="{u}">
<meta property="og:type" content="{CUR['type']}"><meta property="og:site_name" content="Aloof"><meta property="og:title" content="{t}"><meta property="og:description" content="{d}"><meta property="og:url" content="{u}"><meta property="og:image" content="{og}"><meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image"><meta name="twitter:title" content="{t}"><meta name="twitter:description" content="{d}"><meta name="twitter:image" content="{og}">
<link rel=alternate type="application/rss+xml" title="Aloof blog" href="/rss.xml">
<link rel=icon href="/favicon.ico?v=3" sizes="32x32"><link rel=icon href="/icon.svg?v=3" type="image/svg+xml"><link rel=icon type="image/png" sizes="32x32" href="/favicon-32x32.png?v=3"><link rel=icon type="image/png" sizes="16x16" href="/favicon-16x16.png?v=3"><link rel=apple-touch-icon href="/apple-touch-icon.png?v=3"><link rel=manifest href="/site.webmanifest?v=3"><link rel=stylesheet href="/static/aloof.css?v=23">{THEME_JS}</head>"""
def nav(brand):
    return f"""<a class=skip href="#main">Skip to content</a><header class=top><div class=wrap><a class=logo href="/" aria-label="{brand} home"><i></i>{brand}</a>
<nav class=navlinks id=nl aria-label=Primary><a href="/#features">Product</a><a href="/pricing">Pricing</a><a href="/docs">Docs</a><a href="/guides">Guides</a><a href="/blog">Blog</a></nav>
<span class=sp></span><button class=icon-btn onclick="toggleTheme()" aria-label="Toggle dark/light theme">{svg("sun")}</button>
<a href="/login" class=hide-sm data-cta=login data-cta-loc=nav>Log in</a><a class="btn pri sm" href="/login#register" data-cta=deploy_free data-cta-loc=nav>Start free</a>
<button class="icon-btn menu-btn" aria-label="Menu" onclick="document.getElementById('nl').classList.toggle('open')">{svg("menu")}</button></div></header>"""
def footer(brand):
    return f"""<footer class=f><div class=wrap><div class=cols><div><a class=logo href="/"><i></i>{brand}</a><p class=muted style="max-width:300px;margin-top:12px">The cloud for apps your AI ships.</p></div>
<div><h5>Product</h5><a href="/#features">Features</a><a href="/pricing">Pricing</a><a href="/regions">Regions</a><a href="/examples">Example architectures</a><a href="/pricing#gpus">GPUs (soon)</a></div>
<div><h5>Developers</h5><a href="/docs">Docs</a><a href="/guides">Guides</a><a href="/blog">Blog</a><a href="/docs/cli">CLI</a><a href="/api/docs">API reference</a><a href="/docs/mcp">MCP</a><a href="/llms.txt">llms.txt</a><a href="/rss.xml">RSS</a></div>
<div><h5>Company</h5><a href="/about">About</a><a href="/security">Security</a><a href="/status">Status</a><a href="/privacy">Privacy</a><a href="/terms">Terms</a></div>
<div><h5>Account</h5><a href="/login" data-cta=login data-cta-loc=footer>Log in</a><a href="/login#register" data-cta=deploy_free data-cta-loc=footer_links>Sign up</a><a href="/dashboard">Dashboard</a></div></div>
<div class=base><span>© 2026 {brand}</span></div></div></footer>"""
def mcp_tool_names():
    """Tool names registered with @mcp.tool() in mcp_server/server.py, in order. Parsed (not imported) so the web process
    doesn't load the MCP server; llms.txt and the MCP docs use this so the list can't drift from the code."""
    try: src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mcp_server", "server.py")).read()
    except OSError: return []
    return re.findall(r"@mcp\.tool\([^)]*\)\s*\n(?:@[^\n]*\n)*\s*(?:async\s+)?def\s+(\w+)", src)
def posthog_key():
    k = (os.environ.get("POSTHOG_KEY") or os.environ.get("POSTHOG_API_KEY") or "").strip()
    if not k:
        f = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "posthog_key")
        try: k = open(f).read().strip()
        except OSError: k = ""
    return k if re.fullmatch(r"ph[cx]_[A-Za-z0-9]{10,80}", k or "") else ""
POSTHOG_DEFAULT_HOST = "https://us.i.posthog.com"
def posthog_host(): return (os.environ.get("POSTHOG_HOST") or POSTHOG_DEFAULT_HOST).strip().rstrip("/")
def analytics_tag():
    """Empty string (load nothing) unless a PostHog project key is configured."""
    k = posthog_key()
    return f'<script>window.ALOOF_PH={json.dumps({"key": k, "host": posthog_host(), "hosts": analytics_hosts()})}</script><script src="/static/analytics.js?v=6" defer></script>' if k else ""
def analytics_hosts():
    """Only platform hosts load analytics (never <app>.<domain> user apps): base, www and api for the base + alt domains."""
    ds = [d.strip() for d in (os.environ.get("PAAS_BASE_DOMAIN", "aloof.si") + "," + os.environ.get("PAAS_ALT_DOMAINS", "")).split(",") if d.strip()]
    return sorted({h for d in ds for h in (d, "www." + d, "api." + d)})
def page(title, body, brand="Aloof", desc=None):
    d = desc or f"{brand}: cheap backends in the EU, billed per second in USD (GPUs coming soon). Deploy Docker images or git repos; scale to zero; MCP for AI assistants."
    return _contact(f"""{head(title, d)}<body>{nav(brand)}<main id=main>{body}</main>{footer(brand)}{COMMON_JS}{analytics_tag()}</body></html>""")
def _gb(mb): g = mb / 1024; return f"{g:g}" if g < 1 or g == int(g) else str(round(g))
def _money(v, unit=""):
    return (f"${v:,.2f}" if v < 100 or v != int(v) else f"${v:,.0f}") + unit
def _cmp(v): return f"${v:g}" if v else "<span class=muted>—</span>"
def _plan_rows(kind=None, compare=False):
    rows = []
    for k, p in PLANS.items():
        if p["legacy"] or (kind and (p["cpu_class"] if p["kind"] == "cpu" else "gpu") != kind): continue
        if p["kind"] == "cpu":
            c = p["compare_usd_month"]; mo = p["price_usd_month"]
            cmpc = f"<td>{_cmp(c['fly'])}</td><td>{_cmp(c['render'])}</td><td>{_cmp(c['railway'])}</td>" if compare else ""
            rows.append(f"<tr><td><code>{k}</code></td><td>{p['vcpu']:g} {p['cpu_class']}</td><td>{_gb(p['memory_mb'])} GB</td>"
                        f"<td><b>{_money(mo)}</b>/mo</td><td>${p['price_usd_hr']:.4f}/hr</td>{cmpc}</tr>")
        else:
            f = (p.get("compare_usd_hr") or {}).get("fly_last_list")
            cmpc = f"<td>{_cmp(f)}{' <span class=muted>' + p['note'] + '</span>' if p.get('note') else ''}</td>" if compare else ""
            rows.append(f"<tr><td><code>{k}</code></td><td>{p['gpu']}</td><td>{p['vcpu']} vCPU · {_gb(p['memory_mb'])} GB</td>"
                        f"<td><b>${p['price_usd_hr']:.2f}</b>/hr</td><td>${p['price_usd_hr']/3600:.6f}/s</td>{cmpc}</tr>")
    return "".join(rows)
CPU_HEAD = "<tr><th>Plan</th><th>CPU</th><th>RAM</th><th>Always-on (30 days)</th><th>Per hour</th></tr>"
GPU_HEAD = "<tr><th>Plan</th><th>GPU</th><th>Host</th><th>Per hour</th><th>Per second</th></tr>"
def _regions():
    rows = "".join(f"<tr><td><code>{k}</code></td><td>{v}</td></tr>" for k, v in REGIONS.items() if k != "local")
    return f"<div class=scroll><table class=tbl id=regions><tr><th>Region</th><th>Location</th></tr>{rows}</table></div><p class=muted>{REGION_NOTE} More regions will be listed here only once they're live.</p>"
WAITLIST_FORM = """<form id=wl class=inline style="max-width:640px;position:relative" onsubmit="event.preventDefault();var f=this;fetch('/v1/waitlist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:f.email.value,gpu:f.gpu.value||null,source:location.pathname,website:f.website.value})}).then(r=>r.json().then(j=>{if(r.ok)f.outerHTML='<p class=ok><b>'+(j.message||'')+'</b></p>';else{wlmsg.className='bad';wlmsg.textContent=j.detail||'Something went wrong. Try again.'}}))">
<input name=email type=email required placeholder="you@company.com" aria-label="Email"> <select name=gpu aria-label="GPU"><option value="">Any GPU</option>%s</select> <button class="btn pri">Join the GPU waitlist</button><div aria-hidden=true style="position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden"><label>Website <input name=website tabindex=-1 autocomplete=off></label></div><p id=wlmsg role=status></p></form><p class="muted wl-consent" style="font-size:13px;margin:10px 0 0">By joining, you agree that we email you about this launch. We don't share your address, and every email has a link to remove it.</p>"""
def _gpu_section():
    if _plans.GPU_ENABLED:
        return f"""<h2 class=pp-h id=gpus>GPUs</h2><p class=pp-sub>Dedicated GPU per app, billed per second while running. With <code>min 0</code> autoscaling you pay only while serving.</p><div class=scroll><table class=tbl>{GPU_HEAD}{_plan_rows("gpu")}</table></div>"""
    opts = "".join(f"<option value={k}>{p['gpu']}</option>" for k, p in PLANS.items() if p["kind"] == "gpu" and k != "gpu-t4-spot")
    return f"""<h2 class=pp-h id=gpus>GPUs <span class=tag>coming soon</span></h2><div class="card pp-gpu"><p>L4, RTX 4090, L40, A100 80GB and H100, billed per second with scale to zero. GPUs launch in our second phase; leave your email and we'll tell you when they're live.</p><div style="margin-top:16px">{WAITLIST_FORM % opts}</div></div>"""
TERM = [("p", "$ ", "aloof deploy my-api-1234 --git https://github.com/acme/api.git"), ("m", "", "→ cloning acme/api@main"), ("m", "", "→ building Dockerfile (cached layers: 7/9)"),
        ("m", "", "→ pushing image to registry"), ("c", "", "→ starting 2 instances in nbg1"), ("g", "✓ ", "health check passed /healthz (38 ms)"),
        ("g", "✓ ", "live: https://my-api-1234.{base}/"), ("p", "$ ", "aloof autoscale my-api-1234 --min 0 --max 6 --idle 300"), ("g", "✓ ", "scale to zero enabled · billed per second")]
def _term(base):
    import json as _j
    lines = _j.dumps([[c, p, t.replace("{base}", base)] for c, p, t in TERM])
    return f"""<div class=term role=img aria-label="Animated terminal showing a deploy with the aloof CLI"><div class=bar><i></i><i></i><i></i><b>~/acme/api — aloof</b></div><pre id=tt></pre></div>
<script>(function(){{var L={lines},el=document.getElementById('tt'),i=0,j=0,out='';var R=matchMedia('(prefers-reduced-motion: reduce)').matches;
function esc(s){{return s.replace(/&/g,'&amp;').replace(/</g,'&lt;')}}
function line(k){{var l=L[k];return '<span class='+(l[0]=='p'?'p':l[0])+'>'+esc(l[1])+'</span>'+(l[0]=='p'?'<span>':'<span class='+l[0]+'>')+esc(l[2])+'</span>\\n'}}
if(R){{el.innerHTML=L.map(function(_,k){{return line(k)}}).join('');return}}
function tick(){{if(i>=L.length){{setTimeout(function(){{i=0;out='';tick()}},4000);return}}var l=L[i];
 if(l[0]=='p'){{if(j<=l[2].length){{el.innerHTML=out+'<span class=p>'+esc(l[1])+'</span>'+esc(l[2].slice(0,j))+'<span class=cursor></span>';j++;setTimeout(tick,28);return}}}}
 out+=line(i);el.innerHTML=out+'<span class=cursor></span>';i++;j=0;setTimeout(tick,l[0]=='p'?380:520)}}tick()}})();</script>"""
def _feat(icon, title, body): return f'<div class="card reveal"><div class=ico>{svg(icon)}</div><h3>{title}</h3><p>{body}</p></div>'
def _sd():
    """Self-driving numbers, read from the code (selfheal/lb/engine/MCP registry/templates) so docs and homepage can't drift."""
    from . import selfheal as _sh, lb as _lb, engine as _eng
    sh, lbc = _sh.cfg(), _lb.cfg()
    tdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
    have = sorted(f[:-5] for f in os.listdir(tdir) if f.endswith(".toml")) if os.path.isdir(tdir) else []
    order = ["n8n", "uptime-kuma", "umami", "vaultwarden", "ntfy", "pocketbase", "linkding"]
    tpls = [t for t in order if t in have] + [t for t in have if t not in order]
    cdmax = int(lbc["cooldown_max_s"])
    return {"sh": sh, "lb": lbc, "win_min": int(sh["window_s"] // 60), "cd0": int(lbc["cooldown_s"]),
            "cdmax_t": f"{cdmax // 60} minutes" if cdmax % 60 == 0 else f"{cdmax} seconds",
            "ex": "double" if sh["err_x"] == 2 else f"{sh['err_x']:g}×", "px": "double" if sh["p95_x"] == 2 else f"{sh['p95_x']:g}×",
            "releases": _eng.RELEASE_KEEP, "tools": len(mcp_tool_names()), "templates": tpls}
_WORD = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]
def _w(n, cap=False): w = _WORD[n] if 0 <= n <= 10 else str(n); return w.capitalize() if cap else w
FAQ_FULL = None
def _faq_full():
    d = _sd(); sh = d["sh"]
    return [("What does “self-driving” mean today?", "Aloof rolls back deploys that raise errors or latency, replaces instances that fail health checks, routes traffic away from failing instances and autoscales, all without you stepping in. Predictive scaling, auto right-sizing and AI auto-fix are coming soon and not live yet."),
            ("Will automatic rollback cover my app?", f"Only if it gets enough traffic in the {d['win_min']} minutes after a deploy: at least {sh['min_requests']} requests, with at least {sh['min_bad']} bad requests from at least {sh['min_ips']} IPs. Low-traffic apps aren't covered, but health-gated deploys and one-command rollback still are. You can turn automatic rollback off per app."),
            ("Does <code>diagnose_app</code> send my data to an AI model?", "No. It's a read-only MCP tool that applies rules to your app's recent errors, health checks, deploys, rollbacks, restarts and exit codes, and redacts secrets. Your own assistant reads the result and explains it."),
            ("Can an AI agent deploy for me safely?", "Yes. Agents use OAuth 2.1 with PKCE and get tokens you can revoke at any time."),
            ("What can I deploy?", "Any Docker image, or a git repo with a Dockerfile. This covers Node.js, Python, Go, Rust, Ruby, Java/JVM, PHP and static sites."),
            ("How does scale-to-zero affect latency?", "Idle apps sleep and wake on the next request in about a second."),
            ("How are secrets handled?", "Environment variables are encrypted at rest and never shown back. API keys are stored only as SHA-256 hashes."),
            ("Where does my code run?", "On Hetzner Cloud in Nuremberg, Germany. The control plane runs there too, and encrypted off-site backups are stored in Helsinki, Finland. More regions are coming soon."),
            ("Do you support GPUs?", "Not yet. GPUs are coming soon: L4, RTX 4090, L40, A100 80GB and H100, billed per second with scale to zero. <a href='/pricing#gpus'>Join the waitlist</a>." if not _plans.GPU_ENABLED else "Yes: L4, L40, A100 80GB and H100 on dedicated nodes, billed per second. See <a href='/pricing#gpus'>GPU pricing</a>.")]
def _selfdriving_doc():
    d = _sd(); sh, l = d["sh"], d["lb"]
    faqh = "".join(f"<details class=faq><summary>{q}</summary><p>{a}</p></details>" for q, a in _faq_full())
    return f"""<h1>How self-driving works</h1><p>What Aloof does on its own today, with the exact limits it uses. These numbers come straight from the platform code.</p>
<h2 id=rollback>Bad deploys roll themselves back</h2><p>For {d['win_min']} minutes after every deploy, Aloof compares the new release with the previous one. If the 5xx rate is more than {sh['err_pp']:g} points higher and more than {d['ex']}, or p95 latency is more than {d['px']} and over {sh['p95_ms']:g} ms slower, it rolls back with zero downtime and records why. You see the reason in release history, the CLI, the dashboard, over MCP and in the audit log. You can turn it off per app (<code>set_self_heal</code> over MCP).</p>
<div class=note><b>Fine print:</b> a rollback needs at least {sh['min_requests']} requests and at least {sh['min_bad']} bad requests from at least {sh['min_ips']} IPs, so low-traffic apps aren't covered.</div>
<h2 id=diagnose>Ask your assistant why it broke</h2><p>The <code>diagnose_app</code> MCP tool gathers recent errors, failed health checks, deploys, rollbacks, restarts and exit codes, then names the likely cause and fix. Your assistant explains it in plain language. It's read-only and redacts secrets. The causes come from rules, not an AI model.</p>
<h2 id=lb>Traffic routes around failing instances</h2><p>Requests go to the instance with the fewest open connections. {_w(l['fails'], True)} failures in a row (connection errors, timeouts, 502 to 504) take an instance out for {d['cd0']} seconds, backing off up to {d['cdmax_t']}. {_w(l['health_fails'], True)} failed health checks also take it out. Aloof never removes more than half your instances, or the last one. This applies when you run 2 or more instances.</p>
<h2 id=health>Deploys that only switch over when healthy</h2><p>Redeploys only take traffic after the new instance passes its health check, with zero downtime. Health checks run every 5 seconds and replace unhealthy instances automatically. Roll back to any of your last {d['releases']} releases with one command.</p>
<h2 id=scale>Scales itself, down to zero</h2><p>Autoscaling checks in-flight requests every 3 seconds. Idle apps sleep and wake on the next request in about a second, and you pay nothing while they sleep.</p>
<h2 id=mcp>Your AI assistant has the wheel</h2><p>{d['tools']} MCP tools let Claude, ChatGPT or Cursor deploy, scale, debug and launch templates. A remote MCP server with OAuth 2.1 gives them tokens you can revoke at any time. There's also <a href="/llms.txt">llms.txt</a> and an <a href="/api/docs">OpenAPI spec</a>.</p>
<h2 id=templates>One-click templates</h2><p>Deploy {", ".join(d['templates'])} in one click from <a href="/templates">templates</a>, or add a “Deploy on Aloof” button to your own repo's README.</p>
<h2 id=security>Security defaults</h2><p>What the platform does today. We hold no certifications yet.</p><ul>
<li><b>TLS everywhere.</b> Every app, API and MCP endpoint is served over HTTPS with automatically issued certificates. Certificates are only requested for hosts we know.</li>
<li><b>Encrypted secrets.</b> Environment variables are encrypted at rest and never shown back. API keys are stored only as SHA-256 hashes.</li>
<li><b>Isolated containers.</b> Each app runs in its own container with CPU and memory limits. Nodes join over WireGuard and expose no inbound ports.</li></ul>
<p>More on the <a href="/security">security page</a>. Found an issue? Email <a href="mailto:security@aloof.si">security@aloof.si</a>.</p>
<h2 id=soon>Coming soon</h2><p>Not live yet: predictive scaling, auto right-sizing, AI auto-fix, GPUs (<a href="/pricing#gpus">waitlist</a>) and more regions.</p>
<h2 id=faq>FAQ</h2>{faqh}"""
TPL_NAME = {"n8n": "n8n", "uptime-kuma": "Uptime Kuma", "umami": "Umami", "vaultwarden": "Vaultwarden", "ntfy": "ntfy", "pocketbase": "PocketBase", "linkding": "linkding"}
HERO_JS = """<script>(function(){var t=document.getElementById('hv');if(!t)return;var L=[].slice.call(t.querySelectorAll('[data-s]')),c=document.getElementById('hv-chart');
var R=window.matchMedia&&matchMedia('(prefers-reduced-motion: reduce)').matches;
function show(n){L.forEach(function(e){e.classList.toggle('on',+e.dataset.s<=n)});t.dataset.step=n}
if(R){show(9);return}var n=0;function tick(){show(n);n=n>=9?0:n+1;setTimeout(tick,n==0?3200:n==1?600:1100)}
var io=window.IntersectionObserver&&new IntersectionObserver(function(es){if(es[0].isIntersecting){io.disconnect();tick()}});io?io.observe(t):tick()})();</script>"""
def _hero_visual(base):
    # step 1-3 deploy, 4-5 spike, 6-8 rollback; chart bars react via [data-step]
    rows = [(1, "p", "$", "aloof deploy my-api-1234"), (2, "m", "", "→ building v42 · pushing image"), (3, "ok", "✓", f"v42 live at https://my-api-1234.{base}"),
            (4, "warn", "!", "5xx rate 0.4% → 18% · p95 120 → 940 ms"), (5, "m", "", "self-heal: v42 worse than v41 baseline"),
            (6, "ok", "↺", "rolled back to v41 · zero downtime"), (7, "m", "", "reason: 5xx rate 18.2% vs 0.4% baseline (1,240 requests)"), (8, "ok", "✓", "healthy · recorded in release history")]
    lines = "".join(f"<div class='hv-l {c}' data-s={s}><span class=hv-p>{p}</span>{html.escape(t)}</div>" for s, c, p, t in rows)
    bars = "".join(f"<i style='--h:{h}' class='{'sp' if k in (6,7,8) else ''}'></i>" for k, h in enumerate([18, 22, 16, 24, 20, 26, 88, 94, 80, 22, 18, 20, 16, 22]))
    return f"""<div class=hv id=hv data-step=0 role=img aria-label="Animation with example data: a deploy goes live, its error rate spikes, and Aloof rolls it back automatically and records the reason.">
<div class=hv-bar aria-hidden=true><i></i><i></i><i></i><b>aloof · api</b><span class=hv-pill>self-heal on</span><span class=hv-pill style='margin-left:6px;color:#8a92a8;border-color:#3a4256'>example data</span></div>
<div class=hv-body aria-hidden=true>{lines}</div>
<div class=hv-chart id=hv-chart aria-hidden=true><span>5xx rate</span><div class=hv-bars>{bars}</div></div></div>"""
def landing(brand, base, api, mcp):
    T = TIERS; p0 = PLANS["shared-1x-256mb"]["price_usd_month"]; d = _sd()
    ic = lambda n: f"<div class=ico>{svg(n)}</div>"
    cards = "".join(f"<div class='card reveal v5c'>{ic(i)}<h3>{t}</h3><p>{b}</p></div>" for i, t, b in (
        ("shield", "Self-healing deploys", "Bad release? It rolls back on its own."),
        ("globe", "Smart load balancing", "Traffic avoids failing instances automatically."),
        ("scale", "Scales to zero", "Busy, it scales up. Idle, you pay nothing.")))
    from .templating import tpl_logo
    tpl = "".join(f"<a class=tpl href='/templates/{t}' aria-label='{TPL_NAME.get(t, t)} template'>{tpl_logo(t, TPL_NAME.get(t, t), 'sm')}{TPL_NAME.get(t, t)}</a>" for t in d["templates"])
    price = lambda k, name, sub: f"<a class='card pc{' hl' if k == 'hobby' else ''}' href='/pricing' data-cta=pricing_{k} data-cta-loc=home_pricing><span>{name}</span><b>${T[k]['fee_usd']:g}<small>/mo</small></b><em>{sub}</em></a>"
    faq4 = [x for x in _faq_full() if x[0].startswith(("What does", "Will automatic", "Can an AI", "Where does"))]
    faqh = "".join(f"<details class=faq><summary>{q}</summary><p>{a}</p></details>" for q, a in faq4)
    body = f"""<section class="hero v5"><div class=wrap><div>
<div class=eyebrow>The self-driving cloud</div>
<h1>Infrastructure for the age of <span class=grad>superintelligence.</span></h1>
<p class=lead>Deploy your backend. Aloof keeps it running: it rolls back bad deploys, routes around failures and scales itself.</p>
<div class=cta><a class="btn pri" href="/login#register" data-cta=deploy_free data-cta-loc=hero>Deploy free</a><a class="btn ghost" href="/docs/mcp" data-cta=connect_mcp data-cta-loc=hero>Connect your AI</a></div>
<p class=v5-small>Free plan · No credit card · Billed per second</p></div>
{_hero_visual(base)}</div></section>
<section class="s v5s" id=features><div class=wrap><h2 class="t c">It drives itself</h2><div class="grid g3">{cards}</div>
<p class=c style="margin-top:22px"><a href="/docs/self-driving">How self-driving works →</a></p></div></section>
<section class="s v5s" id=ai><div class="wrap v5-split"><div><div class=eyebrow>AI ops</div><h2 class=t>Your AI runs ops</h2><p class=lead2>Connect Claude, ChatGPT or Cursor. They deploy, debug and scale for you.</p>
<p><a class="btn ghost sm" href="/docs/mcp" data-cta=connect_mcp data-cta-loc=ai_ops>Connect your AI</a></p></div>
<div class="chat reveal" role=img aria-label="Chat: you ask why your app is down; the assistant uses diagnose_app and answers that it ran out of memory after the last deploy, and offers to scale to 512 MB.">
<div class="msg you" aria-hidden=true>Why is my app down?</div>
<div class=tool aria-hidden=true>{svg("term")} diagnose_app <span>api</span></div>
<div class="msg ai" aria-hidden=true>It ran out of memory after the last deploy (exit 137). Scale to 512 MB?</div>
<div class=chips aria-hidden=true><span>Scale to 512 MB</span><span>Show logs</span></div></div></div></section>
<section class="s v5s" id=how><div class=wrap><h2 class="t c">Ship in 3 steps</h2><ol class=flow>
<li><b>1</b><span>Bring a repo or Docker image</span></li><li><b>2</b><span>Run <code>aloof deploy</code>, or ask your AI to do it</span></li><li><b>3</b><span>You're live on HTTPS</span></li></ol></div></section>
<section class="s v5s" id=templates><div class=wrap><h2 class="t c">One-click apps</h2><div class=tpls>{tpl}</div><p class=c style="margin-top:22px"><a href="/templates">See all templates →</a></p></div></section>
<section class="s v5s" id=pricing><div class=wrap><h2 class="t c">Pricing</h2><div class="grid g3 pcs">{price("free", "Free", f"includes ${T['free']['included_usage_usd']:g} usage")}{price("hobby", "Hobby", f"includes ${T['hobby']['included_usage_usd']:g} usage")}{price("pro", "Pro", f"includes ${T['pro']['included_usage_usd']:g} usage · 1 user")}</div>
<p class="c muted" style="margin-top:18px">Pay per second. Smallest app from {_money(p0)}/mo. <a href="/pricing">Full pricing →</a></p>
<p class="c muted soonline">Coming soon: predictive scaling · auto right-sizing · GPUs · more regions · <a href="/pricing#gpus">Join waitlist</a></p></div></section>
<section class="s v5s" id=faq><div class=wrap style="max-width:760px"><h2 class="t c">FAQ</h2>{faqh}<p class=c style="margin-top:16px"><a href="/docs/self-driving#faq">All questions →</a></p></div></section>
<section class="ctaband v5cta"><h2>Give your superintelligence a stronger engine.</h2><div class=cta style="justify-content:center"><a class="btn pri" data-cta=deploy_free data-cta-loc=footer_band href="/login#register">Deploy free</a></div></section>{HERO_JS}"""
    return page(f"{brand}: the self-driving cloud for your backends", body, brand,
                desc="Backends that roll back bad deploys, route around failing instances and scale to zero on their own. Drive it from Claude, ChatGPT or Cursor.")
def _pg_rows():
    from .plans import PG_PLANS
    return "".join(f"<tr><td><code>{k}</code> <span class=muted>{p['note']}</span></td><td>{p['vcpu']:g} {'shared' if p['compute'].startswith('shared') else 'dedicated'} vCPU / {p['memory_mb'] if p['memory_mb'] < 1024 else p['memory_mb'] // 1024}{' MB' if p['memory_mb'] < 1024 else ' GB'}</td><td>{p['disk_gb']} GB</td><td><b>${p['price_usd_month']}</b>/mo</td></tr>" for k, p in PG_PLANS.items())
def pricing(brand):
    from .plans import PG_PLANS
    from .stripe_billing import MIN_TOPUP, MAX_TOPUP
    T, R, P = TIERS, RATES, PLANS
    m = lambda k: P[k]["price_usd_month"]; pgm = lambda k: PG_PLANS[k]["price_usd_month"]
    ck = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12 5 5 9-10"/></svg>'
    from . import stripe_billing as _sb
    soon = not _sb.payments_open_cached()
    def plan(k, name, who, feats, cta, hl=False):
        if soon and k != "free": cta = "Paid plans opening soon"
        t = T[k]; lis = "".join(f"<li>{ck}<span>{f}</span></li>" for f in feats)
        return (f"<div class='pp-card{' hl' if hl else ''}'>{'<span class=pp-pop>Most popular</span>' if hl else ''}<div class=pp-name>{name}</div><p class=pp-who>{who}</p>"
                f"<div class=pp-price>${t['fee_usd']:g}<small>/month</small></div>"
                f"<div class=pp-inc>${t['included_usage_usd']:g} of usage included every month</div>"
                f"<a class='btn {'pri' if hl else 'ghost'} pp-cta' href='/login#register' data-cta=deploy_free data-cta-loc=pricing_{k}>{cta}</a>{"<p class='muted pp-soon' style='font-size:13px;margin:8px 0 0;text-align:center'>Start on Free now. Card payments aren't switched on yet, so nothing is charged. <a href=/docs/billing#soon>Details</a></p>" if soon and k != 'free' else ''}<ul class=pp-feat>{lis}</ul></div>")
    free, hob, pro = T["free"], T["hobby"], T["pro"]
    fp = " or ".join(x.replace("shared-1x-", "") for x in free["plans"])
    cards = plan("free", "Free", "Try Aloof, prototypes and side projects.", [
        f"<b>You're never billed.</b> At ${free['included_usage_usd']:g} of usage your apps pause until the 1st or until you upgrade",
        f"Up to {free['max_apps']} apps, {free['max_instances']} instance each, {fp}",
        f"<b>Apps sleep after {free['idle_seconds']//60} minutes idle</b> and wake on the next request in about a second",
        f"{free['max_volume_gb']} GB volumes · {free['log_retention_days']}-day log history", "HTTPS, custom domains and TLS"], "Start free") + \
        plan("hobby", "Hobby", "Solo developers with apps that need to stay on.", [
        f"<b>${hob['included_usage_usd']:g} of usage included</b>, then per second from prepaid credit",
        f"Up to {hob['max_apps']} apps · every instance size", "<b>Always-on or sleep</b>, your choice per app",
        f"{hob['log_retention_days']}-day log history", "Volumes, managed Postgres, custom domains"], "Start with Hobby", True) + \
        plan("pro", "Pro", "One developer running bigger production workloads.", [
        f"<b>${pro['included_usage_usd']:g} of usage included</b>, then per second from prepaid credit",
        "Unlimited apps · every instance size · 1 seat", "Always-on or sleep, per app", f"{pro['log_retention_days']}-day log history",
        "No support plan or SLA included"], "Start with Pro")
    # worked examples, computed from plans.py
    e2 = m("shared-1x-512mb") + pgm("pg-dev"); e3a = 2 * m("shared-1x-1gb"); e3e = 50 * R["egress_usd_gb"]; e3 = e3a + pgm("pg-basic") + e3e
    ex = [(f"A side project on Free ({_gb(P['shared-1x-256mb']['memory_mb'])} GB, sleeps when idle)", f"under ${free['included_usage_usd']:g}", f"<b>$0</b>. At ${free['included_usage_usd']:g} apps pause; you're never billed"),
          ("One 512 MB API, always-on, on Hobby", f"${m('shared-1x-512mb'):.2f}", f"<b>${hob['fee_usd']:g}</b> (fits in included usage)"),
          ("A 512 MB API + Postgres <code>pg-dev</code>, always-on, on Hobby", f"${m('shared-1x-512mb'):.2f} + ${pgm('pg-dev'):.2f} = ${e2:.2f}", f"<b>${hob['fee_usd']:g} plan + ${e2-hob['included_usage_usd']:.2f}</b> from credit"),
          ("On Pro: two 1 GB APIs always-on + Postgres <code>pg-basic</code> + 50 GB egress", f"${e3a:.2f} + ${pgm('pg-basic'):.2f} + ${e3e:.2f} = ${e3:.2f}", f"<b>${pro['fee_usd']:g} plan + ${e3-pro['included_usage_usd']:.2f}</b> from credit")]
    exh = "".join(f"<tr><td data-l='You run'>{a}</td><td data-l='Monthly usage' class=num>{b}</td><td data-l='You pay' class=num>{c}</td></tr>" for a, b, c in ex)
    def rows(cls, lab):
        return "".join(f"<tr><td><code>{k}</code></td><td>{p['vcpu']:g} {lab}</td><td>{_gb(p['memory_mb'])} GB</td><td class=num><b>{_money(p['price_usd_month'])}</b></td><td class=num>${p['price_usd_hr']:.4f}</td></tr>"
                       for k, p in P.items() if p["kind"] == "cpu" and not p["legacy"] and p["cpu_class"] == cls)
    pgline = " · ".join(f"<code>{k}</code> ${v['price_usd_month']:.2f}" for k, v in PG_PLANS.items())
    inc = [("Per-second billing", "Pay only for the seconds an instance runs."), ("Scale to zero", "Sleeping apps cost nothing for compute."),
           ("HTTPS and custom domains", "Certificates issued automatically, free."), ("Free inbound data", "You only pay for egress."),
           ("Deploy from your AI assistant", "Remote MCP server with OAuth, scoped tokens."), ("No card charges for overage", "Usage beyond your plan comes from prepaid credit.")]
    inch = "".join(f"<div><b>{a}</b><p>{b}</p></div>" for a, b in inc)
    faq = [("What happens when I use more than my plan's included usage?", f"Usage beyond it is paid per second from your <b>prepaid credit balance</b>. We never charge your card for overage. Top up any amount from ${MIN_TOPUP:,} to ${MAX_TOPUP:,} via Stripe. Prepaid credit never expires."),
           ("What happens if my prepaid balance hits $0?", "Your apps are <b>scaled to zero and suspended</b>. Before that we warn you: by email when your credit has about 3 days left and again under 24 hours (based on your current usage), plus a banner in the dashboard. You can also set your own dollar threshold. A top-up resumes suspended apps. Volumes and backups keep billing while apps are suspended, so your data stays safe. Keep a buffer in your balance for production apps."),
           ("How is egress billed?", f"Outbound data costs ${R['egress_usd_gb']:.2f}/GB and counts toward your included usage. Inbound data is free."),
           ("Do my apps sleep?", f"On Free, yes: after {free['idle_seconds']//60} minutes idle, and they wake on the next request in about a second. On Hobby and Pro you choose per app: always-on, or scale to zero and pay nothing while idle (volumes are still billed)."),
           ("Where does my code run?", "All compute runs on Hetzner Cloud in Nuremberg, Germany. Encrypted off-site backups are stored in Helsinki, Finland. Typical latency is ~10–40 ms within Europe, ~90–110 ms from US East and ~150–200 ms from India or US West. More regions will be listed only once they're live."),
           ("Can I upgrade, downgrade or cancel anytime?", "Yes. Upgrades are prorated. Downgrades apply immediately, with no refund. If you cancel a card subscription, your plan stays active until the end of the paid period and then drops to Free. Nothing is deleted when you cancel. Details in the <a href='/docs/billing#cancel'>billing docs</a>."),
           ("Can my AI assistant deploy for me?", "Yes. Connect Claude, ChatGPT or Cursor to our remote MCP server (OAuth 2.1 with PKCE) and ask it to deploy, read logs, scale or add a domain. You can revoke their access anytime by deleting the key under API keys in the dashboard. Agents that prefer REST can use <a href='/llms.txt'>llms.txt</a> and our <a href='/api/docs'>OpenAPI spec</a>.")]
    faqh = "".join(f"<details class=faq><summary>{q}</summary><p>{a}</p></details>" for q, a in faq)
    head = "<tr><th>Instance</th><th>CPU</th><th>RAM</th><th class=num>Always-on / month</th><th class=num>Per hour</th></tr>"
    return page(f"Pricing · {brand}", f"""<section class="pp-hero"><div class=wrap><h1>Pricing</h1><p class=lead>Your plan fee comes back as included usage each month. Beyond that you pay per second from prepaid credit, and Free never bills you.</p>
<p class="muted pp-sub2">Included usage resets monthly and doesn't roll over. Prepaid credit never expires. Compute runs in Nuremberg, Germany.</p></div></section>
<section class=pp><div class=wrap>
<div class=pp-grid>{cards}</div>
<p class="muted pp-note">Need committed capacity, dedicated nodes or invoicing? <a href="mailto:sales@aloof.si"><b>Enterprise: contact us</b></a>.</p>
<h2 class=pp-h id=examples>What will I pay?</h2><p class=pp-sub>Real setups, priced always-on for a 30-day month.</p>
<div class=scroll><table class="tbl pp-tbl pp-ex"><tr><th>You run</th><th class=num>Monthly usage</th><th class=num>You pay</th></tr>{exh}</table></div>
<p class="muted pp-d" style="margin-top:12px">Apps that scale to zero cost nothing while asleep; you pay only for the seconds they run. Volumes are still billed while an app sleeps.</p>
<h2 class=pp-h id=compute>Instance prices</h2><p class=pp-sub>Billed per second, from included usage first, then prepaid credit. The monthly price assumes the instance runs all 30 days.</p>
<div class=scroll><table class="tbl pp-tbl">{head}{rows("shared", "shared")}{rows("performance", "dedicated")}</table></div>
<p class="muted pp-d" style="margin-top:12px">Shared CPUs are burstable with a guaranteed 1/8-core baseline. Performance CPUs give you a full reserved vCPU.</p>
<div class=pp-also>
<div><b>Also billed</b><p>Volumes ${R['volume_usd_gb_month']:.2f}/GB-month · egress ${R['egress_usd_gb']:.2f}/GB · backups and snapshots ${R['backup_usd_gb_month']:.2f}/GB-month · extra RAM ${R['extra_ram_usd_gb_month']:.2f}/GB-month</p></div>
<div><b>Free</b><p>Inbound data, custom domains, TLS</p></div>
<div id=postgres><b>Managed Postgres 16</b><p>{pgline} per month. Daily backups, 7 kept.</p></div></div>
{_gpu_section()}
<h2 class=pp-h id=included>Included on every plan</h2><div class=pp-how>{inch}</div>
<h2 class=pp-h id=faq>Pricing FAQ</h2><div class=pp-faq>{faqh}</div>
<div class=pp-end><h2>Start free, upgrade when you need to.</h2><p class=muted>{free['max_apps']} apps and ${free['included_usage_usd']:g} of usage a month on Free. No card required.</p><a class="btn pri" href="/login#register" data-cta=deploy_free data-cta-loc=pricing_end>Create an account</a> <a class="btn ghost" href="/docs/billing">Billing docs</a></div>
</div></section>""", brand, desc="Aloof pricing: Free, Hobby $5/month and Pro $20/month, each with the fee back as included usage. Compute billed per second in Nuremberg, Germany; volumes, egress and managed Postgres in USD.")
DOCS = ["quickstart", "self-driving", "cli", "deploy", "config", "private", "github", "ci", "processes", "networking", "releases", "gpu", "volumes", "postgres", "console", "logs", "autoscaling", "domains", "billing", "mcp", "api", "nodes", "troubleshooting", "support", "templates", "self-healing"]
TITLES = {"quickstart": "Quickstart", "self-driving": "How self-driving works", "cli": "CLI", "deploy": "Deploying apps", "config": "aloof.toml reference", "private": "Private registries & repos", "github": "GitHub App: push-to-deploy & previews", "ci": "Deploy tokens & CI", "processes": "Workers, release commands & cron", "networking": "Private networking, proxy, IPs & certs", "gpu": "GPU workloads", "volumes": "Volumes", "autoscaling": "Autoscaling & scale to zero",
          "domains": "Custom domains", "releases": "Releases & rollback", "postgres": "Managed Postgres", "console": "SSH & exec console", "logs": "Logs, retention & drains", "billing": "Billing, invoices & tax", "mcp": "MCP for AI assistants", "api": "REST API", "nodes": "Bring your own nodes", "troubleshooting": "Troubleshooting & FAQ", "support": "Support", "templates": "One-click templates", "self-healing": "Self-healing deploys"}
def docs(pg, brand, base, api, mcp):
    if pg not in DOCS: return None
    from . import domains as _d
    _ips = [x.strip() for x in os.environ.get('PAAS_EDGE_IPS', '').split(',') if x.strip()]; _v4, _v6 = _d.edge_split(_ips)
    EDGE_A = ' or '.join(f'<code>{x}</code>' for x in _v4) or 'our edge IP (<code>aloof ips &lt;app&gt;</code>)'
    EDGE_AAAA_ROW = ('<tr><td>AAAA</td><td><code>example.com</code></td><td>' + ' or '.join(f'<code>{x}</code>' for x in _v6) + '</td><td>IPv6, added next to the A record (optional, but if you publish an AAAA it must be ours)</td></tr>') if _v6 else ''
    AAAA_SENT = (' (and the <b>AAAA</b> record for IPv6)') if _v6 else ''
    DAYS, LIM_FREE, LIM_HOBBY, LIM_PRO = _d.CLAIM_TTL_DAYS, _d.limit_for('free'), _d.limit_for('hobby'), _d.limit_for('pro')
    ADD_N, VER_N, GAP = _d.RATE['add'][0], _d.RATE['verify'][0], _d.VERIFY_MIN_GAP
    from .site_devx import pages as _devx_pages
    c = ({
"quickstart": f"""<h1>Quickstart</h1><ol><li><a href="/login#register">Create an account</a>. New accounts start with <b>$5 of prepaid credit</b>, and the Free plan includes $1/month of usage with a hard cap. Top up or pick Hobby/Pro when you need more.</li>
<li>Install the CLI. It's a single Python 3 script and runs on <b>macOS and Linux</b>. On <b>Windows</b>, use it inside <a href="https://learn.microsoft.com/windows/wsl/install" rel=noopener>WSL</a>.
<pre># system-wide (asks for your password)
sudo curl -fsSL {api}/cli/aloof -o /usr/local/bin/aloof && sudo chmod +x /usr/local/bin/aloof

# or without sudo, into ~/.local/bin (make sure it's on your PATH)
mkdir -p ~/.local/bin && curl -fsSL {api}/cli/aloof -o ~/.local/bin/aloof && chmod +x ~/.local/bin/aloof</pre>
Check it with <code>aloof --help</code>. It needs <code>python3</code>, which macOS and most Linux distributions already have.</li>
<li>Log in. New here? <code>aloof signup you@company.com</code> creates an account and saves the key for you. Already have an account? Create a key under Dashboard → API keys, then run:
<pre>aloof login</pre>
Paste the key at the hidden prompt. The CLI checks that it works and saves it to <code>~/.aloof.json</code>, readable only by you (the key has full access to your account). For scripts, <code>aloof login --key-stdin</code> reads the key from stdin. In CI, set <code>ALOOF_API_URL</code> and <code>ALOOF_API_TOKEN</code> (a <a href="/docs/ci">deploy token</a>) instead.</li>
<li>Deploy, with a name of your own (app names are global, so pick something unique such as <code>my-api-1234</code>): <pre>aloof deploy &lt;your-app-name&gt; --image nginx:alpine --port 80
live: https://my-api-1234.{base}/</pre>Without <code>--plan</code> you get the largest plan your tier allows (Free: <code>shared-1x-512mb</code>). If you see <code>app name taken</code>, pick another name.</li></ol>
<p>Your app must listen on <code>$PORT</code> (default 8080) over HTTP. Configure it with env vars, and keep persistent data on a <a href="/docs/volumes">volume</a>. If something goes wrong, see <a href="/docs/troubleshooting">Troubleshooting</a>.</p>""",
"cli": """<h1>CLI</h1><pre>aloof plans | apps | usage
aloof deploy &lt;app&gt; --image IMG | --git URL [--ref main] [--plan shared-1x-1gb] [--port 8080] [-e K=V] [-n 2]
aloof status|logs|destroy &lt;app&gt;
aloof scale &lt;app&gt; 3
aloof env &lt;app&gt; K=V K2=          # K2= removes; values are encrypted at rest
aloof volumes create data --size 5 ; aloof volumes attach &lt;app&gt; data --path /data
aloof autoscale &lt;app&gt; --min 0 --max 4 --concurrency 10 [--cpu 70] --idle 300
aloof health &lt;app&gt; --path /healthz --failures 3
aloof domains &lt;app&gt; add api.example.in | verify &lt;domain&gt; | list | remove &lt;domain&gt;
aloof releases &lt;app&gt; ; aloof rollback &lt;app&gt; [version]
aloof pg create db --plan pg-basic ; aloof pg attach db &lt;app&gt; ; aloof pg backup|backups|restore|url|destroy db
aloof snapshots create|list &lt;volume&gt; ; aloof snapshots restore &lt;volume&gt; &lt;snapshot-id&gt;
aloof ssh &lt;app&gt; ; aloof exec &lt;app&gt; -- ls /data
aloof drains &lt;app&gt; add https://logs.example.com/ingest ; aloof history &lt;app&gt; --since 24 --grep error ; aloof audit</pre>
<p>Config is read from <code>~/.aloof.json</code> (<code>{"api": "...", "key": "pk_..."}</code>) or the <code>ALOOF_API_URL</code> / <code>ALOOF_API_KEY</code> env vars.</p>""",
"deploy": f"""<h1>Deploying apps</h1><p><b>From an image</b>: any public image or private registry image (<code>--image ghcr.io/me/api:1.4</code>).</p>
<p><b>From git</b>: we build the repo's <code>Dockerfile</code> (<code>--git URL --ref main</code>, optional <code>subdir</code>) and push it to the {brand} registry, where your nodes pull it.</p>
<p>Redeploys are rolling: new instances must pass a readiness check before old ones stop, and a release that fails it never takes traffic (see <a href="/docs/releases">releases &amp; rollback</a>). Env vars, volumes, autoscale and health settings carry over between deploys.</p>
<p>Health checks: set an HTTP path with <code>aloof health app --path /healthz</code>. After N consecutive failures the instance is replaced.</p>""",
"gpu": """<h1>GPU workloads</h1><p><b>Coming soon.</b> GPUs aren't deployable yet; join the waitlist on the <a href='/pricing#gpus'>pricing page</a> or with <code>POST /v1/waitlist</code>. When they launch:</p><p>Pick a GPU plan: <code>gpu-l4</code>, <code>gpu-rtx4090</code>, <code>gpu-l40</code>, <code>gpu-a100</code> or <code>gpu-h100</code> (billed per second; see <a href='/pricing'>pricing</a>). Each GPU instance gets a dedicated node with the NVIDIA container runtime, and your container sees the GPU via CUDA (<code>--gpus</code>).</p>
<pre>aloof deploy llm --image vllm/vllm-openai:latest --plan gpu-l4 --port 8000 -e MODEL=Qwen/Qwen2.5-7B-Instruct
aloof autoscale llm --min 0 --max 2 --idle 600</pre>
<p>Planned capacity: Hyperstack GPU VMs (A100, H100, L40; Canada/Europe/US) and Runpod pods (L4, RTX 4090; many regions), tried in order of cost and availability. GPU nodes are released after 10 idle minutes. A cold start on GPU means provisioning a node, which takes a few minutes, so keep <code>min 1</code> for latency-sensitive inference.</p>""",
"volumes": """<h1>Volumes</h1><pre>aloof volumes create pgdata --size 10
aloof volumes attach db pgdata --path /var/lib/postgresql/data
aloof volumes detach db pgdata      # data is kept
aloof volumes delete pgdata         # must be detached first</pre><p>A volume lives on one node, so apps with volumes always run there. Volumes are single-writer: on redeploy the old instance stops before the new one starts.</p>
<h2>Snapshots</h2><pre>aloof snapshots create pgdata
aloof snapshots list pgdata
aloof snapshots restore pgdata snap_20261013080000_ab12cd   # stops the attached app, restores, starts it again</pre>
<p>A snapshot is a compressed archive of the whole volume, kept on the edge host's disk (10 per volume, oldest pruned first) and billed at $0.22 per GB-month of archive size. Snapshots of volumes on remote nodes aren't supported yet. Once a day each volume is also snapshotted, encrypted (AES-256-GCM) and copied off-site (7 daily, 4 weekly, 3 monthly): <code>aloof snapshots offsite &lt;volume&gt;</code>, <code>aloof snapshots offsite-restore &lt;volume&gt; &lt;id&gt;</code>.</p>""",
"releases": """<h1>Releases &amp; rollback</h1><p>Every deploy, env change, volume or database attach, and rollback creates a numbered release with the exact image and config it ran.</p>
<pre>aloof releases api
v4   failed      2026-10-13 09:12  deploy         ghcr.io/me/api:1.5
v3   active      2026-10-13 09:02  config change  ghcr.io/me/api:1.4
aloof rollback api        # previous release
aloof rollback api 2      # a specific one</pre>
<p><b>Zero-downtime deploys</b>: new instances start next to the old ones and must pass readiness (your HTTP health path, or any HTTP response) before traffic moves and the old ones stop. If a new instance fails readiness, it's removed, the release is marked <code>failed</code>, and the previous release keeps serving. Apps with a volume are single-writer, so their old instance stops first and there is a short gap.</p>
<p><b>Rollback</b> redeploys an earlier release's image, env, plan, port, command, scale and health settings, without a rebuild (git builds are tagged per release; the last 20 images are kept). Volumes, database attachments and domains stay as they are now. Also available as <code>POST /v1/apps/{name}/rollback</code>, the <code>rollback_app</code> MCP tool, and Dashboard → app → Releases.</p>
<h2 id=self-heal>Self-healing deploys</h2><p><b>On by default</b> for every app, no setup needed (see <a href="/docs/self-healing">Self-healing deploys</a> to turn it off). For 5 minutes after each deploy or config change, the edge compares the new release with the previous one. If the new release's 5xx rate is more than 5 percentage points higher <i>and</i> more than double the previous release's, or its p95 latency is more than double <i>and</i> more than 500 ms slower, it is rolled back automatically to the previous release. The reason and the numbers are recorded on the release (<code>aloof releases</code>, the API, MCP and dashboard), in the app's events and in the audit log.</p>
<p>To avoid false alarms: both releases need at least 100 requests; at least 10 failing (or slow) requests from at least 3 different client IPs are required; 5xx on routes that were already failing before the deploy don't count; upstream timeouts and client disconnects don't count. With no baseline nothing is rolled back. Rollbacks are never auto-rolled back, each deploy gets at most one automatic rollback, and a newer deploy or deleting the app cancels it. The watch lives in the edge process, so an edge restart during the 5 minutes ends that watch. Turn it off per app with <code>aloof self-heal &lt;app&gt; off</code> or the <code>set_self_heal</code> MCP tool.</p>""",
"postgres": """<h1>Managed Postgres</h1><pre>aloof pg plans
aloof pg create shop-db --plan pg-basic
aloof pg attach shop-db api            # injects DATABASE_URL into api and redeploys it
aloof pg backup shop-db ; aloof pg backups shop-db ; aloof pg restore shop-db pgb_…
aloof pg url shop-db                   # prints the connection URL (audited)</pre>
<p>Postgres 16 runs next to your apps in the EU and is reachable only over a private network that its attached apps join (hostname <code>pg-&lt;name&gt;</code>). It's never exposed on the public edge. Data lives on a volume.</p>
<p><b>Backups</b>: a <code>pg_dump</code> runs daily; the last 7 are kept on the edge host's disk, with on-demand backups and restore whenever you need them. Backup storage costs $0.22 per GB-month. These local copies are logical backups, not point-in-time recovery.</p>
<p><b>Off-site</b>: once a day each database is also encrypted on our side (AES-256-GCM) and uploaded to S3-compatible object storage in a different location. Retention is 7 daily, 4 weekly and 3 monthly copies, and the newest copy is test-restored into a scratch Postgres every week. List and restore with <code>aloof pg offsite shop-db</code> and <code>aloof pg offsite-restore shop-db &lt;id&gt;</code>, or from Dashboard → Postgres.</p>
<table><tr><th>Plan</th><th>CPU / RAM</th><th>Disk</th><th>Price</th></tr>{PGROWS}</table>
<p>Not yet: high availability/replicas, point-in-time recovery, extensions beyond the official image, or attaching to apps on remote nodes.</p>
<h2 id=local>Connecting from your laptop</h2><p>Databases are never on the public internet. Open a tunnel with the CLI, then connect to <code>localhost</code>:</p>
<pre>aloof pg url shop-db                    # user, password and database name (audited)
aloof proxy 5432 -a pg-shop-db          # forwards localhost:5432 to the database; leave it running
psql "postgres://aloof:&lt;password&gt;@localhost:5432/&lt;database&gt;"</pre>
<p>The tunnel runs over your API key, and each session is written to the audit log. If port 5432 is taken locally, use <code>aloof proxy 15432:5432 -a pg-shop-db</code> and connect to port 15432.</p>
<h2 id=limits>Connections and limits</h2><p>Each database runs the stock Postgres 16 configuration, so <code>max_connections</code> is Postgres's default of 100, shared by every app and tunnel connected to it. On the smaller plans memory runs out well before that, so use a connection pool in your app (most frameworks have one) and keep it to a few connections per instance. Storage is the plan's disk size, listed above.</p>
<h2 id=resize>Changing plan or disk size</h2><p><b>Support will resize it for you.</b> Send a request through the <a href="/docs/support">Support page</a> with the database name, the plan you want (see the table above) and a time window that suits you. The move involves a short write pause while the data is copied to the new size, and the new plan is billed from the switch.</p>
<p><b>Or do it yourself</b>: resizing in place isn't self-serve yet, and backups restore only into the database they came from, so copy the data over a tunnel. Create the new database (<code>aloof pg create shop-db2 --plan pg-standard</code>), run <code>pg_dump</code> against the old one through <code>aloof proxy</code>, <code>pg_restore</code> into the new one the same way, then attach the new database to your app (which updates <code>DATABASE_URL</code> and redeploys it). Destroy the old one when you're happy.</p>""",
"console": """<h1>SSH &amp; exec console</h1><pre>aloof ssh api                 # interactive shell (bash or sh) in a running instance
aloof ssh api --instance 3f2a…
aloof exec api -- ls -la /data   # one command, prints output, exits with its code</pre>
<p>The console is a WebSocket at <code>/v1/apps/{name}/console</code>, authenticated with your API key (CLI) or your dashboard session, and only on our own origin, which blocks cross-site use. Dashboard → app → Console gives you the same shell in the browser.</p>
<p><b>Audited</b>: opening and closing a session, each line you enter, and every exec command and its exit code are written to the audit log with your IP (<code>aloof audit</code>, Dashboard → Audit log). Don't type secrets on the command line; they would be recorded.</p>""",
"logs": """<h1>Logs, retention &amp; drains</h1><pre>aloof logs api                         # live: platform events + recent output of each instance
aloof history api --since 24 --grep error
aloof drains api add https://logs.example.com/ingest
aloof drains api add syslog+tls://logs.example.com:6514
aloof drains api list | remove drn_…</pre>
<p><b>Retention</b>: stdout/stderr and platform events are collected every few seconds and kept for 1 day (Free), 7 days (Hobby) or 30 days (Pro). Query them with <code>GET /v1/apps/{name}/logs/history?since=&amp;contains=</code>.</p>
<p><b>Drains</b> forward every line as it's collected. HTTPS drains receive a JSON array of <code>{ts, app, instance, stream, message}</code> per POST, and credentials in the URL (<code>https://user:token@host/path</code>) are sent as Basic auth. Syslog drains (<code>syslog+tls</code>, <code>syslog+tcp</code>, <code>syslog+udp</code>) receive RFC 5424 lines with octet-counting framing on TCP/TLS. Drain URLs are encrypted at rest and shown redacted, and private or loopback targets are refused.</p>""",
"autoscaling": """<h1>Autoscaling &amp; scale to zero</h1><p>Every 3s {brand} looks at in-flight requests per instance (and optionally CPU %), and scales between <code>min</code> and <code>max</code>. Scale-down waits 15s.</p>
<pre>aloof autoscale api --min 1 --max 6 --concurrency 20 --cpu 70
aloof autoscale preview --min 0 --max 2 --idle 120    # scale to zero after 2 idle minutes</pre>
<p>With <code>min 0</code>, the first request after idle is held at the edge while an instance starts (about 1s for a small CPU app).</p>""".replace("{brand}", brand),
"domains": f"""<h1>Custom domains</h1><p>Every app gets <code>https://&lt;app&gt;.{base}</code>. You can also serve it from your own domain, such as <code>api.example.com</code> or <code>example.com</code>, with a free HTTPS certificate that we issue and renew automatically. It works the same way as on Fly.io, Railway and Render: add the domain, create a few DNS records at your registrar, and we do the rest.</p>
<h2 id=steps>Step by step</h2><ol>
<li><b>Add the domain</b> (dashboard → your app → Domains, or the CLI, API or MCP):<pre>aloof domains my-app add api.example.com</pre></li>
<li><b>Create the DNS records it prints</b> at the place where your DNS is hosted (usually your registrar, or Cloudflare, Route 53, etc.):
<table><tr><th>Type</th><th>Name</th><th>Value</th><th>Purpose</th></tr>
<tr><td>TXT</td><td><code>_aloof-challenge.api.example.com</code></td><td><code>aloof-verify=&lt;token&gt;</code></td><td>proves you own the domain (always needed)</td></tr>
<tr><td>CNAME</td><td><code>api.example.com</code></td><td><code>my-app.{base}</code></td><td>routes traffic for a <b>subdomain</b></td></tr>
<tr><td>A</td><td><code>example.com</code></td><td>{EDGE_A}</td><td>routes traffic for a <b>root (apex) domain</b>, or instead of the CNAME</td></tr>
{EDGE_AAAA_ROW}</table>The exact values, including your own token, are shown in the dashboard, <code>aloof domains my-app list</code> and the API response.</li>
<li><b>Wait a few minutes.</b> We re-check DNS automatically (after 2 minutes, then every 5, 10 and 30 minutes, backing off to every 6 hours), so a pending domain becomes <b>verified</b> by itself. To check right now, press <b>Check now</b> in the dashboard or run <code>aloof domains my-app verify api.example.com</code>.</li>
<li><b>Open <code>https://api.example.com</code></b>. The certificate is issued on the first HTTPS request, which usually takes a few seconds.</li></ol>
<h2 id=subdomain-vs-apex>Subdomain or root domain?</h2><a id=root></a><ul>
<li><b>Subdomain</b> (<code>api.example.com</code>, <code>www.example.com</code>): add a <b>CNAME</b> to <code>&lt;app&gt;.{base}</code> plus the TXT record. This is the simplest option, and it keeps following us if our server address ever changes.</li>
<li><b>Root / apex domain</b> (<code>example.com</code>): DNS doesn't allow a plain CNAME there. Add the <b>A</b> record{AAAA_SENT} plus the TXT record. Or, if your DNS provider offers <b>ALIAS, ANAME or CNAME flattening</b> (Cloudflare, Namecheap, DNSimple, Route 53 for AWS targets only, and others), point that record at <code>&lt;app&gt;.{base}</code> instead. Use one style or the other, not both.</li></ul>
<h2 id=www>www and root domain together</h2><p>Add the one you want as your main address, for example <code>example.com</code>. Once it is verified, we also answer on <code>www.example.com</code> with a permanent redirect (HTTP 308, path and query kept) to <code>https://example.com</code>, with no extra verification, as long as <code>www</code> points at us as well (CNAME to <code>&lt;app&gt;.{base}</code>, or the same A/AAAA records). The same works the other way: if you add <code>www.example.com</code> and verify it, <code>example.com</code> redirects to it. If you'd rather serve both without redirecting, add both as separate domains (each counts toward your limit). The redirect is only ever to a domain you have verified.</p>
<h2 id=registrars>Registrar and DNS-provider tips</h2>
<p>In every panel: a <b>Name/Host</b> field is usually relative to your domain, so for <code>api.example.com</code> enter <code>api</code> (and <code>_aloof-challenge.api</code> for the TXT record). For the root domain the name is <code>@</code>. Remove any older A, AAAA or CNAME record for the same name first. A name can't have a CNAME next to other records.</p>
<h3 id=cloudflare>Cloudflare</h3><p>DNS → Records → Add record. Set the routing records (CNAME, A, AAAA) to <b>DNS only</b> (grey cloud). With the orange-cloud proxy on, the domain resolves to Cloudflare's addresses instead of ours, so the routing check fails and we can't issue a certificate; domains behind the proxy aren't supported. The TXT record is never proxied. Cloudflare allows a CNAME on the root domain (it flattens it for you), so <code>example.com</code> → CNAME <code>&lt;app&gt;.{base}</code> works there.</p>
<h3 id=godaddy>GoDaddy</h3><p>My Products → your domain → DNS → Add New Record. GoDaddy has no ALIAS/ANAME and no CNAME on the root, so for <code>example.com</code> use the <b>A</b> record{AAAA_SENT} with Host <code>@</code>, and delete the default "Parked" A record. Subdomains use a CNAME with Host <code>api</code>. TXT Host: <code>_aloof-challenge.api</code>. GoDaddy's minimum TTL is 600 seconds, so allow about 10 minutes.</p>
<h3 id=namecheap>Namecheap</h3><p>Domain List → Manage → Advanced DNS → Add New Record. Host is <code>api</code> or <code>@</code>. For the root domain you can use an <b>ALIAS Record</b> to <code>&lt;app&gt;.{base}</code> or the A record{AAAA_SENT}. Delete the default "URL Redirect" or parking records for the same host. TXT Host: <code>_aloof-challenge.api</code>. The nameservers must be Namecheap BasicDNS or PremiumDNS for Advanced DNS to apply.</p>
<h3 id=route53>Amazon Route 53</h3><p>Hosted zones → your zone → Create record. Name is <code>api</code> (leave it empty for the root domain). Route 53's "Alias" option only targets AWS resources, so it can't point at us: for the root domain use <b>A</b> and <b>AAAA</b> records with the plain values, and for subdomains a <b>CNAME</b>. Put the TXT value in quotes: <code>"aloof-verify=…"</code>.</p>
<h2 id=propagation>Propagation</h2><p>New records usually show up within a few minutes, but resolvers can cache the old answer for as long as the previous record's TTL (often up to an hour, rarely 24–48 hours). There's nothing to do while you wait: we keep checking, and you can also press Check now. Manual checks are rate limited (see Limits).</p>
<h2 id=status>Status and monitoring</h2><p>Each domain has a status and a last-checked time (dashboard, CLI <code>aloof domains &lt;app&gt; list</code>, API <code>GET /v1/domains</code>, MCP <code>list_domains</code>):</p><ul>
<li><b>pending</b>: not verified yet. We keep re-checking. An unverified claim is <b>released after {DAYS} days</b>, which frees the name for anyone to claim, so add it again if you need more time.</li>
<li><b>verified</b>: ownership and routing both check out, and HTTPS is on. We re-check every 30 minutes.</li>
<li><b>dns_error</b>: it was verified, but its DNS no longer checks out (a record was changed or deleted, or a proxy was switched on). The domain is <b>not removed</b> and keeps being served and renewed as long as traffic still reaches us, but fix the records. It returns to verified automatically once DNS is right again. One failed lookup doesn't flag a domain: it has to fail on two checks in a row.</li></ul>
<h2 id=limits>Limits</h2><ul>
<li>Custom domains per account (every app counts, pending ones too): <b>Free {LIM_FREE}, Hobby {LIM_HOBBY}, Pro {LIM_PRO}</b>. Remove a domain to free a slot; the www/apex redirect twin doesn't count.</li>
<li>Adding domains: up to {ADD_N} per hour per account. Manual verification: up to {VER_N} per hour per account, and one check per domain every {GAP} seconds. Over the limit you get HTTP 429 with a <code>Retry-After</code> header. The automatic re-checks don't count.</li>
<li>The platform also caps the total number of certificate-issuing custom hostnames. If it were ever full, a domain with correct DNS stays pending and verifies automatically when there is room.</li>
<li>Not supported yet: wildcard domains (<code>*.example.com</code>), and domains behind Cloudflare's orange-cloud proxy.</li></ul>
<h2 id=verify-failed>If verify fails</h2><ul>
<li><b><code>TXT … not found</code></b>: the record hasn't propagated yet, or the name is wrong. Many DNS panels add your domain automatically, so enter just <code>_aloof-challenge.api</code> rather than the full name. Check with <code>dig TXT _aloof-challenge.api.example.com +short</code>.</li>
<li><b><code>CNAME points to …</code></b> or <b><code>resolves to …; expected …</code></b>: the routing record points somewhere else. Remove old A/AAAA/CNAME records for the same name, turn off proxying (Cloudflare), and check with <code>dig api.example.com +short</code>. If you publish an AAAA record it must be ours too, or IPv6 visitors would land elsewhere; remove it or use the value we show.</li>
<li><b>"domain claimed by another account"</b>: someone already holds a verified claim, or a recent one, on that name. Unverified claims are released after {DAYS} days, and another account can take over a pending claim older than an hour. If you own the domain and a verified claim is wrong, contact <a href="/docs/support">Support</a>.</li>
<li><b>Verified, but HTTPS shows a certificate error</b>: give it a minute after the first request. CAA records on your domain must allow Let's Encrypt (<code>letsencrypt.org</code>). Also, <code>aloof certs</code> shows the status.</li></ul>
<h2 id=automation>CLI, API and MCP</h2><pre>aloof domains my-app add example.com | list | verify example.com | remove example.com
POST   /v1/apps/&lt;app&gt;/domains            {{"domain": "example.com"}}
POST   /v1/apps/&lt;app&gt;/domains/&lt;domain&gt;/verify
GET    /v1/apps/&lt;app&gt;/domains            (one app)     GET /v1/domains  (whole account, with your limit)
DELETE /v1/apps/&lt;app&gt;/domains/&lt;domain&gt;
MCP tools: add_domain, list_domains, verify_domain, remove_domain</pre>
<p>Each domain in the response carries <code>status</code>, <code>last_checked_at</code>, <code>dns_records</code> (<code>ownership</code>, <code>routing</code>, plus <code>a</code>, <code>aaaa</code> and <code>recommended</code>) and, for root or <code>www</code> domains, <code>www_pair</code>.</p>""",
"billing": """<h1>Billing, invoices &amp; tax</h1>
<p><b>The short version:</b> you pick a plan (Free, Hobby $5/month or Pro $20/month), and the plan fee comes back to you as included usage. Apps are billed per second only while they run. If you use more than your plan includes, the rest comes out of a prepaid balance that you top up by card. Every new account starts with $5 of credit in that balance. Prices are in US dollars and exclude tax.</p>
<h2 id=cards>Paying by card</h2><p>Card payments go through Stripe Checkout. Stripe stores your card, so we never see the number. There are two things you can pay for by card:</p><ul>
<li><b>Prepaid credit (top-ups)</b>: a one-off payment of $10 to $5,000. The credit is added to your balance as soon as Stripe confirms the payment, and Stripe sends an invoice for it.</li>
<li><b>A plan subscription (Hobby or Pro)</b>: Stripe charges the plan fee when you subscribe and then once a month until you cancel. Your workspace switches to the new plan as soon as Stripe confirms the first payment.</li></ul>
<p><b>Usage is never charged to your card directly.</b> Apps are metered per second while they run. Usage up to your plan's included amount is covered by the plan; anything above it comes out of your prepaid balance. When a paid account's balance runs out, its apps are suspended (volumes and databases are kept) and they resume automatically after a top-up. Unless you turn the alerts off, we email you before that happens: when your balance has about 3 days left, and again at less than 24 hours.</p>
<p>If a card payment fails or you close the checkout page, nothing is added and your plan doesn't change. You can try again from Dashboard → Billing &amp; usage.</p>
<p id=soon class=muted>If card payments are ever paused on our side, the Billing page shows <b>Paid plans opening soon</b> and card payments won't go through until it's back. Nothing is charged when that happens, and your balance and Free plan keep working.</p>
<h2>Upgrading, topping up and cancelling</h2><ul>
<li><b>Upgrade or change plan</b>: Dashboard → Billing &amp; usage → Plan. Choose the plan and either <b>Subscribe by card</b> (Stripe charges the fee monthly) or <b>Pay from balance</b> (this month's fee comes out of your credit, pro-rated).</li>
<li><b>Add credit</b>: Dashboard → Billing &amp; usage → Balance → <b>Add credit</b>, between $10 and $5,000 per top-up. Credit is added as soon as Stripe confirms the payment.</li>
<li><b>Cards, receipts and cancelling a subscription</b>: Dashboard → Billing &amp; usage → <b>Billing portal</b> (Stripe's customer portal).</li></ul>
<h2 id=cancel>Cancelling and changing plans</h2><ul>
<li><b>Cancel any time.</b> There's no contract or minimum term. If you pay for Hobby or Pro by card, cancel in Dashboard → Billing &amp; usage → <b>Billing portal</b>. You keep the plan until the end of the month you've paid for, then the workspace moves to Free and nothing more is charged for the plan. If you pay for the plan from your balance, switch to Free in Dashboard → Billing &amp; usage → Plan. The change is immediate.</li>
<li><b>Upgrades</b> take effect immediately. Paying from your balance, you're charged only for the rest of the current month (pro-rated by day). By card, Stripe bills the new plan from the change.</li>
<li><b>Downgrades</b> take effect at the end of a card-paid month, or immediately for balance-paid plans. Before moving to Free, change or remove apps on plans Free doesn't include; the dashboard tells you which.</li>
<li><b>Plan fees aren't refunded</b> for the part of a month you don't use, whether you cancel, downgrade or stop using the service.</li></ul>
<h2 id=usage>What happens to usage and credit</h2><ul>
<li><b>Cancelling a plan doesn't stop your apps.</b> Compute is billed per second while instances run, and volumes, backups and snapshots are billed until you delete them. To stop all charges, destroy the apps, databases and volumes you no longer need.</li>
<li><b>Included usage</b> resets on the 1st of each month and doesn't carry over.</li>
<li><b>Prepaid credit doesn't expire</b> while your account is open. It stays in your balance after you cancel a plan and keeps paying for any usage.</li>
<li><b>The $5 starting credit</b> every new account gets is promotional. It can be used for any usage but has no cash value and isn't refundable.</li></ul>
<h2 id=refunds>Refunds</h2><ul>
<li><b>Top-ups are non-refundable as a rule</b>, because credit is consumed as you go.</li>
<li><b>Mistakes and surprises</b>: if you created resources by mistake, or a large bill came from something you didn't intend (for example an attack or runaway traffic), contact <a href="/docs/support">support</a>. We'll review it and, where it's fair, refund or credit the difference.</li>
<li><b>Billing errors</b>: if we charged you incorrectly (a duplicate payment, wrong amount or a metering bug), we'll correct it, refunding to the original payment method or crediting your balance, whichever you prefer. Please tell us within 60 days of the charge.</li>
<li>Card refunds go back to the card through Stripe and usually show up within 5–10 business days.</li></ul>
<h2 id=delete>Deleting your account</h2><p>Self-service deletion isn't in the dashboard yet. Request it through <a href="/docs/support">support</a> from the email address on the account, so we can confirm it's you. Before we delete it, cancel any card subscription and export what you want to keep. When the account is deleted, its apps, databases, volumes, backups, API keys and sessions are removed, and any remaining credit is forfeited. Invoices and payment records are kept as long as tax and accounting law requires.</p>
<h2>What is metered</h2><p>Per second, in cents: compute while an instance runs (plan $/hr ÷ 3600), volumes per provisioned GB-month ($0.22, attached or not), backups and snapshots per GB-month ($0.22), and egress at $0.06/GB. Usage up to your plan's included amount (Free $1, Hobby $5, Pro $20) isn't deducted from your balance. Stopped and scaled-to-zero apps cost nothing; volumes are still billed.</p>
<p>See this month's breakdown in Dashboard → Billing &amp; usage. When a paid account's credit runs out, its apps are suspended, and they resume automatically after a top-up.</p>
<p><b>Invoices and tax</b>: Stripe issues an invoice for every payment, and you can download past invoices and receipts from the Billing portal. Prices are in US dollars; taxes may apply where required.</p>
<p><b>Free tier</b> is hard-capped and never draws on your wallet: 3 apps on <code>shared-1x-256mb</code>/<code>shared-1x-512mb</code>, 1 instance each, always scale-to-zero after at most 5 minutes idle, 1 GB of volumes. Once the month's usage reaches the included $1, apps pause (requests get HTTP 402) until the 1st of next month or until you upgrade.</p>
<p>v1 plan ids (<code>cpu-small</code> etc.) still work and are billed at the equivalent current price. Indian customers can optionally be offered INR/UPI top-ups via Razorpay (off by default).</p>
<h2>API</h2><p>Everything above is also available over the API: <code>POST /v1/billing/checkout {"credit_usd": 25}</code>, <code>POST /v1/billing/subscribe {"tier":"pro"}</code>, <code>POST /v1/billing/portal</code>, <code>PUT /v1/billing/tier {"tier":"pro"}</code> (pay from balance) and <code>GET /v1/billing</code>.</p>""",
"mcp": f"""<h1>MCP for AI assistants</h1><p>{brand} exposes a Model Context Protocol server, so assistants can pick plans, deploy, read logs, scale, attach volumes and add domains for you or your users.</p>
<p><b>Remote (OAuth 2.1)</b>: add <code>{mcp}</code> as a connector in Claude, ChatGPT or Cursor. The assistant registers itself (dynamic client registration), sends you to {brand} to approve, and gets a token (PKCE). Approving gives it full access to your account. See <a href="#oauth">How OAuth sign-in works</a> below.</p>
<p><b>Local (stdio)</b>:</p><pre>{{"mcpServers": {{"aloof": {{"command": "python3", "args": ["mcp_server/server.py"],
  "env": {{"ALOOF_API_URL": "{api}", "ALOOF_API_KEY": "pk_..."}}}}}}}}</pre>
<p>Tools ({len(mcp_tool_names())}): {", ".join(mcp_tool_names())}.</p>
<p>Discovery: <a href="/llms.txt">/llms.txt</a>, <a href="/.well-known/mcp.json">/.well-known/mcp.json</a>.</p>
<h2 id=oauth>How OAuth sign-in works</h2>
<h3 id=authorize>The Authorize page</h3><p>When you add the connector, your assistant opens an {brand} page titled <b>Connect &lt;app&gt; to {brand}</b>. It shows the app's name, whether the app is verified, and the address you'll be sent back to. Sign in with your email and password, or paste an API key, then choose <b>Approve</b> or <b>Deny</b>. The page is valid for 10 minutes. If it expires, start the connection again from your assistant.</p>
<p><b>Approving gives the app full access to your account.</b> It can deploy and delete apps, change environment variables and secrets, manage volumes, databases and domains, and spend your credit. There are no narrower permissions yet.</p>
<h3 id=no-password>If you sign in with email links</h3><p>Accounts created with an emailed sign-in link don't have a password. You have two options: sign in to the dashboard, create a key under <b>API keys</b> and paste it on the Authorize page, or set a password under <b>Account</b> and use it there. The Authorize page links to both.</p>
<h3 id=badges>Verified and unverified apps</h3><p>An app is shown as <b>Verified</b> when it sends you back to an https address on a known MCP client's domain: claude.ai, claude.com, chatgpt.com, chat.openai.com, openai.com, cursor.com, cursor.sh, smithery.ai, glama.ai, vscode.dev, insiders.vscode.dev, github.com, windsurf.com, codeium.com or mcpjam.com (or a subdomain of one). Anything else is <b>Unverified</b>: the name was picked by whoever registered the app, so only approve if you started the connection yourself a moment ago and recognise the address shown.</p>
<h3 id=tokens>Token lifetimes</h3><ul><li>Authorization code: 5 minutes, single use.</li><li>Access token: 1 hour.</li><li>Refresh token: 30 days. Each refresh returns a new refresh token and invalidates the old one, so an assistant that keeps using its connection stays signed in.</li></ul>
<h3 id=limits>Limits and cleanup</h3><ul><li>App registration: 10 per IP address per hour (per /64 for IPv6). Over the limit, registration returns HTTP 429 with <code>Retry-After: 3600</code>.</li><li>Registered apps that never complete sign-in are deleted after 24 hours. The assistant just registers again next time.</li></ul>
<h3 id=redirects>Allowed redirect addresses</h3><ul><li><code>https://</code> with any host.</li><li><code>http://</code> only for <code>localhost</code>, <code>127.0.0.1</code> or <code>[::1]</code>, on any port.</li><li>Desktop app schemes: <code>cursor:</code>, <code>vscode:</code>, <code>vscode-insiders:</code>, <code>windsurf:</code> and <code>claude:</code>.</li><li>No fragments (<code>#</code>), wildcards, user:password parts, spaces or control characters. 1 to 10 redirect addresses per app.</li></ul>
<h3 id=revoke>Revoking access</h3><p><b>Disconnect one app:</b> in the dashboard open <b>Account → Connected apps</b> and click <b>Disconnect</b>, or use the API: <code>GET /v1/auth/connected-apps</code> lists each connected client (<code>client_id</code>, <code>name</code>, <code>redirect_host</code>, token counts, <code>expires_at</code>) and <code>DELETE /v1/auth/connected-apps/{{client_id}}</code> deletes its access and refresh tokens immediately. The app has to ask you to approve it again to reconnect.</p><p><b>Cut off everything at once:</b> access is also tied to an API key, so deleting that key makes the assistant's next request fail.</p><ul><li>If you approved with your email and password, {brand} created a key named <code>mcp-oauth</code>. Delete it under <b>API keys</b> in the dashboard. Approving another assistant with your password replaces this key, which also disconnects the previous assistant.</li><li>If you approved by pasting an API key, delete that key.</li></ul>""",
"api": f"""<h1>REST API</h1><p>Base URL <code>{api}</code>. Authenticate with <code>Authorization: Bearer pk_...</code>. The full interactive reference is at <a href="/api/docs">/api/docs</a>.</p>
<pre>curl -X POST {api}/v1/apps -H "Authorization: Bearer $ALOOF_API_KEY" -H "Content-Type: application/json" \\
  -d '{{"name":"my-api-1234","source":{{"image":"ghcr.io/me/api:1"}},"port":8080,"env":{{"DATABASE_URL":"..."}}}}'</pre><h2 id=deploy-button>Deploy buttons</h2><p>The one-click deploy link <code>/deploy?template=&lt;id&gt;</code> needs a signed-in browser session: logged-out visitors are sent to <code>/login?next=…</code> (HTTP 303) and land back on the deploy page after signing in. To deploy a template without a browser, call <code>POST /v1/templates/{{id}}/deploy</code> with an API key.</p>""",
"troubleshooting": f"""<h1>Troubleshooting &amp; FAQ</h1>
<h2 id=402>Requests return HTTP 402</h2><p>402 means the app is paused for billing, not that it crashed. On the <b>Free</b> plan it happens when the month's usage reaches the included $1: apps resume on the 1st of next month, or straight away when you upgrade. On a <b>paid</b> plan it means your prepaid credit ran out: apps resume automatically after a top-up. Both are in Dashboard → Billing &amp; usage. The API also returns 402 when an action needs credit you don't have, such as upgrading from your balance.</p>
<h2 id=failed-releases>A deploy failed</h2><p>A failed release never takes traffic. If an earlier release was running, it keeps serving and the app shows <i>v2 failed … still serving v1</i>. Open the app in the dashboard to see which step failed and the last log lines, or run <code>aloof releases &lt;app&gt;</code>. The usual causes, in order:</p><ol>
<li><b>Wrong port</b>: the app has to listen on <code>0.0.0.0:$PORT</code> (default 8080), and <code>--port</code> or <code>internal_port</code> has to match. Listening on <code>127.0.0.1</code> fails the readiness check.</li>
<li><b>Health path</b>: if you set one with <code>aloof health &lt;app&gt; --path /healthz</code>, it must return a 2xx/3xx status within a few seconds of starting.</li>
<li><b>The release command failed</b> (see below).</li>
<li><b>Image or repo can't be pulled</b>: for private sources, run <code>aloof registry login</code> or set up a <a href="/docs/private">deploy key</a>.</li>
<li><b>The build failed</b>: see build logs below.</li></ol>
<p>After fixing it, deploy again or press <b>Retry deploy</b> on the app. To go back to a known-good version without rebuilding, run <code>aloof rollback &lt;app&gt;</code>.</p>
<p><b>Diagnose</b>: <code>GET /v1/apps/&lt;app&gt;/diagnose</code> (or the <code>diagnose_app</code> MCP tool) returns recent error log lines, failed health checks, recent deploys, rollbacks and restarts, instance states and exit codes, and likely causes with a suggested fix. It is read-only and redacts secrets and env values.</p>
<h2 id=build-logs>Where are the build logs?</h2><p>Build and deploy output is recorded as platform events. You'll find it in the deploy panel on the app's page in the dashboard (shown while a deploy runs and when it fails), under the app's <b>Logs</b> tab, and with <code>aloof logs &lt;app&gt;</code> (the <i>platform events</i> section). The build step stores the last 40 lines of <code>docker build</code> output when the build finishes, so you'll see them in one batch rather than line by line.</p>
<h2 id=release-command>What happens when the release command fails?</h2><p><code>release_command</code> (for example database migrations) runs once, in a separate machine, after the build and before any new instance starts. If it exits with a non-zero code, the deploy stops: no new instances start, the release is marked failed, the previous release keeps serving, and the last 30 lines of its output are in the logs prefixed with <code>[release]</code>. Fix the command and deploy again. Make migrations safe to run twice, because a retry runs them again.</p>
<h2 id=brief-downtime>A short gap during a deploy</h2><p>Apps with a volume attached are single-writer, so the old instance stops before the new one starts, which causes a brief gap. Apps without a volume roll with no downtime.</p>
<h2 id=domains>Custom domain won't verify</h2><p>See <a href="/docs/domains#verify-failed">Custom domains → If verify fails</a>. Cloudflare users: set the records to DNS only.</p>
<h2 id=postgres>Can't reach Postgres from my laptop</h2><p>Databases are private. Use <code>aloof proxy 5432 -a pg-&lt;name&gt;</code>; see <a href="/docs/postgres#local">Managed Postgres</a>.</p>
<h2 id=more>Still stuck?</h2><p>Support doesn't have a public email address yet; see <a href="/docs/support">Support</a> for where things stand.</p>""",
"support": f"""<h1>Support</h1>
<div class=note><b>There's no public support email yet.</b> We're still setting up our support inbox, so we can't give you an address or a response time today. Until it's ready, the best places to get unstuck are <a href="/docs/troubleshooting">Troubleshooting &amp; FAQ</a> and the read-only diagnose tool (<code>GET /v1/apps/&lt;app&gt;/diagnose</code>, or <code>diagnose_app</code> over MCP), which cover most deploy problems. This page will list the address as soon as it exists.</div>
<p>When you contact us, include your account email, the app name, the time it happened (with your timezone) and what you ran. Never send API keys, passwords or <code>DATABASE_URL</code> values; we'll never ask for them.</p>
<p><b>Security issues</b>: see the <a href="/security">security page</a>.</p>
<p>Many answers are in <a href="/docs/troubleshooting">Troubleshooting &amp; FAQ</a>.</p>""",
"templates": f"""<h1>One-click templates</h1><p>Templates deploy a ready-made open-source app (n8n, Umami, and others) with its database, disk and secrets set up for you. Browse them on the <a href="/templates">templates page</a>; each one lists exactly what it creates and what it costs.</p>
<h2>Deploy one</h2><pre>aloof templates                         # list ids and descriptions
aloof deploy --template umami my-stats  # create the app (add -e KEY=value for required settings)
aloof deploy --template n8n my-n8n --plan shared-1x-1gb   # pick a different plan</pre>
<p>You can also use the <b>Deploy on Aloof</b> button on a template page (you'll be asked to log in first), <code>POST /v1/templates/&lt;id&gt;/deploy</code>, or the <code>deploy_template</code> MCP tool. Deploy tokens can't create apps from templates; use a full-access key or your login.</p>
<h2>What it costs</h2><p>A template costs the same as the pieces it creates: the app's plan, plus any disk (volume) and managed Postgres database it comes with. Each template page adds these up for you as a monthly figure if the app runs all month. Billing is per second, and apps that sleep cost nothing while asleep, so real cost is usually lower. See <a href="/docs/billing">Billing</a>.</p>
<h2>Settings you must provide</h2><p>Some templates need a value only you can supply, such as an API key or a domain. The template page lists these as <b>required</b>. If one is missing, the deploy is refused before anything is created, with a message naming the setting, and you aren't charged. Secrets the app needs to generate (like encryption keys) are created for you and shown once, so save them when they appear.</p>
<p>Templates currently run in Nuremberg, Germany (<code>nbg1</code>).</p>""",
"self-healing": """<h1>Self-healing deploys</h1><p><b>It's on by default</b> for every app, including new ones. You don't have to set anything up.</p>
<p>After each deploy or config change, Aloof watches the new release for 5 minutes and compares it with the previous one. If the new release is clearly worse (many more 5xx errors, or much slower), it goes back to the previous release automatically. The reason and numbers are saved on the release. The full thresholds and safeguards are in <a href="/docs/releases#self-heal">Releases &amp; rollback</a>.</p>
<h2>Check or change it</h2><pre>aloof self-heal my-app          # show on/off, and which release is being watched
aloof self-heal my-app off      # turn it off for this app
aloof self-heal my-app on       # turn it back on</pre>
<p>The same is available over the API: <code>GET</code> or <code>PUT /v1/apps/&lt;app&gt;/self-heal</code> with <code>{"enabled": false}</code>. Turning it off only affects that app, and each change is written to the audit log.</p>
<h2>When it does nothing</h2><p>A release isn't watched when the previous release didn't serve enough traffic to compare against (at least 100 requests are needed), and rollbacks are never rolled back. A very quiet app can still deploy a bad release, so keep a health check configured too.</p>""",
"nodes": f"""<h1>Bring your own nodes</h1><p>An admin can add any Ubuntu 22.04/24.04 server to a pool, for example Yotta Shakti bare metal or your own GPU box. The node gets a single-use join token, connects to the control plane over WireGuard (outbound UDP only, so no inbound ports are needed), and runs the {brand} agent.</p>
<pre>curl -X POST {api}/v1/admin/nodes -H "Authorization: Bearer $ADMIN" -d '{{"pool":"self-hosted","plan":"gpu-h100","gpu":true}}'
# run the returned one-liner on the server:
curl -fsSL '{api}/v1/nodes/bootstrap.sh?node=n-…&token=jt_…' | sudo bash</pre>
<p>The bootstrap installs Docker, the NVIDIA container toolkit (on GPU nodes) and WireGuard, registers the node and starts <code>aloof-agent</code> under systemd.</p>"""} | _devx_pages(brand, base, api) | {"self-driving": _selfdriving_doc()})[pg]
    if pg == "postgres":
        from .plans import PG_PLANS
        c = c.replace("{PGROWS}", "".join(f"<tr><td><code>{k}</code></td><td>{p['vcpu']:g} {'shared' if p['compute'].startswith('shared') else 'dedicated'} vCPU / {_gb(p['memory_mb'])} GB</td><td>{p['disk_gb']} GB</td><td><b>${p['price_usd_month']}</b>/mo</td></tr>" for k, p in PG_PLANS.items()))
    groups = [("Getting started", ["quickstart", "cli", "deploy", "templates", "config", "releases"]), ("Source & CI", ["private", "github", "ci"]), ("Apps", ["processes", "networking"]), ("Platform", ["autoscaling", "volumes", "postgres", "domains", "gpu"]), ("Operate", ["console", "logs", "self-healing"]), ("Account", ["billing"]), ("Integrations", ["mcp", "api", "nodes"])]
    navh = "".join(f"<h4>{g}</h4>" + "".join(f'<a href="/docs/{d}" class="{"on" if d == pg else ""}"{" aria-current=page" if d == pg else ""}>{TITLES[d]}</a>' for d in ds) for g, ds in groups)
    c = c.replace("<table>", "<div class=scroll><table class=tbl>").replace("</table>", "</table></div>")
    return page(f"{TITLES[pg]} · {brand} docs", f"<div class='wrap docs'><aside aria-label='Docs navigation'>{navh}</aside><article class=docmain style='min-width:0;max-width:780px'>{c}</article></div>", brand,
                desc=("Aloof documentation: quickstart, CLI, aloof.toml config, deploys from Docker images or git, managed Postgres, volumes, domains, billing and the MCP server for AI assistants." if pg == "quickstart"
                      else f"{TITLES[pg]} on Aloof: how it works, commands and limits. Part of the Aloof docs for EU backend hosting with scale to zero and MCP deploys."))
def login(brand):
    return page(f"Log in · {brand}", f"""<div class=auth><div class=card><a class=logo href="/" style="margin-bottom:18px"><i></i>{brand}</a>
<h1 id=title>Welcome back</h1><p class=muted id=subt style="margin:0">Log in to deploy and manage your apps.</p>
<div class=tabs role=tablist aria-label="Log in or create an account"><button type=button role=tab id=tl class=on aria-selected=true onclick="mode('login')">Log in</button><button type=button role=tab id=tr aria-selected=false onclick="mode('register')">Create account</button></div>
<form onsubmit="event.preventDefault();go(M)"><div id=msg role=status aria-live=polite class=muted style="min-height:20px;font-size:14px"></div>
<label for=email>Email</label><input id=email type=email autocomplete=email required placeholder="you@company.com">
<label for=pw id=pwl>Password</label><input id=pw type=password autocomplete=current-password placeholder="Your password" aria-describedby=pwh><small id=pwh class="muted helper" style="display:none">At least 10 characters.</small>
<p style="margin-top:18px"><button class="btn pri" style="width:100%;justify-content:center" id=lb>Log in</button></p></form>
<p class=muted style="text-align:center;font-size:14px;margin:6px 0">or <a href="#" class=maglink role=button onclick="magic();return false">email me a sign-in link</a></p>
<p class="muted secure-note">{svg("lock")}<span>Sessions use HttpOnly cookies. By continuing you agree to the <a href="/terms">Terms</a> and <a href="/privacy">Privacy policy</a>.</span></p></div></div>
<script>
var M='login';function mode(m){{M=m;tl.classList.toggle('on',m=='login');tr.classList.toggle('on',m=='register');tl.setAttribute('aria-selected',m=='login');tr.setAttribute('aria-selected',m=='register');title.textContent=m=='login'?'Welcome back':'Create your account';
subt.textContent=m=='login'?'Log in to deploy and manage your apps.':'Free plan with $1/month of included usage.';lb.textContent=m=='login'?'Log in':'Create account';pwh.style.display=m=='login'?'none':'block';pw.placeholder=m=='login'?'Your password':'Choose a password';pw.autocomplete=m=='login'?'current-password':'new-password'}}
if(location.hash=='#register')mode('register');addEventListener('hashchange',function(){{mode(location.hash=='#register'?'register':'login')}});
function errText(j,r){{var d=j&&j.detail;if(Array.isArray(d))d=d.map(function(x){{var f=(x.loc||[]).slice(-1)[0];return (f?f+': ':'')+(x.msg||'')}}).join('; ');if(!d)d=r&&r.status==429?'Too many attempts. Wait a minute and try again.':'Something went wrong on our side. Please try again.';d=String(d);return d.charAt(0).toUpperCase()+d.slice(1)}}
async function post(u,b){{var r,j={{}};try{{r=await fetch(u,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(b)}})}}catch(e){{return {{ok:false,j:{{detail:'Could not reach Aloof. Check your connection and try again.'}}}}}}try{{j=await r.json()}}catch(e){{}}return {{ok:r.ok,r:r,j:j}}}}
async function go(kind){{if(lb.disabled)return;if(kind=='register'&&window.aloofTrack)aloofTrack('signup_started',{{method:'password'}});var t=lb.textContent;lb.disabled=true;lb.textContent=kind=='register'?'Creating account…':'Logging in…';msg.textContent='';
 const x=await post('/v1/auth/'+kind,{{email:email.value,password:pw.value,attribution:window.aloofAttribution?aloofAttribution():null}}),j=x.j; if(!x.ok){{lb.disabled=false;lb.textContent=t;msg.className='bad';msg.textContent=errText(j,x.r);return}}
 if(j.api_key) sessionStorage.newKey=j.api_key; var n=new URLSearchParams(location.search).get('next'),d='/dashboard';try{{if(n&&n.charAt(0)=='/'&&n.charAt(1)!='/'&&!/[\\\\\\x00-\\x20\\x7f]/.test(n)){{var u=new URL(n,location.origin);if(u.origin===location.origin&&u.pathname.charAt(1)!='/')d=u.pathname+u.search+u.hash}}}}catch(e){{}}location=d}}
async function magic(){{if(!email.value||!email.checkValidity()){{msg.className='bad';msg.textContent='Enter your email address first, then tap the sign-in link option again.';email.focus();return}}if(M=='register'&&window.aloofTrack)aloofTrack('signup_started',{{method:'magic_link'}});msg.className='muted';msg.textContent='Sending…';
 const x=await post('/v1/auth/magic',{{email:email.value,attribution:window.aloofAttribution?aloofAttribution():null}}),j=x.j; msg.className=x.ok?'ok':'bad'; msg.textContent=x.ok?(j.detail||'Check your inbox for a sign-in link.'):errText(j,x.r)}}
</script>""", brand)
LEGAL = {
"security": ("Security", """<p>This page lists what the platform does today. We don't claim certifications we don't hold.</p>
<h3>Transport</h3><ul><li>All public endpoints (site, API, MCP, apps) are served over HTTPS; certificates are issued automatically and only for known hosts.</li><li>Nodes connect to the control plane over WireGuard and expose no inbound ports.</li></ul>
<h3>Data at rest</h3><ul><li>App environment variables are encrypted at rest (Fernet) and never returned by the API.</li><li>API keys and session tokens are stored as SHA-256 hashes.</li></ul>
<h3>Isolation</h3><ul><li>Each app instance runs in its own container with CPU and memory limits.</li></ul>
<h3>Accounts</h3><ul><li>HttpOnly, SameSite session cookies; state-changing cookie requests require a CSRF header.</li><li>Single-use, expiring magic links. OAuth 2.1 with PKCE for MCP clients; tokens are scoped and revocable.</li></ul>
<h3>Infrastructure</h3><ul><li>Compute and the control plane run on Hetzner Cloud in Nuremberg, Germany. Off-site backups are stored in Helsinki, Finland.</li><li>Payments are processed by Stripe; card data never reaches our servers.</li></ul>
<h3>Reporting</h3><p>Email <a href="mailto:security@aloof.si">security@aloof.si</a>. Please give us reasonable time to fix an issue before disclosing it.</p>"""),
"privacy": ("Privacy policy", """<p><b>Effective: 14 October 2026</b></p>
<p>This policy explains what personal data Aloof collects, why, who we share it with, how long we keep it and what rights you have. It applies to www.aloof.si, the dashboard, the docs, the API, the CLI and the MCP server (together, the "Service"). If something here is unclear, email <a href="mailto:privacy@aloof.si">privacy@aloof.si</a>.</p>

<h2 id="who">1. Who we are</h2>
<p>Aloof is operated by <b>Enigmatic IT Solutions FZE</b>, a company registered in the United Arab Emirates ("Aloof", "we", "us").</p>
<p>For personal data about you as an Aloof user or website visitor (your account, billing, support requests and how you use the Service), we are the <b>controller</b>. For data that you or your end users put into the apps, databases, volumes and backups you run on Aloof, we are the <b>processor</b>: we handle it only on your instructions, and you are the controller (see section 4 and our <a href="/terms">Terms of Service</a>).</p>

<h2 id="data">2. What we collect</h2>
<h3>Data you give us</h3>
<ul>
<li><b>Account data:</b> your email address and, if you choose one, a password. Passwords are stored only as a salted scrypt hash.</li>
<li><b>Billing data:</b> billing name, country, billing address details and any VAT, GST or other tax ID you add to your billing profile. Card and other payment details are entered on Stripe's pages and never reach our servers; we receive a Stripe customer ID, payment and subscription status and invoice references.</li>
<li><b>Support and communications:</b> what you write to us by email or through support, and our replies.</li>
<li><b>Waitlists:</b> if you join a waitlist (for example for GPUs or a new region), your email, what you signed up for, any company name or note you add, the page you came from, the time you agreed and the IP address of the request.</li>
</ul>
<h3>Data generated when you use the Service</h3>
<ul>
<li><b>Operational data:</b> apps, releases, instances, domains, volumes and databases you create; usage and metering records (compute seconds, storage, backups, egress) used to bill you; platform events and logs.</li>
<li><b>Security and audit data:</b> session records, API key and deploy token metadata (prefix, label, created and last-used times), and an audit log of sensitive actions (for example console exec commands) that may include the IP address the request came from.</li>
<li><b>Sign-in data:</b> single-use magic links (valid 15 minutes) and session cookies (valid up to 14 days). Session tokens, API keys and magic-link tokens are stored only as SHA-256 hashes.</li>
<li><b>AI agent and MCP data:</b> when you connect an AI assistant through our MCP server or API, we record the OAuth client name and host, the scopes granted, and the actions the agent takes on your account, just as if you had taken them yourself.</li>
<li><b>Sign-up attribution:</b> the campaign (UTM parameters) and referring site that brought you to Aloof, stored on your account when you sign up.</li>
<li><b>Integrations you connect:</b> if you install our GitHub App or add registry or git credentials, the installation ID, account login, linked repositories and branches, and the credentials you provide (encrypted at rest).</li>
</ul>
<h3>Customer data</h3>
<p>Your application code and images, environment variables, application logs (stdout/stderr), databases, volumes, snapshots and backups, and anything your end users send to your apps ("Customer Data"). We process Customer Data only to provide the Service to you. We don't look at it, analyse it, or use it for our own purposes, except where needed to keep the Service running, to fix a problem you ask us to help with, to enforce our Terms, or where the law requires it.</p>

<h2 id="analytics">3. Analytics</h2>
<p>On this website, the docs and the dashboard we use PostHog Cloud US (PostHog Inc., data stored in the United States) to count page views and clicks on sign-up buttons, and to see which campaign or referring site brought you here (UTM parameters and referrer). It runs without cookies: no identifier is stored on your device beyond the current browser tab, so no consent banner is shown. When you're logged in, events are linked to your account id so we can understand how the product is used. We don't send your email, app data or logs to PostHog.</p>
<p>Our servers also send a small number of product events to PostHog US under the same account id (for example sign-up completed, first deploy, MCP client connected, plan upgraded, waitlist joined), with only the fields needed to count them, such as the plan, amount or MCP client name. When you sign up or log in we also attach your account email to that account id in PostHog, so we can answer support questions about your sign-up and spot test accounts.</p>

<h2 id="purposes">4. Why we use your data and our legal bases</h2>
<p>We rely on the legal bases in the EU and UK General Data Protection Regulation (GDPR) and, where it applies, the UAE Federal Decree-Law No. 45 of 2021 on the Protection of Personal Data (PDPL). Under the PDPL, the purposes below fall within the exceptions to consent for performing a contract, complying with legal obligations, and protecting our and others' legitimate interests, or rely on your consent where we ask for it.</p>
<ul>
<li><b>Creating and running your account and the Service</b> (sign-in, deploying and running your apps, backups, support): performance of our contract with you.</li>
<li><b>Billing, invoicing and tax</b> (metering usage, charging cards through Stripe, issuing invoices, applying VAT/GST): performance of the contract and compliance with legal obligations (tax and accounting law).</li>
<li><b>Security, fraud and abuse prevention</b> (audit logs, rate limits, investigating breaches of our acceptable use rules): our legitimate interest in keeping the Service and its users safe, and legal obligations where they apply.</li>
<li><b>Service emails</b> (sign-in links, billing notices, security and service announcements): performance of the contract.</li>
<li><b>Product analytics and attribution</b> (section 3): our legitimate interest in understanding how the Service is found and used so we can improve it. You can object at any time (section 9).</li>
<li><b>Waitlist emails:</b> your consent, given when you join. We email you once when the item is available and for nothing else, and you can unsubscribe with the link in the email.</li>
<li><b>Legal claims and requests from authorities:</b> legal obligations and our legitimate interest in establishing, exercising or defending legal claims.</li>
<li><b>Customer Data:</b> processed on your instructions as your processor; your own legal basis applies.</li>
</ul>
<p>We don't sell personal data, we don't share it for advertising, and we don't use Customer Data to train AI models. We don't make decisions about you based solely on automated processing that have legal or similarly significant effects. The automatic pausing of apps when credit or the Free plan allowance runs out is a mechanical result of your plan and balance, and you can contact support about it.</p>

<h2 id="subprocessors">5. Who we share data with (subprocessors)</h2>
<p>We use the following service providers to run Aloof. Each one processes data only on our instructions and under a data processing agreement.</p>
<ul>
<li><b>Hetzner Online GmbH</b> (Germany). <b>What:</b> hosting of compute, the control plane, databases and volumes in Nuremberg, Germany, and encrypted off-site backups in Hetzner Object Storage in Helsinki, Finland. <b>Data:</b> all account, operational and Customer Data. <b>Location:</b> EU.</li>
<li><b>Stripe</b> (Stripe Payments Europe Ltd., Ireland, and its affiliates including Stripe, Inc., USA). <b>What:</b> payments, subscriptions, invoices, the customer billing portal and tax calculation. <b>Data:</b> email, billing name, address, country, tax ID, payment details and transaction history. <b>Location:</b> EU and USA. Stripe also acts as an independent controller for some data, for example for fraud prevention and its own legal obligations; see <a href="https://stripe.com/privacy">Stripe's privacy policy</a>.</li>
<li><b>PostHog Inc.</b> (PostHog Cloud US). <b>What:</b> cookieless product analytics (section 3). <b>Data:</b> page views, clicks, referrer and UTM parameters, browser and device information, account id and product events. No email, app data or logs. <b>Location:</b> United States.</li>
<li><b>Our transactional email delivery provider</b>. <b>What:</b> delivering sign-in links, billing and service emails, and waitlist emails over SMTP. <b>Data:</b> your email address and the content of those emails.</li>
</ul>
<p>We also share data, only as far as needed, with: Let's Encrypt (Internet Security Research Group, USA), which issues TLS certificates for your app and custom domain names (certificates are published in public Certificate Transparency logs, so these domain names become public); integrations you choose to connect, such as GitHub, container registries, log drain destinations or AI assistants; our professional advisers (lawyers, accountants, auditors); a buyer or successor if Aloof is sold or reorganised, who must keep this policy's protections; and courts, regulators or law enforcement where the law requires it.</p>
<p>We'll update this list before adding a new subprocessor that handles Customer Data. Customers who need notice of changes can ask to be notified at <a href="mailto:privacy@aloof.si">privacy@aloof.si</a>.</p>

<h2 id="transfers">6. International transfers</h2>
<p>Our infrastructure and the data it holds (your account, operational data and Customer Data) are hosted in the European Union, in Germany and Finland, and analytics data is stored in the EU (Frankfurt).</p>
<p>Aloof is operated from the United Arab Emirates, so personal data is accessed from the UAE by the people who run and support the Service. Some providers, such as Stripe, may also process data in the United States. The European Commission has not issued an adequacy decision for the UAE. Where personal data protected by the GDPR is transferred outside the EEA or UK, we rely on the European Commission's Standard Contractual Clauses (and the UK Addendum where relevant) or another lawful transfer mechanism, together with the safeguards in section 8. Transfers out of the UAE are made in line with the PDPL. You can ask for a copy of the safeguards we use at <a href="mailto:privacy@aloof.si">privacy@aloof.si</a>.</p>

<h2 id="retention">7. How long we keep data</h2>
<ul>
<li><b>Account and billing records:</b> while your account is open and for 7 years after it closes.</li>
<li><b>Apps, databases, volumes and other Customer Data:</b> while your account is open. When it's deleted, its apps, databases, volumes, backups, API keys and sessions are removed.</li>
<li><b>Application logs:</b> 1 day on Free, 7 days on Hobby and 30 days on Pro, then deleted automatically.</li>
<li><b>Off-site backups:</b> encrypted copies are rotated on a 7 daily, 4 weekly and 3 monthly schedule, so deleted data can remain in backups for up to about 3 months before it is overwritten.</li>
<li><b>Sign-in data:</b> magic links expire after 15 minutes and sessions after 14 days.</li>
<li><b>Invoices, payment and tax records:</b> as long as tax and accounting law requires, even after the account is deleted.</li>
<li><b>Audit and security logs:</b> 12 months.</li>
<li><b>Waitlist entries:</b> until the item launches and we've emailed you, or until you unsubscribe, whichever is first.</li>
<li><b>Analytics events in PostHog:</b> 12 months.</li>
<li><b>Support emails:</b> 24 months.</li>
</ul>
<p>We may keep data longer where we need it to resolve a dispute, enforce our Terms or meet a legal obligation, and only for as long as that need lasts.</p>

<h2 id="security">8. Security</h2>
<p>We use technical and organisational measures to protect personal data, including: HTTPS on every public endpoint; environment variables and stored credentials encrypted at rest; passwords stored as scrypt hashes and API keys and session tokens as SHA-256 hashes; HttpOnly, SameSite session cookies with CSRF protection; single-use, expiring magic links; scoped, revocable OAuth tokens for MCP clients; each app instance isolated in its own container; nodes connected over WireGuard; and client-side AES-256-GCM encryption of off-site backups. See our <a href="/security">Security page</a> for details. No system is perfectly secure. If a personal data breach is likely to put your rights at risk, we'll notify you and the relevant authorities as the law requires. For Customer Data we'll notify you without undue delay so you can meet your own obligations.</p>

<h2 id="rights">9. Your rights</h2>
<p>Depending on where you live, you have the right to:</p>
<ul>
<li><b>access</b> the personal data we hold about you and get a copy;</li>
<li><b>correct</b> inaccurate or incomplete data;</li>
<li><b>delete</b> your data ("right to be forgotten");</li>
<li><b>restrict</b> or <b>object</b> to processing, including processing based on our legitimate interests such as analytics;</li>
<li><b>data portability</b>: receive data you gave us in a structured, machine-readable format;</li>
<li><b>withdraw consent</b> at any time where we rely on it, without affecting what we did before;</li>
<li><b>complain</b> to a data protection authority. In the EU, that's the authority where you live or work, or where you think the problem happened. In the UAE, it's the UAE Data Office.</li>
</ul>
<p><b>How to exercise them:</b> email <a href="mailto:privacy@aloof.si">privacy@aloof.si</a> from the email address on your account, so we can confirm it's you. You can see and export most of your data yourself through the dashboard, API and CLI, and update your billing details in Dashboard → Billing &amp; usage. To delete your account, follow <a href="/docs/billing#delete">Deleting your account</a>. We'll reply within one month (extendable by two months for complex requests, in which case we'll tell you why), and we don't charge for reasonable requests.</p>
<p>If your request is about Customer Data in an app run by one of our customers, please contact that customer, who is the controller. We'll help them respond.</p>

<h2 id="children">10. Children</h2>
<p>Aloof is a service for developers and businesses. It isn't directed at children, and you must be at least 18 (or the age of majority where you live) to create an account. We don't knowingly collect personal data from children. If you think a child has given us personal data, contact <a href="mailto:privacy@aloof.si">privacy@aloof.si</a> and we'll delete it.</p>

<h2 id="cookies">11. Cookies</h2>
<p>We use one cookie, and it's strictly necessary: an HttpOnly, SameSite session cookie that keeps you signed in after you log in with a password or magic link, which we protect against cross-site request forgery by requiring an extra header on requests that change anything. It isn't used for tracking or advertising and doesn't need consent. Our analytics are cookieless (section 3). The analytics script keeps campaign and referrer information in your browser's session storage for the current tab only, and it is cleared when you close the tab. We don't use advertising or third-party tracking cookies. Stripe sets its own cookies on its checkout and billing portal pages, under its own policy.</p>

<h2 id="changes">12. Changes to this policy</h2>
<p>We may update this policy as the Service or the law changes. We'll change the effective date at the top, and for material changes we'll tell you by email or in the dashboard before they take effect.</p>

<h2 id="contact">13. Contact</h2>
<p>Privacy questions and requests: <a href="mailto:privacy@aloof.si">privacy@aloof.si</a>. Legal notices: <a href="mailto:legal@aloof.si">legal@aloof.si</a>.</p>"""),
"terms": ("Terms of service", """<p><b>Effective: 14 October 2026</b></p>
<p>These Terms of Service ("Terms") are an agreement between you and <b>Enigmatic IT Solutions FZE</b>, a company registered in the United Arab Emirates ("Aloof", "we", "us"). They cover your use of Aloof: the website at www.aloof.si, the dashboard, docs, API, CLI, MCP server and the hosting platform behind them (together, the "Service"). Our <a href="/privacy">Privacy Policy</a> explains how we handle personal data.</p>

<h2 id="acceptance">1. Accepting these Terms</h2>
<p>By creating an account, or by using the Service, you agree to these Terms. If you use the Service for a company or other organisation, you confirm that you're authorised to accept these Terms for it, and "you" means that organisation. If you don't agree, don't use the Service.</p>
<p>You must be at least 18 years old, or the age of majority where you live, to use the Service.</p>

<h2 id="accounts">2. Your account</h2>
<ul>
<li>Give us a valid email address and keep your account and billing details accurate. You can sign in with a password or with a single-use magic link sent to your email.</li>
<li>Keep your password, email account, API keys, deploy tokens and OAuth grants secure. You're responsible for everything done with your account and credentials, and for the people and tools you give access to.</li>
<li><b>AI agents.</b> You can let AI assistants and agents use the Service through our API and MCP server, including to create an account, deploy, scale, run commands or spend credit. Anything an agent does with access you or your organisation granted is treated as your action under these Terms. You can revoke an agent's access at any time. If you build a product that lets your own users act on Aloof through an agent, you're responsible for their use as well.</li>
<li>Tell us straight away at <a href="mailto:security@aloof.si">security@aloof.si</a> if you think your account or credentials have been compromised.</li>
<li>One person or organisation shouldn't open multiple accounts to get around Free plan limits or the starting credit.</li>
</ul>

<h2 id="service">3. The Service</h2>
<p>Aloof is a platform for hosting backends, APIs, workers and related services. You can deploy from a git repository or container image, attach volumes and managed Postgres databases, add custom domains, read logs and manage everything through the dashboard, CLI, API and MCP server. Compute and data are hosted on Hetzner Cloud in Nuremberg, Germany, and encrypted off-site backups are stored in Helsinki, Finland.</p>
<p>We keep improving the Service, so features may be added, changed or removed. If we remove a feature you're paying for, or make a change that materially reduces the Service, we'll give you reasonable notice where we can. Features we describe as beta, preview or coming soon may change or stop at any time and are provided without any commitment.</p>

<h2 id="aup">4. Acceptable use</h2>
<p>You may not use the Service, or let anyone else use it, to:</p>
<ul>
<li>host, store or distribute illegal content, or content that infringes anyone's intellectual property, privacy or other rights;</li>
<li>abuse others, including harassment, threats, malware, phishing, fraud, or attacks on other systems (such as denial-of-service attacks, port scanning or unauthorised access attempts);</li>
<li>send spam or other unsolicited bulk messages;</li>
<li>mine cryptocurrency;</li>
<li>probe, test or break our security or isolation, access other customers' data, or interfere with the Service or its infrastructure (security research is welcome when reported to <a href="mailto:security@aloof.si">security@aloof.si</a> and done responsibly, without harming data or availability);</li>
<li>get around usage limits, billing, metering or suspension, for example by creating accounts to get repeated free usage;</li>
<li>resell or provide the Service itself as a hosting platform to others without our written permission (running your own products and services on Aloof for your users is fine);</li>
<li>break any law or regulation that applies to you or to us, including sanctions and export control laws.</li>
</ul>
<p>We can remove content or suspend resources that break these rules (see section 7). If you notice abuse on Aloof, report it to <a href="mailto:legal@aloof.si">legal@aloof.si</a>.</p>

<h2 id="customer-data">5. Your data and how we process it</h2>
<p>"Customer Data" means your code, container images, environment variables, application logs, databases, volumes, snapshots and backups, and anything your end users send to your apps. You keep all rights to your Customer Data. You give us only the permissions we need to host, run, copy, back up, transmit and display it to provide the Service to you.</p>
<p>You're responsible for your Customer Data and its lawfulness, for having the rights and permissions to use it, for your own end users, and for keeping your own backups of anything important. We run backups to help recover from failures, but they don't replace your own.</p>
<h3>Data processing terms</h3>
<p>Where Customer Data includes personal data, you are the controller (or a processor acting for your own customer), and we are your processor (or sub-processor). This section is our data processing agreement with you under Article 28 of the GDPR, and it also applies under the UAE PDPL and other data protection laws as far as they apply. We will:</p>
<ul>
<li><b>Follow your instructions.</b> Process Customer Data only to provide the Service, as described in these Terms and as you set it up and use it, unless the law requires otherwise (in which case we'll tell you first if the law allows).</li>
<li><b>Keep it confidential.</b> Make sure the people we authorise to process Customer Data are bound by confidentiality.</li>
<li><b>Keep it secure.</b> Keep appropriate technical and organisational measures in place, as described on our <a href="/security">Security page</a>.</li>
<li><b>Use approved subprocessors.</b> You authorise the subprocessors listed in our <a href="/privacy#subprocessors">Privacy Policy</a>. We'll update that list before adding or replacing a subprocessor that handles Customer Data, and you can object at <a href="mailto:privacy@aloof.si">privacy@aloof.si</a>. If we can't resolve a reasonable objection, you can stop using the Service, and we'll refund any prepaid credit you haven't used. We hold our subprocessors to data protection obligations at least as protective as these, and we stay responsible for them.</li>
<li><b>Help you.</b> Help you, as far as reasonably possible, respond to requests from data subjects, carry out data protection impact assessments and meet your security and breach obligations. Most data can be managed directly through the dashboard, API and CLI.</li>
<li><b>Tell you about breaches.</b> Notify you without undue delay after becoming aware of a personal data breach affecting your Customer Data, with the information you reasonably need.</li>
<li><b>Delete it at the end.</b> When you delete resources or your account, delete the Customer Data in them. Encrypted backup copies roll off within our backup rotation (up to about 3 months), unless the law requires us to keep them.</li>
<li><b>Show compliance.</b> Make available the information reasonably needed to show we meet these obligations, and allow and contribute to audits by you or an auditor you appoint, with reasonable notice, at your cost, no more than once a year unless a regulator requires it or there's been a breach, and subject to confidentiality.</li>
<li><b>Transfer it lawfully.</b> Customer Data is hosted in the EU. Where it is accessed from or transferred outside the EEA (including from the UAE, where Aloof is operated), we'll use the European Commission's Standard Contractual Clauses or another lawful transfer mechanism.</li>
</ul>
<p>The subject matter of the processing is the hosting of your apps and data; it lasts for as long as you use the Service; the types of personal data and categories of data subjects are whatever you and your end users put into the Service. Our own handling of your account and billing data, as controller, is covered by our <a href="/privacy">Privacy Policy</a>. If you need a signed data processing agreement, email <a href="mailto:privacy@aloof.si">privacy@aloof.si</a>.</p>
<p>We may look at Customer Data only to provide or support the Service, fix a problem, keep the Service secure, enforce these Terms or comply with the law, and only as far as needed.</p>

<h2 id="billing">6. Fees and billing</h2>
<p>Each workspace is on a plan: <b>Free</b>, <b>Hobby</b> ($5 a month) or <b>Pro</b> ($20 a month). The plan fee comes back to you as included usage for that month. Usage beyond what your plan includes is paid from a prepaid credit balance that you top up by card through Stripe. Prices are in US dollars; taxes may apply where required. Stripe issues an invoice for every payment. Current prices are on our <a href="/pricing">Pricing page</a>.</p>
<ul>
<li>Plans (Hobby, Pro) are monthly and renew until cancelled. You can cancel at any time. A card-paid plan stays active until the end of the paid month; a balance-paid plan changes immediately. Plan fees for a partly used month aren't refunded.</li>
<li>Usage (compute per second, storage, backups, egress) is charged as it accrues, whatever your plan, until you delete the resources.</li>
<li>Prepaid credit doesn't expire while the account is open and is non-refundable, except where we charged you in error or decide a refund is fair after reviewing an unintended charge. The starting credit is promotional and has no cash value.</li>
<li>Billing errors are corrected by refund or credit when reported within 60 days.</li>
<li>Deleting your account removes its resources and forfeits remaining credit. Invoices are kept as the law requires.</li>
<li>The Free plan is hard-capped and never draws on your balance: when the month's included usage is used up, your apps pause until the next month or until you upgrade. On a paid plan, when your credit runs out, your apps are suspended and resume automatically after a top-up.</li>
<li>Card payments are processed by Stripe under Stripe's terms. You authorise us, through Stripe, to charge your payment method for subscriptions and top-ups you choose.</li>
<li>We may change prices with at least 30 days' notice by email or in the dashboard. Changes apply from your next billing month; if you don't agree, you can cancel before then.</li>
</ul>
<p>The full policy, with how to cancel and request a refund, is in <a href="/docs/billing#cancel">Billing</a>.</p>
<p>If you're a consumer, nothing in these Terms limits any mandatory rights you have under the consumer law where you live.</p>

<h2 id="suspension">7. Suspension and termination</h2>
<p><b>By you.</b> You can stop using the Service at any time. Cancel any plan, destroy resources you no longer need, and request account deletion as described in <a href="/docs/billing#delete">Deleting your account</a>.</p>
<p><b>Suspension by us.</b> We may suspend all or part of your account or resources, without prior notice where the situation calls for it, if: you break section 4 or another important part of these Terms; your use creates a security risk or harms the Service, other customers or third parties; your payment fails or your credit runs out; we're required to by law or by an authority; or we reasonably suspect fraud. We'll limit a suspension to what's needed, tell you why where we're allowed to, and lift it once the problem is fixed.</p>
<p><b>Termination by us.</b> We may terminate your account: immediately, if you seriously or repeatedly break these Terms or we must do so by law; or for any other reason with at least 30 days' notice. If we terminate for a reason other than your breach, we'll refund any prepaid credit you haven't used.</p>
<p><b>What happens next.</b> When your account ends, your right to use the Service stops and your resources and Customer Data are deleted as described in section 5. Before you close an account, or during any notice period, export what you want to keep. Sections that by their nature should continue (including 5, 6 for amounts owed, 9 to 12 and 15) survive termination.</p>

<h2 id="sla">8. Availability and support</h2>
<p>We work to keep the Service available and reliable, but <b>we don't guarantee any particular uptime or performance</b>, and there are no service credits, unless we agree otherwise in writing (for example in a separate service level agreement). The Service may be interrupted for maintenance, upgrades, failures of our providers or networks, or events outside our control. Live status checks are on our <a href="/status">Status page</a>. Support is provided as described in our <a href="/docs/support">Support docs</a>.</p>

<h2 id="warranties">9. Disclaimer of warranties</h2>
<p>The Service is provided <b>"as is" and "as available"</b>. As far as the law allows, we disclaim all warranties, whether express, implied or statutory, including warranties of merchantability, fitness for a particular purpose, title, non-infringement, and that the Service will be uninterrupted, error-free or secure, or that data won't be lost. You're responsible for how you use the Service and for deciding whether it's suitable for your needs. This doesn't affect warranties or rights that can't be excluded by law.</p>

<h2 id="liability">10. Limitation of liability</h2>
<p>As far as the law allows:</p>
<ul>
<li>neither party is liable for any indirect, incidental, special, consequential or punitive damages, or for loss of profits, revenue, business, goodwill or data, however caused, even if told they were possible;</li>
<li>our total liability for all claims arising from or relating to these Terms or the Service is limited to the greater of (a) the amounts you paid us in the 12 months before the event giving rise to the claim and (b) US$100.</li>
</ul>
<p>These limits don't apply to liability that can't be limited by law, such as for fraud, gross negligence, wilful misconduct, or death or personal injury caused by negligence, or to your payment obligations and your obligations under section 11.</p>

<h2 id="indemnity">11. Indemnity</h2>
<p>You'll defend and indemnify Aloof, its affiliates and their officers, employees and contractors against third-party claims, and the resulting losses, damages, fines and reasonable costs (including legal fees), that arise from your Customer Data, your apps or your end users, your breach of these Terms or of the law, or anything done through your account (including by AI agents you've authorised). We'll tell you promptly about any such claim, let you control the defence, and cooperate reasonably at your cost. You can't settle a claim in a way that imposes obligations on us without our written consent.</p>

<h2 id="ip">12. Intellectual property</h2>
<p>We and our licensors own the Service, including its software, design, documentation, and the Aloof name and logo. We give you a limited, non-exclusive, non-transferable right to use the Service under these Terms while your account is active. You may not copy, modify or reverse engineer the Service except as the law allows or as an open-source licence permits (section 13), and you may not use our brand in a way that suggests we endorse you without our permission.</p>
<p>If you send us feedback or suggestions, we can use them freely, without any obligation to you.</p>

<h2 id="third-party">13. Third-party services and open source</h2>
<p>The Service works with third-party services that you may choose to use, such as GitHub, container registries, log drain destinations, DNS providers, Let's Encrypt and AI assistants. Your use of them is governed by their own terms, and we're not responsible for them. Payments are handled by Stripe under Stripe's terms.</p>
<p>Parts of the Service, including the CLI and other tools we publish, may be available under open-source licences. Those licences govern your use of that code, and nothing in these Terms limits your rights under them. You're responsible for complying with the licences of any software, images and dependencies you deploy.</p>

<h2 id="changes">14. Changes to these Terms</h2>
<p>We may update these Terms from time to time. We'll change the effective date at the top, and for material changes we'll give you at least 30 days' notice by email or in the dashboard before they take effect, unless a change is needed sooner for legal or security reasons. If you keep using the Service after the changes take effect, you accept them. If you don't agree, stop using the Service and close your account before then.</p>

<h2 id="law">15. Governing law and disputes</h2>
<p>These Terms, and any dispute or claim arising out of or in connection with them or the Service (including non-contractual disputes), are governed by the laws of the United Arab Emirates.</p>
<p>Before starting any formal proceedings, please contact us at <a href="mailto:legal@aloof.si">legal@aloof.si</a> so we can try to resolve the dispute informally. If it isn't resolved within 30 days, either party may bring it before the competent courts of the United Arab Emirates, which will have exclusive jurisdiction. Either party may still seek urgent interim relief in any competent court.</p>
<p>If you're a consumer, you may also have the right to bring a claim in the courts where you live, and the mandatory consumer protection laws of your country continue to apply.</p>

<h2 id="general">16. General</h2>
<ul>
<li><b>Entire agreement.</b> These Terms, the Privacy Policy and any policies they refer to are the whole agreement between you and us about the Service, and replace any earlier agreements on the same subject.</li>
<li><b>Force majeure.</b> Neither party is liable for a delay or failure caused by events beyond its reasonable control, such as natural disasters, wars, power or network failures, provider outages, or government action. This doesn't excuse payment obligations.</li>
<li><b>Assignment.</b> You can't transfer these Terms without our written consent. We may transfer them to an affiliate or as part of a merger, acquisition or sale of assets, and we'll tell you if we do.</li>
<li><b>Severability and waiver.</b> If part of these Terms is unenforceable, the rest stays in effect. If we don't enforce a right, we haven't waived it.</li>
<li><b>Notices.</b> We'll send notices to the email address on your account or show them in the dashboard. Send legal notices to <a href="mailto:legal@aloof.si">legal@aloof.si</a>.</li>
<li><b>Language.</b> These Terms are written in English. If they're translated, the English version prevails, unless the law requires otherwise.</li>
</ul>

<h2 id="contact">17. Contact</h2>
<p>Legal questions and notices: <a href="mailto:legal@aloof.si">legal@aloof.si</a>. Privacy: <a href="mailto:privacy@aloof.si">privacy@aloof.si</a>. Security reports: <a href="mailto:security@aloof.si">security@aloof.si</a>.</p>"""),
"status": ("Status", """<p id=st class=muted>Checking…</p><div class=scroll><table class=tbl id=stt><tr><th>Component</th><th>Status</th></tr></table></div>
<script>(async()=>{const rows=[['API','/healthz'],['Plans','/v1/plans'],['Docs','/llms.txt']];let ok=0;for(const [n,u] of rows){let s='down';try{const t=performance.now();const r=await fetch(u);s=r.ok?'operational ('+Math.round(performance.now()-t)+' ms)':'degraded';if(r.ok)ok++}catch(e){}
stt.insertAdjacentHTML('beforeend','<tr><td>'+n+'</td><td><span class="status '+(s.startsWith('op')?'s-running':'s-failed')+'">'+s+'</span></td></tr>')}st.textContent=ok==rows.length?'All systems operational.':'Some checks failed.';st.className=ok==rows.length?'ok':'bad'})()</script>
<p class=muted>Live checks from your browser. A hosted status page with history is planned.</p>"""),
}
def legal(name, brand):
    if name not in LEGAL: return None
    t, b = LEGAL[name]
    return page(f"{t} · {brand}", f"<div class=wrap style='max-width:820px;padding:64px 24px 80px'><div class=eyebrow>{brand}</div><h1 style='font-size:40px;letter-spacing:-.03em;margin:8px 0 18px'>{t}</h1><div class=sub>{b}</div></div>", brand)

def not_found(brand="Aloof"):
    return page(f"Page not found · {brand}", """<section class="hero nf"><div class=wrap style="display:block;text-align:center;max-width:620px">
<div class=nf-code aria-hidden=true>404</div><h1 style="font-size:40px">This page wandered off.</h1>
<p class=lead style="margin:14px auto 28px">The page you asked for doesn't exist or has moved. Your apps are fine.</p>
<div class=cta style="justify-content:center"><a class="btn pri" href="/">Back to home</a><a class="btn ghost" href="/dashboard">Open dashboard</a><a class="btn ghost" href="/docs">Docs</a></div></div></section>""", brand)
