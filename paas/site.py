"""Public site: landing, pricing, docs, login. Server-rendered, no JS framework."""
import html
from .plans import PLANS, GST_RATE, TIERS, RATES, USD_INR
CSS = """*{box-sizing:border-box}body{margin:0;font-family:Inter,system-ui,-apple-system,sans-serif;color:#1d1d1f;background:#fbfbfa;line-height:1.55}
a{color:#3b5bdb;text-decoration:none}a:hover{text-decoration:underline}
nav{display:flex;align-items:center;gap:22px;padding:16px 32px;border-bottom:1px solid #eee;background:#fff;position:sticky;top:0}
nav .logo{font-weight:800;font-size:20px;color:#111;letter-spacing:-.5px}nav .sp{flex:1}
.btn{display:inline-block;background:#111;color:#fff;padding:10px 18px;border-radius:8px;font-weight:600}.btn:hover{text-decoration:none;background:#333}
.btn.alt{background:#fff;color:#111;border:1px solid #ccc}
.wrap{max-width:1080px;margin:0 auto;padding:0 24px}
.hero{padding:80px 0 50px}.hero h1{font-size:52px;line-height:1.08;letter-spacing:-1.5px;margin:0 0 18px}.hero p{font-size:20px;color:#555;max-width:720px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:18px;margin:30px 0}
.card{background:#fff;border:1px solid #eaeaea;border-radius:12px;padding:20px}.card h3{margin:0 0 6px}
pre{background:#111;color:#e6e6e6;padding:16px;border-radius:10px;overflow:auto;font-size:13.5px}code{font-family:ui-monospace,Menlo,monospace;font-size:.92em}
p code,li code,td code{background:#f0f0ef;padding:1px 5px;border-radius:4px}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:12px;overflow:hidden}th,td{padding:10px 12px;border-bottom:1px solid #eee;text-align:left;font-size:14.5px}
th{background:#f5f5f4}.muted{color:#777}.pill{display:inline-block;background:#eef2ff;color:#3b5bdb;border-radius:20px;padding:2px 10px;font-size:12px;font-weight:600}
footer{padding:40px 0;color:#888;font-size:14px;border-top:1px solid #eee;margin-top:60px}
.docs{display:grid;grid-template-columns:210px 1fr;gap:40px;padding:30px 0}.docs aside a{display:block;padding:4px 0;color:#444}.docs aside a.on{font-weight:700;color:#111}
.docs h1{margin-top:0}@media(max-width:720px){.docs{grid-template-columns:1fr}.hero h1{font-size:36px}}
input{padding:10px;border:1px solid #ccc;border-radius:8px;width:100%;margin:4px 0 12px;font-size:15px}"""
def _gb(mb): g = mb / 1024; return f"{g:g}" if g < 1 or g == int(g) else str(round(g))
def page(title, body, brand="Aloof"):
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><meta name=description content="{brand}: backend and GPU hosting in India, billed in INR. Deploy Docker images or git repos; scale to zero; MCP for AI assistants.">
<style>{CSS}</style></head><body><nav><a class=logo href="/">{brand}</a><a href="/pricing">Pricing</a><a href="/docs">Docs</a><a href="/docs/mcp">MCP</a>
<span class=sp></span><a href="/login">Log in</a><a class=btn href="/login#register">Start free</a></nav>{body}
<footer><div class=wrap>{brand} · Made in India · <a href="/llms.txt">llms.txt</a> · <a href="/.well-known/mcp.json">MCP manifest</a> · <a href="/api/docs">API reference</a></div></footer></body></html>"""
def _usd(inr): return f"${inr / USD_INR:,.2f}"
def _cmp(v): return f"${v:g}" if v else "<span class=muted>—</span>"
def _plan_rows(kind=None, compare=True):
    rows = []
    for k, p in PLANS.items():
        if p["legacy"] or (kind and (p["cpu_class"] if p["kind"] == "cpu" else "gpu") != kind): continue
        if p["kind"] == "cpu":
            c = p["compare_usd_month"]; mo = p["price_inr_month"]
            cmpc = f"<td>{_cmp(c['fly'])}</td><td>{_cmp(c['render'])}</td><td>{_cmp(c['railway'])}</td>" if compare else ""
            rows.append(f"<tr><td><code>{k}</code></td><td>{p['vcpu']:g} {p['cpu_class']}</td><td>{_gb(p['memory_mb'])} GB</td>"
                        f"<td><b>₹{mo:,}</b>/mo <span class=muted>({_usd(mo)})</span></td><td>₹{p['price_inr_hr']:.3f}/hr</td>{cmpc}</tr>")
        else:
            f = (p.get("compare_usd_hr") or {}).get("fly_last_list")
            cmpc = f"<td>{_cmp(f)}{' <span class=muted>' + p['note'] + '</span>' if p.get('note') else ''}</td>" if compare else ""
            rows.append(f"<tr><td><code>{k}</code></td><td>{p['gpu']}</td><td>{p['vcpu']} vCPU · {_gb(p['memory_mb'])} GB</td>"
                        f"<td><b>₹{p['price_inr_hr']:g}</b>/hr <span class=muted>({_usd(p['price_inr_hr'])})</span></td><td>₹{p['price_inr_hr']/3600:.4f}/s</td>{cmpc}</tr>")
    return "".join(rows)
CPU_HEAD = "<tr><th>Plan</th><th>CPU</th><th>RAM</th><th>Always-on (30 days)</th><th>Per hour</th><th>Fly.io</th><th>Render</th><th>Railway*</th></tr>"
GPU_HEAD = "<tr><th>Plan</th><th>GPU</th><th>Host</th><th>Per hour</th><th>Per second</th><th>Fly.io (last list price)</th></tr>"
def landing(brand, base, api, mcp):
    return page(f"{brand}: backend & GPU hosting in India", f"""<div class=wrap><section class=hero><span class=pill>Mumbai · Delhi · Chennai · Bengaluru</span>
