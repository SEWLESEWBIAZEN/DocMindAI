#!/bin/sh
# Obtain and renew the Let's Encrypt certificate with the HTTP-01 webroot challenge.
# nginx serves /.well-known/acme-challenge/ from the shared webroot and reloads
# every 6 hours, which picks up renewed certificates.
set -eu
: "${SERVER_NAME:?}" "${LETSENCRYPT_EMAIL:?}"
live="/etc/letsencrypt/live/${SERVER_NAME}"
staging=""
[ "${LETSENCRYPT_STAGING:-false}" = "true" ] && staging="--staging"

if [ -f "$live/.selfsigned" ]; then
  # Remove nginx's temporary certificate so certbot can create its own lineage.
  rm -rf "$live" "/etc/letsencrypt/renewal/${SERVER_NAME}.conf"
fi
if [ ! -s "$live/fullchain.pem" ]; then
  sleep 10  # let nginx come up
  certbot certonly --webroot -w /var/www/acme -d "${SERVER_NAME}" \
    --email "${LETSENCRYPT_EMAIL}" --agree-tos --no-eff-email --non-interactive $staging
fi
while :; do
  certbot renew --webroot -w /var/www/acme --quiet $staging || echo "renewal attempt failed; will retry"
  sleep 12h
done
