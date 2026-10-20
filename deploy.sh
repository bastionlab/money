#!/bin/sh
# Push the source into CT 108, refresh env, restart the API and (re)arm the sync timer.
set -e
CT=108
DIR=$(dirname "$(readlink -f "$0")")

echo "-> syncing code"
tar -C "$DIR" --exclude=__pycache__ -cf - app systemd | pct exec $CT -- tar -C /opt/money -xf -

echo "-> env"
TG=$(pct exec 107 -- grep -E '^TELEGRAM_(TOKEN|CHAT_ID)=' /etc/deals/deals.env || true)
pct exec $CT -- sh -c "umask 077; mkdir -p /etc/money; printf 'MONEY_API_TOKEN=%s\n%s\n' '$(cat "$DIR/token")' '$TG' > /etc/money/money.env"

echo "-> units"
pct exec $CT -- sh -c 'install -m644 /opt/money/systemd/money-* /etc/systemd/system/ \
  && systemctl daemon-reload && systemctl enable -q --now money-api money-sync.timer'

echo "-> restarting"
pct exec $CT -- systemctl restart money-api
sleep 2
pct exec $CT -- sh -c 'systemctl is-active money-api money-sync.timer docker | tr "\n" " "; echo'
