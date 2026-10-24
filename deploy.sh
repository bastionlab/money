#!/bin/sh
# Push the source into the money CT, refresh its env, restart the API and (re)arm the sync timer.
# The CT and the addresses it needs come from the dashboard's /etc/dash/config.json (DASH_CT overrides the CT);
# a Firefly token set on the dashboard's Settings → Money is handed into the CT.
set -e
DIR=$(dirname "$(readlink -f "$0")")
CONF=/etc/dash/config.json
c() { jq -r "($1) // empty" "$CONF"; }
CT=${DASH_CT:-$(c .cts.money.id)}
[ -n "$CT" ] || { echo "no cts.money in $CONF" >&2; exit 1; }

echo "-> syncing code (CT $CT)"
tar -C "$DIR" --exclude=__pycache__ -cf - app systemd requirements.txt | pct exec "$CT" -- tar -C /opt/money -xf -
pct exec "$CT" -- sh -c '/opt/money/venv/bin/pip install -q -r /opt/money/requirements.txt'

echo "-> env"
pct exec "$CT" -- sh -c "umask 077; mkdir -p /etc/money; printf 'MONEY_API_TOKEN=%s\nMONEY_OLLAMA_URL=http://%s:11434\nMONEY_API_ALLOW=%s,%s,127.0.0.1\nMONEY_REDIRECT_URL=https://dash.%s/money\n' \
  '$(cat "$DIR/token")' '$(c .cts.ai.ip)' '$(c .cts.dash.ip)' '$(c .lan.host_ip)' '$(c .tailnet)' > /etc/money/money.env"
FT=$(jq -r '.firefly_token.value // empty' /etc/dash/secrets/money.json 2>/dev/null || true)
if [ -n "$FT" ]; then
  printf '%s' "$FT" | pct exec "$CT" -- sh -c 'umask 077; cat > /etc/money/firefly_token'
  echo "   firefly token from Settings"
fi

echo "-> units"
pct exec "$CT" -- sh -c 'install -m644 /opt/money/systemd/money-* /etc/systemd/system/ \
  && systemctl daemon-reload && systemctl enable -q --now money-api money-sync.timer'

echo "-> restarting"
pct exec "$CT" -- systemctl restart money-api
sleep 2
pct exec "$CT" -- sh -c 'systemctl is-active money-api money-sync.timer docker | tr "\n" " "; echo'
