#!/bin/sh
# First-time setup of a fresh Debian CT for the money backend: Docker + Firefly III, the API's venv and dirs.
# Run once by the dashboard's backend-install (DASH_CT = the new CT, which needs features nesting=1,keyctl=1),
# then deploy.sh. Safe to run again: an existing Firefly .env (its APP_KEY!) is never replaced.
# After it: open Firefly (http://<ct ip>:8080), create your user, Profile → OAuth → Personal access token,
# and paste it on the dashboard under Settings → Money → Firefly token.
set -e
DIR=$(dirname "$(readlink -f "$0")")
CT=${DASH_CT:?DASH_CT not set}
[ -s "$DIR/token" ] || (umask 077; openssl rand -hex 32 > "$DIR/token")
IP=$(pct exec "$CT" -- hostname -I | awk '{print $1}')
TZ=$(timedatectl show -p Timezone --value 2>/dev/null || echo Europe/Berlin)
pct exec "$CT" -- sh -c 'export DEBIAN_FRONTEND=noninteractive
  apt-get update -q && apt-get install -y -q python3 python3-venv ca-certificates docker.io docker-compose >/dev/null
  mkdir -p /opt/money /opt/firefly/upload /opt/firefly/database /etc/money && chmod 700 /etc/money
  chown -R 33:33 /opt/firefly/upload /opt/firefly/database
  [ -x /opt/money/venv/bin/pip ] || python3 -m venv /opt/money/venv'
pct push "$CT" "$DIR/firefly/docker-compose.yml" /opt/firefly/docker-compose.yml --perms 600
pct exec "$CT" -- sh -c "[ -f /opt/firefly/.env ] || (umask 077; printf 'APP_KEY=%s\nAPP_ENV=production\nAPP_DEBUG=false\nSITE_OWNER=admin@example.com\nDEFAULT_LANGUAGE=de_DE\nDEFAULT_LOCALE=equal\nTZ=%s\nTRUSTED_PROXIES=**\nDB_CONNECTION=sqlite\nAPP_URL=http://%s:8080\nLOG_CHANNEL=stack\nAPP_LOG_LEVEL=notice\n' \"\$(head -c 24 /dev/urandom | base64 | tr -dc A-Za-z0-9 | head -c 32)\" '$TZ' '$IP' > /opt/firefly/.env)
  cd /opt/firefly && docker compose up -d >/dev/null"
echo "money CT $CT ready for deploy.sh; Firefly at http://$IP:8080 (create a user + token, then Settings → Money)"