<h1>Ship backends and GPU apps<br>to India, in one command.</h1>
<p>{brand} runs your Docker image or git repo on low-cost Indian CPU and GPU capacity. You get HTTPS at <code>&lt;app&gt;.{base}</code>, autoscaling down to zero, volumes, and INR billing with GST invoices. AI assistants can deploy for you over MCP.</p>
<p><a class=btn href="/login#register">Deploy your first app</a> &nbsp; <a class="btn alt" href="/docs">Read the docs</a></p></section>
<pre>$ aloof deploy my-api --git https://github.com/me/api.git
  building… running
live: https://my-api.{base}/

$ aloof deploy llm --image vllm/vllm-openai --plan gpu-l4 --port 8000
$ aloof autoscale llm --min 0 --max 4 --idle 300     # pay only while serving</pre>
<div class=grid>
<div class=card><h3>CPU from ₹179/mo</h3>Shared and dedicated (performance) vCPUs on Indian clouds, priced at or below Fly.io and Render in INR, and billed per second.</div>
<div class=card><h3>GPUs by the second</h3>L4 ₹72/hr, L40S ₹149/hr, A100 ₹279/hr, H100 ₹369/hr on dedicated nodes. Scale to zero between requests.</div>
<div class=card><h3>Scale to zero</h3>Idle apps cost nothing. The first request wakes them in about a second (CPU) and you're billed per second.</div>
<div class=card><h3>Built for AI agents</h3>An MCP server with OAuth lets Claude, ChatGPT and Cursor deploy and manage apps for your users. <a href="/docs/mcp">Connect →</a></div>
<div class=card><h3>India-first billing</h3>Prepaid INR wallet via UPI, cards and netbanking (Razorpay), with GST tax invoices and your GSTIN.</div>
<div class=card><h3>Data stays in India</h3>Workloads run in Indian regions only, which suits DPDP-conscious products.</div></div>
<h2>Pricing</h2><table>{CPU_HEAD.replace("<th>Fly.io</th><th>Render</th><th>Railway*</th>","")}{_plan_rows("shared", False)}</table>
<p class=muted>Free tier with ₹100/month of usage. Prices exclude 18% GST, billed per second. <a href="/pricing">Performance CPUs, GPUs, volumes and tiers →</a></p></div>""", brand)
def pricing(brand):
    T = TIERS
    def tier(k, name, blurb):
        t = T[k]; fee = "₹0" if not t["fee_inr"] else f"₹{t['fee_inr']:,}"
        return (f"<div class=card><h3>{name}</h3><div style='font-size:28px;font-weight:700'>{fee}<span class=muted style='font-size:14px'>/month"
                f"{'' if not t['fee_inr'] else ' (' + _usd(t['fee_inr']) + ')'}</span></div><p>{blurb}</p><ul>"
                f"<li>₹{t['included_usage_inr']:,} of usage included</li><li>{t['egress_gb']} GB egress included</li>"
                f"<li>{'Unlimited seats' if not t['seats'] else '1 seat'} · {'unlimited apps' if not t['max_apps'] else str(t['max_apps']) + ' apps'}</li></ul>"
                f"<p class=muted>{t['compare']}</p></div>")
    R = RATES
    return page(f"Pricing · {brand}", f"""<div class=wrap><section class=hero><h1>Pay per second, in rupees</h1>
