"""Public site: landing, pricing, docs, login. Server-rendered, no JS framework."""
import html, os
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
<link rel=icon href="/favicon.ico?v=3" sizes="32x32"><link rel=icon href="/icon.svg?v=3" type="image/svg+xml"><link rel=icon type="image/png" sizes="32x32" href="/favicon-32x32.png?v=3"><link rel=icon type="image/png" sizes="16x16" href="/favicon-16x16.png?v=3"><link rel=apple-touch-icon href="/apple-touch-icon.png?v=3"><link rel=manifest href="/site.webmanifest?v=3"><link rel=stylesheet href="/static/aloof.css?v=15">{THEME_JS}</head>"""
def nav(brand):
    return f"""<a class=skip href="#main">Skip to content</a><header class=top><div class=wrap><a class=logo href="/" aria-label="{brand} home"><i></i>{brand}</a>
<nav class=navlinks id=nl aria-label=Primary><a href="/#features">Product</a><a href="/pricing">Pricing</a><a href="/docs">Docs</a><a href="/guides">Guides</a><a href="/blog">Blog</a></nav>
<span class=sp></span><button class=icon-btn onclick="toggleTheme()" aria-label="Toggle dark/light theme">{svg("sun")}</button>
<a href="/login" class=hide-sm>Log in</a><a class="btn pri sm" href="/login#register">Start free</a>
<button class="icon-btn menu-btn" aria-label="Menu" onclick="document.getElementById('nl').classList.toggle('open')">{svg("menu")}</button></div></header>"""
def footer(brand):
    return f"""<footer class=f><div class=wrap><div class=cols><div><a class=logo href="/"><i></i>{brand}</a><p class=muted style="max-width:300px;margin-top:12px">Cheap, per-second hosting for backends and APIs. Compute in Nuremberg, Germany; billed in USD.</p></div>
<div><h5>Product</h5><a href="/#features">Features</a><a href="/pricing">Pricing</a><a href="/regions">Regions</a><a href="/customers">Example architectures</a><a href="/pricing#gpus">GPUs (soon)</a></div>
<div><h5>Developers</h5><a href="/docs">Docs</a><a href="/guides">Guides</a><a href="/blog">Blog</a><a href="/docs/cli">CLI</a><a href="/api/docs">API reference</a><a href="/docs/mcp">MCP</a><a href="/llms.txt">llms.txt</a><a href="/rss.xml">RSS</a></div>
<div><h5>Company</h5><a href="/about">About</a><a href="/security">Security</a><a href="/status">Status</a><a href="/privacy">Privacy</a><a href="/terms">Terms</a></div>
<div><h5>Account</h5><a href="/login">Log in</a><a href="/login#register">Sign up</a><a href="/dashboard">Dashboard</a></div></div>
<div class=base><span>© 2026 {brand}</span><span>Prices in USD, excluding tax.</span></div></div></footer>"""
def page(title, body, brand="Aloof", desc=None):
    d = desc or f"{brand}: cheap backends in the EU, billed per second in USD (GPUs coming soon). Deploy Docker images or git repos; scale to zero; MCP for AI assistants."
    return f"""{head(title, d)}<body>{nav(brand)}<main id=main>{body}</main>{footer(brand)}{COMMON_JS}</body></html>"""
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
TERM = [("p", "$ ", "aloof deploy api --git https://github.com/acme/api.git"), ("m", "", "→ cloning acme/api@main"), ("m", "", "→ building Dockerfile (cached layers: 7/9)"),
        ("m", "", "→ pushing image to registry"), ("c", "", "→ starting 2 instances in eu-de"), ("g", "✓ ", "health check passed /healthz (38 ms)"),
        ("g", "✓ ", "live: https://api.{base}/"), ("p", "$ ", "aloof autoscale api --min 0 --max 6 --idle 300"), ("g", "✓ ", "scale to zero enabled · billed per second")]
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
def landing(brand, base, api, mcp):
    g = {k: PLANS[k]["price_usd_hr"] for k in ("gpu-l4", "gpu-l40s", "gpu-a100", "gpu-h100")}
    gpu_card = (f"<div class=card><h3>GPUs by the second</h3><p>L4 ${g['gpu-l4']:.2f}/hr, A100 80GB ${g['gpu-a100']:.2f}/hr, H100 ${g['gpu-h100']:.2f}/hr on dedicated nodes.</p></div>"
                if _plans.GPU_ENABLED else _feat("bolt", "GPUs: coming soon", "L4, RTX 4090, L40, A100 80GB and H100, billed per second with scale to zero. <a href='/pricing#gpus'>Join the waitlist →</a>"))
    cpu = [k for k, p in PLANS.items() if p["kind"] == "cpu" and not p["legacy"] and p["cpu_class"] == "shared"]
    pcards = "".join(f"<div class='card reveal'><h3><code>{k}</code></h3><div class=price>{_money(PLANS[k]['price_usd_month'])}<small>/mo</small></div><p>{PLANS[k]['vcpu']:g} shared vCPU · {_gb(PLANS[k]['memory_mb'])} GB RAM · per second</p></div>" for k in cpu[:4])
    T = TIERS; p0 = PLANS["shared-1x-256mb"]["price_usd_month"]
    faq = [("Where does my code run?", "On Hetzner Cloud in Nuremberg, Germany. The control plane runs there too, and encrypted off-site backups are stored in Helsinki, Finland."),
           ("What can I deploy?", "Any Docker image, or a git repo with a Dockerfile. This covers Node.js, Python, Go, Rust, Ruby, Java/JVM, PHP and static sites."),
           ("How does scale-to-zero affect latency?", "Idle apps sleep and wake on the next request in about a second."),
           ("How are secrets handled?", "Environment variables are encrypted at rest and never shown back. API keys are stored only as SHA-256 hashes."),
           ("Can an AI agent deploy for me safely?", "Yes. Agents use OAuth 2.1 with PKCE and get scoped tokens that you can revoke at any time."),
           ("Do you support GPUs?", "GPUs are coming soon: L4, RTX 4090, L40, A100 80GB and H100, billed per second with scale to zero. <a href='/pricing#gpus'>Join the waitlist</a>." if not _plans.GPU_ENABLED else "Yes: L4, L40, A100 80GB and H100 on dedicated nodes, billed per second. See <a href='/pricing#gpus'>GPU pricing</a>.")]
    faqh = "".join(f"<details class=faq><summary>{q}</summary><p>{a}</p></details>" for q, a in faq)
    tier = lambda k, name, who: f"<div class='card reveal{' hl' if k == 'pro' else ''}'><h3>{name}</h3><div class=price>${T[k]['fee_usd']}<small>/mo{' per workspace' if k == 'pro' else ''}</small></div><p>{'Includes $' + str(T[k]['included_usage_usd']) + '/month of usage.' if k == 'free' else 'Includes $' + str(T[k]['included_usage_usd']) + ' of usage.'} {who}</p></div>"
    body = f"""<section class=hero><div class=wrap><div>
