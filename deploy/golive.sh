#!/bin/bash
# Run ON the edge server (deploy/hetzner_golive.py does this for you on a fresh Hetzner CX33).
# Rewrites supervisord.conf environment from golive.env, renders data/Caddyfile.live (ACME, base + alt domains) and reloads.
set -e; cd "$(dirname "$0")"; [ -f golive.env ] || { echo "cp golive.env.example golive.env and fill it in"; exit 1; }; set -a; . ./golive.env; set +a
grep -v '^\s*#' golive.env | grep -qE '=\s*"?<' && { echo "fill in golive.env placeholders first (or blank them)"; exit 1; }
E="PAAS_BRAND=\"$PAAS_BRAND\",PAAS_BASE_DOMAIN=\"$PAAS_BASE_DOMAIN\",PAAS_ALT_DOMAINS=\"$PAAS_ALT_DOMAINS\",PAAS_URL_DOMAIN=\"$PAAS_URL_DOMAIN\",PAAS_API_HOST=\"$PAAS_API_HOST\",PAAS_MCP_HOST=\"$PAAS_MCP_HOST\",PAAS_PUBLIC_API_URL=\"$PAAS_PUBLIC_API_URL\",PAAS_PUBLIC_MCP_URL=\"$PAAS_PUBLIC_MCP_URL\",PAAS_PROXY_URL=\"$PAAS_PROXY_URL\",PAAS_SITE_URL=\"$PAAS_SITE_URL\",PAAS_EDGE_IPS=\"$PAAS_EDGE_IPS\",PAAS_WG_ENDPOINT=\"$PAAS_WG_ENDPOINT\",PAAS_NODE_API_URL=\"$PAAS_NODE_API_URL\",PAAS_REGISTRY=\"127.0.0.1:5000\",PAAS_WG_APPLY=\"1\",PAAS_REGISTRY_NODE=\"$PAAS_REGISTRY_NODE\",PAAS_ENV_FILE=\"$PWD/golive.env\""
# Caddyfile: global email only if set; one site block per domain
LIVE=../data/Caddyfile.live; mkdir -p ../data
G=""; [ -n "$ACME_EMAIL" ] && G="email $ACME_EMAIL"
SITE=$(sed -n '/# __SITE__ begin/,/# __SITE__ end/p' Caddyfile.prod)
{ sed -n '1,/# __SITE__ begin/p' Caddyfile.prod | sed '$d' | sed "s|__GLOBAL__|$G|"
  for d in $PAAS_BASE_DOMAIN ${PAAS_ALT_DOMAINS//,/ }; do
    if [ "$d" = "$PAAS_BASE_DOMAIN" ] && [ "${PAAS_CANONICAL_WWW:-0}" = 1 ]; then
      # www.<base> is canonical; the apex (if its DNS exists) redirects to www
      printf 'www.%s {\n\timport aloof_sec\n\treverse_proxy 127.0.0.1:8000\n}\n%s {\n\timport aloof_sec\n\tredir https://www.%s{uri} permanent\n}\napi.%s {\n\timport aloof_sec\n\treverse_proxy 127.0.0.1:8000\n}\n' "$d" "$d" "$d" "$d"
      echo "$SITE" | sed -n '/^mcp\.__D__ {/,$p' | grep -v '^# __SITE__' | sed "s|__D__|$d|g"
    else echo "$SITE" | sed "s|__D__|$d|g"; fi; done
  sed -n '/# __SITE__ end/,$p' Caddyfile.prod | sed '1d'; } > "$LIVE"
/usr/local/bin/caddy validate --config "$LIVE" --adapter caddyfile >/dev/null 2>&1 || { /usr/local/bin/caddy validate --config "$LIVE" --adapter caddyfile; exit 1; }
LIVE_ABS=$(cd ../data && pwd)/Caddyfile.live
sed -i "/^\[program:paas\]/,/^\[/ s|^environment=.*|environment=$E,HOME=\"/home/box\"|" supervisord.conf
sed -i "/^\[program:mcp\]/,/^\[/ s|^environment=.*|environment=PAAS_ENV_FILE=\"$PWD/golive.env\",ALOOF_API_URL=\"http://localhost:8000\",MCP_PUBLIC_URL=\"$MCP_PUBLIC_URL\",PAAS_BRAND=\"$PAAS_BRAND\",HOME=\"/home/box\"|" supervisord.conf
sed -i "/^\[program:caddy\]/,/^\[/ { s|--config [^ ]*|--config $LIVE_ABS|; s|^environment=.*|environment=XDG_DATA_HOME=\"/var/lib/aloof/caddy\",XDG_CONFIG_HOME=\"/var/lib/aloof/caddy\"| }" supervisord.conf
sudo mkdir -p /var/lib/aloof/caddy
# containers must not reach the cloud metadata service (persistent across reboots and Docker restarts)
if command -v systemctl >/dev/null && [ -d /run/systemd/system ]; then
  sudo install -m 755 metadata-block.sh /usr/local/sbin/aloof-metadata-block.sh
  sudo install -m 644 aloof-metadata-block.service /etc/systemd/system/aloof-metadata-block.service
  sudo systemctl daemon-reload && sudo systemctl reenable aloof-metadata-block.service >/dev/null 2>&1 && sudo systemctl restart aloof-metadata-block.service
fi
sudo supervisorctl -c supervisord.conf reread && sudo supervisorctl -c supervisord.conf update
sudo supervisorctl -c supervisord.conf restart paas mcp caddy
