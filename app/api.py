"""API for the dashboard's /money page.

Same trust model as the deals API: LAN bind, source-IP allowlist (CT 104 + pve), bearer token.
Bank secrets are write-only from here: they are stored, never returned.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from collections import defaultdict
from datetime import date, timedelta

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from . import eb, firefly, store, tr
from .config import CATEGORIES, NOT_SPENDING, save_secrets, secrets

TOKEN = os.environ.get("MONEY_API_TOKEN", "")
ALLOWED = {ip for ip in os.environ.get("MONEY_API_ALLOW", "192.168.1.54,192.168.1.50,127.0.0.1").split(",") if ip}
INCOME = {"Gehalt", "Sonstige Einnahmen"}

app = FastAPI(title="money", docs_url=None, redoc_url=None)
log = logging.getLogger("api")
login = tr.Login()
_cache: dict = {}


async def guard(request: Request, authorization: str = Header(default="")) -> None:
    if request.client and request.client.host not in ALLOWED:
        raise HTTPException(403, "host not allowed")
    if not TOKEN or authorization != f"Bearer {TOKEN}":
        raise HTTPException(401, "bad token")


def kick_sync() -> None:
    _cache.clear()
    subprocess.run(["systemctl", "start", "--no-block", "money-sync.service"], check=False)


def _syncing() -> bool:
    r = subprocess.run(["systemctl", "is-active", "money-sync.service"], capture_output=True, text=True)
    return r.stdout.strip() in ("active", "activating")


def _month_bounds(m: str) -> tuple[date, date]:
    y, mo = map(int, m.split("-"))
    start = date(y, mo, 1)
    end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return start, end


def _ledger(month: str) -> dict:
    """Spending/income for the month + 6-month trend, straight from Firefly (so edits there show up)."""
    hit = _cache.get(month)
    if hit and time.time() - hit[0] < 120:
        return hit[1]
    start, end = _month_bounds(month)
    trend_start = (start - timedelta(days=160)).replace(day=1)
    with firefly.client() as c:
        sp = firefly.splits(c, trend_start, end)
    months: dict[str, dict] = defaultdict(lambda: {"income": 0.0, "spending": 0.0})
    cats: dict[str, float] = defaultdict(float)
    recent = []
    for s in sp:
        cat = s.get("category_name") or "Sonstiges"
        if s["type"] not in ("withdrawal", "deposit") or cat in NOT_SPENDING:
            continue
        m = s["date"][:7]
        amt = float(s["amount"])
        if s["type"] == "withdrawal":
            months[m]["spending"] += amt
            delta = amt
        elif cat in INCOME or not s.get("category_name"):
            months[m]["income"] += amt
            delta = 0
        else:  # refund booked under a spending category
            months[m]["spending"] -= amt
            delta = -amt
        if m == month and delta:
            cats[cat] += delta
    for s in sorted(sp, key=lambda s: s["date"], reverse=True):
        if s["date"][:7] != month or s["type"] not in ("withdrawal", "deposit"):
            continue
        withdrawal = s["type"] == "withdrawal"
        recent.append({"group_id": s["group_id"], "journal_id": s["transaction_journal_id"], "date": s["date"][:10],
                       "amount": -float(s["amount"]) if withdrawal else float(s["amount"]),
                       "payee": s["destination_name"] if withdrawal else s["source_name"],
                       "description": s["description"], "category": s.get("category_name"),
                       "account": s["source_name"] if withdrawal else s["destination_name"]})
    out = {"month": month,
           "income": round(months[month]["income"], 2), "spending": round(months[month]["spending"], 2),
           "categories": sorted(({"name": k, "amount": round(v, 2)} for k, v in cats.items() if round(v, 2) > 0),
                                key=lambda x: -x["amount"]),
           "trend": [{"month": k, "income": round(v["income"], 2), "spending": round(v["spending"], 2)}
                     for k, v in sorted(months.items())][-6:],
           "transactions": recent[:300]}
    _cache[month] = (time.time(), out)
    return out


@app.get("/overview", dependencies=[Depends(guard)])
def overview(month: str | None = None) -> dict:
    month = month or date.today().strftime("%Y-%m")
    with store.connect() as con:
        accounts = [dict(r) for r in con.execute("SELECT key,source,name,iban,balance,balance_at,last_sync,error,session_id FROM accounts ORDER BY source,name")]
        links = [dict(r) for r in con.execute("SELECT session_id,aspsp,valid_until,created FROM links")]
        pf = [dict(r) for r in con.execute("SELECT day,value,cash,invested FROM portfolio ORDER BY day")]
        last_pos = con.execute("SELECT positions FROM portfolio ORDER BY day DESC LIMIT 1").fetchone()
        subs = [dict(r) for r in con.execute("SELECT * FROM subs WHERE dismissed=0 ORDER BY state, amount DESC")]
        run = con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    ledger, ledger_error = None, None
    try:
        ledger = _ledger(month)
    except Exception as e:  # noqa: BLE001 - still show accounts/setup if Firefly is down
        ledger_error = str(e)[:200]
    invest_value = (pf[-1]["value"] - (pf[-1]["cash"] or 0)) if pf else 0
    cash = sum(a["balance"] or 0 for a in accounts)
    monthly_subs = sum(s["amount"] * {"weekly": 4.33, "monthly": 1, "quarterly": 1 / 3, "yearly": 1 / 12}[s["period"]]
                       for s in subs if s["state"] == "active")
    return {
        "setup": {"eb": eb.configured(), "tr": tr.configured(), "tr_login": login.view(),
                  "eb_app_id": secrets().get("eb", {}).get("app_id", "")[:8]},
        "syncing": _syncing(),
        "last_run": dict(run) | {"summary": json.loads(run["summary"])} if run else None,
        "accounts": accounts, "links": links,
        "net_worth": round(cash + invest_value, 2), "cash": round(cash, 2), "invested_value": round(invest_value, 2),
        "portfolio": {"series": pf[-400:], "positions": json.loads(last_pos["positions"]) if last_pos else []},
        "subs": subs, "subs_monthly": round(monthly_subs, 2),
        "ledger": ledger, "ledger_error": ledger_error, "categories": CATEGORIES,
    }


@app.post("/sync", dependencies=[Depends(guard)])
def sync_now() -> dict:
    kick_sync()
    return {"ok": True}


class EbConfig(BaseModel):
    app_id: str
    key_pem: str


@app.post("/eb/config", dependencies=[Depends(guard)])
def eb_config(body: EbConfig) -> dict:
    s = secrets()
    old = s.get("eb")
    s["eb"] = {"app_id": body.app_id.strip(), "key_pem": body.key_pem.strip() + "\n"}
    save_secrets(s)
    try:
        app_info = eb.application()
    except Exception as e:  # noqa: BLE001
        if old:
            s["eb"] = old
        else:
            s.pop("eb")
        save_secrets(s)
        raise HTTPException(400, f"Enable Banking rejected the key: {e}") from e
    return {"ok": True, "name": app_info.get("name"), "active": app_info.get("active"),
            "redirect_urls": app_info.get("redirect_urls")}


@app.get("/eb/aspsps", dependencies=[Depends(guard)])
def eb_aspsps(country: str = "DE") -> list[dict]:
    try:
        return eb.aspsps(country)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e


class EbLink(BaseModel):
    aspsp: str
    country: str = "DE"


@app.post("/eb/link", dependencies=[Depends(guard)])
def eb_link(body: EbLink) -> dict:
    try:
        maxs = next((a["max_seconds"] for a in eb.aspsps(body.country) if a["name"] == body.aspsp), None)
        r = eb.start(body.aspsp, body.country, maxs)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e
    store.put("eb_pending", {"state": r["state"], "aspsp": body.aspsp, "at": time.time()})
    return {"url": r["url"]}


class EbFinish(BaseModel):
    code: str
    state: str = ""


@app.post("/eb/finish", dependencies=[Depends(guard)])
def eb_finish(body: EbFinish) -> dict:
    pending = store.get("eb_pending") or {}
    if pending.get("state") and body.state and body.state != pending["state"]:
        raise HTTPException(400, "this bank login was not started here (state mismatch)")
    try:
        sess = eb.finish(body.code)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e
    aspsp = (sess.get("aspsp") or {}).get("name") or pending.get("aspsp") or "Bank"
    until = (sess.get("access") or {}).get("valid_until")
    added = []
    with firefly.client() as c, store.connect() as con:
        # renewing a consent replaces the old session for the same bank, the accounts stay
        old = [r["session_id"] for r in con.execute("SELECT session_id FROM links WHERE aspsp=?", (aspsp,))]
        con.execute("INSERT INTO links (session_id,aspsp,valid_until,created) VALUES (?,?,?,?)",
                    (sess["session_id"], aspsp, until, time.time()))
        for a in sess.get("accounts", []):
            if isinstance(a, str):
                a = eb.account_details(a) | {"uid": a}
            iban = (a.get("account_id") or {}).get("iban")
            label = a.get("name") or a.get("product") or (f"…{iban[-4:]}" if iban else a["uid"][:6])
            name = f"{aspsp} {label}".strip()
            existing = con.execute("SELECT key FROM accounts WHERE source='eb' AND ((iban IS NOT NULL AND iban=?) OR name=?)",
                                   (iban, name)).fetchone()
            if existing and existing["key"] != f"eb:{a['uid']}":
                # same account, new uid under the new consent: move its history pointer over
                con.execute("UPDATE accounts SET key=?, session_id=?, error=NULL WHERE key=?",
                            (f"eb:{a['uid']}", sess["session_id"], existing["key"]))
            elif existing:
                con.execute("UPDATE accounts SET session_id=?, error=NULL WHERE key=?", (sess["session_id"], existing["key"]))
            else:
                fid = firefly.ensure_asset(c, name, iban, a.get("currency") or "EUR")
                con.execute("INSERT INTO accounts (key,source,name,iban,currency,session_id,firefly_id) VALUES (?,?,?,?,?,?,?)",
                            (f"eb:{a['uid']}", "eb", name, iban, a.get("currency") or "EUR", sess["session_id"], fid))
            added.append(name)
        for sid in old:
            con.execute("DELETE FROM links WHERE session_id=?", (sid,))
    store.put("eb_pending", None)
    kick_sync()
    return {"ok": True, "aspsp": aspsp, "accounts": added, "valid_until": until}


class Unlink(BaseModel):
    session_id: str


@app.post("/eb/unlink", dependencies=[Depends(guard)])
def eb_unlink(body: Unlink) -> dict:
    """Stop importing; the booked history stays in Firefly."""
    with store.connect() as con:
        con.execute("DELETE FROM links WHERE session_id=?", (body.session_id,))
        con.execute("DELETE FROM accounts WHERE session_id=?", (body.session_id,))
    _cache.clear()
    return {"ok": True}


class TrConfig(BaseModel):
    phone: str
    pin: str


@app.post("/tr/config", dependencies=[Depends(guard)])
def tr_config(body: TrConfig) -> dict:
    phone = body.phone.replace(" ", "")
    if phone.startswith("0"):
        phone = "+49" + phone[1:]
    s = secrets()
    s["tr"] = {"phone": phone, "pin": body.pin.strip()}
    save_secrets(s)
    if tr.TR_COOKIES.exists():
        tr.TR_COOKIES.unlink()
    return login.start(on_ok=kick_sync)


@app.post("/tr/login", dependencies=[Depends(guard)])
def tr_login() -> dict:
    if not tr.configured():
        raise HTTPException(400, "no Trade Republic login saved")
    return login.start(on_ok=kick_sync)


class Code(BaseModel):
    code: str


@app.post("/tr/code", dependencies=[Depends(guard)])
def tr_code(body: Code) -> dict:
    try:
        return login.code(body.code, on_ok=kick_sync)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/tr/login", dependencies=[Depends(guard)])
def tr_login_state() -> dict:
    return login.view()


class TxCategory(BaseModel):
    group_id: str
    journal_id: str
    category: str


@app.post("/tx/category", dependencies=[Depends(guard)])
def tx_category(body: TxCategory) -> dict:
    if body.category not in CATEGORIES:
        raise HTTPException(400, "unknown category")
    with firefly.client() as c:
        firefly.set_category(c, body.group_id, body.journal_id, body.category)
    _cache.clear()
    return {"ok": True}


class SubKey(BaseModel):
    key: str


@app.post("/subs/dismiss", dependencies=[Depends(guard)])
def subs_dismiss(body: SubKey) -> dict:
    """'Not a subscription' -- hides it for good (detection keeps the row, so it won't come back as new)."""
    with store.connect() as con:
        con.execute("UPDATE subs SET dismissed=1 WHERE key=?", (body.key,))
    return {"ok": True}
