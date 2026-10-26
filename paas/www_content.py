"""Guides, changelog, example architectures, regions, about, security. Plain facts only: nothing here claims a customer, metric or certification."""
GUIDES = {
 "fastapi": ("FastAPI", "python", "requirements.txt with fastapi", 'uvicorn main:app --host 0.0.0.0 --port $PORT', "alembic upgrade head",
   "Expose <code>/healthz</code> returning 200 and set it as the health check path. Use <code>DATABASE_URL</code> from <code>aloof pg attach</code> with SQLAlchemy or asyncpg."),
 "django": ("Django", "python", "manage.py + requirements.txt with django", 'gunicorn <project>.wsgi --bind 0.0.0.0:$PORT', "python manage.py migrate --noinput",
   "Set <code>ALLOWED_HOSTS</code> to include <code>.aloof.si</code> and your custom domain, and <code>CSRF_TRUSTED_ORIGINS</code> to your https origin. Serve static files with WhiteNoise; <code>collectstatic</code> runs at build time."),
 "flask": ("Flask", "python", "requirements.txt with flask", 'gunicorn app:app --bind 0.0.0.0:$PORT', None,
   "Add <code>gunicorn</code> to requirements. For background jobs (RQ, Celery), add a <code>worker</code> process group; it shares the image and never sleeps."),
 "nextjs": ("Next.js", "javascript", "package.json with next", 'npm run start', None,
   "<code>HOSTNAME=0.0.0.0</code> is set so <code>next start</code> listens on all interfaces. For smaller images use <code>output: \"standalone\"</code>. Call internal APIs over <code>http://&lt;app&gt;.internal:8080</code> from server code."),
 "express": ("Node.js / Express", "javascript", "package.json with a start script", 'npm start', None,
   "Listen on <code>process.env.PORT</code>. The package manager (npm, pnpm, yarn, bun) is picked from your lockfile."),
 "rails": ("Ruby on Rails", "ruby", "Gemfile with rails", 'bin/rails server -b 0.0.0.0 -p $PORT', "bin/rails db:migrate",
   "Set <code>RAILS_MASTER_KEY</code> with <code>aloof secrets set</code>. Add <code>worker = \"bundle exec sidekiq\"</code> (or <code>bin/jobs</code> for Solid Queue) under <code>[processes]</code>."),
 "go": ("Go", "go", "go.mod", '/app/server', None,
   "The generated Dockerfile builds a static binary in a multi-stage build, so images are small and cold starts are fast. Good for scale-to-zero."),
 "docker": ("Any Docker image", "docker", "an existing image or Dockerfile", '(your CMD)', None,
   "Deploy a public or private image directly: <code>aloof deploy --image ghcr.io/acme/api:1.4</code>. For private registries run <code>aloof registry login ghcr.io -u USER --password-stdin</code> first."),
}
def guide_html(slug):
    n, lang, detect, cmd, rel, notes = GUIDES[slug]
    rel_toml = f'\n\n[deploy]\n  release_command = "{rel}"' if rel else ""
    return f"""<p>Deploy a {n} app to Aloof in a few minutes. You need the CLI and an account.</p>
<pre># install the CLI into ~/.local/bin (make sure that folder is on your PATH)
$ mkdir -p ~/.local/bin && curl -fsSL https://api.aloof.si/cli/aloof -o ~/.local/bin/aloof && chmod +x ~/.local/bin/aloof

# new here: create a free account (no card) and save the key
$ aloof signup you@example.com
# already have an account: paste an API key from Dashboard → API keys
$ aloof login</pre>
<p>Prefer a system-wide install? See the <a href="/docs/quickstart">Quickstart</a>. If you'd rather not touch your PATH, download it into the current folder and run it as <code>./aloof</code>.</p>
<h2>1. Generate config</h2><p><code>aloof launch</code> detects {n} from {detect} and writes a Dockerfile (unless you have one), <code>aloof.toml</code> and <code>.dockerignore</code>.</p>
<pre>$ aloof launch --name my-{slug}</pre>
<h2>2. Review aloof.toml</h2><pre>app = "my-{slug}"
primary_region = "nbg1"

[http_service]
  internal_port = 8080
  auto_stop_machines = true
  min_machines_running = 0

[processes]
  web = "{cmd}"{rel_toml}</pre>
<p>Aloof sets <code>PORT=8080</code>. With <code>min_machines_running = 0</code> the app sleeps when idle and you pay per second only while it runs.</p>
<h2>3. Database (optional)</h2><pre>$ aloof pg create my-{slug}-db --plan pg-dev
$ aloof pg attach my-{slug}-db my-{slug}     # sets DATABASE_URL over the private network</pre>
<h2>4. Deploy</h2><pre>$ aloof deploy
live: https://my-{slug}.aloof.si/</pre>
<p>The directory is uploaded (respecting <code>.dockerignore</code>) and built remotely. {"The release command runs before new instances take traffic; if it fails, the previous release keeps serving." if rel else "New instances must pass their health check before the old ones stop."}</p>
<h2>Notes for {n}</h2><p>{notes}</p>
<h2>Next</h2><ul><li><a href="/docs/config">aloof.toml reference</a></li><li><a href="/docs/github">Deploy from GitHub, previews per PR</a></li><li><a href="/docs/ci">Deploy from CI with a scoped token</a></li></ul>"""

