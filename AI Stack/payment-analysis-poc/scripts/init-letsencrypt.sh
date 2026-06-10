#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# init-letsencrypt.sh
# Run ONCE on the VM after docker compose up to obtain the first SSL cert.
#
# Usage:
#   chmod +x scripts/init-letsencrypt.sh
#   ./scripts/init-letsencrypt.sh yourdomain.com your@email.com
# ─────────────────────────────────────────────────────────────────────────────

set -e

DOMAIN=${1:?"Usage: $0 <domain> <email>  e.g.  $0 payments.example.com admin@example.com"}
EMAIL=${2:?"Usage: $0 <domain> <email>"}
NGINX_CONF="nginx/conf.d/app.conf"
COMPOSE_FILE="docker-compose.yml"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Domain : $DOMAIN"
echo " Email  : $EMAIL"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── 1. Patch domain placeholder in nginx config ───────────────────────────────
echo "→ Patching nginx config with domain '$DOMAIN' …"
sed -i "s/YOUR_DOMAIN/$DOMAIN/g" "$NGINX_CONF"

# ── 2. Download recommended TLS params from Let's Encrypt ────────────────────
echo "→ Downloading recommended TLS options …"
mkdir -p certbot/conf
if [ ! -f certbot/conf/options-ssl-nginx.conf ]; then
    curl -s https://raw.githubusercontent.com/certbot/certbot/master/certbot-nginx/certbot_nginx/_internal/tls_configs/options-ssl-nginx.conf \
        -o certbot/conf/options-ssl-nginx.conf
fi
if [ ! -f certbot/conf/ssl-dhparams.pem ]; then
    curl -s https://raw.githubusercontent.com/certbot/certbot/master/certbot/certbot/ssl-dhparams.pem \
        -o certbot/conf/ssl-dhparams.pem
fi

# ── 3. Start nginx in HTTP-only mode (needs to be up for ACME challenge) ──────
echo "→ Starting nginx (HTTP only) …"
docker compose up -d nginx

echo "→ Waiting for nginx to be ready …"
sleep 5

# ── 4. Request certificate ────────────────────────────────────────────────────
echo "→ Requesting Let's Encrypt certificate …"
docker compose run --rm certbot certonly \
    --webroot \
    --webroot-path=/var/www/certbot \
    --email "$EMAIL" \
    --agree-tos \
    --no-eff-email \
    -d "$DOMAIN"

# ── 5. Uncomment the HTTPS server block in nginx config ───────────────────────
echo "→ Enabling HTTPS block in nginx config …"
# Remove the comment markers around the HTTPS block
sed -i '/# HTTPS_BLOCK_START/,/# HTTPS_BLOCK_END/{
    /# HTTPS_BLOCK_START/d
    /# HTTPS_BLOCK_END/d
    s/^# //
}' "$NGINX_CONF"

# ── 6. Reload nginx to pick up the new cert and HTTPS config ──────────────────
echo "→ Reloading nginx …"
docker compose exec nginx nginx -s reload

echo ""
echo "✅ Done! Your app is now live at https://$DOMAIN"
echo ""
echo "To auto-renew certs, add this cron job (runs twice daily as recommended):"
echo "  0 0,12 * * * cd $(pwd) && docker compose run --rm certbot renew && docker compose exec nginx nginx -s reload"
