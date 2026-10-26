"""Categorization: Firefly rules first, then what you taught it (payee memory), then qwen on Ollama (Local AI).

`think: false` is load-bearing: thinking costs ~25x the tokens for a one-word answer.
"""
from __future__ import annotations

import json
import logging
import re

import httpx

from .config import CATEGORIES, MODEL, OLLAMA

log = logging.getLogger("ai")

SYSTEM = """Du ordnest Kontobewegungen eines Privatkontos in Deutschland einer Kategorie zu.
Antworte pro Buchung mit genau einer Kategorie aus dieser Liste, in derselben Reihenfolge:
{cats}

Hinweise:
- PayPal-Lastschriften nennen den Händler im Verwendungszweck ("Ihr Einkauf bei X") – nach X einordnen.
- REWE, Edeka, Lidl, Aldi, Netto, Kaufland, Penny, dm, Rossmann -> Lebensmittel
- Lieferando, Wolt, Uber Eats, Restaurants, Cafés, Bars -> Restaurant & Lieferdienst
- Miete, Hausverwaltung, Nebenkosten -> Wohnen; Strom, Gas, Internet, Handy -> Strom & Internet
- BVG, Deutsche Bahn, Tanken, Sharing, Uber/Bolt -> Mobilität
- Netflix, Spotify, Apple, Google, Software, Cloud, Mitgliedschaften -> Abos & Software
- Lohn/Gehalt -> Gehalt; Überweisung an/von eigenes Konto, Trade Republic Einzahlung -> Umbuchung
- Wenn unklar -> Sonstiges"""

SCHEMA = {"type": "object", "properties": {"categories": {"type": "array", "items": {"type": "string", "enum": CATEGORIES}}},
          "required": ["categories"]}


def norm_payee(s: str) -> str:
    """'PayPal Europe S.a.r.l. … Ihr Einkauf bei Spotify' -> 'spotify'; strips ids, dates, legal suffixes."""
    s = (s or "").lower()
    m = re.search(r"(?:ihr einkauf bei|einkauf bei|zahlung an)\s+([^,./]+)", s)
    if m:
        s = m.group(1)
    s = re.sub(r"\b(gmbh|ag|se|kg|e\.?v\.?|s\.?a\.?r\.?l\.?|ltd|inc|co|europe|deutschland|germany)\b", " ", s)
    s = re.sub(r"[0-9]{3,}|[^a-zäöüß ]", " ", s)
    return " ".join(s.split()[:3])


INTERMEDIARIES = re.compile(r"paypal|klarna|sofort|amazon payments|stripe|adyen|mollie", re.I)


def merchant(payee: str, purpose: str) -> str:
    """PayPal/Klarna debits name the real shop in the purpose ('… Ihr Einkauf bei Spotify AB') -- book that as payee."""
    if INTERMEDIARIES.search(payee or "") or not payee:
        m = re.search(r"(?:ihr einkauf bei|einkauf bei|zahlung an)\s+(.+?)(?:\s*[,;]|\s+\d{6,}|$)", purpose or "", re.I)
        if m:
            return m.group(1).strip()[:80]
    return payee


def categorize(items: list[dict]) -> list[str]:
    """items: {payee, purpose, amount}. Returns one category per item (Sonstiges on any failure)."""
    out: list[str] = []
    for i in range(0, len(items), 12):
        out += _batch(items[i:i + 12])
    return out


def _batch(items: list[dict]) -> list[str]:
    lines = "\n".join(f"{n + 1}. {it['amount']:+.2f} € | {it['payee'][:60]} | {it['purpose'][:140]}"
                      for n, it in enumerate(items))
    body = {"model": MODEL, "stream": False, "think": False, "format": SCHEMA, "options": {"temperature": 0},
            "messages": [{"role": "system", "content": SYSTEM.format(cats=", ".join(CATEGORIES))},
                         {"role": "user", "content": lines}]}
    try:
        r = httpx.post(f"{OLLAMA}/api/chat", json=body, timeout=180)
        r.raise_for_status()
        cats = json.loads(r.json()["message"]["content"])["categories"]
    except Exception as e:  # noqa: BLE001 - the model being down just means "Sonstiges" for now
        log.warning("qwen: %s", e)
        return ["Sonstiges"] * len(items)
    if len(cats) != len(items):
        return [_batch([it])[0] for it in items] if len(items) > 1 else ["Sonstiges"]
    return [c if c in CATEGORIES else "Sonstiges" for c in cats]