# from `git log` of this repository; dates are commit dates
CHANGELOG = [
 ("2026-10-13", "Website: blog, guides, changelog, regions, status history and owner dashboard", ["Server-side uptime checks for www, API and MCP every minute, with 90-day history on /status.", "Regions page with live and planned locations and a waitlist per region.", "RSS feed, sitemap, OpenGraph images."]),
 ("2026-10-13", "Private registries, private git and local deploys", ["Per-app registry credentials for GHCR, Docker Hub, ECR, GCR or any registry (<code>aloof registry login</code>).", "Private git over deploy keys or HTTPS tokens (GitHub, GitLab, Bitbucket, any host), plus a GitHub App with push-to-deploy and preview apps per PR.", "<code>aloof deploy</code> from a local directory (tar upload, .dockerignore, remote build) and <code>aloof launch</code> with framework detection and a Nixpacks fallback.", "<code>aloof.toml</code>: services, env, process groups, health checks, release_command, mounts, autoscale, cron.", "Scoped, expiring deploy tokens; staged secrets; <code>.internal</code> private networking; <code>aloof proxy</code>, <code>ips</code> and <code>certs</code>."]),
 ("2026-10-13", "Encrypted off-site backups", ["Client-side AES-256-GCM to S3-compatible storage (Hetzner Object Storage, Helsinki) for Postgres, volumes and the control-plane database.", "GFS retention 7/4/3, weekly test restore, admin alert after 26 h without a fresh backup.", "Restore from the API, CLI, MCP or dashboard."]),
 ("2026-10-13", "Releases, managed Postgres and audited console", ["Zero-downtime, health-gated releases and one-command rollback.", "Managed Postgres with attach, daily backups and restore. Volume snapshots.", "Audited <code>exec</code> and web console; log retention and HTTP/syslog drains."]),
 ("2026-10-13", "New design", ["Dark and light design system, new landing, pricing and docs, an app-style dashboard with metrics, security and status pages."]),
 ("2026-10-13", "CPU hosting on Hetzner Cloud", ["Phase 1: CPU apps in Germany with per-second billing. GPUs stay behind a waitlist until 10 paid customers."]),
 ("2026-10-12", "USD billing worldwide", ["Stripe Checkout for credit top-ups and Hobby/Pro subscriptions; USD price list with a 30% margin floor."]),
 ("2026-10-12", "First release", ["Deploy Docker images and git repos, scale to zero, custom domains with automatic HTTPS, volumes and a remote MCP server for AI assistants."]),
]

