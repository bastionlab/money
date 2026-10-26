# Bastion Money

The backend for the Money add-on of [Bastion](https://github.com/bastionlab/core), a phone-first dashboard
for a home server on Proxmox VE. It runs in a container of its own: [Firefly III](https://www.firefly-iii.org)
keeps the ledger, and a small Python service pulls in your bank accounts and Trade Republic, sorts every
booking into a category with a local model, and serves the numbers to the dashboard's /money page.

> **Read this before you connect a bank.**
> - Trade Republic access uses [pytr](https://github.com/pytr-org/pytr), an **unofficial** API. Trade
>   Republic does not support it and can break or block it at any time.
> - Bank access goes through [Enable Banking](https://enablebanking.com). Its free mode is for reading
>   **your own** accounts, for personal use. Check its terms yourself.
> - Your financial data stays on your server. Nothing goes to a cloud service except the bank and broker
>   connections themselves and the Discord webhooks you set up. You are responsible for it.

## Features

- **Bank sync**: any bank Enable Banking supports (built and tested with DKB and PayPal), via PSD2.
- **Trade Republic**: cash account, trades and portfolio. Log in with one tap in the TR app.
- **Categories by a local model**: a qwen model on Ollama sorts new bookings. Your own choices win:
  a rule you set for a payee, then what you picked for that payee before, then the model.
- **Own transfers**: money between your own accounts is paired and kept out of spending. A PayPal purchase
  is matched with the bank debit that paid for it, so it counts once.
- **Cash split**: a supermarket payment with cash-back (say 205.98 €) is split into groceries and cash.
- **Subscriptions (Abos)**: recurring payments are found by rhythm and amount. You can also pin one by hand
  from a booking, add regular income (salary, allowance) and net friends' shares against an Abo.
- **Depot history**: holdings rebuilt from every trade, times price history, like the chart in the TR app.
- **Net worth**, a **month-end forecast**, **unusual charges** (far above normal, or charged twice in a day)
  and a **month report**.
- **Search, payee aliases, CSV export** of what is on screen or a whole year.
- **Discord notifications**: new Abos, price changes, unusual charges, the month report, consents about to
  expire and failed syncs.

## How sync works

A systemd timer runs one sync three times a day (07:15, 13:15 and 19:15, plus up to 10 minutes random
delay). PSD2 allows four unattended pulls per account per day, so this leaves room for one "sync now" tap.

One run does this, in order:

1. Pull new bookings from every linked bank (Enable Banking) and from Trade Republic into Firefly. The first
   sync of an account reaches back 400 days and sets the opening balance so Firefly matches the bank.
2. Categorize the new bookings and pair transfers between your own accounts.
3. Split cash-back, refresh subscriptions, look for unusual charges.
4. Snapshot the portfolio and rebuild the depot history.
5. Remind you of consents that run out soon. In the first days of a month, send last month's report.

Every booking is remembered by its provider id, so a re-poll never books it twice. One failing source does
not stop the others.

**Bank consent.** PSD2 caps a consent at 180 days. The service asks for the maximum and sends a reminder a
week before it runs out. Renewing is one bank login from the dashboard.

**Trade Republic login.** TR caps a login at 24 hours. There is no automatic login prompt: when the session
has expired, the account shows "login needed" and you log in from the dashboard when you want fresh data.

## Layout

```
app/          the service: api.py (FastAPI for the dashboard), sync.py (one sync run),
              eb.py, tr.py, firefly.py (sources and ledger), ai.py (categories),
              subs.py, cashback.py, depot.py, insights.py, notify.py, store.py, config.py
firefly/      docker-compose.yml for Firefly III
systemd/      money-api.service, money-sync.service, money-sync.timer
bootstrap.sh  first-time setup of a fresh container
deploy.sh     push the code, write the env, restart
```

## Install

**From Bastion (recommended).** Install Money from the Add-ons page. It creates a Debian container
(2 GB RAM, 12 GB disk by default, with nesting and keyctl for Docker), clones this repo and runs
`bootstrap.sh` and `deploy.sh`. It needs the Local AI add-on for categories.

**By hand**, on the Proxmox host as root, for an existing Debian container:

```sh
git clone https://github.com/bastionlab/money && cd money
DASH_CT=<ct id> ./bootstrap.sh   # Docker, Firefly III, venv, dirs
DASH_CT=<ct id> ./deploy.sh      # code, env, systemd units
```

`bootstrap.sh` is safe to run again; it never replaces Firefly's existing `.env` (and its app key). Without
`DASH_CT`, `deploy.sh` takes the container from Bastion's config.

Then:

1. Open Firefly on port 8080 of the container, create your user, and make a personal access token
   (Profile → OAuth → Personal access tokens).
2. Paste it in Bastion under Settings → Money and run `deploy.sh` again (Bastion does this for you).
3. Connect banks and Trade Republic from the Money settings. You need your own Enable Banking app.

## Config

- `deploy.sh` reads Bastion's `/etc/dash/config.json` for the container, the Local AI address, the
  addresses allowed to call the API and the dashboard URL that Enable Banking sends you back to. It writes
  them to `/etc/money/money.env` inside the container.
- Secrets live in `/etc/money` inside the container, readable by root only: the API token, the Firefly
  token, your Enable Banking key and Trade Republic login. Bank secrets can be set from the dashboard but
  are never sent back.
- The API listens on port 8090. It only answers allowed addresses with the right bearer token.
- State (sync log, links, subscriptions, portfolio, TR cookies) is a SQLite database in `/var/lib/money`.
- Discord webhooks come from Bastion (`/etc/discord.json`, channels `money` and `alerts`).
- Category names and messages are in German for now.

## Development

The code runs from `/opt/money` in the container with a venv in `/opt/money/venv`. Pinned dependencies are
in `requirements.txt`. After an edit, run `deploy.sh` to push it and restart the API.

```sh
pct exec <ct id> -- systemctl start money-sync              # one sync now
pct exec <ct id> -- journalctl -u money-sync -n 50          # what it did
pct exec <ct id> -- sh -c 'cd /opt/money && venv/bin/python -m app.cashback undo'   # undo cash splits
```

## License

[GNU AGPL-3.0](LICENSE), like the Bastion core.
