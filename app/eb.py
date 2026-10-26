"""Enable Banking (PSD2 aggregator). Free 'restricted' mode reads your own accounts once they are
linked in the EB control panel. A consent lasts up to 180 days; after that one bank login renews it."""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import jwt

from .config import CONSENT_DAYS, REDIRECT_URL, secrets

API = "https://api.enablebanking.com"


def configured() -> bool:
    s = secrets().get("eb", {})
    return bool(s.get("app_id") and s.get("key_pem"))


def _token() -> str:
    s = secrets()["eb"]
    now = int(time.time())
    return jwt.encode({"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": now, "exp": now + 3600},
                      s["key_pem"], algorithm="RS256", headers={"kid": s["app_id"]})


def _req(method: str, path: str, **kw) -> dict:
    r = httpx.request(method, API + path, headers={"Authorization": f"Bearer {_token()}"}, timeout=60, **kw)
    if r.status_code >= 400:
        try:
            msg = r.json().get("message") or r.text
        except ValueError:
            msg = r.text
        raise RuntimeError(f"Enable Banking {path.split('?')[0]}: {r.status_code} {str(msg)[:300]}")
    return r.json()


def application() -> dict:
    return _req("GET", "/application")


def aspsps(country: str = "DE") -> list[dict]:
    return [{"name": a["name"], "country": a["country"], "logo": a.get("logo"),
             "psu_types": a.get("psu_types", []), "max_seconds": a.get("maximum_consent_validity")}
            for a in _req("GET", "/aspsps", params={"country": country})["aspsps"]]


def start(aspsp: str, country: str = "DE", max_seconds: int | None = None) -> dict:
    """Begin a bank login; the browser goes to `url` and comes back to REDIRECT_URL?code=…&state=…"""
    if not REDIRECT_URL:
        raise RuntimeError("MONEY_REDIRECT_URL is not set (deploy.sh writes it to /etc/money/money.env)")
    state = uuid.uuid4().hex
    secs = min(CONSENT_DAYS * 86400, max_seconds or CONSENT_DAYS * 86400) - 300
    until = (datetime.now(timezone.utc) + timedelta(seconds=secs)).isoformat(timespec="seconds")
    j = _req("POST", "/auth", json={"access": {"valid_until": until}, "aspsp": {"name": aspsp, "country": country},
                                    "state": state, "redirect_url": REDIRECT_URL, "psu_type": "personal"})
    return {"url": j["url"], "state": state}


def finish(code: str) -> dict:
    return _req("POST", "/sessions", json={"code": code})


def session(session_id: str) -> dict:
    return _req("GET", f"/sessions/{session_id}")


def account_details(uid: str) -> dict:
    return _req("GET", f"/accounts/{uid}/details")


def balance(uid: str) -> float | None:
    bals = _req("GET", f"/accounts/{uid}/balances").get("balances", [])
    # prefer booked/closing balances over 'available' (which includes overdraft limits)
    order = ["CLBD", "ITBD", "XPCD", "ITAV", "CLAV", "OTHR"]
    bals.sort(key=lambda b: order.index(b.get("balance_type")) if b.get("balance_type") in order else 99)
    return float(bals[0]["balance_amount"]["amount"]) if bals else None


def transactions(uid: str, date_from: str) -> list[dict]:
    out, key = [], None
    while True:
        params = {"date_from": date_from}
        if key:
            params["continuation_key"] = key
        j = _req("GET", f"/accounts/{uid}/transactions", params=params)
        out += j.get("transactions", [])
        key = j.get("continuation_key")
        if not key:
            return out