ARCHES = [
 ("SaaS API with a worker", "FastAPI or Rails web tier, a queue worker process group, managed Postgres, nightly cron cleanup.",
  ["<code>web</code>: shared-1x-512mb, autoscale 1–3", "<code>worker</code>: shared-1x-256mb, always on", "<code>pg-basic</code> attached over the private network", "<code>[[cron]]</code> <code>0 3 * * *</code> cleanup"], "deploy-fastapi-postgres"),
 ("Next.js frontend + private API", "Public Next.js app that calls a private API by name. The API has no public route.",
  ["<code>shop</code> (Next.js), public, custom domain", "<code>shop-api</code> reached as <code>http://shop-api.internal:8080</code>", "Preview app per pull request via the GitHub App"], "deploy-nextjs-and-rails"),
 ("Webhook receiver that sleeps", "A small service that handles Stripe/GitHub webhooks and costs cents a month because it sleeps between events.",
  ["shared-1x-256mb, <code>min_machines_running = 0</code>", "Cold start on first event, then warm for the idle window", "Secrets stored encrypted, write-only"], "scale-to-zero-economics"),
 ("AI agent-operated backend", "An assistant connected over MCP deploys, scales and reads logs, using a deploy token scoped to one app.",
  ["Remote MCP at <code>mcp.aloof.si</code> with OAuth", "Deploy token limited to one app, 30-day expiry", "Every exec/restore/secret change in the audit log"], "mcp-let-your-agent-deploy"),
 ("Stateful service with backups", "A service with a volume (single writer), plus Postgres with off-site encrypted backups and a tested restore path.",
  ["Volume mounted at <code>/data</code>, snapshots on demand", "Daily off-site copies to Helsinki, weekly test restore", "Restore with <code>aloof pg offsite-restore</code>"], "postgres-offsite-backups"),
 ("Scheduled jobs only", "No web tier at all: cron machines that run a report or sync and exit, billed per second while they run.",
  ["<code>[[cron]]</code> entries in aloof.toml", "Runs on the private network with the app's env", "Exit codes and output kept per run"], None),
]

# Regions page rows. Live/soon for compute regions is decided at request time by paas.regions.status() (healthy nodes), never here.
REGION_PURPOSE = {"nbg1": "Compute, control plane, edge, managed Postgres, volumes",
                  "fsn1": "Second German location for capacity and node-level redundancy.",
                  "hel1": "Compute next to our backup location.",
                  "us-east": "For users on the US East Coast.",
                  "us-west": "For users on the US West Coast.",
                  "sg": "For users in Southeast Asia."}
LIVE_REGIONS = [("nbg1", "Nuremberg, Germany", "Hetzner Cloud (nbg1)", REGION_PURPOSE["nbg1"]),
                ("hel1 (storage)", "Helsinki, Finland", "Hetzner Object Storage", "Encrypted off-site backups only, no compute")]
SOON_REGIONS = [("fsn1", "Falkenstein, Germany", "Hetzner Cloud", REGION_PURPOSE["fsn1"]),
                ("hel1", "Helsinki, Finland", "Hetzner Cloud", REGION_PURPOSE["hel1"]),
                ("us-east", "Vint Hill, Virginia, US", "OVHcloud (planned)", REGION_PURPOSE["us-east"]),
                ("us-west", "Hillsboro, Oregon, US", "OVHcloud (planned)", REGION_PURPOSE["us-west"]),
                ("sg", "Singapore", "OVHcloud (planned)", REGION_PURPOSE["sg"])]

