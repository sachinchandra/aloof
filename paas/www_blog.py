"""Blog posts. Every number is computed from the live price list (plans.py) or describes behaviour that exists in this codebase."""
from .plans import PLANS, PG_PLANS, RATES
P = lambda k: PLANS[k]
def _m(v): return f"${v:,.2f}"
def posts():
    s1, s1g, s2 = P("shared-1x-256mb"), P("shared-1x-1gb"), P("shared-2x-2gb")
    hr = s1g["price_usd_hr"]; mo = s1g["price_usd_month"]
    busy2 = hr * 2 * 30; busy8 = hr * 8 * 22
    cmp_rows = "".join(f"<tr><td><code>{k}</code></td><td>{_m(PLANS[k]['price_usd_month'])}</td>" + "".join(f"<td>{_m(PLANS[k]['compare_usd_month'][x]) if PLANS[k]['compare_usd_month'][x] else '—'}</td>" for x in ("fly", "render", "railway")) + "</tr>"
                       for k in ("shared-1x-256mb", "shared-1x-512mb", "shared-1x-1gb", "shared-2x-2gb", "perf-1x-2gb", "perf-2x-4gb"))
    return [
{"slug": "deploy-fastapi-postgres", "title": "Deploying FastAPI with Postgres on Aloof", "date": "2026-10-13", "tags": ["guide", "python"],
 "summary": "From an empty directory to a FastAPI service with managed Postgres, migrations before traffic, and a background worker, using aloof launch and aloof.toml.",
 "html": f"""<p>This post walks through a production-shaped FastAPI deploy: an HTTP API, a managed Postgres database on a private network, migrations that run <em>before</em> a release takes traffic, and a worker process that shares the image. Every command here works today.</p>
<h2>1. Detect and generate</h2><p>In a directory with <code>main.py</code> and a <code>requirements.txt</code> that lists <code>fastapi</code>, run:</p>
<pre>$ aloof launch --name notes-api
detected: fastapi
wrote Dockerfile
wrote aloof.toml
wrote .dockerignore</pre>
<p>The generated Dockerfile is deliberately boring: <code>python:3.12-slim</code>, <code>pip install -r requirements.txt</code>, and <code>uvicorn main:app --host 0.0.0.0 --port $PORT</code>. Aloof injects <code>PORT</code> (8080 by default), so the same image runs locally and on the platform. If you already have a Dockerfile, <code>launch</code> leaves it alone.</p>
<h2>2. Add a database</h2><pre>$ aloof pg create notes-db --plan pg-dev
$ aloof pg attach notes-db notes-api      # injects DATABASE_URL, joins the private network</pre>
<p><code>pg-dev</code> is {_m(PG_PLANS['pg-dev']['price_usd_month'])}/month. The database never gets a public port: the app reaches it over a private Docker network, and <code>DATABASE_URL</code> is stored encrypted with the app's other secrets. Daily logical backups are kept on the host, and encrypted copies go off-site (see <a href="/blog/postgres-offsite-backups">the backups post</a>).</p>
<h2>3. Migrations before traffic</h2><p>Add a release command and a worker to <code>aloof.toml</code>:</p>
<pre>[processes]
  web = "uvicorn main:app --host 0.0.0.0 --port 8080"
  worker = "python -m app.worker"

[deploy]
  release_command = "alembic upgrade head"

[[http_service.checks]]
  path = "/healthz"
  interval = "10s"</pre>
<p>On every deploy the build finishes first. Then <code>alembic upgrade head</code> runs once in a one-off machine with the same image, env and private network. Only if it exits 0 do new web instances start, and they must pass <code>/healthz</code> before the old ones stop. A failing migration leaves the previous release serving.</p>
<h2>4. Deploy</h2><pre>$ aloof deploy
uploading build context: 23 files, 0.1 MB (.dockerignore respected)
  release_command: alembic upgrade head
  [release] INFO  [alembic.runtime.migration] Running upgrade -> 3f2a, notes
  deploy complete (v1 live)
  process group worker: 1 x `python -m app.worker`
live: https://notes-api.aloof.si/</pre>
<p>The worker runs as <code>notes-api-worker</code>. It isn't routed and doesn't sleep; scale it independently with <code>aloof scale count worker=2</code>.</p>
<h2>What it costs</h2><p>A <code>shared-1x-256mb</code> web instance is {_m(s1['price_usd_month'])}/month always-on. Add <code>pg-dev</code> and a 256 MB worker and the stack is {_m(s1['price_usd_month']*2 + PG_PLANS['pg-dev']['price_usd_month'])}/month before usage like egress ({_m(RATES['egress_usd_gb'])}/GB). With <code>auto_stop_machines = true</code> the web tier only bills while it's awake.</p>"""},
{"slug": "deploy-nextjs-and-rails", "title": "Next.js and Rails on Aloof: two stacks, one workflow", "date": "2026-10-13", "tags": ["guide", "javascript", "ruby"],
 "summary": "What aloof launch generates for a Next.js app and a Rails app, where the two differ (build output, assets, migrations), and the settings worth changing.",
 "html": """<p>Next.js and Rails sit at opposite ends of the framework spectrum, but on Aloof the loop is the same: <code>aloof launch</code>, review two files, <code>aloof deploy</code>. This post shows what's generated and why.</p>
<h2>Next.js</h2><p><code>launch</code> recognises Next.js from <code>next</code> in <code>package.json</code>, picks your package manager from the lockfile (npm, pnpm, yarn or bun), and generates:</p>
<pre>FROM node:20-slim
WORKDIR /app
ENV NODE_ENV=production PORT=8080 HOSTNAME=0.0.0.0
COPY . .
RUN NODE_ENV=development npm ci || npm install
RUN npm run build
EXPOSE 8080
CMD ["npm","run","start"]</pre>
<p>Two details matter. Dev dependencies are installed so <code>next build</code> works, and <code>HOSTNAME=0.0.0.0</code> makes <code>next start</code> listen on all interfaces rather than localhost. For smaller images, switch to Next's <code>output: "standalone"</code> and a two-stage Dockerfile; Aloof builds whatever Dockerfile you commit.</p>
<p>If your Next.js app talks to a backend on Aloof, call it over the private network (<code>http://api.internal:8080</code>) from server components and route handlers. That traffic never leaves the host. See <a href="/blog/private-networking-internal">private networking</a>.</p>
<h2>Rails</h2><p>A <code>Gemfile</code> containing <code>rails</code> produces a <code>ruby:3.3-slim</code> image with build tools and <code>libpq</code>, <code>bundle install</code> without development and test groups, and a best-effort <code>assets:precompile</code>. It also sets a release command:</p>
<pre>[deploy]
  release_command = "bin/rails db:migrate"</pre>
<p>Set your secrets once, staged so you deploy only once:</p>
<pre>aloof secrets set RAILS_MASTER_KEY=$(cat config/master.key) --stage
aloof pg create shop-db &amp;&amp; aloof pg attach shop-db shop
aloof secrets deploy</pre>
<p>Background jobs go in <code>[processes]</code> (for example <code>worker = "bundle exec sidekiq"</code> or <code>"bin/jobs"</code> for Solid Queue), and recurring tasks in <code>[[cron]]</code>.</p>
<h2>Same workflow, same guarantees</h2><ul><li>Builds run remotely; <code>.dockerignore</code> is respected when the directory is uploaded.</li><li>The release command runs before new instances start; a failure keeps the old release live.</li><li>Every deploy is a numbered release you can roll back to without rebuilding.</li></ul>
<p>Full walkthroughs: <a href="/guides/nextjs">Next.js guide</a>, <a href="/guides/rails">Rails guide</a>.</p>"""},
{"slug": "scale-to-zero-economics", "title": "The economics of scale-to-zero", "date": "2026-10-13", "tags": ["pricing"],
 "summary": "When sleeping instances save real money, when they don't, and how to reason about cold starts, with the arithmetic shown.",
 "html": f"""<p>Per-second billing only matters if your app is sometimes idle. This post works through the numbers using Aloof's current price list.</p>
<h2>The baseline</h2><p>A <code>shared-1x-1gb</code> instance costs <b>${hr:.4f}/hour</b>, which is {_m(mo)} for a 30-day month always-on. With scale-to-zero, an app that's busy two hours a day costs about <b>{_m(busy2)}</b>/month. One that serves office hours (8 h × 22 working days) costs about <b>{_m(busy8)}</b>.</p>
<div class=scroll><table class=tbl><tr><th>Usage pattern</th><th>Billed hours / month</th><th>Cost on shared-1x-1gb</th></tr>
<tr><td>Always on</td><td>720</td><td>{_m(mo)}</td></tr><tr><td>Office hours (8 h × 22 d)</td><td>176</td><td>{_m(busy8)}</td></tr>
<tr><td>2 h/day</td><td>60</td><td>{_m(busy2)}</td></tr><tr><td>Webhook receiver, ~10 min/day awake</td><td>5</td><td>{_m(hr*5)}</td></tr></table></div>
<p>"Awake" includes the idle window before sleep: an app sleeps after <code>idle_seconds</code> with no in-flight requests (60 s by default, and at most 5 minutes on the free tier). A request every 50 seconds keeps it awake permanently, so look at your traffic gaps rather than your request count.</p>
<h2>What a cold start costs you</h2><p>When a sleeping app gets a request, the edge holds it, starts an instance and forwards the request once the instance passes its readiness check. Latency for that first request is container start plus your app's boot time. That ranges from well under a second for a Go binary to many seconds for a large Rails or JVM app. Measure your own: <code>curl -w '%{{time_total}}'</code> after the app has slept.</p>
<h2>When not to scale to zero</h2><ul><li><b>Latency-sensitive APIs</b>: set <code>min_machines_running = 1</code> and let autoscaling handle peaks.</li><li><b>Workers and consumers</b>: process groups like <code>worker</code> never sleep, by design. A queue consumer can't be woken by HTTP.</li><li><b>Apps with volumes and long boots</b>: the single-writer volume rule means one instance at a time, so every wake is a full boot.</li></ul>
<h2>Free tier</h2><p>The free tier always sleeps after idle and is hard-capped at $1 of usage per month. When the cap is reached, apps pause with HTTP 402 rather than bill you. That cap is the reason it can be free.</p>"""},
{"slug": "mcp-let-your-agent-deploy", "title": "MCP: let your AI agent deploy (safely)", "date": "2026-10-13", "tags": ["ai", "mcp"],
 "summary": "How Aloof's remote MCP server works, what an agent can and can't do, and how to keep a human in the loop for destructive actions.",
 "html": """<p>Aloof ships a Model Context Protocol server at <code>https://mcp.aloof.si/mcp</code>. Any MCP client that supports remote servers with OAuth (Claude, ChatGPT connectors, Cursor and others) can connect. You can also run it locally over stdio.</p>
<h2>Authentication</h2><p>The remote server implements OAuth 2.1 with PKCE and dynamic client registration. The client discovers the authorization server, you log in to Aloof in the browser, and the client receives a scoped token you can revoke from the dashboard. Headless setups can send an API key as a Bearer token instead.</p>
<h2>What the agent can do</h2><p>34 tools cover the platform: <code>deploy_app</code>, <code>deploy_with_config</code> (an aloof.toml as JSON), <code>get_status</code>, <code>app_status</code>, <code>get_logs</code>, <code>scale_app</code>, <code>set_env</code>, <code>set_autoscale</code>, domains, volumes, managed Postgres, releases and rollback, snapshots and off-site restores, private registry and git credentials, deploy tokens and audited exec.</p>
<pre><span class=muted># you:</span> "Deploy github.com/acme/api on Aloof with 512 MB, scale to zero, and tell me the URL."
<span class=muted># the agent:</span> list_plans → deploy_app(name="api", git_url=..., plan="shared-1x-512mb")
           → set_autoscale(min=0) → app_status → https://api.aloof.si/</pre>
<h2>Keeping it safe</h2><ul><li><b>Least privilege</b>: give CI and agents a deploy token scoped to one app (<code>aloof tokens create -a api --expiry 30d</code>). It can't touch billing, auth or other apps.</li><li><b>Everything is audited</b>: exec commands, console input, restores, token creation and secret changes are written to the audit log with the token or key that did them.</li><li><b>Destructive tools say so</b>: restore and destroy tool descriptions tell the model to confirm with the user first. Keep your client's "approve tool calls" setting on for these.</li><li><b>Secrets stay secret</b>: env values are write-only through the API. An agent can set a secret but never read it back.</li></ul>
<h2>For agents that prefer REST</h2><p><a href="/llms.txt">/llms.txt</a> is a compact, accurate description of the API, and the OpenAPI spec is at <a href="/api/docs">/api/docs</a>. Agents with shell access can also just use the single-file CLI.</p>"""},
{"slug": "private-networking-internal", "title": "Private networking with .internal", "date": "2026-10-13", "tags": ["networking"],
 "summary": "Every Aloof account gets a private network where apps reach each other as <app>.internal. How it works, how it's isolated, and its current limits.",
 "html": """<p>Most backends are several processes: a web frontend, an API, a worker and a database. They should talk privately, without a public hop and without hard-coded IPs. On Aloof every app, worker and managed database of an account joins that account's private network, and each is reachable by name:</p>
<pre>http://api.internal:8080           # another app
http://api-worker.internal:9090    # a process group's metrics port
postgres://...@pg-mydb:5432/...    # injected as DATABASE_URL</pre>
<h2>How it works</h2><p>Each account has its own bridge network on the host. When an instance starts, it's attached with two DNS aliases, <code>&lt;app&gt;</code> and <code>&lt;app&gt;.internal</code>, resolved by the network's embedded DNS. All instances of an app share the alias, so the name keeps working across redeploys and scaling. One-off machines (release commands and cron jobs) join the same network, so a migration can reach your database and a cron job can call your API.</p>
<h2>Isolation</h2><p>Networks are per account, so another account's containers have no route to yours and can't resolve your names. Our integration test checks exactly this on a real Docker host: an app in account A fetches from another app in A by <code>.internal</code> name, and the same request from account B fails. Public traffic only reaches an app through the edge proxy on its HTTP port. Other ports you open are private.</p>
<h2>Reaching private ports from your laptop</h2><pre>$ aloof proxy 5432 -a reports-db
proxying 127.0.0.1:5432 -> reports-db:5432 (Ctrl-C to stop)</pre>
<p><code>aloof proxy</code> tunnels TCP over an authenticated WebSocket to the instance's private address. Use it for psql, admin UIs and debugging. Sessions are audited.</p>
<h2>Current limits</h2><ul><li><code>.internal</code> spans one host today. Apps placed on different remote nodes don't share a network yet. A WireGuard mesh between nodes is the next step (the control plane already reaches nodes over WireGuard).</li><li>On remote nodes, <code>aloof proxy</code> reaches only the app's HTTP port.</li><li>There's no cross-account peering.</li></ul>"""},
{"slug": "zero-downtime-deploys-rollbacks", "title": "Zero-downtime deploys and instant rollbacks", "date": "2026-10-13", "tags": ["deploys"],
 "summary": "The exact sequence Aloof follows on every deploy, what happens when something fails at each step, and how rollback avoids a rebuild.",
 "html": """<p>A deploy should either fully succeed or change nothing. Here's the sequence Aloof runs on every release, and the failure behaviour at each step.</p>
<h2>The sequence</h2><ol><li><b>Release record</b>: a new numbered release is created with the full config (image, env, plan, scale, health).</li><li><b>Build</b>: pull the image with your registry credentials, or fetch git or the uploaded context and build it. On failure the release is marked failed and nothing running is touched.</li><li><b>Release command</b>: if configured, it runs once in a one-off machine. A non-zero exit fails the release, and old instances keep serving.</li><li><b>Start new instances</b>: each must pass readiness (your HTTP health path, or any non-5xx response) before it's marked running. Only running instances receive traffic from the edge.</li><li><b>Cut over</b>: the old instances stop only after all new ones are ready. If any new instance fails readiness, the new ones are removed and the previous release keeps serving, with no downtime.</li></ol>
<p>One deliberate exception: apps with a volume are single-writer. Two instances must never mount the same data, so the old instance stops before the new one starts. Expect a brief gap for those apps.</p>
<h2>Rollback without a rebuild</h2><pre>$ aloof releases api
v7    active      2026-10-13 14:02  deploy        paas/api:r1791...
v6    superseded  2026-10-13 11:40  deploy        paas/api:r1791...
$ aloof rollback api 6</pre>
<p>Each release stores its image reference and config. Rolling back redeploys that image with that release's env, plan, port, command, scale and health settings through the same readiness-gated sequence, so a rollback is also zero-downtime. Volumes, database attachments and domains are left as they are. The last 20 release images are kept.</p>
<h2>What "zero-downtime" doesn't cover</h2><ul><li>Database migrations that break the old release: use expand/contract migrations so old and new code both work during the cutover.</li><li>Long-lived connections (WebSockets) on stopped instances are closed and must reconnect.</li><li>Canary and blue-green strategies are accepted in aloof.toml but currently run as rolling deploys.</li></ul>"""},
{"slug": "postgres-offsite-backups", "title": "Managed Postgres backups, off-site and encrypted", "date": "2026-10-13", "tags": ["postgres", "backups"],
 "summary": "How Aloof backs up managed Postgres and volumes: local dumps, client-side AES-256-GCM, a separate location, GFS retention and a weekly test restore.",
 "html": """<p>A backup on the same disk as the data protects you from mistakes, not from losing the disk. Aloof keeps both kinds of backup.</p>
<h2>Layer 1: local, fast to restore</h2><p>Each managed database gets a daily <code>pg_dump</code> in custom format (7 kept), and you can take one any time with <code>aloof pg backup</code>. Volumes get on-demand snapshots. These restore in seconds because they're on the same host.</p>
<h2>Layer 2: off-site, encrypted before upload</h2><p>Once a day, fresh dumps of every database, every volume snapshot, and the control plane's own database go to S3-compatible object storage. The storage is Hetzner Object Storage in <b>Helsinki</b>, a different location from our compute in Nuremberg.</p><ul>
<li><b>Client-side encryption</b>: data is encrypted with AES-256-GCM in 1 MB chunks on our server before upload. Each chunk is authenticated, and the final chunk is marked, so truncation or tampering is detected on restore. The object store only ever sees ciphertext.</li>
<li><b>Platform secrets</b> (encryption keys, tokens) get a second layer: a separate passphrase-derived key (scrypt), so the backup key alone can't open them.</li>
<li><b>Retention</b>: 7 daily, 4 weekly and 3 monthly backups (grandfather-father-son), plus a bucket lifecycle rule as a backstop.</li>
<li><b>Integrity</b>: SHA-256 is checked on download before anything is restored.</li></ul>
<h2>Backups you've never restored aren't backups</h2><p>A weekly job downloads the latest copies, decrypts them, and restores the control-plane database into a scratch Postgres. It then checks the table count, and checks that volume archives and the secrets bundle open. Results are recorded per backup. The admin dashboard raises an alert if the newest off-site backup is more than 26 hours old or a verification fails.</p>
<h2>Restoring</h2><pre>$ aloof pg offsite mydb
$ aloof pg offsite-restore mydb pg_20261013082102_065225</pre>
<p>The same works for volumes (<code>aloof snapshots offsite-restore</code>), through the API and MCP, and from the dashboard.</p>
<h2>Limits</h2><p>These are logical backups. There's no point-in-time recovery or streaming replica yet, so the recovery point is your newest dump. Take an on-demand backup before risky migrations.</p>"""},
{"slug": "aloof-vs-fly-render-railway", "title": "Aloof vs Fly.io, Render and Railway: an honest comparison", "date": "2026-10-13", "tags": ["comparison"],
 "summary": "Where Aloof is cheaper or simpler, and where Fly.io, Render or Railway are clearly the better choice today.",
 "html": f"""<p>We built Aloof to be a cheaper, simpler home for backends. "Cheaper" is easy to claim, so here are the numbers, and also the places where the alternatives are better.</p>
<h2>Price</h2><p>Always-on monthly prices for comparable instances. Competitor figures are their public list prices as we recorded them; check their sites for current numbers. A dash means no direct equivalent.</p>
<div class=scroll><table class=tbl><tr><th>Aloof plan</th><th>Aloof</th><th>Fly.io</th><th>Render</th><th>Railway*</th></tr>{cmp_rows}</table></div>
<p class=muted>*Railway bills by resource usage; we show the cost of the equivalent resources running 24/7.</p>
<h2>Where Aloof is a good fit</h2><ul><li>Backends, APIs, workers and side projects whose users are in or near Europe.</li><li>Fly-style workflows (<code>aloof.toml</code>, <code>launch</code>, <code>secrets</code>, process groups, release commands, previews) at a lower price.</li><li>AI-agent-driven operations through a first-party MCP server.</li><li>Teams that want encrypted off-site backups and an audit log without assembling them.</li></ul>
<h2>Where the others are better today</h2><ul><li><b>Global footprint</b>: Fly.io runs in dozens of regions with anycast. Aloof runs in one region (Nuremberg, Germany). Users in Asia or the Americas will see higher latency.</li><li><b>Compliance</b>: Fly.io, Render and Railway publish SOC 2 reports. Aloof holds no certifications.</li><li><b>Teams</b>: all three have organisations with roles. Aloof has single-user accounts plus scoped deploy tokens.</li><li><b>Managed data</b>: Postgres HA and point-in-time recovery, managed Redis/Key-Value, and private services across regions are more mature elsewhere.</li><li><b>Track record</b>: Aloof is new. The others have years of production history.</li><li><b>Raw TCP/UDP</b>: Fly.io exposes arbitrary public ports. Aloof serves HTTP/HTTPS only.</li></ul>
<h2>The honest summary</h2><p>If your users are in Europe and you want Fly-like ergonomics with per-second billing at or below list prices, Aloof is worth a try; the free tier is hard-capped at $1, so it costs nothing to test. If you need multiple regions, compliance reports or org-level RBAC today, use one of the others. Where we run today is on the <a href="/regions">regions page</a>.</p>"""},
]
