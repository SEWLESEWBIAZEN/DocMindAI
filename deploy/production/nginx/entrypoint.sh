#!/bin/sh
# Start nginx with a temporary self-signed certificate if none exists yet, so the
# ACME HTTP challenge can run; reload every 6 hours to pick up renewed certificates.
set -eu
# Only DocuMind's server blocks: the stock image's default site would answer plain HTTP
# for "localhost" instead of redirecting (the runtime image also deletes it at build time).
rm -f /etc/nginx/conf.d/default.conf
live="/etc/nginx/certs/live/${SERVER_NAME}"
if [ ! -s "$live/fullchain.pem" ]; then
  echo "No certificate for ${SERVER_NAME} yet: using a temporary self-signed one."
  mkdir -p "$live"
  touch "$live/.selfsigned"
  openssl req -x509 -nodes -newkey rsa:2048 -days 2 -subj "/CN=${SERVER_NAME}" \
    -keyout "$live/privkey.pem" -out "$live/fullchain.pem" >/dev/null 2>&1
fi
# Hosts without IPv6 can't bind [::]: drop those listen lines from a working copy.
mkdir -p /tmp/templates
if [ -e /proc/net/if_inet6 ]; then
  cp /etc/nginx/templates/*.template /tmp/templates/
else
  for t in /etc/nginx/templates/*.template; do
    sed '/listen \[::\]/d' "$t" > "/tmp/templates/$(basename "$t")"
  done
fi
export NGINX_ENVSUBST_TEMPLATE_DIR=/tmp/templates
( while :; do sleep 6h; nginx -s reload; done ) &
exec /docker-entrypoint.sh nginx -g 'daemon off;'