ABOUT = """<p>Aloof is a small, independent hosting platform for backends, APIs and workers. You bring a Docker image, a git repo or a local directory; we run it, bill it per second, and put it to sleep when it's idle.</p>
<h2>Why we built it</h2><p>Running a side project or a small production API shouldn't cost $20+ a month per service, and it shouldn't take a week of YAML. Fly.io got the developer experience right; we wanted that experience, the same config model, and prices at or below list, with sensible defaults like scale-to-zero, encrypted off-site backups and an audit log built in.</p>
<h2>Where we are today</h2><ul><li><b>New.</b> First release in October 2026.</li><li><b>One region.</b> Compute runs in Nuremberg, Germany. Others are on the <a href="/regions">regions page</a>, marked as not live.</li><li><b>No certifications.</b> We don't hold SOC 2 or ISO 27001. The <a href="/security">security page</a> lists what the platform actually does.</li><li><b>Agent-native.</b> Every feature is available over a REST API, a single-file CLI and an MCP server, so AI assistants can operate it safely.</li></ul>
<h2>How we make money</h2><p>Usage-based compute, storage and egress, plus optional Hobby and Pro plans. Every price carries a margin over our real infrastructure cost, so the free tier can be hard-capped instead of subsidised by surprise bills.</p>
<h2>Contact</h2><p>General: <a href="mailto:hello@aloof.si">hello@aloof.si</a> · Security: <a href="mailto:security@aloof.si">security@aloof.si</a> · Design partners: <a href="/examples#partner">apply here</a>.</p>"""

SECURITY = """<p>This page lists what the platform does today and what it doesn't. We hold no certifications (no SOC 2, ISO 27001 or HIPAA) and we don't claim any.</p>
<h2>Transport</h2><ul><li>All public endpoints (site, API, MCP and apps) use HTTPS with automatically issued certificates. Certificates are only requested for hosts we know about (on-demand TLS is gated by an ask endpoint).</li><li>Remote nodes connect to the control plane over WireGuard and expose no inbound management ports.</li></ul>
<h2>Secrets and data at rest</h2><ul><li>App environment variables, registry credentials, git deploy keys and tokens are encrypted at rest (Fernet) and are write-only through the API.</li><li>API keys, deploy tokens and session tokens are stored only as SHA-256 hashes.</li><li>Off-site backups are encrypted on our server with AES-256-GCM before upload, so the storage provider only sees ciphertext. Platform secrets get a second, passphrase-derived key (scrypt).</li></ul>
<h2>Isolation</h2><ul><li>Each instance runs in its own container with CPU and memory limits. Containers share the host kernel; there are no microVMs yet.</li><li>Each account has its own private network. Other accounts can't route to or resolve your apps, and an integration test checks this.</li><li>Only an app's HTTP port is reachable publicly, through the edge proxy. Managed Postgres has no public port.</li></ul>
<h2>Access and accountability</h2><ul><li>HttpOnly, SameSite session cookies; cookie-authenticated writes require a CSRF header.</li><li>Single-use, expiring magic links. OAuth 2.1 with PKCE for MCP clients; tokens are scoped and revocable.</li><li>Deploy tokens can be scoped to one app with an expiry, and can't touch billing or auth.</li><li>exec, console sessions, proxy sessions, restores, token creation and secret changes are written to a per-account audit log.</li><li>Owner admin access requires an admin flag on the account, a separate admin token, and a session check that is ready for a second factor.</li></ul>
<h2>Backups and recovery</h2><ul><li>Daily logical Postgres backups on the host, and daily encrypted copies off-site in a separate location (Helsinki), kept 7 daily / 4 weekly / 3 monthly.</li><li>A weekly automated test restore. An alert fires if the newest off-site backup is older than 26 hours.</li><li>Not yet: point-in-time recovery, replicas, multi-region failover.</li></ul>
<h2>Infrastructure and processors</h2><ul><li>Compute and the control plane run on Hetzner Cloud in Nuremberg, Germany. Backups go to Hetzner Object Storage in Helsinki, Finland.</li><li>Stripe processes payments; card data never reaches our servers.</li></ul>
<h2>Reporting a vulnerability</h2><p>Email <a href="mailto:security@aloof.si">security@aloof.si</a> with steps to reproduce. Please give us reasonable time to fix an issue before you disclose it. We don't run a paid bug bounty.</p>"""
