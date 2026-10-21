"""Docs pages for source/CI/process features (merged into site.docs)."""
def pages(brand, base, api):
    return {
"cli": """<h1>CLI</h1><pre>aloof signup you@company.com      # new account; saves the key to ~/.aloof.json
aloof login [--key pk_... | --key-stdin]   # save an existing API key after checking it works
aloof launch                      # detect framework, write Dockerfile + aloof.toml + .dockerignore
aloof deploy                      # build the current dir remotely (tar upload, .dockerignore respected) using aloof.toml
aloof deploy &lt;app&gt; --image IMG | --git URL [--ref main] [--plan shared-1x-1gb] [--port 8080] [-e K=V] [-n 2]
aloof config validate | show
aloof status | logs [-f] | destroy   [-a app]        (app defaults to aloof.toml's app)
aloof releases &lt;app&gt; ; aloof rollback &lt;app&gt; [version]
aloof self-heal &lt;app&gt; [on|off]    # show or set automatic rollback of bad deploys (on by default)
aloof templates                   # list one-click templates ; deploy one with: aloof deploy &lt;app&gt; --template &lt;id&gt;
aloof scale count 3 | scale count web=2 worker=1 | scale vm perf-2x-4gb [--group worker]
aloof secrets set K=V [K2=V2] [--stage] | unset K | list | deploy
aloof tokens create [-a app | --org] [--expiry 30d] | list | revoke &lt;id&gt;
aloof registry login ghcr.io -u me --password-stdin [-a app] | list | logout &lt;server&gt;
aloof git deploy-key github.com [-a app] | git token gitlab.com [-a app] | list | remove &lt;id&gt;
aloof github install | repos | status | link &lt;app&gt; owner/repo [--branch main] [--no-previews] | unlink &lt;app&gt;
aloof previews list | create &lt;pr&gt; &lt;ref&gt; | destroy &lt;pr&gt;
aloof cron list | run &lt;job&gt;
aloof certs | ips | proxy 5432[:5432]   [-a app]
aloof env &lt;app&gt; K=V K2=          # K2= removes; values are encrypted at rest
aloof volumes create data --size 5 ; aloof volumes attach &lt;app&gt; data --path /data
aloof autoscale &lt;app&gt; --min 0 --max 4 --concurrency 10 [--cpu 70] --idle 300
aloof health &lt;app&gt; --path /healthz --failures 3
aloof domains &lt;app&gt; add api.example.in | verify &lt;domain&gt; | list | remove &lt;domain&gt;
aloof pg create db --plan pg-basic ; aloof pg attach db &lt;app&gt; ; aloof pg backup|backups|restore|url|destroy|offsite|offsite-restore db
aloof snapshots create|list|restore|offsite|offsite-restore &lt;volume&gt; ...
aloof ssh &lt;app&gt; ; aloof exec &lt;app&gt; -- ls /data
aloof drains &lt;app&gt; add https://logs.example.com/ingest ; aloof history &lt;app&gt; --since 24 --grep error ; aloof audit</pre>
<p>Config is read from <code>~/.aloof.json</code> (<code>{"api": "...", "key": "pk_..."}</code>) or the env vars <code>ALOOF_API_URL</code> plus <code>ALOOF_API_TOKEN</code> (deploy token) or <code>ALOOF_API_KEY</code>.</p>""",
"deploy": f"""<h1>Deploying apps</h1>
<p><b>From your directory</b>: <code>aloof launch</code> detects Node (Next.js, Remix, Nuxt), Python (Django, FastAPI, Flask), Go, Rust, Ruby/Rails, PHP/Laravel, Java (Maven/Gradle), Elixir/Phoenix and static sites (step-by-step <a href="/guides">guides</a> exist for eight of these), then writes a Dockerfile and an <a href="/docs/config">aloof.toml</a>. <code>aloof deploy</code> uploads the directory, honouring <code>.dockerignore</code>, and builds it on our builders. If there is no Dockerfile, the builder uses Nixpacks and falls back to the generated Dockerfile.</p>
<p><b>From an image</b>: any public image. Private images work from Docker Hub, GHCR, GitLab, ECR, GCR/Artifact Registry or any v2 registry after <code>aloof registry login</code> (see <a href="/docs/private">private registries</a>).</p>
<p><b>From git</b>: public or private repos, using deploy keys, HTTPS tokens or the <a href="/docs/github">GitHub App</a>. Pass <code>--git URL --ref main</code>, with an optional <code>subdir</code>. Built images are pushed to the {brand} registry, which your nodes pull from.</p>
<p>Redeploys are rolling: new instances must pass a readiness check before old ones stop, and a release that fails it never takes traffic (see <a href="/docs/releases">releases &amp; rollback</a>). A <a href="/docs/processes">release_command</a> runs before any new instance starts. Env vars, volumes, autoscale and health settings carry over between deploys.</p>
<p>Health checks: set an HTTP path with <code>aloof health app --path /healthz</code> or <code>[[http_service.checks]]</code>. After N consecutive failures the instance is replaced.</p>""",
"config": """<h1>aloof.toml reference</h1><p>Fly-style config. <code>aloof config validate</code> checks it against the server. Unknown keys give warnings, not errors.</p><pre>app = "my-app"
primary_region = "nbg1"     # see `aloof regions`; only live regions accept new apps

[build]
  dockerfile = "Dockerfile"       # or image = "ghcr.io/me/app:1" | git = "git@github.com:me/app.git" + ref
  [build.build_args]
    NODE_ENV = "production"

[env]
  LOG_LEVEL = "info"              # secrets: aloof secrets set (encrypted, never in this file)

[processes]                       # process groups: web gets HTTP, the others run as workers
  web = "gunicorn app:app --bind 0.0.0.0:8080"
  worker = "celery -A app worker"

[scale]
  web = 2
  worker = 1

[http_service]
  internal_port = 8080
  force_https = true
  auto_stop_machines = true       # scale to zero when idle
  min_machines_running = 0
  [http_service.concurrency]
    soft_limit = 25               # autoscale target per instance
  [[http_service.checks]]
    path = "/healthz"
    interval = "10s"

[deploy]
  release_command = "python manage.py migrate --noinput"

[[mounts]]
  source = "data"                 # created on first deploy
  destination = "/data"
  initial_size = "3gb"

[autoscale]                       # explicit alternative to auto_stop_machines
  min = 1
  max = 4
  target_concurrency = 20

[[vm]]
  size = "shared-1x-1gb"          # or cpus = 2, memory = "2gb"

[[cron]]
  name = "nightly"
  schedule = "0 3 * * *"          # UTC; @hourly/@daily/@weekly work too
  command = "python manage.py cleanup"</pre>
<p>Only the first <code>[[services]]</code> entry is routed (HTTP/HTTPS on 80/443). Raw TCP/UDP public ports aren't offered.</p>""",
"private": """<h1>Private registries &amp; repos</h1><h2>Registries</h2><pre>aloof registry login ghcr.io -u my-user --password-stdin &lt;&lt;&lt; "$GHCR_TOKEN"
aloof registry login docker.io -u me                       # Docker Hub access token
aloof registry login 123456789012.dkr.ecr.eu-central-1.amazonaws.com -u AKIA... -p SECRET   # AWS keys; a short-lived ECR token is minted per pull
aloof registry login europe-docker.pkg.dev -u _json_key --password-stdin &lt; key.json        # GCR / Artifact Registry
aloof registry login registry.example.com -u ci -a my-app  # per-app override</pre>
<p>Credentials are encrypted at rest. They are written only to a private, temporary Docker config for a single pull or build, including the <code>FROM</code> lines in your Dockerfile. Every deploy pulls with the owning account's credentials, so a private image cached on a host can't be used by another account. Images are then mirrored into the platform registry, so edge and self-hosted nodes run them without ever seeing your registry credentials.</p>
<h2>Git</h2><pre>aloof git deploy-key github.com -a my-app        # prints an ed25519 public key: add it as a read-only deploy key
aloof deploy my-app --git git@github.com:me/private.git --ref main
aloof git token gitlab.com -a my-app             # read-only token (GitHub: x-access-token, GitLab: oauth2, Bitbucket: x-token-auth)
aloof deploy my-app --git https://gitlab.com/me/private.git</pre>
<p>This works with any git host over SSH or HTTPS. Tokens are passed as an HTTP header through the environment, never on a command line or in <code>.git/config</code>. Submodules are fetched with the same credentials. On GitHub you can use the <a href="/docs/github">GitHub App</a> instead, which needs no long-lived keys.</p>""",
"github": f"""<h1>GitHub App: push-to-deploy &amp; previews</h1><pre>aloof github install                  # opens GitHub; pick the repos
aloof github repos
aloof github link my-app me/my-repo --branch main</pre>
<p>A push to the branch deploys that commit. A pull request gets a scale-to-zero preview app <code>my-app-pr-&lt;n&gt;</code> at <code>https://my-app-pr-&lt;n&gt;.{base}/</code> and a PR comment with the link. The preview is destroyed when the PR closes, and PRs from forks are built without your secrets. Every deploy reports a commit status and a check run.</p>
<p>Clones use short-lived installation tokens. Webhooks are verified with HMAC-SHA256 and de-duplicated. An installation is linked to your account only after GitHub confirms that the user who installed it can access it.</p>
<p>Not on GitHub? Previews work with any git host through the API: <code>aloof previews create 42 feature-branch</code>.</p>""",
"ci": """<h1>Deploy tokens &amp; CI</h1><pre>aloof tokens create -a my-app --expiry 90d     # app-scoped (or --org for every app in the account)
aloof tokens list ; aloof tokens revoke tok_...</pre>
<p>A deploy token (<code>dt_...</code>) can deploy, scale, set secrets and read logs for its app, including the app's previews and worker groups. It can't manage tokens, billing, auth or admin endpoints. Expired or revoked tokens stop working immediately, and the audit log records which token did what.</p>
<h2>GitHub Actions</h2><pre>name: deploy
on: {push: {branches: [main]}}
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: sachinchandra/aloof/.github/actions/deploy@main
        with:
          token: ${{ secrets.ALOOF_API_TOKEN }}
          # app: my-app        (defaults to aloof.toml)</pre>
<p>Or skip the action: <code>curl -fsSL https://api.aloof.si/cli/aloof -o aloof &amp;&amp; ALOOF_API_TOKEN=... python3 aloof deploy</code>.</p>""",
"processes": """<h1>Workers, release commands &amp; cron</h1><p><b>Process groups</b>: <code>[processes]</code> in aloof.toml. <code>web</code> receives HTTP. Each other group runs as a worker app <code>&lt;app&gt;-&lt;group&gt;</code> with the same image, env and networks, no health probe and no sleeping. Scale a group with <code>aloof scale count worker=3</code>, and resize it with <code>aloof scale vm perf-2x-4gb --group worker</code>.</p>
<p><b>Release command</b>: <code>[deploy] release_command</code> runs once after the build, before any new instance starts, in a one-off machine with the same image, env, private networks and volumes. If it exits non-zero, the release fails and the previous release keeps serving.</p>
<p><b>Cron / scheduled machines</b>: <code>[[cron]]</code> jobs run on schedule (UTC, 5-field or @daily) as one-off machines from the current release. The exit code and output of the last 50 runs of each job are kept: <code>aloof cron list</code>, <code>aloof cron run nightly</code>.</p>
<p><b>Preview deploys</b>: per PR through the <a href="/docs/github">GitHub App</a>, or <code>aloof previews create &lt;n&gt; &lt;ref&gt;</code> for any git host.</p>""",
"networking": """<h1>Private networking, proxy, IPs &amp; certs</h1><p>Each account has a private network. Your other apps reach every app and worker as <code>&lt;app&gt;.internal</code>, on any port. Other accounts' apps are on separate networks and can't reach yours. Managed Postgres is reachable the same way (see <a href="/docs/postgres">Postgres</a>).</p>
<pre>aloof proxy 5432 -a my-db-app        # localhost:5432 -> instance:5432 over an authenticated WebSocket
aloof proxy 8081:9000 -a my-app      # local 8081 -> private port 9000
aloof ips -a my-app                  # shared public edge IPv4/IPv6 + private addresses
aloof certs -a my-app                # Let's Encrypt status/expiry for &lt;app&gt;.domain + custom domains</pre>
<p>Limits: <code>.internal</code> resolves between apps on the same host. Apps placed on different remote nodes don't share a network yet. On remote nodes, <code>proxy</code> reaches only the app's <code>internal_port</code>. Public IPs are the shared edge; dedicated and anycast IPs aren't offered.</p>""",
}
