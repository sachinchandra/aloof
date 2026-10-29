# Aloof vs Fly.io

> Fly.io facts checked 2026-10-28 against fly.io's own docs, linked in each row. This page compares features only; see each provider's site for their prices.

Fly.io is the better pick if you need many regions and edge routing. Aloof is built for small backends that an AI assistant can deploy and run with limited, server-enforced permissions.

| | **Aloof** | **Fly.io** |
|---|---|---|
| Regions | 1 (Nuremberg, Germany) | 17, as listed 2026-10-28 ([source](https://fly.io/docs/reference/regions/)) |
| Billing | Per second while running | Per second |
| Scale to zero | Yes (on by default on Free) | Yes ([source](https://fly.io/docs/launch/autostop-autostart/)) |
| Remote MCP server | Yes, __MCP_TOOL_COUNT__ tools, OAuth with read/write/admin scopes | No. Experimental local `fly mcp server` in flyctl ([source](https://fly.io/docs/flyctl/mcp-server/)) |
| Health-gated deploys | Yes, with automatic rollback on errors or latency spikes | Yes, canary and blue-green strategies ([source](https://fly.io/docs/launch/deploy/)) |
| Managed Postgres | Yes | Yes |
| Largest machine | 8 dedicated CPU, 16 GB RAM | Up to 128 GB RAM ([source](https://fly.io/pricing)) |

## Where Fly.io is stronger
- Many regions and edge routing. Aloof runs in one region for now.
- Much bigger machines.
- A longer track record and a larger ecosystem.

## Where Aloof is stronger
- Bad releases roll back on their own, traffic routes around failing instances, and apps can scale to zero (on by default on Free).
- The MCP server has read, write and admin scopes, checked on the server. Deploy tokens can't delete anything by default.
- Release status shows what you asked for next to what is actually serving, so an assistant can't mistake an old release for a new one.

## Aloof pricing
Free costs $0 and is hard-capped at $1 of usage a month. Hobby is $5/mo and Pro $20/mo (single seat), and the plan fee comes back as included usage. Apps start at $1.99/mo always-on and Postgres at $2.99/mo. Full list on the [pricing page](https://www.aloof.si/pricing?utm_source=aloof_compare_fly&utm_medium=referral&utm_campaign=agent_discovery_w44).

First-time customers get their first month of Hobby free with code FIRST1 (then $5/mo, cancel anytime).