<p>Same model as Fly.io, Render and Railway: a workspace plan, then per-second compute, volumes per GB-month, and egress per GB.
Prices are in INR excluding {int(GST_RATE*100)}% GST; USD at ₹{USD_INR:g}/$ is shown for comparison.</p></section>
<div class=grid>{tier("free", "Free", "Prototypes and side projects. Up to 3 apps on shared-1x-256mb/512mb, 1 instance each, asleep after 5 min idle. Hard-capped at the included ₹100: apps pause instead of billing you, and wake next month or on upgrade.")}
{tier("hobby", "Hobby", "Solo developers running always-on apps.")}
{tier("pro", "Pro", "Teams shipping to production. Billed per workspace.")}
<div class=card><h3>Enterprise</h3><div style='font-size:28px;font-weight:700'>Custom</div><p>Committed GPU capacity, dedicated nodes, SLAs, invoicing.</p><p><a href="mailto:sales@aloof.si">Contact us</a></p></div></div>
<h2>Shared CPU</h2><p class=muted>Burstable vCPUs with a guaranteed 1/8-core baseline. Good for APIs, bots and workers.</p><table>{CPU_HEAD}{_plan_rows("shared")}</table>
<h2>Performance CPU</h2><p class=muted>Dedicated vCPUs.</p><table>{CPU_HEAD}{_plan_rows("performance")}</table>
<p class=muted>*Railway bills actual use at $20/vCPU and $10/GB per month; the figure shown assumes full utilisation of the same size. Competitor prices are list prices in USD, read 12 Oct 2026: fly.io/docs/about/pricing (iad), render.com/pricing, railway.com/pricing.</p>
<h2>GPUs</h2><p class=muted>Dedicated GPU node per app, billed per second while running. With <code>min 0</code> autoscaling you pay only while serving. Render and Railway don't offer GPUs, and Fly.io retired its GPUs on 31 Jul 2026.</p><table>{GPU_HEAD}{_plan_rows("gpu")}</table>
<h2>Storage &amp; network</h2><table><tr><th>Resource</th><th>{brand}</th><th>Fly.io</th><th>Render</th><th>Railway</th></tr>
<tr><td>Volumes (provisioned GB-month)</td><td><b>₹{R['volume_inr_gb_month']:g}</b> <span class=muted>({_usd(R['volume_inr_gb_month'])})</span></td><td>$0.15</td><td>$0.25</td><td>$0.15</td></tr>
<tr><td>Egress per GB beyond allowance</td><td><b>₹{R['egress_inr_gb']:g}</b> <span class=muted>({_usd(R['egress_inr_gb'])})</span></td><td>$0.12 (India) · $0.02 (US/EU)</td><td>$0.15</td><td>$0.05</td></tr>
<tr><td>Extra RAM (GB-month)</td><td>₹{R['extra_ram_inr_gb_month']:,} <span class=muted>({_usd(R['extra_ram_inr_gb_month'])})</span></td><td>$6.00</td><td>—</td><td>$10</td></tr>
<tr><td>Free tier limits</td><td colspan=4>3 apps · shared-1x-256mb / 512mb · 1 instance · sleeps after 5 min idle · 1 GB volumes · hard cap at ₹100 usage per month (never charged)</td></tr>
<tr><td>Inbound data, custom domains, TLS</td><td>Free</td><td>Free (10 certs)</td><td>2–25 domains</td><td>2–20 domains</td></tr></table>
<div class=grid><div class=card><h3>Top-ups</h3>₹100 to ₹5,00,000 via UPI, cards and netbanking (Razorpay). A GST invoice is issued automatically.</div>
<div class=card><h3>Included usage</h3>Like Railway, each plan's fee comes back as usage credit, and you pay extra only beyond it.</div>
<div class=card><h3>No idle charges</h3>Stopped and scaled-to-zero apps cost nothing; volumes are still billed.</div></div></div>""", brand)
DOCS = ["quickstart", "cli", "deploy", "gpu", "volumes", "autoscaling", "domains", "billing", "mcp", "api", "nodes"]
TITLES = {"quickstart": "Quickstart", "cli": "CLI", "deploy": "Deploying apps", "gpu": "GPU workloads", "volumes": "Volumes", "autoscaling": "Autoscaling & scale to zero",
          "domains": "Custom domains", "billing": "Billing & GST", "mcp": "MCP for AI assistants", "api": "REST API", "nodes": "Bring your own nodes"}
def docs(pg, brand, base, api, mcp):
    if pg not in DOCS: return None
    c = {
"quickstart": f"""<h1>Quickstart</h1><ol><li><a href="/login#register">Create an account</a>. You get ₹500 credit.</li>
<li>Install the CLI: <code>curl -fsSL {api}/cli/aloof -o /usr/local/bin/aloof && chmod +x /usr/local/bin/aloof</code></li>
<li>Log in: <code>aloof signup you@company.in</code>, or paste the API key from the dashboard into <code>~/.aloof.json</code>.</li>
<li>Deploy: <pre>aloof deploy hello --image nginx:alpine --port 80
live: https://hello.{base}/</pre></li></ol>
<p>Your app must listen on <code>$PORT</code> (default 8080) over HTTP. Configure it with env vars, and keep persistent data on a <a href="/docs/volumes">volume</a>.</p>""",
"cli": """<h1>CLI</h1><pre>aloof plans | apps | usage
aloof deploy &lt;app&gt; --image IMG | --git URL [--ref main] [--plan shared-1x-1gb] [--port 8080] [-e K=V] [-n 2]
aloof status|logs|destroy &lt;app&gt;
aloof scale &lt;app&gt; 3
aloof env &lt;app&gt; K=V K2=          # K2= removes; values are encrypted at rest
aloof volumes create data --size 5 ; aloof volumes attach &lt;app&gt; data --path /data
aloof autoscale &lt;app&gt; --min 0 --max 4 --concurrency 10 [--cpu 70] --idle 300
aloof health &lt;app&gt; --path /healthz --failures 3
aloof domains &lt;app&gt; add api.example.in | verify &lt;domain&gt; | list | remove &lt;domain&gt;</pre>
<p>Config is read from <code>~/.aloof.json</code> (<code>{"api": "...", "key": "pk_..."}</code>) or the <code>ALOOF_API_URL</code> / <code>ALOOF_API_KEY</code> env vars.</p>""",
"deploy": f"""<h1>Deploying apps</h1><p><b>From an image</b>: any public image or private registry image (<code>--image ghcr.io/me/api:1.4</code>).</p>
<p><b>From git</b>: we build the repo's <code>Dockerfile</code> (<code>--git URL --ref main</code>, optional <code>subdir</code>) and push it to the {brand} registry, where your nodes pull it.</p>
<p>Redeploys are rolling: new instances must pass a readiness check before old ones stop. Env vars, volumes, autoscale and health settings carry over between deploys.</p>
<p>Health checks: set an HTTP path with <code>aloof health app --path /healthz</code>. After N consecutive failures the instance is replaced.</p>""",
"gpu": f"""<h1>GPU workloads</h1><p>Pick a GPU plan: <code>gpu-t4-spot</code>, <code>gpu-l4</code>, <code>gpu-l40s</code>, <code>gpu-a100</code> or <code>gpu-h100</code> (billed per second; see <a href='/pricing'>pricing</a>). Each GPU instance gets a dedicated node with the NVIDIA container runtime, and your container sees the GPU via CUDA (<code>--gpus</code>).</p>
<pre>aloof deploy llm --image vllm/vllm-openai:latest --plan gpu-l4 --port 8000 -e MODEL=Qwen/Qwen2.5-7B-Instruct
aloof autoscale llm --min 0 --max 2 --idle 600</pre>
<p>Capacity comes from E2E Networks, Jarvislabs, Ola Krutrim, AWS Mumbai spot (T4) or self-hosted nodes, tried in order of cost and availability. GPU nodes are released after 10 idle minutes. A cold start on GPU means provisioning a node, which takes a few minutes, so keep <code>min 1</code> for latency-sensitive inference.</p>""",
"volumes": """<h1>Volumes</h1><pre>aloof volumes create pgdata --size 10
aloof volumes attach db pgdata --path /var/lib/postgresql/data
aloof volumes detach db pgdata      # data is kept
aloof volumes delete pgdata         # must be detached first</pre><p>A volume lives on one node, so apps with volumes always run there. Volumes are single-writer: on redeploy the old instance stops before the new one starts.</p>""",
"autoscaling": """<h1>Autoscaling &amp; scale to zero</h1><p>Every 3s {brand} looks at in-flight requests per instance (and optionally CPU %), and scales between <code>min</code> and <code>max</code>. Scale-down waits 15s.</p>
<pre>aloof autoscale api --min 1 --max 6 --concurrency 20 --cpu 70
aloof autoscale preview --min 0 --max 2 --idle 120    # scale to zero after 2 idle minutes</pre>
<p>With <code>min 0</code>, the first request after idle is held at the edge while an instance starts (about 1s for a small CPU app).</p>""".replace("{brand}", brand),
"domains": f"""<h1>Custom domains</h1><p>Every app gets <code>https://&lt;app&gt;.{base}</code>. To add your own domain:</p>
<pre>aloof domains api add api.example.in</pre><p>Then create the two DNS records it prints:</p>
<table><tr><th>Type</th><th>Name</th><th>Value</th><th>Purpose</th></tr>
<tr><td>TXT</td><td><code>_aloof-challenge.api.example.in</code></td><td><code>aloof-verify=&lt;token&gt;</code></td><td>proves you own the domain</td></tr>
<tr><td>CNAME</td><td><code>api.example.in</code></td><td><code>&lt;app&gt;.{base}</code></td><td>routes traffic (or an A record to our edge IP)</td></tr></table>
<p>Run <code>aloof domains api verify api.example.in</code>. Once both records check out, the certificate is issued automatically on the first HTTPS request.</p>""",
"billing": """<h1>Billing &amp; GST</h1><p>Your wallet is prepaid in INR. Top up from the dashboard with UPI, cards or netbanking (Razorpay). A ₹1,000 top-up costs ₹1,180 (18% GST) and adds ₹1,000 credit. Each top-up generates a GST tax invoice: CGST+SGST if you're in our state, IGST otherwise, with your GSTIN if you add one in Billing → Profile.</p>
<p><b>What is metered:</b> compute per second while an instance runs (plan ₹/hr ÷ 3600); volumes per provisioned GB-month (₹16, pro-rated per second, attached or not);
egress per GB beyond your tier's monthly allowance (₹4/GB); and the tier fee on the 1st of each month (Hobby ₹399, Pro ₹1,699). Usage up to the tier's included amount
(Free ₹100, Hobby ₹399, Pro ₹1,699) isn't deducted, which is the same model as Railway. Upgrades are pro-rated; switch with <code>PUT /v1/billing/tier {"tier":"pro"}</code> or from the dashboard.</p>
<p>See this month's breakdown in Dashboard → Billing or at <code>GET /v1/billing</code> (<code>month</code>). When credit runs out, apps are suspended, and they resume automatically after a top-up.
<p><b>Free tier</b> is hard-capped and never draws on your wallet: 3 apps on <code>shared-1x-256mb</code>/<code>shared-1x-512mb</code>, 1 instance each, always scale-to-zero after at most 5 minutes idle, 1 GB of volumes. Once the month's usage reaches the included ₹100, apps pause (requests get HTTP 402) until the 1st of next month or until you upgrade.</p>
v1 plan ids (<code>cpu-small</code> etc.) still work and are billed at the equivalent v2 price.</p>""",
"mcp": f"""<h1>MCP for AI assistants</h1><p>{brand} exposes a Model Context Protocol server, so assistants can pick plans, deploy, read logs, scale, attach volumes and add domains for you or your users.</p>
<p><b>Remote (OAuth 2.1)</b>: add <code>{mcp}</code> as a connector in Claude, ChatGPT or Cursor. The assistant registers itself (dynamic client registration), sends you to {brand} to approve, and gets a scoped token (PKCE).</p>
<p><b>Local (stdio)</b>:</p><pre>{{"mcpServers": {{"aloof": {{"command": "python3", "args": ["mcp_server/server.py"],
  "env": {{"ALOOF_API_URL": "{api}", "ALOOF_API_KEY": "pk_..."}}}}}}}}</pre>
<p>Tools: list_plans, signup, deploy_app, get_status, list_apps, get_logs, scale_app, set_env, destroy_app, get_usage, create_volume, list_volumes, attach_volume, detach_volume, set_autoscale, add_domain.</p>
<p>Discovery: <a href="/llms.txt">/llms.txt</a>, <a href="/.well-known/mcp.json">/.well-known/mcp.json</a>.</p>""",
"api": f"""<h1>REST API</h1><p>Base URL <code>{api}</code>. Authenticate with <code>Authorization: Bearer pk_...</code>. The full interactive reference is at <a href="/api/docs">/api/docs</a>.</p>
<pre>curl -X POST {api}/v1/apps -H "Authorization: Bearer $ALOOF_API_KEY" -H "Content-Type: application/json" \\
  -d '{{"name":"api","source":{{"image":"ghcr.io/me/api:1"}},"plan":"shared-1x-1gb","port":8080,"env":{{"DATABASE_URL":"..."}}}}'</pre>""",
