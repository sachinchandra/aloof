# Aloof

**Cheap global GPUs and backends, billed per second in US dollars.** Deploy a Docker image or a git repo with a Dockerfile and get HTTPS at
`https://<app>.aloof.si`, autoscaling down to zero, persistent volumes, custom domains and simple USD billing through Stripe.
It stays cheap because the capacity comes from low-cost Indian clouds (E2E Networks, Jarvislabs, Ola Krutrim), AWS Mumbai spot, or your own nodes.
AI assistants can deploy for you over MCP.

> Status: working prototype. Everything below runs end to end on a single box. Going live needs real provider keys, Stripe, SMTP and DNS (see [Go live](#go-live)).

## Regions
All compute runs in **India** today: Mumbai, Delhi NCR, Chennai and Bengaluru. That's ideal for GPU inference and batch jobs, side projects,
and APIs serving South Asia. Expect roughly 120–250 ms RTT from Europe and the US. Other regions will only be listed once they actually exist.

## Features
- **Deploy** from an image or git (`aloof deploy`, REST, dashboard or MCP), with rolling redeploys, env vars (encrypted at rest) and health checks with auto-replace.
- **Autoscaling and scale to zero**: concurrency- and CPU-based. Idle apps sleep and the first request wakes them (≈1 s on CPU) while it's held at the edge.
- **Volumes** that survive redeploys and restarts, pinned to the node they live on.
- **Custom domains**: TXT ownership check plus CNAME/A routing, with automatic TLS through Caddy (Let's Encrypt in production).
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
| GPU per hour | T4 spot $0.55 · L4 $1.10 · L40S $2.19 · A100 80GB $3.99 · H100 $5.49 | Render/Railway none; Fly retired GPUs |
| Volumes / egress | $0.22 per GB-month / $0.06 per GB | Fly $0.15 / $0.02–0.12 · Render $0.25 / $0.15 · Railway $0.15 / $0.05 |

The full table is at `/pricing`. `paas/plans.py` enforces a 30% minimum margin after payment fees, against worst-case costs converted at a
pessimistic ₹83/$ (`PAAS_COST_USD_INR`). A test fails if any plan or rate falls under it, and the scheduler refuses pools that would.

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
Tools: `list_plans, signup, deploy_app, get_status, list_apps, get_logs, scale_app, set_env, destroy_app, get_usage, create_volume, list_volumes, attach_volume, detach_volume, set_autoscale, add_domain`.

## Layout
```
paas/api.py           REST API, dashboard/site routes, edge proxy
paas/engine.py        deploy/scale/volumes, scheduling (pick_provider), accounts
paas/controller.py    autoscaler + health loops
paas/plans.py         plans (USD), tiers, rates, cost model, fees, 30% margin floor
paas/billing.py       USD wallet (cents), metering, free-tier cap
paas/stripe_billing.py  Stripe Checkout, subscriptions, portal, webhooks, Stripe Tax
paas/razorpay_legacy.py optional INR/UPI top-ups + GST invoices (off by default)
paas/drivers/         local_docker, remote (node agent + registry), providers (E2E/Jarvislabs/Krutrim/AWS/self-hosted), wg
paas/agent/agent.py   node agent (Docker + NVIDIA runtime) on remote VMs
mcp_server/           MCP server + OAuth 2.1 authorization server
dashboard/, cli/, deploy/, scripts/, tests/
```

## Configuration and secrets
Nothing secret is committed. At first start the box creates `data/secret.key` (Fernet key for encryption at rest), `data/admin_token` and
`data/database_url`; `data/` is git-ignored. In production, `deploy/golive.sh` points `PAAS_ENV_FILE` at `deploy/golive.env` (also git-ignored), which holds the Stripe/SMTP/provider secrets. The environment variables `PAAS_SECRET_KEY` (comma list `new,old` for rotation, then run
`scripts/rotate_secret_key.py`), `PAAS_ADMIN_TOKEN` and `PAAS_DB_URL` override those files. Stripe (`STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_HOBBY`, `STRIPE_PRICE_PRO`, `STRIPE_TAX`), provider and SMTP settings are documented in
[`deploy/golive.env.example`](deploy/golive.env.example). A provider pool activates once its keys are set (`GET /v1/providers`).

## Tests
```bash
sg docker -c '.venv/bin/python -m pytest -q tests/'        # unit + mocked-cloud node lifecycle (real Docker)
.venv/bin/python tests/oauth_e2e.py ; .venv/bin/python tests/oauth_password.py
.venv/bin/python tests/mcp_e2e.py ; .venv/bin/python tests/mcp_volumes.py ; .venv/bin/python tests/ui_smoke.py
.venv/bin/python tests/loadtest.py <app> <concurrency> <seconds>
```

## Go live
`cp deploy/golive.env.example deploy/golive.env`, fill it in (edge IP, ACME email, Stripe keys and price IDs, SMTP, provider keys), point the Stripe webhook at `https://api.aloof.si/v1/billing/stripe/webhook`, point DNS
`A aloof.si, api, mcp, *` at the edge, then run `deploy/golive.sh` on the edge server.

## License
[MIT](LICENSE)