<h1>Infrastructure for the age of <span class=grad>superintelligence.</span></h1>
<p class=lead>Let Claude, ChatGPT or Cursor deploy, scale and monitor your backends over MCP. They run in the EU, scale to zero, are billed per second, and GPUs are coming soon.</p>
<div class=cta><a class="btn pri" href="/login#register">Deploy free</a><a class="btn ghost" href="/docs/mcp">Connect your AI assistant (MCP)</a></div>
<div class=trust><span>{svg("lock")} TLS on every app</span><span>{svg("key")} Secrets encrypted at rest</span><span>{svg("eu")} EU hosting</span><span>{svg("bolt")} Per-second billing in USD</span></div></div>
{_term(base)}</div></section>
<div class=wrap><div class=works aria-label="Works with">{stack_row()}</div></div>
<section class=s id=features><div class=wrap><div class=eyebrow>Platform</div><h2 class=t>What's built in</h2>
<div class="grid g3">{_feat("git", "Deploy from git or an image", "Push a repo with a Dockerfile or point at any image: Node.js, Python, Go, Rust, Ruby, Java/JVM, PHP or static sites. Redeploys only switch over after the new instance passes its health check.")}
{_feat("scale", "Pay nothing while it's idle", "Scale on in-flight requests or CPU. Idle apps sleep and wake on the next request in about a second.")}
{_feat("disk", "State that survives", "Persistent volumes survive restarts and redeploys, billed per provisioned GB-month.")}
{_feat("globe", "Your domain, with TLS handled", "Add one TXT record and a CNAME. We issue and renew certificates for you.")}
{_feat("bot", "Let your AI assistant run deploys", "A remote MCP server with OAuth 2.1 lets Claude, ChatGPT or Cursor deploy, read logs, scale and add domains. You get scoped tokens you can revoke at any time, plus <a href='/llms.txt'>llms.txt</a> and an OpenAPI spec.")}
{gpu_card}</div></div></section>
<section class=s id=how><div class=wrap><div class=eyebrow>How it works</div><h2 class=t>How a deploy works</h2>
<div class="grid g3"><div class="card reveal"><div class=ico>1</div><h3>Bring your code.</h3><p>Use a git repo with a Dockerfile or any container image.</p></div>
<div class="card reveal"><div class=ico>2</div><h3>Deploy.</h3><p>Run one CLI command, use the dashboard or REST API, or ask your assistant over MCP.</p></div>
<div class="card reveal"><div class=ico>3</div><h3>Go live.</h3><p>You get an HTTPS URL on <code>.{base}</code> (or your own domain), autoscaling and logs, billed per second.</p></div></div></div></section>
<section class=s id=security><div class=wrap><div class=eyebrow>Security</div><h2 class=t>Security defaults</h2><p class=t>What the platform does today. We hold no certifications yet.</p>
<div class="grid g3">{_feat("lock", "TLS everywhere", "Every app, API and MCP endpoint is served over HTTPS with automatically issued certificates. Certificates are only requested for hosts we know.")}
{_feat("key", "Encrypted secrets", "Environment variables are encrypted at rest and never shown back. API keys are stored only as SHA-256 hashes.")}
{_feat("box", "Isolated containers", "Each app runs in its own container with CPU and memory limits. Nodes join over WireGuard and expose no inbound ports.")}</div>
<p class=muted style="margin-top:22px">Read more on the <a href="/security">security page</a>. Found an issue? Email <a href="mailto:security@aloof.si">security@aloof.si</a>.</p></div></section>
<section class=s id=pricing><div class=wrap><div class=eyebrow>Pricing</div><h2 class=t>Pricing</h2>
<div class="grid g3">{tier("free", "Free", "Good for side projects and trying Aloof.")}{tier("hobby", "Hobby", "For personal apps that need to stay on.")}{tier("pro", "Pro", "For teams shipping production backends.")}</div>
<p class=t style="margin-top:20px">Usage beyond that is billed per second. Instance sizes start at <code>shared-1x-256mb</code> ({_money(p0)}/mo if run all month). Prices are in USD and exclude tax. Payments are handled by Stripe.</p>
<div class="grid">{pcards}</div><p style="margin-top:20px"><a href="/pricing">Performance CPUs, volumes, egress and tiers →</a></p></div></section>
<section class=s id=faq><div class=wrap style="max-width:820px"><div class=eyebrow>FAQ</div><h2 class=t>FAQ</h2>{faqh}</div></section>
<section class=ctaband><h2>Give your superintelligence a stronger engine.</h2><p class=sub style="margin:0 0 26px">Deploy your first backend free in one command.</p><div class=cta style="justify-content:center"><a class="btn pri" href="/login#register">Deploy free</a><a class="btn ghost" href="/docs">Read the docs</a></div></section>"""
    return page(f"{brand}: infrastructure for the age of superintelligence", body, brand)
def _pg_rows():
    from .plans import PG_PLANS
    return "".join(f"<tr><td><code>{k}</code> <span class=muted>{p['note']}</span></td><td>{p['vcpu']:g} {'shared' if p['compute'].startswith('shared') else 'dedicated'} vCPU / {p['memory_mb'] if p['memory_mb'] < 1024 else p['memory_mb'] // 1024}{' MB' if p['memory_mb'] < 1024 else ' GB'}</td><td>{p['disk_gb']} GB</td><td><b>${p['price_usd_month']}</b>/mo</td></tr>" for k, p in PG_PLANS.items())
def pricing(brand):
    T, R = TIERS, RATES
    ck = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12 5 5 9-10"/></svg>'
    def plan(k, name, who, feats, cta, hl=False):
        t = T[k]; per = " per workspace" if k == "pro" else ""
        lis = "".join(f"<li>{ck}<span>{f}</span></li>" for f in feats)
        return (f"<div class='pp-card{' hl' if hl else ''}'><div class=pp-name>{name}</div><p class=pp-who>{who}</p>"
                f"<div class=pp-price>${t['fee_usd']:g}<small>/month{per}</small></div>"
                f"<div class=pp-inc>{'$' + format(t['included_usage_usd'], 'g') + ' of usage included'}{' · hard cap' if t.get('hard_cap') else ''}</div>"
                f"<a class='btn {'pri' if hl else 'ghost'} pp-cta' href='/login#register'>{cta}</a><ul class=pp-feat>{lis}</ul></div>")
    free, hob, pro = T["free"], T["hobby"], T["pro"]
    cards = plan("free", "Free", "For trying Aloof and side projects.", [
        f"{free['max_apps']} apps, 1 instance each", "shared-1x-256mb and shared-1x-512mb", f"Apps sleep after {free['idle_seconds']//60} min idle",
        f"Volumes up to {free['max_volume_gb']} GB", f"{free['log_retention_days']} day of log history", f"Never billed: apps pause at the included ${free['included_usage_usd']:g}"], "Start free") + \
        plan("hobby", "Hobby", "For personal apps that stay on.", [
        f"Up to {hob['max_apps']} apps, 1 seat", "Every CPU size, autoscaling", "Always-on apps or scale to zero", "Volumes, managed Postgres, custom domains",
        f"{hob['log_retention_days']} days of log history", "Usage beyond the included amount billed per second"], "Start with Hobby", True) + \
        plan("pro", "Pro", "For teams running production.", [
        "Unlimited apps", "One user plus scoped deploy tokens (team seats coming later)", "Everything in Hobby",
        f"{pro['log_retention_days']} days of log history", "Usage beyond the included amount billed per second"], "Start with Pro")
    faq = [("What does “usage included” mean?", f"Your plan fee comes back as credit for compute, storage and egress. Hobby is ${hob['fee_usd']:g} a month and includes ${hob['included_usage_usd']:g} of usage, so a small always-on app often costs only the plan fee."),
           ("What happens on the Free plan if I reach the limit?", f"Apps pause when they've used the included ${free['included_usage_usd']:g} for the month. You're never charged on Free."),
           ("Do sleeping apps cost anything?", "No. Apps that are stopped or scaled to zero don't accrue compute charges. Volumes are still billed per provisioned GB-month."),
           ("How do I pay?", "Add prepaid credit with a card through Stripe Checkout ($10 to $5,000 per top-up), or subscribe to Hobby or Pro. Each payment gets an invoice."),
           ("Are taxes included?", "No. Prices are in USD and exclude VAT, GST and sales tax, which are added at checkout where they apply. You can add a VAT or GST ID to your billing profile."),
           ("Can I change plans later?", "Yes, at any time from Billing in the dashboard. Upgrades paid from your balance are charged pro-rata.")]
    faqh = "".join(f"<details class=faq><summary>{q}</summary><p>{a}</p></details>" for q, a in faq)
    head = "<tr><th>Plan</th><th>vCPU</th><th>Memory</th><th class=num>Per hour</th><th class=num>Per month</th></tr>"
    def rows(cls):
        return "".join(f"<tr><td><code>{k}</code></td><td>{p['vcpu']:g} {cls}</td><td>{_gb(p['memory_mb'])} GB</td><td class=num>${p['price_usd_hr']:.4f}</td><td class=num><b>{_money(p['price_usd_month'])}</b></td></tr>"
                       for k, p in PLANS.items() if p["kind"] == "cpu" and not p["legacy"] and p["cpu_class"] == cls)
    return page(f"Pricing · {brand}", f"""<section class="pp-hero"><div class=wrap><h1>Pricing</h1><p class=lead>Start free. Pay per second for what you run.</p></div></section>
