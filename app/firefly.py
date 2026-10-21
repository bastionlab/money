"""Thin Firefly III API client: the ledger owns transactions, categories and rules."""
from __future__ import annotations

import logging
from datetime import date

import httpx

from .config import CATEGORIES, FIREFLY, firefly_token

log = logging.getLogger("firefly")


class Duplicate(Exception):
    pass


def client() -> httpx.Client:
    return httpx.Client(base_url=f"{FIREFLY}/api/v1", timeout=60, headers={
        "Authorization": f"Bearer {firefly_token()}", "Accept": "application/vnd.api+json",
        "Content-Type": "application/json"})


def _check(r: httpx.Response) -> dict:
    if r.status_code == 422 and "Duplicate" in r.text:
        raise Duplicate(r.text[:200])
    if r.status_code >= 400:
        raise RuntimeError(f"firefly {r.request.method} {r.request.url.path}: {r.status_code} {r.text[:300]}")
    return r.json() if r.content else {}


def paged(c: httpx.Client, path: str, params: dict | None = None) -> list[dict]:
    out, page = [], 1
    while True:
        j = _check(c.get(path, params={**(params or {}), "page": page, "limit": 500}))
        out += j.get("data", [])
        meta = j.get("meta", {}).get("pagination", {})
        if page >= meta.get("total_pages", 1):
            return out
        page += 1


def ensure_categories(c: httpx.Client) -> None:
    have = {d["attributes"]["name"] for d in paged(c, "/categories")}
    for name in CATEGORIES:
        if name not in have:
            _check(c.post("/categories", json={"name": name}))


def ensure_asset(c: httpx.Client, name: str, iban: str | None, currency: str = "EUR") -> str:
    for d in paged(c, "/accounts", {"type": "asset"}):
        a = d["attributes"]
        if a["name"] == name or (iban and a.get("iban") == iban):
            return d["id"]
    body = {"name": name, "type": "asset", "account_role": "defaultAsset", "currency_code": currency}
    if iban:
        body["iban"] = iban
    r = c.post("/accounts", json=body)
    if r.status_code == 422 and "currency" in r.text:
        # PayPal reports multi-currency wallets with codes Firefly has not enabled; book them in the default (EUR)
        body.pop("currency_code")
        r = c.post("/accounts", json=body)
    return _check(r)["data"]["id"]


def set_opening_balance(c: httpx.Client, account_id: str, amount: float, day: date) -> None:
    _check(c.put(f"/accounts/{account_id}", json={
        "opening_balance": f"{amount:.2f}", "opening_balance_date": day.isoformat()}))


def account_balance(c: httpx.Client, account_id: str) -> float:
    return float(_check(c.get(f"/accounts/{account_id}"))["data"]["attributes"]["current_balance"])


def add(c: httpx.Client, *, account_id: str, amount: float, day: str, payee: str, description: str,
        external_id: str, notes: str = "", category: str | None = None) -> tuple[str, str, str | None]:
    """Book one bank line. Negative = withdrawal to an expense account named after the payee.
    Returns (group id, journal id, category Firefly's rules set)."""
    tx = {"date": day, "amount": f"{abs(amount):.2f}", "description": description[:250] or payee or "—",
          "external_id": external_id, "notes": notes[:5000]}
    if amount < 0:
        tx |= {"type": "withdrawal", "source_id": account_id, "destination_name": payee or "(unbekannt)"}
    else:
        tx |= {"type": "deposit", "destination_id": account_id, "source_name": payee or "(unbekannt)"}
    if category:
        tx["category_name"] = category
    j = _check(c.post("/transactions", json={"error_if_duplicate_hash": True, "apply_rules": True,
                                              "fire_webhooks": False, "transactions": [tx]}))
    split = j["data"]["attributes"]["transactions"][0]
    return j["data"]["id"], split["transaction_journal_id"], split.get("category_name")


def set_category(c: httpx.Client, group_id: str, journal_id: str, category: str) -> None:
    _check(c.put(f"/transactions/{group_id}", json={
        "apply_rules": False, "transactions": [{"transaction_journal_id": journal_id, "category_name": category}]}))


def update(c: httpx.Client, group_id: str, journal_id: str, **fields) -> None:
    tx = {"transaction_journal_id": journal_id} | {k: f"{v:.2f}" if k == "amount" else v for k, v in fields.items()}
    _check(c.put(f"/transactions/{group_id}", json={"apply_rules": False, "transactions": [tx]}))


def delete(c: httpx.Client, group_id: str) -> None:
    r = c.delete(f"/transactions/{group_id}")
    if r.status_code not in (204, 404):
        _check(r)


def splits(c: httpx.Client, start: date, end: date) -> list[dict]:
    """All splits in a date range, flattened, each tagged with its group id."""
    out = []
    for g in paged(c, "/transactions", {"start": start.isoformat(), "end": end.isoformat(), "type": "all"}):
        for s in g["attributes"]["transactions"]:
            out.append(s | {"group_id": g["id"]})
    return out

