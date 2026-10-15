# Aloof

**Cheap backends, billed per second in US dollars.** Deploy a Docker image or a git repo with a Dockerfile and get HTTPS at
`https://<app>.aloof.si`, autoscaling down to zero, persistent volumes, custom domains and simple USD billing through Stripe.
It stays cheap because phase 1 runs on Hetzner Cloud in the EU (shared-vCPU CX servers). GPUs (Hyperstack VMs, Runpod pods) come in phase 2.
AI assistants can deploy for you over MCP.

> Status: working prototype. Everything below runs end to end on a single box. Going live needs real provider keys, Stripe, SMTP and DNS (see [Go live](#go-live)).

## Regions
Phase 1: all compute and the control plane/edge run in the **EU** on Hetzner Cloud: Nuremberg, Germany (`nbg1`); encrypted off-site backups go to Helsinki, Finland. Other regions are listed as coming soon on /regions. Expect ~10–40 ms within Europe, ~90–110 ms from US East and ~150–200 ms from India or US West. E2E Networks
(Delhi) stays available as an optional CPU overflow pool and is only listed when its keys are set. Other regions will only be listed once they exist.

**GPUs are coming soon.** Phase 2 adds L4 / RTX 4090 (Runpod pods) and L40 / A100 80GB / H100 (Hyperstack VMs) once there are 10 paying
customers (`GET /v1/admin/metrics`). Until then `PAAS_GPU_ENABLED=0`: GPU plans are hidden from the API, MCP and `/llms.txt`, deploys
on them are refused, and the pricing page shows a waitlist (`POST /v1/waitlist`).

## Features
- **Deploy** from an image or git (`aloof deploy`, REST, dashboard or MCP), with rolling redeploys, env vars (encrypted at rest) and health checks with auto-replace.
- **Autoscaling and scale to zero**: concurrency- and CPU-based. Idle apps sleep and the first request wakes them (≈1 s on CPU) while it's held at the edge.
- **Volumes** that survive redeploys and restarts, pinned to the node they live on.
- **Custom domains**: TXT ownership check plus CNAME (subdomains) or A + AAAA (apex; ALIAS/ANAME/flattening also works) routing, background re-checks with a `pending`/`verified`/`dns_error` status, www/apex 308 pairing, per-plan limits, automatic TLS through Caddy (Let's Encrypt in production).
- **Multi-cloud scheduler**: bin-packs CPU apps, gives each GPU app a dedicated node, and refuses any pool where a plan would fall under the margin floor. Nodes join over WireGuard and run `paas/agent/agent.py`.
- **Accounts**: email + password, magic links, session cookies with a CSRF header, and API keys stored only as SHA-256 hashes.
- **Billing in USD (cents)**: a prepaid wallet with Stripe Checkout top-ups, Stripe subscriptions for the Hobby and Pro tiers, the Stripe customer portal, webhook signature verification,
  idempotent crediting, and Stripe Tax for VAT/GST/sales tax on invoices. Compute, volumes (GB-month) and egress are metered per second. Razorpay INR/UPI is optional and off by default.
- **MCP server** (stdio and streamable HTTP with OAuth 2.1: DCR, PKCE, RFC 9728), plus `/llms.txt` and an OpenAPI spec.

## Pricing (USD, excluding tax)
| | Price | Fly.io / Render / Railway |
|---|---|---|
| Free | $0: 3 apps, shared-1x-256mb/512mb, sleeps after 5 min idle, hard-capped at $1 of usage a month | Fly none · Render $0 · Railway $0 + $1 |
| Hobby / Pro | $5 / $20 per workspace a month, with the fee returned as included usage | Railway $5 / $20 (same model) · Render Pro $25 |
| Shared CPU | $1.99 (256 MB) · $3.49 · $5.99 · $11.99 · $44 (4 vCPU / 8 GB) a month | Fly $2.19 · $3.69 · $6.70 · $13.39 |
| Performance CPU | $24 (1 vCPU / 2 GB) · $59 · $119 · $239 (8 vCPU / 16 GB) a month | Fly $33–264 · Render $25–300 |
| GPU per hour (phase 2, coming soon) | RTX 4090 $0.79 · L4 $1.10 · L40 $1.89 · A100 80GB $3.99 · H100 $5.49 | Render/Railway none; Fly retired GPUs |
| Volumes / egress | $0.22 per GB-month / $0.06 per GB | Fly $0.15 / $0.02–0.12 · Render $0.25 / $0.15 · Railway $0.15 / $0.05 |

The full table is at `/pricing`. `paas/plans.py` enforces a 30% minimum margin after payment fees, against worst-case costs converted at a
pessimistic ₹83/$ (`PAAS_COST_USD_INR`); Hetzner/Hyperstack/Runpod USD list prices get a 10% buffer. On a Hetzner CX43
(8 vCPU / 16 GB, $18.49 + IPv4) every CPU plan clears 62–79%; the CPX42 fallback is refused for all shared plans. A test fails if any plan or rate falls under it, and the scheduler refuses pools that would.

## Quick start (local)
Requires Linux with Docker, Python 3.11+ and sudo.
```bash
bash deploy/setup-box.sh      # installs packages, venv, Postgres 16 + registry in Docker, WireGuard hub, example images
./run.sh                      # supervisord: dockerd, paas (API :8000, edge :8080), mcp (:8001), caddy (:80/:443)
./run.sh status | restart <prog> | stop      # logs in data/*.log
```
| URL | What |
|---|---|
| http://localhost:8000/ | landing, `/pricing`, `/docs`, `/dashboard`, `/api/docs` (OpenAPI), `/llms.txt` |
| https://\<app\>.localhost/ | apps through Caddy (or `http://localhost:8080/_app/<app>/`) |
| http://localhost:8001/mcp | MCP (OAuth 2.1, or `Authorization: Bearer pk_...`) |

Caddy's local CA is at `data/caddy/caddy/pki/authorities/local/root.crt` (`curl --cacert <it> https://<app>.localhost/`).

## CLI (`cli/aloof`)
```
aloof signup you@example.com ; aloof plans
aloof deploy web --image nginx:alpine --port 80 | --git https://github.com/u/r.git [--ref main] [--plan shared-1x-1gb] [-e K=V] [-n 2]
aloof status|logs|destroy <app> ; aloof scale <app> 3 ; aloof env <app> K=V K2=
aloof volumes create data --size 2 ; aloof volumes attach <app> data --path /data ; aloof volumes list|detach|delete
aloof autoscale <app> --min 0 --max 4 --concurrency 10 [--cpu 70] --idle 300      # min 0 = scale to zero
aloof health <app> --path /healthz --failures 3
aloof domains <app> add api.example.in | verify <domain> | list | remove <domain>
```
The CLI stores your API URL and key in `~/.paas.json`.

## MCP
Add `https://mcp.aloof.si/mcp` as a connector in Claude, ChatGPT or Cursor (OAuth), or run it locally:
```json
{"mcpServers": {"aloof": {"command": "python3", "args": ["mcp_server/server.py"],
  "env": {"ALOOF_API_URL": "http://localhost:8000", "ALOOF_API_KEY": "pk_..."}}}}
```
Tools (42, parsed from `mcp_server/server.py`): `list_plans, signup, deploy_app, get_status, list_apps, get_logs, scale_app, set_env, destroy_app, get_usage, list_templates, deploy_template, create_volume, list_volumes, attach_volume, detach_volume, set_autoscale, add_domain, list_domains, verify_domain, remove_domain, join_gpu_waitlist, list_releases, diagnose_app, rollback_app, set_self_heal, create_postgres, list_regions, list_postgres, attach_postgres, backup_postgres, snapshot_volume, restore_volume, exec_command, add_log_drain, list_offsite_backups, restore_offsite_backup, registry_login, add_git_credential, deploy_with_config, app_status, create_deploy_token`.

## Custom domains: how to connect one

Works like Fly.io, Railway and Render: add the domain, create DNS records at your registrar, wait for the automatic check, done. Full guide with per-registrar tips (Cloudflare, GoDaddy, Namecheap, Route 53): `/docs/domains` on the site.

```
aloof domains my-app add api.example.com      # prints the records to create
aloof domains my-app list                     # status, last checked, records still needed
aloof domains my-app verify api.example.com   # check now (rate limited); we also re-check automatically
aloof domains my-app remove api.example.com
```

| Record | Name | Value | When |
|---|---|---|---|
| TXT | `_aloof-challenge.<domain>` | `aloof-verify=<token>` | always: proves ownership |
| CNAME | `api.example.com` | `<app>.aloof.si` | subdomains |
| A / AAAA | `example.com` | the edge IPv4 / IPv6 shown by the API (`PAAS_EDGE_IPS`) | root (apex) domains, or instead of the CNAME |
| ALIAS / ANAME / flattened CNAME | `example.com` | `<app>.aloof.si` | apex, if your DNS provider offers it, instead of A/AAAA |

- **Cloudflare**: set the routing records to *DNS only* (grey cloud); proxied (orange cloud) domains are rejected because they resolve to Cloudflare, not to us.
- **Status**: `pending` (re-checked after 2 min, then 5, 10, 30 min, 1 h, 3 h, 6 h), `verified` (re-checked every 30 min), `dns_error` (was verified, DNS later broke: still served, never removed, recovers by itself). Unverified claims are released after 7 days.
- **www/apex**: once `example.com` is verified, `www.example.com` redirects to it with a 308 (and the reverse for a verified `www`), if it points at us too. Only to verified domains.
- **Limits** per account: Free 1, Hobby 3, Pro 10 domains (`PAAS_DOMAIN_LIMIT_<TIER>` overrides); 20 adds and 30 manual verifies per hour per account (HTTP 429 + `Retry-After`); `PAAS_MAX_CUSTOM_DOMAINS` (500) caps certificate-issuing hostnames platform-wide.
- **API / MCP**: `POST /v1/apps/{app}/domains`, `POST .../{domain}/verify`, `GET /v1/apps/{app}/domains`, `GET /v1/domains`, `DELETE .../{domain}`; MCP tools `add_domain`, `list_domains`, `verify_domain`, `remove_domain`.
- **Operator config**: `PAAS_EDGE_IPS` is a comma list of the edge's public IPv4 *and* IPv6 addresses (e.g. `178.104.200.75,2a01:4f8:1c1f:71d2::1`); the controller re-check interval is `PAAS_DOMAIN_TICK_S` (60 s). Migration v12 adds the status columns.

## Layout
```
paas/api.py           REST API, dashboard/site routes, edge proxy
paas/engine.py        deploy/scale/volumes, scheduling (pick_provider), accounts
paas/controller.py    autoscaler + health loops
paas/plans.py         plans (USD), tiers, rates, cost model, fees, 30% margin floor
paas/billing.py       USD wallet (cents), metering, free-tier cap
paas/stripe_billing.py  Stripe Checkout, subscriptions, portal, webhooks, Stripe Tax
paas/razorpay_legacy.py optional INR/UPI top-ups + GST invoices (off by default)
paas/drivers/         local_docker, remote (node agent + registry), providers (Hetzner, Hyperstack, E2E/Jarvislabs/Krutrim/AWS, self-hosted), runpod (pod driver), wg
paas/agent/agent.py   node agent (Docker + NVIDIA runtime) on remote VMs
mcp_server/           MCP server + OAuth 2.1 authorization server
dashboard/, cli/, deploy/, scripts/, tests/
```

## Configuration and secrets
Nothing secret is committed. At first start the box creates `data/secret.key` (Fernet key for encryption at rest), `data/admin_token` and
`data/database_url`; `data/` is git-ignored. In production, `deploy/golive.sh` points `PAAS_ENV_FILE` at `deploy/golive.env` (also git-ignored), which holds the Stripe/SMTP/provider secrets. The environment variables `PAAS_SECRET_KEY` (comma list `new,old` for rotation, then run
`scripts/rotate_secret_key.py`), `PAAS_ADMIN_TOKEN` and `PAAS_DB_URL` override those files. Stripe (`STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_HOBBY`, `STRIPE_PRICE_PRO`, `STRIPE_TAX`), provider and SMTP settings are documented in
[`deploy/golive.env.example`](deploy/golive.env.example). A provider pool activates once its keys are set (`GET /v1/providers`).

## Health monitoring
Both the API and MCP server expose `/healthz` endpoints. To enable email alerts on failure:
```bash
export HEALTH_CHECK_ENABLED=1
export HEALTH_ALERT_EMAIL=ops@example.com
# SMTP settings from golive.env are used; runs via cron every 5 minutes after setup-box.sh
```
A GitHub Actions workflow (`.github/workflows/health-check.yml`) also pings production every 15 minutes.

## Tests
```bash
pip install -r requirements-dev.txt                        # test dependencies (pytest, respx, dnslib, ruff, pip-audit)
sg docker -c '.venv/bin/python -m pytest -q tests/'        # unit + mocked-cloud node lifecycle (real Docker)
.venv/bin/python tests/oauth_e2e.py ; .venv/bin/python tests/oauth_password.py
.venv/bin/python tests/mcp_e2e.py ; .venv/bin/python tests/mcp_volumes.py ; .venv/bin/python tests/ui_smoke.py
.venv/bin/python tests/loadtest.py <app> <concurrency> <seconds>
```

## Go live (Hetzner)
1. `cp deploy/golive.env.example deploy/golive.env` and fill it in: `HCLOUD_TOKEN`, `ACME_EMAIL`, Stripe keys and price IDs, SMTP, seller details.
2. `python3 deploy/hetzner_golive.py` (needs `~/.ssh/id_ed25519.pub`). It creates the firewall and a CX33 `aloof-edge` in `fsn1`, waits for
   cloud-init to run `deploy/setup-box.sh`, copies `golive.env` over SSH with the server IP filled in, runs `deploy/golive.sh` (Caddy with
   Let's Encrypt), and prints the DNS records.
3. Create the DNS records it prints: `A`/`AAAA` for `aloof.si`, `www`, `api`, `mcp` and `*` pointing at the server.
4. Point the Stripe webhook at `https://api.aloof.si/v1/billing/stripe/webhook`.
CPU nodes (CX43) are then created and deleted on demand by the scheduler with the same `HCLOUD_TOKEN`.

## License
[MIT](LICENSE)