<section class=pp><div class=wrap>
<div class=pp-grid>{cards}</div>
<p class="muted pp-note">Prices in USD, excluding tax. Need committed capacity, dedicated nodes or invoicing? <a href="mailto:sales@aloof.si">Talk to us</a>.</p>
<h2 class=pp-h id=compute>Compute</h2><p class=pp-sub>Billed per second while an instance runs. The monthly figure is the cost of running all month.</p>
<h3 class=pp-h3>Shared CPU</h3><p class="muted pp-d">Burstable vCPUs with a guaranteed 1/8-core baseline. Good for APIs, bots and workers.</p>
<div class=scroll><table class="tbl pp-tbl">{head}{rows("shared")}</table></div>
<h3 class=pp-h3>Performance CPU</h3><p class="muted pp-d">A full dedicated vCPU for each vCPU in the plan.</p>
<div class=scroll><table class="tbl pp-tbl">{head}{rows("performance")}</table></div>
<h2 class=pp-h id=storage>Storage and network</h2>
<div class=scroll><table class="tbl pp-tbl"><tr><th>Resource</th><th>Unit</th><th class=num>Price</th></tr>
<tr><td>Volumes</td><td>per provisioned GB-month</td><td class=num><b>${R['volume_usd_gb_month']:.2f}</b></td></tr>
<tr><td>Backups and volume snapshots</td><td>per GB-month stored</td><td class=num><b>${R['backup_usd_gb_month']:.2f}</b></td></tr>
<tr><td>Egress</td><td>per GB, counts toward included usage</td><td class=num><b>${R['egress_usd_gb']:.2f}</b></td></tr>
<tr><td>Extra RAM</td><td>per GB-month</td><td class=num><b>${R['extra_ram_usd_gb_month']:.2f}</b></td></tr>
<tr><td>Inbound data, custom domains and TLS</td><td>—</td><td class=num><b>Free</b></td></tr></table></div>
<h2 class=pp-h id=postgres>Managed Postgres</h2><p class=pp-sub>Postgres 16 on a private network next to your apps. Daily backups (7 kept), on-demand backup and restore. The price covers compute and the data disk; backup storage is billed as above.</p>
<div class=scroll><table class="tbl pp-tbl"><tr><th>Plan</th><th>CPU / RAM</th><th>Disk</th><th class=num>Per month</th></tr>{_pg_rows().replace("<td><b>$", "<td class=num><b>$")}</table></div>
{_gpu_section()}
<h2 class=pp-h id=billing>How billing works</h2>
<div class=pp-how>
<div><b>1. Pick a plan</b><p>Free, Hobby or Pro. The plan fee comes back to you as usage credit each month.</p></div>
<div><b>2. Run your apps</b><p>Compute is metered per second while instances run. Volumes are billed per provisioned GB-month, egress per GB.</p></div>
<div><b>3. Pay only beyond the credit</b><p>Usage past the included amount comes from your prepaid balance. Apps scaled to zero cost nothing.</p></div></div>
<h2 class=pp-h id=faq>Pricing FAQ</h2><div class=pp-faq>{faqh}</div>
<div class=pp-end><h2>Start free, upgrade when you need to.</h2><p class=muted>{T['free']['max_apps']} apps and ${free['included_usage_usd']:g} of usage a month on the Free plan. No card required.</p><a class="btn pri" href="/login#register">Create an account</a> <a class="btn ghost" href="/docs/billing">Billing docs</a></div>
</div></section>""", brand, desc="Aloof pricing: Free, Hobby $5 and Pro $20 per workspace with usage included. Compute billed per second; volumes, egress and managed Postgres in USD.")
DOCS = ["quickstart", "cli", "deploy", "config", "private", "github", "ci", "processes", "networking", "releases", "gpu", "volumes", "postgres", "console", "logs", "autoscaling", "domains", "billing", "mcp", "api", "nodes", "troubleshooting", "support"]
TITLES = {"quickstart": "Quickstart", "cli": "CLI", "deploy": "Deploying apps", "config": "aloof.toml reference", "private": "Private registries & repos", "github": "GitHub App: push-to-deploy & previews", "ci": "Deploy tokens & CI", "processes": "Workers, release commands & cron", "networking": "Private networking, proxy, IPs & certs", "gpu": "GPU workloads", "volumes": "Volumes", "autoscaling": "Autoscaling & scale to zero",
          "domains": "Custom domains", "releases": "Releases & rollback", "postgres": "Managed Postgres", "console": "SSH & exec console", "logs": "Logs, retention & drains", "billing": "Billing, invoices & tax", "mcp": "MCP for AI assistants", "api": "REST API", "nodes": "Bring your own nodes", "troubleshooting": "Troubleshooting & FAQ", "support": "Support"}
def docs(pg, brand, base, api, mcp):
    if pg not in DOCS: return None
    EDGE_TXT = ' or '.join(f'<code>{x}</code>' for x in os.environ.get('PAAS_EDGE_IPS', '').split(',') if x.strip() and ':' not in x) or 'our edge IP (<code>aloof ips &lt;app&gt;</code>)'
    from .site_devx import pages as _devx_pages
    c = ({
"quickstart": f"""<h1>Quickstart</h1><ol><li><a href="/login#register">Create an account</a>. The Free plan includes $1/month of usage with a hard cap; top up or pick Hobby/Pro when you need more.</li>
<li>Install the CLI. It's a single Python 3 script and runs on <b>macOS and Linux</b>. On <b>Windows</b>, use it inside <a href="https://learn.microsoft.com/windows/wsl/install" rel=noopener>WSL</a>.
<pre># system-wide (asks for your password)
sudo curl -fsSL {api}/cli/aloof -o /usr/local/bin/aloof && sudo chmod +x /usr/local/bin/aloof

