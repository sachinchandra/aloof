#!/bin/bash
# Run ON the edge server (deploy/hetzner_golive.py does this for you on a fresh Hetzner CX33).
# Rewrite supervisord.conf environment from golive.env and switch Caddy to the ACME config. Run on the public edge server.
set -e; cd "$(dirname "$0")"; [ -f golive.env ] || { echo "cp golive.env.example golive.env and fill it in"; exit 1; }; set -a; . ./golive.env; set +a
grep -q '<' golive.env && { echo "fill in golive.env placeholders first"; exit 1; }
E="PAAS_BRAND=\"$PAAS_BRAND\",PAAS_BASE_DOMAIN=\"$PAAS_BASE_DOMAIN\",PAAS_API_HOST=\"$PAAS_API_HOST\",PAAS_MCP_HOST=\"$PAAS_MCP_HOST\",PAAS_PUBLIC_API_URL=\"$PAAS_PUBLIC_API_URL\",PAAS_PUBLIC_MCP_URL=\"$PAAS_PUBLIC_MCP_URL\",PAAS_PROXY_URL=\"$PAAS_PROXY_URL\",PAAS_EDGE_IPS=\"$PAAS_EDGE_IPS\",PAAS_WG_ENDPOINT=\"$PAAS_WG_ENDPOINT\",PAAS_NODE_API_URL=\"$PAAS_NODE_API_URL\",PAAS_REGISTRY=\"127.0.0.1:5000\",PAAS_REGISTRY_NODE=\"$PAAS_REGISTRY_NODE\",PAAS_ENV_FILE=\"$PWD/golive.env\""
sed -i "/^\[program:paas\]/,/^\[/ s|^environment=.*|environment=$E,HOME=\"/home/box\"|" supervisord.conf
sed -i "/^\[program:mcp\]/,/^\[/ s|^environment=.*|environment=PAAS_ENV_FILE=\"$PWD/golive.env\",ALOOF_API_URL=\"http://localhost:8000\",MCP_PUBLIC_URL=\"$MCP_PUBLIC_URL\",PAAS_BRAND=\"$PAAS_BRAND\",HOME=\"/home/box\"|" supervisord.conf
sed -i "/^\[program:caddy\]/,/^\[/ { s|Caddyfile --adapter|Caddyfile.prod --adapter|; s|^environment=.*|environment=ACME_EMAIL=\"$ACME_EMAIL\",PAAS_API_HOST=\"$PAAS_API_HOST\",PAAS_MCP_HOST=\"$PAAS_MCP_HOST\",XDG_DATA_HOME=\"/var/lib/aloof/caddy\"| }" supervisord.conf
sudo supervisorctl -c supervisord.conf reread && sudo supervisorctl -c supervisord.conf update