"nodes": f"""<h1>Bring your own nodes</h1><p>An admin can add any Ubuntu 22.04/24.04 server to a pool, for example Yotta Shakti bare metal or your own GPU box. The node gets a single-use join token, connects to the control plane over WireGuard (outbound UDP only, so no inbound ports are needed), and runs the {brand} agent.</p>
<pre>curl -X POST {api}/v1/admin/nodes -H "Authorization: Bearer $ADMIN" -d '{{"pool":"self-hosted","plan":"gpu-h100","gpu":true}}'
# run the returned one-liner on the server:
curl -fsSL '{api}/v1/nodes/bootstrap.sh?node=n-…&token=jt_…' | sudo bash</pre>
<p>The bootstrap installs Docker, the NVIDIA container toolkit (on GPU nodes) and WireGuard, registers the node and starts <code>aloof-agent</code> under systemd.</p>"""}[pg]
    nav = "".join(f'<a href="/docs/{d}" class="{"on" if d == pg else ""}">{TITLES[d]}</a>' for d in DOCS)
    return page(f"{TITLES[pg]} · {brand} docs", f"<div class='wrap docs'><aside>{nav}</aside><main>{c}</main></div>", brand)
def login(brand):
    return page(f"Log in · {brand}", """<div class=wrap style="max-width:420px;padding:50px 24px"><div class=card>
<h2 id=title>Log in</h2><div id=msg class=muted></div>
<label>Email<input id=email type=email autocomplete=email></label>
<label id=pwl>Password<input id=pw type=password autocomplete=current-password></label>
<p><button class=btn onclick="go('login')" id=lb>Log in</button> <button class="btn alt" onclick="go('register')" id=rb>Create account</button></p>
<p class=muted>or <a href="#" onclick="magic();return false">email me a sign-in link</a></p></div></div>
<script>
if(location.hash=='#register'){title.textContent='Create your account';pw.autocomplete='new-password'}
async function go(kind){const r=await fetch('/v1/auth/'+kind,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:email.value,password:pw.value})});
 const j=await r.json(); if(!r.ok){msg.style.color='#c00';msg.textContent=j.detail||'error';return}
 if(j.api_key) sessionStorage.newKey=j.api_key; location='/dashboard'}
async function magic(){const r=await fetch('/v1/auth/magic',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:email.value})});
 const j=await r.json(); msg.style.color=r.ok?'#080':'#c00'; msg.textContent=j.detail}
</script>""", brand)