# or without sudo, into ~/.local/bin (make sure it's on your PATH)
mkdir -p ~/.local/bin && curl -fsSL {api}/cli/aloof -o ~/.local/bin/aloof && chmod +x ~/.local/bin/aloof</pre>
Check it with <code>aloof --help</code>. It needs <code>python3</code>, which macOS and most Linux distributions already have.</li>
<li>Log in. New here? <code>aloof signup you@company.com</code> creates an account and saves the key for you. Already have an account? Copy your API key from Dashboard → API keys and save it in <code>~/.aloof.json</code>:
<pre>{{"api": "{api}", "key": "pk_..."}}</pre>
Then <code>chmod 600 ~/.aloof.json</code>, since the key has full access to your account. In CI, set <code>ALOOF_API_URL</code> and <code>ALOOF_API_TOKEN</code> (a <a href="/docs/ci">deploy token</a>) instead.</li>
<li>Deploy: <pre>aloof deploy hello --image nginx:alpine --port 80
live: https://hello.{base}/</pre></li></ol>
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
<p><b>Rollback</b> redeploys an earlier release's image, env, plan, port, command, scale and health settings, without a rebuild (git builds are tagged per release; the last 20 images are kept). Volumes, database attachments and domains stay as they are now. Also available as <code>POST /v1/apps/{name}/rollback</code>, the <code>rollback_app</code> MCP tool, and Dashboard → app → Releases.</p>""",
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
<h2 id=resize>Changing plan or disk size</h2><p>Resizing a database in place isn't available yet, and backups restore only into the database they came from. To move to a bigger plan today, copy the data over a tunnel: create the new database (<code>aloof pg create shop-db2 --plan pg-standard</code>), run <code>pg_dump</code> against the old one through <code>aloof proxy</code> and <code>pg_restore</code> into the new one the same way, then attach the new database to your app (which updates <code>DATABASE_URL</code> and redeploys it) and destroy the old one when you're happy. If you'd rather not do that yourself, <a href="/docs/support">contact support</a>.</p>""",
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
"domains": f"""<h1>Custom domains</h1><p>Every app gets <code>https://&lt;app&gt;.{base}</code>. To add your own domain:</p>
<pre>aloof domains api add api.example.com</pre><p>Then create the DNS records it prints at your DNS provider:</p>
<table><tr><th>Type</th><th>Name</th><th>Value</th><th>Purpose</th></tr>
<tr><td>TXT</td><td><code>_aloof-challenge.api.example.com</code></td><td><code>aloof-verify=&lt;token&gt;</code></td><td>proves you own the domain</td></tr>
<tr><td>CNAME</td><td><code>api.example.com</code></td><td><code>&lt;app&gt;.{base}</code></td><td>routes traffic (subdomains)</td></tr>
<tr><td>A</td><td><code>example.com</code></td><td>{EDGE_TXT}</td><td>routes traffic (root domains, or instead of the CNAME)</td></tr></table>
<p>Run <code>aloof domains api verify api.example.com</code>. Once both records check out, the certificate is issued automatically on the first HTTPS request, which usually takes a few seconds. <code>aloof certs</code> shows the status. The edge IP is also printed by <code>aloof ips &lt;app&gt;</code>.</p>
<h2 id=root>Root (apex) domains</h2><p>DNS doesn't allow a CNAME on a root domain like <code>example.com</code>, so use the <b>A record</b> above. If your provider supports CNAME flattening or ALIAS/ANAME records, you can point those at <code>&lt;app&gt;.{base}</code> instead. To serve both <code>example.com</code> and <code>www.example.com</code>, add each as a domain.</p>
<h2 id=cloudflare>Cloudflare</h2><p>Set the records to <b>DNS only</b> (grey cloud). With the orange-cloud proxy on, the domain resolves to Cloudflare's IPs instead of ours, so the routing check fails and we can't issue a certificate. TXT records are never proxied, so they're fine as they are.</p>
<h2 id=propagation>Propagation</h2><p>New records usually show up within a few minutes, but resolvers can cache old answers for as long as the previous record's TTL (often up to an hour, rarely 24–48 hours). It's safe to run <code>verify</code> again as often as you like.</p>
<h2 id=verify-failed>If verify fails</h2><ul>
<li><b><code>TXT … not found</code></b>: the record hasn't propagated yet, or the name is wrong. Many DNS panels add your domain automatically, so enter just <code>_aloof-challenge.api</code> rather than the full name. Check with <code>dig TXT _aloof-challenge.api.example.com +short</code>.</li>
<li><b><code>CNAME points to …</code></b> or <b><code>resolves to …; expected …</code></b>: the routing record points somewhere else. Remove old A/AAAA/CNAME records for the same name, turn off proxying (Cloudflare), and check with <code>dig api.example.com +short</code>.</li>
<li><b>Verified, but HTTPS shows a certificate error</b>: give it a minute after the first request, then check <code>aloof certs</code>. CAA records on your domain must allow Let's Encrypt (<code>letsencrypt.org</code>).</li></ul>""",
"billing": """<h1>Billing, invoices &amp; tax</h1>
<p><b>The short version:</b> you pick a plan (Free, Hobby $5/month or Pro $20/month per workspace), and the plan fee comes back to you as included usage. Apps are billed per second only while they run. If you use more than your plan includes, the rest comes out of a prepaid balance that you top up by card. Prices are in US dollars and exclude tax.</p>
<h2>Upgrading, topping up and cancelling</h2><ul>
<li><b>Upgrade or change plan</b>: Dashboard → Billing &amp; usage → Plan. Choose the plan and either <b>Subscribe by card</b> (Stripe charges the fee monthly) or <b>Pay from balance</b> (this month's fee comes out of your credit, pro-rated).</li>
<li><b>Add credit</b>: Dashboard → Billing &amp; usage → Balance → <b>Add credit</b>, between $10 and $5,000 per top-up. Credit is added as soon as Stripe confirms the payment.</li>
<li><b>Cards, receipts and cancelling a subscription</b>: Dashboard → Billing &amp; usage → <b>Billing portal</b> (Stripe's customer portal).</li></ul>
<div class=note><b>Cancellation and refund policy: pending.</b> The terms for what happens to the rest of a billing period and to unused prepaid credit when you cancel haven't been published yet. Until they are, contact us (see <a href="/docs/support">Support</a>) before cancelling if you have questions about a refund.</div>
<h2>What is metered</h2><p>Per second, in cents: compute while an instance runs (plan $/hr ÷ 3600), volumes per provisioned GB-month ($0.22, attached or not), backups and snapshots per GB-month ($0.22), and egress at $0.06/GB. Usage up to your plan's included amount (Free $1, Hobby $5, Pro $20) isn't deducted from your balance. Stopped and scaled-to-zero apps cost nothing; volumes are still billed.</p>
<p>See this month's breakdown in Dashboard → Billing &amp; usage. When a paid account's credit runs out, its apps are suspended, and they resume automatically after a top-up.</p>
<p><b>Invoices and tax</b>: Stripe issues an invoice for every payment. VAT, GST or US sales tax is calculated at checkout from your billing address, and you can enter a VAT/GST ID for reverse charge where it applies.</p>
<p><b>Free tier</b> is hard-capped and never draws on your wallet: 3 apps on <code>shared-1x-256mb</code>/<code>shared-1x-512mb</code>, 1 instance each, always scale-to-zero after at most 5 minutes idle, 1 GB of volumes. Once the month's usage reaches the included $1, apps pause (requests get HTTP 402) until the 1st of next month or until you upgrade.</p>
<p>v1 plan ids (<code>cpu-small</code> etc.) still work and are billed at the equivalent current price. Indian customers can optionally be offered INR/UPI top-ups via Razorpay (off by default).</p>
<h2>API</h2><p>Everything above is also available over the API: <code>POST /v1/billing/checkout {"credit_usd": 25}</code>, <code>POST /v1/billing/subscribe {"tier":"pro"}</code>, <code>POST /v1/billing/portal</code>, <code>PUT /v1/billing/tier {"tier":"pro"}</code> (pay from balance) and <code>GET /v1/billing</code>.</p>""",
"mcp": f"""<h1>MCP for AI assistants</h1><p>{brand} exposes a Model Context Protocol server, so assistants can pick plans, deploy, read logs, scale, attach volumes and add domains for you or your users.</p>
<p><b>Remote (OAuth 2.1)</b>: add <code>{mcp}</code> as a connector in Claude, ChatGPT or Cursor. The assistant registers itself (dynamic client registration), sends you to {brand} to approve, and gets a scoped token (PKCE).</p>
<p><b>Local (stdio)</b>:</p><pre>{{"mcpServers": {{"aloof": {{"command": "python3", "args": ["mcp_server/server.py"],
  "env": {{"ALOOF_API_URL": "{api}", "ALOOF_API_KEY": "pk_..."}}}}}}}}</pre>
<p>Tools: list_plans, signup, deploy_app, get_status, list_apps, get_logs, scale_app, set_env, destroy_app, get_usage, create_volume, list_volumes, attach_volume, detach_volume, set_autoscale, add_domain, list_releases, rollback_app, create_postgres, list_postgres, attach_postgres, backup_postgres, snapshot_volume, restore_volume, exec_command, add_log_drain, list_offsite_backups, restore_offsite_backup, registry_login, add_git_credential, deploy_with_config, app_status, create_deploy_token.</p>
<p>Discovery: <a href="/llms.txt">/llms.txt</a>, <a href="/.well-known/mcp.json">/.well-known/mcp.json</a>.</p>""",
"api": f"""<h1>REST API</h1><p>Base URL <code>{api}</code>. Authenticate with <code>Authorization: Bearer pk_...</code>. The full interactive reference is at <a href="/api/docs">/api/docs</a>.</p>
<pre>curl -X POST {api}/v1/apps -H "Authorization: Bearer $ALOOF_API_KEY" -H "Content-Type: application/json" \\
  -d '{{"name":"api","source":{{"image":"ghcr.io/me/api:1"}},"plan":"shared-1x-1gb","port":8080,"env":{{"DATABASE_URL":"..."}}}}'</pre>""",
