"""Paths and endpoints. Secrets live in /etc/money (root-only), state in /var/lib/money."""
from __future__ import annotations

import json
import os
from pathlib import Path

ETC = Path(os.environ.get("MONEY_ETC", "/etc/money"))
STATE = Path(os.environ.get("MONEY_STATE", "/var/lib/money"))
DB = STATE / "money.db"
SECRETS = ETC / "secrets.json"
TR_COOKIES = STATE / "tr-cookies.txt"

FIREFLY = os.environ.get("FIREFLY_URL", "http://127.0.0.1:8080")
FIREFLY_TOKEN_FILE = ETC / "firefly_token"

OLLAMA = os.environ.get("MONEY_OLLAMA_URL", "http://192.168.1.55:11434")  # deploy.sh sets it from the dashboard config
MODEL = "qwen3.5:9b"

# Enable Banking sends the browser back here after the bank login; it must be whitelisted in the EB app.
REDIRECT_URL = os.environ.get("MONEY_REDIRECT_URL", "https://dash.example.ts.net/money")
# PSD2 caps consents at 180 days; ask for the max and remind a week before it runs out.
CONSENT_DAYS = 180
REMIND_DAYS = 7
# first sync of a fresh account reaches this far back
HISTORY_DAYS = 400

# Categories the ledger is seeded with. Umbuchung + Investments are excluded from spending.
CATEGORIES = [
    "Lebensmittel", "Restaurant & Lieferdienst", "Wohnen", "Strom & Internet", "Mobilität",
    "Shopping", "Abos & Software", "Freizeit", "Gesundheit", "Versicherungen", "Reisen",
    "Bargeld", "Gebühren", "Bildung", "Geschenke & Spenden", "Steuern",
    "Gehalt", "Sonstige Einnahmen", "Investments", "Umbuchung", "Sonstiges",
]
NOT_SPENDING = {"Umbuchung", "Investments"}


def secrets() -> dict:
    try:
        return json.loads(SECRETS.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_secrets(data: dict) -> None:
    ETC.mkdir(parents=True, exist_ok=True)
    tmp = SECRETS.with_suffix(".tmp")
    old = os.umask(0o077)
    try:
        tmp.write_text(json.dumps(data, indent=1))
    finally:
        os.umask(old)
    tmp.replace(SECRETS)


def firefly_token() -> str:
    return FIREFLY_TOKEN_FILE.read_text().strip()
