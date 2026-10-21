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

from . import eb, firefly, insights, notify, store, subs, tr
from .ai import norm_payee
from .config import CATEGORIES, HISTORY_DAYS, NOT_SPENDING, save_secrets, secrets

TOKEN = os.environ.get("MONEY_API_TOKEN", "")
ALLOWED = {ip for ip in os.environ.get("MONEY_API_ALLOW", "192.168.1.54,192.168.1.50,127.0.0.1").split(",") if ip}
INCOME = {"Gehalt", "Sonstige Einnahmen"}
# money in under these is income too (a tax refund, an unsorted payment), not a refund that lowers spending
INCOME_LIKE = {"Sonstiges", "Steuern", "Geschenke & Spenden"}

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
    if _syncing():  # a run already going may have loaded the account list before this change: run once more after it
        store.put("sync_again", True)
    subprocess.run(["systemctl", "start", "--no-block", "money-sync.service"], check=False)


def _syncing() -> bool:
    r = subprocess.run(["systemctl", "is-active", "money-sync.service"], capture_output=True, text=True)
    return r.stdout.strip() in ("active", "activating")


def _month_bounds(m: str) -> tuple[date, date]:
    y, mo = map(int, m.split("-"))
    start = date(y, mo, 1)
    end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return start, end


def _all_splits() -> list[dict]:
    """Every booking of the history window, cached a few minutes (search, payee history, picking subscriptions)."""
    hit = _cache.get("_all")
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    with firefly.client() as c:
        sp = firefly.splits(c, date.today() - timedelta(days=HISTORY_DAYS + 30), date.today() + timedelta(days=1))
    _cache["_all"] = (time.time(), sp)
    return sp


def _aliases() -> dict:
    hit = _cache.get("_aliases")
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    a = store.get("aliases", {}) or {}
    _cache["_aliases"] = (time.time(), a)
    return a


def _tx(s: dict) -> dict:
    withdrawal = s["type"] == "withdrawal"
    return {"group_id": s["group_id"], "journal_id": s["transaction_journal_id"], "date": s["date"][:10],
            "alias": _aliases().get(_pkey(s)),
            "amount": -float(s["amount"]) if withdrawal else float(s["amount"]),
            "payee": s["destination_name"] if withdrawal else s["source_name"],
            "description": s["description"], "category": s.get("category_name"),
            "account": s["source_name"] if withdrawal else s["destination_name"]}


def _pkey(s: dict) -> str:
    other = s.get("destination_name") if s["type"] == "withdrawal" else s.get("source_name")
    return norm_payee(other or "") or norm_payee(s.get("description") or "")


def _ledger(month: str) -> dict:
    """Spending/income for the month + 12-month trend, straight from Firefly (so edits there show up)."""
    hit = _cache.get(month)
    if hit and time.time() - hit[0] < 120:
        return hit[1]
    start, end = _month_bounds(month)
    trend_start = (start - timedelta(days=340)).replace(day=1)
    with firefly.client() as c:
        sp = firefly.splits(c, trend_start, end)
    months: dict[str, dict] = defaultdict(lambda: {"income": 0.0, "spending": 0.0})
    cats: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))  # month -> cat -> €
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
        elif cat in INCOME or cat in INCOME_LIKE or not s.get("category_name"):
            months[m]["income"] += amt
            delta = 0
        else:  # refund booked under a spending category
            months[m]["spending"] -= amt
            delta = -amt
        if delta:
            cats[m][cat] += delta
    for s in sorted(sp, key=lambda s: s["date"], reverse=True):
        if s["date"][:7] == month and s["type"] in ("withdrawal", "deposit"):
            recent.append(_tx(s))
    # what a category usually costs: the mean of the 3 months before this one
    prev = []
    p = start
    for _ in range(3):
        p = (p - timedelta(days=1)).replace(day=1)
        prev.append(p.strftime("%Y-%m"))
    seen_prev = [m for m in prev if m in months]
    avg = {k: sum(cats[m].get(k, 0) for m in seen_prev) / len(seen_prev) for k in CATEGORIES} if seen_prev else {}
    out = {"month": month,
           "income": round(months[month]["income"], 2), "spending": round(months[month]["spending"], 2),
           "categories": sorted(({"name": k, "amount": round(v, 2), "avg": round(avg.get(k, 0), 2)}
                                 for k, v in cats[month].items() if round(v, 2) > 0), key=lambda x: -x["amount"]),
           "avg_spending": round(sum(months[m]["spending"] for m in seen_prev) / len(seen_prev), 2) if seen_prev else None,
           "trend": [{"month": k, "income": round(v["income"], 2), "spending": round(v["spending"], 2)}
                     for k, v in sorted(months.items()) if k <= month][-12:],
           "transactions": recent[:1500]}
    if month == date.today().strftime("%Y-%m"):
        out["forecast"] = _forecast(out, [months[m] for m in prev if m in months], sp)
    _cache[month] = (time.time(), out)
    return out