"troubleshooting": f"""<h1>Troubleshooting &amp; FAQ</h1>
<h2 id=402>Requests return HTTP 402</h2><p>402 means the app is paused for billing, not that it crashed. On the <b>Free</b> plan it happens when the month's usage reaches the included $1: apps resume on the 1st of next month, or straight away when you upgrade. On a <b>paid</b> plan it means your prepaid credit ran out: apps resume automatically after a top-up. Both are in Dashboard → Billing &amp; usage. The API also returns 402 when an action needs credit you don't have, such as upgrading from your balance.</p>
<h2 id=failed-releases>A deploy failed</h2><p>A failed release never takes traffic. If an earlier release was running, it keeps serving and the app shows <i>v2 failed … still serving v1</i>. Open the app in the dashboard to see which step failed and the last log lines, or run <code>aloof releases &lt;app&gt;</code>. The usual causes, in order:</p><ol>
<li><b>Wrong port</b>: the app has to listen on <code>0.0.0.0:$PORT</code> (default 8080), and <code>--port</code> or <code>internal_port</code> has to match. Listening on <code>127.0.0.1</code> fails the readiness check.</li>
<li><b>Health path</b>: if you set one with <code>aloof health &lt;app&gt; --path /healthz</code>, it must return a 2xx/3xx status within a few seconds of starting.</li>
<li><b>The release command failed</b> (see below).</li>
<li><b>Image or repo can't be pulled</b>: for private sources, run <code>aloof registry login</code> or set up a <a href="/docs/private">deploy key</a>.</li>
<li><b>The build failed</b>: see build logs below.</li></ol>
<p>After fixing it, deploy again or press <b>Retry deploy</b> on the app. To go back to a known-good version without rebuilding, run <code>aloof rollback &lt;app&gt;</code>.</p>
<h2 id=build-logs>Where are the build logs?</h2><p>Build and deploy output is recorded as platform events. You'll find it in the deploy panel on the app's page in the dashboard (shown while a deploy runs and when it fails), under the app's <b>Logs</b> tab, and with <code>aloof logs &lt;app&gt;</code> (the <i>platform events</i> section). The build step stores the last 40 lines of <code>docker build</code> output when the build finishes, so you'll see them in one batch rather than line by line.</p>
<h2 id=release-command>What happens when the release command fails?</h2><p><code>release_command</code> (for example database migrations) runs once, in a separate machine, after the build and before any new instance starts. If it exits with a non-zero code, the deploy stops: no new instances start, the release is marked failed, the previous release keeps serving, and the last 30 lines of its output are in the logs prefixed with <code>[release]</code>. Fix the command and deploy again. Make migrations safe to run twice, because a retry runs them again.</p>
<h2 id=brief-downtime>A short gap during a deploy</h2><p>Apps with a volume attached are single-writer, so the old instance stops before the new one starts, which causes a brief gap. Apps without a volume roll with no downtime.</p>
<h2 id=domains>Custom domain won't verify</h2><p>See <a href="/docs/domains#verify-failed">Custom domains → If verify fails</a>. Cloudflare users: set the records to DNS only.</p>
<h2 id=postgres>Can't reach Postgres from my laptop</h2><p>Databases are private. Use <code>aloof proxy 5432 -a pg-&lt;name&gt;</code>; see <a href="/docs/postgres#local">Managed Postgres</a>.</p>
<h2 id=more>Still stuck?</h2><p>See <a href="/docs/support">Support</a>.</p>""",
"support": f"""<h1>Support</h1>
<div class=note><b>Support contact: pending.</b> The support email address and response times haven't been published yet. This page will list them.</div>
<p>When you contact us, include your account email, the app name, the time it happened (with your timezone) and what you ran. Never send API keys, passwords or <code>DATABASE_URL</code> values; we'll never ask for them.</p>
<p><b>Security issues</b>: see the <a href="/security">security page</a>.</p>
<p>Many answers are in <a href="/docs/troubleshooting">Troubleshooting &amp; FAQ</a>.</p>""",
"nodes": f"""<h1>Bring your own nodes</h1><p>An admin can add any Ubuntu 22.04/24.04 server to a pool, for example Yotta Shakti bare metal or your own GPU box. The node gets a single-use join token, connects to the control plane over WireGuard (outbound UDP only, so no inbound ports are needed), and runs the {brand} agent.</p>
<pre>curl -X POST {api}/v1/admin/nodes -H "Authorization: Bearer $ADMIN" -d '{{"pool":"self-hosted","plan":"gpu-h100","gpu":true}}'
# run the returned one-liner on the server:
curl -fsSL '{api}/v1/nodes/bootstrap.sh?node=n-…&token=jt_…' | sudo bash</pre>
<p>The bootstrap installs Docker, the NVIDIA container toolkit (on GPU nodes) and WireGuard, registers the node and starts <code>aloof-agent</code> under systemd.</p>"""} | _devx_pages(brand, base, api))[pg]
    if pg == "postgres":
        from .plans import PG_PLANS
        c = c.replace("{PGROWS}", "".join(f"<tr><td><code>{k}</code></td><td>{p['vcpu']:g} {'shared' if p['compute'].startswith('shared') else 'dedicated'} vCPU / {p['memory_mb']} MB</td><td>{p['disk_gb']} GB</td><td><b>${p['price_usd_month']}</b>/mo</td></tr>" for k, p in PG_PLANS.items()))
    groups = [("Getting started", ["quickstart", "cli", "deploy", "config", "releases"]), ("Source & CI", ["private", "github", "ci"]), ("Apps", ["processes", "networking"]), ("Platform", ["autoscaling", "volumes", "postgres", "domains", "gpu"]), ("Operate", ["console", "logs"]), ("Account", ["billing"]), ("Integrations", ["mcp", "api", "nodes"])]
    navh = "".join(f"<h4>{g}</h4>" + "".join(f'<a href="/docs/{d}" class="{"on" if d == pg else ""}"{" aria-current=page" if d == pg else ""}>{TITLES[d]}</a>' for d in ds) for g, ds in groups)
    c = c.replace("<table>", "<div class=scroll><table class=tbl>").replace("</table>", "</table></div>")
    return page(f"{TITLES[pg]} · {brand} docs", f"<div class='wrap docs'><aside aria-label='Docs navigation'>{navh}</aside><article class=docmain style='min-width:0;max-width:780px'>{c}</article></div>", brand)
