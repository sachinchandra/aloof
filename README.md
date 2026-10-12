# Aloof

**Backend and GPU hosting for India, billed per second in rupees.** Deploy a Docker image or a git repo with a Dockerfile and get HTTPS at
`https://<app>.aloof.si`, autoscaling down to zero, persistent volumes, custom domains and INR billing with GST invoices.
Capacity comes from Indian clouds (E2E Networks, Jarvislabs, Ola Krutrim), AWS Mumbai spot, or your own nodes. AI assistants can deploy for you over MCP.

> Status: working prototype. Everything below runs end to end on a single box. Going live needs real provider keys, Razorpay, SMTP and DNS (see [Go live](#go-live)).

## Features
- **Deploy** from an image or git (`aloof deploy`, REST, dashboard or MCP), with rolling redeploys, env vars (encrypted at rest) and health checks with auto-replace.
- **Autoscaling and scale to zero**: concurrency- and CPU-based. Idle apps sleep and the first request wakes them (≈1 s on CPU) while it's held at the edge.
- **Volumes** that survive redeploys and restarts, pinned to the node they live on.
- **Custom domains**: TXT ownership check plus CNAME/A routing, with automatic TLS through Caddy (Let's Encrypt in production).
- **Multi-cloud scheduler**: bin-packs CPU apps, gives each GPU app a dedicated node, and refuses any pool where a plan would fall under the margin floor. Nodes join over WireGuard and run `paas/agent/agent.py`.
- **Accounts**: email + password, magic links, session cookies with a CSRF header, and API keys stored only as SHA-256 hashes.
- **Billing**: prepaid INR wallet, Razorpay Checkout and webhooks, GST invoices (CGST+SGST or IGST), and per-second metering of compute, volumes (GB-month) and egress.
- **MCP server** (stdio and streamable HTTP with OAuth 2.1: DCR, PKCE, RFC 9728), plus `/llms.txt` and an OpenAPI spec.

## Pricing (INR excluding 18% GST; USD at ₹88)
| | Price | |
|---|---|---|
| Free | ₹0 | 3 apps, shared-1x-256mb/512mb, sleeps after 5 min idle, hard-capped at ₹100 of usage a month |
| Hobby / Pro | ₹399 / ₹1,699 a month | the fee comes back as included usage (Railway model) |
| Shared CPU | ₹179 to ₹4,199 a month | 256 MB to 8 GB, priced at or below Fly.io |
| Performance CPU | ₹2,199 to ₹21,999 a month | 1 to 8 dedicated vCPU |
| GPU per hour | T4 spot ₹39 · L4 ₹72 · L40S ₹149 · A100 80GB ₹279 · H100 ₹369 | per second, scale to zero |
| Volumes / egress | ₹16 per GB-month / ₹4 per GB beyond the allowance | |

The full table and the competitor comparison are at `/pricing`. Cost basis and margins are in [PLAN.md](PLAN.md).
`paas/plans.py` enforces a 30% minimum gross margin against the worst-case cost of every pool, and a test fails if any plan falls under it.

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
paas/plans.py         plans, tiers, rates, cost model, 30% margin floor
paas/billing.py       Razorpay, GST invoices, metering, free-tier cap
paas/drivers/         local_docker, remote (node agent + registry), providers (E2E/Jarvislabs/Krutrim/AWS/self-hosted), wg
paas/agent/agent.py   node agent (Docker + NVIDIA runtime) on remote VMs
mcp_server/           MCP server + OAuth 2.1 authorization server
dashboard/, cli/, deploy/, scripts/, tests/
```

## Configuration and secrets
Nothing secret is committed. At first start the box creates `data/secret.key` (Fernet key for encryption at rest), `data/admin_token` and
`data/database_url`; `data/` is git-ignored. The environment variables `PAAS_SECRET_KEY` (comma list `new,old` for rotation, then run
`scripts/rotate_secret_key.py`), `PAAS_ADMIN_TOKEN` and `PAAS_DB_URL` override those files. Provider, Razorpay and SMTP settings are documented in
[`deploy/golive.env.example`](deploy/golive.env.example). A provider pool activates once its keys are set (`GET /v1/providers`).

## Tests
```bash
sg docker -c '.venv/bin/python -m pytest -q tests/'        # unit + mocked-cloud node lifecycle (real Docker)
.venv/bin/python tests/oauth_e2e.py ; .venv/bin/python tests/oauth_password.py
.venv/bin/python tests/mcp_e2e.py ; .venv/bin/python tests/mcp_volumes.py ; .venv/bin/python tests/ui_smoke.py
.venv/bin/python tests/loadtest.py <app> <concurrency> <seconds>
```

## Go live
`cp deploy/golive.env.example deploy/golive.env`, fill it in (edge IP, ACME email, Razorpay, SMTP, provider keys), point DNS
`A aloof.si, api, mcp, *` at the edge, then run `deploy/golive.sh` on the edge server.

## License
[MIT](LICENSE)