def _forecast(L: dict, prev: list[dict], sp: list[dict]) -> dict:
    """Month end, roughly: what's spent + Abos still due + the usual day-to-day pace for the days left,
    and the salary if it hasn't come yet."""
    today = date.today()
    start, end = _month_bounds(today.strftime("%Y-%m"))
    left = (end - today).days
    with store.connect() as con:
        active = [dict(r) for r in con.execute("SELECT * FROM subs WHERE dismissed=0 AND state='active'")]
    step = {"weekly": 7, "monthly": 30.4, "quarterly": 91, "yearly": 365}
    due = 0.0
    for x in active:
        d = date.fromisoformat(x["next_date"]) if x.get("next_date") else None
        while d and d <= end:
            if d > today:
                due += x["amount"]
            d += timedelta(days=round(step[x["period"]]))
    subs_month = sum(x["amount"] * 30.4 / step[x["period"]] for x in active)
    spend_prev = sorted(p["spending"] for p in prev)
    usual = spend_prev[len(spend_prev) // 2] if spend_prev else L["spending"] / max(today.day, 1) * 30.4
    pace = max(0.0, usual - subs_month) / (end.day)  # everyday spending per day, without the Abos
    spend = L["spending"] + due + pace * left
    # salary: the usual Gehalt of the last months, minus what came in already
    gehalt: dict[str, float] = defaultdict(float)
    for x in sp:
        if x["type"] == "deposit" and x.get("category_name") == "Gehalt":
            gehalt[x["date"][:7]] += float(x["amount"])
    this = gehalt.pop(today.strftime("%Y-%m"), 0.0)
    past = sorted(v for k, v in gehalt.items() if k >= (start - timedelta(days=95)).strftime("%Y-%m"))
    usual_pay = past[len(past) // 2] if past else 0.0
    income = L["income"] + max(0.0, usual_pay - this)
    return {"spending": round(spend, 2), "income": round(income, 2), "saldo": round(income - spend, 2),
            "subs_due": round(due, 2), "pace_day": round(pace, 2), "days_left": left,
            "rest_out": round(spend - L["spending"], 2), "rest_in": round(income - L["income"], 2)}


@app.get("/overview", dependencies=[Depends(guard)])
def overview(month: str | None = None) -> dict:
    month = month or date.today().strftime("%Y-%m")
    with store.connect() as con:
        accounts = [dict(r) for r in con.execute("SELECT key,source,name,iban,balance,balance_at,last_sync,error,session_id FROM accounts ORDER BY source,name")]
        links = [dict(r) for r in con.execute("SELECT session_id,aspsp,valid_until,created FROM links")]
        pf = [dict(r) for r in con.execute("SELECT day,value,cash,invested FROM portfolio ORDER BY day")]
        last_pos = con.execute("SELECT positions FROM portfolio ORDER BY day DESC LIMIT 1").fetchone()
        subs = [dict(r) for r in con.execute("SELECT * FROM subs WHERE dismissed=0 ORDER BY state, amount DESC")]
        hidden = [dict(r) for r in con.execute("SELECT key,payee,name,amount,period FROM subs WHERE dismissed=1 ORDER BY payee")]
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
        "setup": {"eb": eb.configured(), "tr": tr.configured(), "tr_login": login.view(), "telegram": notify.view(),
                  "eb_app_id": secrets().get("eb", {}).get("app_id", "")[:8]},
        "syncing": _syncing(),
        "last_run": dict(run) | {"summary": json.loads(run["summary"])} if run else None,
        "accounts": accounts, "links": links,
        "net_worth": round(cash + invest_value, 2), "cash": round(cash, 2), "invested_value": round(invest_value, 2),
        "portfolio": {"series": pf[-2:], "positions": _labelled(json.loads(last_pos["positions"]) if last_pos else [])},
        "subs": [x | {"alias": _aliases().get(x.get("pkey") or x["key"])} for x in subs], "subs_hidden": hidden, "subs_monthly": round(monthly_subs, 2),
        "ledger": ledger, "ledger_error": ledger_error, "categories": CATEGORIES,
    }


def _labelled(positions: list[dict]) -> list[dict]:
    built = (store.get("depot") or {}).get("positions", {})
    total = sum(p["value"] for p in positions) or 1
    return [p | {"label": built.get(p["isin"], {}).get("label") or p["name"], "share": round(p["value"] / total, 4)}
            for p in positions]


@app.get("/depot", dependencies=[Depends(guard)])
def depot_history() -> dict:
    """Per-position and total depot value + cost basis over time (rebuilt every sync from trades × prices)."""
    return store.get("depot") or {"positions": {}, "total": []}


@app.get("/tx/search", dependencies=[Depends(guard)])
def tx_search(q: str = "", limit: int = 200) -> dict:
    """Across the whole history: payee, purpose, category, account, or an amount ('12,99')."""
    q = q.strip().lower()
    if not q:
        return {"q": q, "transactions": []}
    try:
        num = float(q.replace("€", "").replace(".", "").replace(",", ".")) if any(ch.isdigit() for ch in q) else None
    except ValueError:
        num = None
    out = []
    for s in sorted(_all_splits(), key=lambda s: s["date"], reverse=True):
        if s["type"] not in ("withdrawal", "deposit"):
            continue
        hay = " ".join(str(s.get(k) or "") for k in ("destination_name", "source_name", "description", "category_name")).lower()
        hay += " " + (_aliases().get(_pkey(s)) or "").lower()
        if q in hay or (num is not None and abs(abs(float(s["amount"])) - abs(num)) < 0.005):
            out.append(_tx(s))
            if len(out) >= min(limit, 500):
                break
    return {"q": q, "transactions": out}


@app.get("/payee", dependencies=[Depends(guard)])
def payee(name: str) -> dict:
    """Everything booked with one payee: list, monthly totals, and whether it already is a subscription."""
    key = norm_payee(name)
    rows = sorted((s for s in _all_splits() if s["type"] in ("withdrawal", "deposit") and _pkey(s) == key),
                  key=lambda s: s["date"], reverse=True)
    txs = [_tx(s) for s in rows]
    by_month: dict[str, float] = defaultdict(float)
    for t in txs:
        by_month[t["date"][:7]] += t["amount"]
    with store.connect() as con:
        sub = [dict(r) for r in con.execute("SELECT * FROM subs WHERE (pkey=? OR key=?)", (key, key))]
    year_ago = (date.today() - timedelta(days=365)).isoformat()
    return {"key": key, "transactions": txs[:300],
            "months": [{"month": k, "amount": round(v, 2)} for k, v in sorted(by_month.items())],
            "total_12m": round(sum(t["amount"] for t in txs if t["date"] >= year_ago), 2),
            "count": len(txs), "subs": sub}


class SubKey(BaseModel):
    key: str


class SubAdd(BaseModel):
    journal_ids: list[str]
    period: str | None = None
    name: str | None = None


@app.post("/subs/add", dependencies=[Depends(guard)])
def subs_add(body: SubAdd) -> dict:
    """You pick the bookings that are one subscription; payee + price + rhythm come from them."""
    ids = set(body.journal_ids)
    picked = sorted((s for s in _all_splits() if s["transaction_journal_id"] in ids and s["type"] == "withdrawal"),
                    key=lambda s: s["date"])
    if not picked:
        raise HTTPException(400, "keine Abbuchungen ausgewählt")
    keys = {_pkey(s) for s in picked}
    if len(keys) > 1 or not next(iter(keys)):
        raise HTTPException(400, "die Buchungen gehören zu verschiedenen Empfängern")
    pkey = keys.pop()
    amounts = sorted(float(s["amount"]) for s in picked)
    median = amounts[len(amounts) // 2]
    days = sorted({date.fromisoformat(s["date"][:10]) for s in picked})
    period = body.period if body.period in subs.PER_DAYS else subs.guess_period(days)
    key = f"m:{pkey}:{round(median)}"
    sub = {"key": key, "payee": picked[-1].get("destination_name") or pkey, "name": (body.name or "").strip()[:80] or None,
           "category": picked[-1].get("category_name"), "period": period, "amount": round(float(picked[-1]["amount"]), 2),
           "pkey": pkey, "match_amount": round(float(picked[-1]["amount"]), 2)}
    with store.connect() as con:
        con.execute("INSERT INTO subs (key,payee,name,category,period,amount,prev_amount,count,first_seen,state,manual,pkey,match_amount,dismissed) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,0) ON CONFLICT(key) DO UPDATE SET period=excluded.period, name=COALESCE(excluded.name,name), dismissed=0",
                    (key, sub["payee"], sub["name"], sub["category"], period, sub["amount"], sub["amount"], len(picked),
                     store.now(), "active", pkey, sub["match_amount"]))
        # the detector's row for the same payee/price would double-count it
        con.execute("UPDATE subs SET dismissed=1 WHERE manual=0 AND key=? AND abs(amount-?) <= 0.25*?", (pkey, median, median))
        row = dict(con.execute("SELECT * FROM subs WHERE key=?", (key,)).fetchone())
        subs.refresh_manual(con, row, _all_splits())
    return {"ok": True, "key": key, "period": period}


class SubEdit(BaseModel):
    key: str
    period: str | None = None
    name: str | None = None


@app.post("/subs/edit", dependencies=[Depends(guard)])
def subs_edit(body: SubEdit) -> dict:
    with store.connect() as con:
        row = con.execute("SELECT * FROM subs WHERE key=?", (body.key,)).fetchone()
        if not row:
            raise HTTPException(404, "kein solches Abo")
        if body.period in subs.PER_DAYS:
            nxt = None
            if row["last_date"]:
                nxt = (date.fromisoformat(row["last_date"]) + timedelta(days=round(subs.PER_DAYS[body.period]))).isoformat()
            con.execute("UPDATE subs SET period=?, next_date=COALESCE(?,next_date) WHERE key=?", (body.period, nxt, body.key))
        if body.name is not None:
            con.execute("UPDATE subs SET name=? WHERE key=?", (body.name.strip()[:80] or None, body.key))
    return {"ok": True}


@app.post("/subs/remove", dependencies=[Depends(guard)])
def subs_remove(body: SubKey) -> dict:
    """Hand-picked: gone. Detected: hidden (restorable)."""
    with store.connect() as con:
        con.execute("DELETE FROM subs WHERE key=? AND manual=1", (body.key,))
        con.execute("UPDATE subs SET dismissed=1 WHERE key=?", (body.key,))
    return {"ok": True}


@app.post("/subs/restore", dependencies=[Depends(guard)])
def subs_restore(body: SubKey) -> dict:
    with store.connect() as con:
        con.execute("UPDATE subs SET dismissed=0 WHERE key=?", (body.key,))
    return {"ok": True}


class Rename(BaseModel):
    name: str
    alias: str = ""


@app.post("/payee/rename", dependencies=[Depends(guard)])
def payee_rename(body: Rename) -> dict:
    """A readable name for a payee ('H.R..Ullrich.Handels.Ge/Berlin' -> 'Ullrich'), shown everywhere; empty = back to the bank's."""
    key = norm_payee(body.name)
    if not key:
        raise HTTPException(400, "kein Empfänger")
    a = store.get("aliases", {}) or {}
    if body.alias.strip():
        a[key] = body.alias.strip()[:60]
    else:
        a.pop(key, None)
    store.put("aliases", a)
    _cache.clear()
    return {"ok": True, "key": key}


@app.get("/tx/export", dependencies=[Depends(guard)])
def tx_export(start: str, end: str) -> dict:
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    with firefly.client() as c:
        sp = firefly.splits(c, a, b)
    return {"transactions": [_tx(s) for s in sorted(sp, key=lambda s: s["date"]) if s["type"] in ("withdrawal", "deposit")][:20000]}


@app.get("/anomalies", dependencies=[Depends(guard)])
def anomalies(month: str) -> dict:
    start, end = _month_bounds(month)
    out = insights.anomalies(_all_splits(), start, end)
    for x in out:
        x["alias"] = _aliases().get(norm_payee(x["payee"] or ""))
    return {"anomalies": out}


class Jid(BaseModel):
    journal_id: str


@app.post("/anomalies/ok", dependencies=[Depends(guard)])
def anomaly_ok(body: Jid) -> dict:
    ok = store.get("anomaly_ok", []) or []
    ok.append(body.journal_id)
    store.put("anomaly_ok", ok[-2000:])
    return {"ok": True}


@app.get("/networth", dependencies=[Depends(guard)])
def networth() -> dict:
    hit = _cache.get("_nw")
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    with firefly.client() as c:
        out = insights.networth(c)
    _cache["_nw"] = (time.time(), out)
    return out


class Token(BaseModel):
    token: str


@app.post("/telegram/config", dependencies=[Depends(guard)])
def telegram_config(body: Token) -> dict:
    try:
        return notify.setup_own(body.token.strip())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)[:200]) from e