def login(brand):
    return page(f"Log in · {brand}", f"""<div class=auth><div class=card><a class=logo href="/" style="margin-bottom:18px"><i></i>{brand}</a>
<h1 id=title>Welcome back</h1><p class=muted id=subt style="margin:0">Log in to deploy and manage your apps.</p>
<div class=tabs role=tablist><button type=button role=tab id=tl class=on onclick="mode('login')">Log in</button><button type=button role=tab id=tr onclick="mode('register')">Create account</button></div>
<form onsubmit="event.preventDefault();go(M)"><div id=msg role=status aria-live=polite class=muted style="min-height:20px;font-size:14px"></div>
<label for=email>Email</label><input id=email type=email autocomplete=email required placeholder="you@company.com">
<label for=pw id=pwl>Password</label><input id=pw type=password autocomplete=current-password placeholder="Your password" aria-describedby=pwh><small id=pwh class="muted helper" style="display:none">At least 10 characters.</small>
<p style="margin-top:18px"><button class="btn pri" style="width:100%;justify-content:center" id=lb>Log in</button></p></form>
<p class=muted style="text-align:center;font-size:14px">or <a href="#" onclick="magic();return false">email me a sign-in link</a></p>
<p class="muted secure-note">{svg("lock")}<span>Sessions use HttpOnly cookies. By continuing you agree to the <a href="/terms">Terms</a> and <a href="/privacy">Privacy policy</a>.</span></p></div></div>
<script>
var M='login';function mode(m){{M=m;tl.classList.toggle('on',m=='login');tr.classList.toggle('on',m=='register');title.textContent=m=='login'?'Welcome back':'Create your account';
subt.textContent=m=='login'?'Log in to deploy and manage your apps.':'Free plan with $1/month of included usage.';lb.textContent=m=='login'?'Log in':'Create account';pwh.style.display=m=='login'?'none':'block';pw.placeholder=m=='login'?'Your password':'Choose a password';pw.autocomplete=m=='login'?'current-password':'new-password'}}
if(location.hash=='#register')mode('register');
async function go(kind){{const r=await fetch('/v1/auth/'+kind,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email:email.value,password:pw.value}})}});
 const j=await r.json(); if(!r.ok){{msg.className='bad';msg.textContent=j.detail||'error';return}}
 if(j.api_key) sessionStorage.newKey=j.api_key; var n=new URLSearchParams(location.search).get('next');location=(n&&n.charAt(0)=='/'&&n.charAt(1)!='/'&&n.indexOf(String.fromCharCode(92))<0)?n:'/dashboard'}}
async function magic(){{const r=await fetch('/v1/auth/magic',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email:email.value}})}});
 const j=await r.json(); msg.className=r.ok?'ok':'bad'; msg.textContent=j.detail}}
</script>""", brand)
LEGAL = {
"security": ("Security", """<p>This page lists what the platform does today. We don't claim certifications we don't hold.</p>
<h3>Transport</h3><ul><li>All public endpoints (site, API, MCP, apps) are served over HTTPS; certificates are issued automatically and only for known hosts.</li><li>Nodes connect to the control plane over WireGuard and expose no inbound ports.</li></ul>
<h3>Data at rest</h3><ul><li>App environment variables are encrypted at rest (Fernet) and never returned by the API.</li><li>API keys and session tokens are stored as SHA-256 hashes.</li></ul>
<h3>Isolation</h3><ul><li>Each app instance runs in its own container with CPU and memory limits.</li></ul>
<h3>Accounts</h3><ul><li>HttpOnly, SameSite session cookies; state-changing cookie requests require a CSRF header.</li><li>Single-use, expiring magic links. OAuth 2.1 with PKCE for MCP clients; tokens are scoped and revocable.</li></ul>
<h3>Infrastructure</h3><ul><li>Compute and the control plane run on Hetzner Cloud in Nuremberg, Germany. Off-site backups are stored in Helsinki, Finland.</li><li>Payments are processed by Stripe; card data never reaches our servers.</li></ul>
<h3>Reporting</h3><p>Email <a href="mailto:security@aloof.si">security@aloof.si</a>. Please give us reasonable time to fix an issue before disclosing it.</p>"""),
"privacy": ("Privacy policy", """<p><b>Draft.</b> A full policy will be published before general availability.</p><p>We collect your email, billing details you provide, and operational data needed to run and bill your apps (logs, usage). Payments are handled by Stripe. We don't sell personal data. Data is hosted in the EU. Contact: <a href="mailto:privacy@aloof.si">privacy@aloof.si</a>.</p>"""),
"terms": ("Terms of service", """<p><b>Draft.</b> Full terms will be published before general availability.</p><p>Don't use the service for illegal content, abuse, spam or cryptocurrency mining. Prepaid credit is consumed per second of usage; the free tier is hard-capped. Contact: <a href="mailto:legal@aloof.si">legal@aloof.si</a>.</p>"""),
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