@app.post("/telegram/test", dependencies=[Depends(guard)])
def telegram_test() -> dict:
    v = notify.view()
    return v | {"sent": notify.send("✅ Money-Benachrichtigungen kommen ab jetzt hier an.")}


@app.get("/report", dependencies=[Depends(guard)])
def report(month: str) -> dict:
    return {"text": insights.month_report(month)}


class Month(BaseModel):
    month: str


@app.post("/report/send", dependencies=[Depends(guard)])
def report_send(body: Month) -> dict:
    return {"sent": notify.send(insights.month_report(body.month))}


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
    scope: str = "one"   # one | payee


@app.post("/tx/category", dependencies=[Depends(guard)])
def tx_category(body: TxCategory) -> dict:
    """One booking, or every booking with the same payee in the same direction (money in / out stay apart),
    plus a rule so future bookings from that payee land there too. Transfers/investments are left alone."""
    if body.category not in CATEGORIES:
        raise HTTPException(400, "unknown category")
    n = 1
    with firefly.client() as c:
        firefly.set_category(c, body.group_id, body.journal_id, body.category)
        if body.scope == "payee":
            sp = _all_splits()
            me = next((s for s in sp if s["transaction_journal_id"] == body.journal_id), None)
            key = _pkey(me) if me else ""
            if key:
                for s in sp:
                    if s["type"] == me["type"] and s["transaction_journal_id"] != body.journal_id and _pkey(s) == key \
                            and s.get("category_name") != body.category and s.get("category_name") not in NOT_SPENDING:
                        firefly.set_category(c, s["group_id"], s["transaction_journal_id"], body.category)
                        n += 1
                rules = store.get("payee_rules", {}) or {}
                rules[f"{me['type']}:{key}"] = body.category
                store.put("payee_rules", rules)
    _cache.clear()
    return {"ok": True, "changed": n}



@app.post("/subs/dismiss", dependencies=[Depends(guard)])
def subs_dismiss(body: SubKey) -> dict:
    """'Not a subscription' -- hides it for good (detection keeps the row, so it won't come back as new)."""
    with store.connect() as con:
        con.execute("UPDATE subs SET dismissed=1 WHERE key=?", (body.key,))
    return {"ok": True}
