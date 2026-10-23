"""Trade Republic through the unofficial pytr. The v2 web login is confirmed with one tap in the TR app
(no code to type); the session cookie is kept and resumed until TR expires it."""
from __future__ import annotations

import asyncio
import logging
import tempfile
import threading
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .config import TR_COOKIES, secrets

log = logging.getLogger("tr")


def configured() -> bool:
    s = secrets().get("tr", {})
    return bool(s.get("phone") and s.get("pin"))


def _api():
    from pytr.api import TradeRepublicApi
    s = secrets()["tr"]
    TR_COOKIES.parent.mkdir(parents=True, exist_ok=True)
    return TradeRepublicApi(phone_no=s["phone"], pin=s["pin"], save_cookies=True, cookies_file=TR_COOKIES,
                            use_v2_login=True, locale="de")


def session():
    """A logged-in client from the saved cookie, or None if TR wants a fresh login."""
    tr = _api()
    try:
        if not tr.resume_websession():
            return None
        # resuming hits /auth/web/session, which hands out fresh cookies; pytr never writes them back, so
        # without this the file keeps the login-time tr_refresh and dies 24 h after the tap
        tr.save_websession()
        return tr
    except Exception as e:  # noqa: BLE001 - pytr raises whatever requests/TR throw
        log.info("resume failed: %s", e)
        return None


class Login:
    """One interactive login, run in a thread so the API can report its state while you tap in the app."""

    def __init__(self):
        self.state = "idle"     # idle | waiting_app | need_code | ok | error
        self.error = ""
        self.started = 0.0
        self._tr = None
        self._lock = threading.Lock()

    def view(self) -> dict:
        return {"state": self.state, "error": self.error, "started": self.started}

    def start(self, on_ok=None) -> dict:
        with self._lock:
            if self.state in ("waiting_app", "need_code") and time.time() - self.started < 150:
                return self.view()
            self.state, self.error, self.started = "waiting_app", "", time.time()
        threading.Thread(target=self._run, args=(on_ok,), daemon=True).start()
        time.sleep(1.5)
        return self.view()

    def _run(self, on_ok):
        try:
            tr = self._tr = _api()
            tr.initiate_weblogin()
            if tr.weblogin_needs_authenticator:
                self.state = "need_code"
                return
            tr.complete_weblogin()   # polls until you confirm in the app (~2 min window)
            self._done(on_ok)
        except Exception as e:  # noqa: BLE001
            self.state, self.error = "error", str(e)[:300]

    def code(self, code: str, on_ok=None) -> dict:
        if self.state != "need_code" or not self._tr:
            raise ValueError("no login is waiting for a code")
        try:
            self._tr.complete_weblogin(code.strip())
            self._done(on_ok)
        except Exception as e:  # noqa: BLE001
            self.state, self.error = "error", str(e)[:300]
        return self.view()

    def _done(self, on_ok):
        self.state = "ok"
        if on_ok:
            on_ok()


def unattended_login(timeout_note) -> object | None:
    """Used by the timer when the cookie died: start a login, push a note, wait for the tap."""
    tr = _api()
    tr.initiate_weblogin()
    if tr.weblogin_needs_authenticator:
        return None
    timeout_note()
    tr.complete_weblogin()
    return tr


def portfolio(tr) -> dict:
    from pytr.portfolio import Portfolio
    p = Portfolio(tr)
    asyncio.run(p.portfolio_loop())
    positions = []
    for pos in p.positions:
        size = Decimal(str(pos.get("netSize", "0")))
        if size == 0:
            continue
        positions.append({"isin": pos["instrumentId"], "name": pos.get("name") or pos["instrumentId"],
                          "shares": float(size), "value": float(pos["netValue"]),
                          "buyin": float(Decimal(str(pos.get("averageBuyIn", 0))) * size)})
    cash = sum(float(c.get("amount", 0)) for c in (p.cash or []) if c.get("currencyId", "EUR") == "EUR")
    return {"positions": sorted(positions, key=lambda x: -x["value"]), "cash": cash,
            "value": round(sum(x["value"] for x in positions) + cash, 2),
            "invested": round(sum(x["buyin"] for x in positions), 2)}


# TR event types that move money between your own pockets or into securities, not spending
INVEST = {"BUY", "SELL", "DIVIDEND", "SPINOFF", "SPLIT", "SWAP", "TRANSFER_IN", "TRANSFER_OUT",
          "TRADE_INVOICE", "SAVEBACK", "PRIVATE_MARKETS_ORDER"}


def transactions(tr, since: float) -> list[dict]:
    """Cash-account movements since a unix time: card payments, transfers, interest, buys/sells."""
    from pytr.event import Event
    from pytr.timeline import Timeline
    with tempfile.TemporaryDirectory() as d:
        tl = Timeline(tr, Path(d), not_before=since, store_event_database=False)
        asyncio.run(tl.tl_loop())
        raw = tl.events
    out = []
    for ev in raw:
        try:
            e = Event.from_dict(ev)
        except Exception as ex:  # noqa: BLE001 - unknown event shapes must not stop the sync
            log.info("skip event %s: %s", ev.get("eventType"), ex)
            continue
        if e.value in (None, 0) or e.event_type is None:
            continue
        kind = getattr(e.event_type, "name", None) or str(e.event_type)
        out.append({"id": ev["id"], "date": e.date.date().isoformat() if isinstance(e.date, datetime) else str(e.date)[:10],
                    "amount": round(float(e.value), 2), "payee": e.title or "Trade Republic",
                    "purpose": " · ".join(x for x in [ev.get("subtitle"), e.note] if x), "kind": kind,
                    "invest": kind in INVEST or bool(e.isin)})
    return out


def _run(tr, coro):
    """pytr keeps one websocket bound to the loop that opened it; each asyncio.run needs a fresh one."""
    tr._ws = None
    return asyncio.run(coro)


async def _one(tr, payload: dict, timeout: float = 20):
    """Subscribe, take the first answer, unsubscribe."""
    sid = await tr.subscribe(payload)
    while True:
        s, _, resp = await asyncio.wait_for(tr.recv(), timeout)
        if s == sid:
            await tr.unsubscribe(sid)
            return resp


def trades(tr) -> list[dict]:
    """Every buy/sell (savings plan, round-up, saveback, manual order) over the whole account history:
    {isin, day, shares (+ bought / − sold), amount (cash moved, − for a buy)}. Only ~2 s even for all of it."""
    from pytr.event import Event
    from pytr.timeline import Timeline
    with tempfile.TemporaryDirectory() as d:
        tl = Timeline(tr, Path(d), not_before=0, store_event_database=False)
        _run(tr, tl.tl_loop())
        raw = tl.events
    out = []
    for ev in raw:
        try:
            e = Event.from_dict(ev)
        except Exception:  # noqa: BLE001
            continue
        if not e.isin or not e.shares:
            continue
        value = float(e.value or 0)
        kind = getattr(e.event_type, "name", "") or ""
        sell = value > 0 or kind == "SELL"
        out.append({"isin": e.isin, "day": e.date.date().isoformat() if isinstance(e.date, datetime) else str(e.date)[:10],
                    "shares": -abs(float(e.shares)) if sell else abs(float(e.shares)), "amount": value})
    return sorted(out, key=lambda x: x["day"])


def instrument(tr, isin: str) -> dict:
    r = _run(tr, _one(tr, {"type": "instrument", "id": isin}))
    return {"short": r.get("shortName") or isin, "name": r.get("name") or "", "type": r.get("typeId") or "",
            "exchange": next((x for x in ["LSX", *(r.get("exchangeIds") or [])] if x in (r.get("exchangeIds") or ["LSX"])), "LSX")}


def closes(tr, isin: str, exchange: str = "LSX") -> dict[str, float]:
    """Daily closes for the last year, weekly before that (TR's 'max' range is weekly)."""
    out: dict[str, float] = {}

    async def both():
        weekly = await _one(tr, {"type": "aggregateHistoryLight", "id": f"{isin}.{exchange}", "range": "max"})
        daily = await _one(tr, {"type": "aggregateHistoryLight", "id": f"{isin}.{exchange}", "range": "1y"})
        return weekly, daily
    for r in _run(tr, both()):
        for a in r.get("aggregates") or []:
            out[datetime.fromtimestamp(a["time"] / 1000).date().isoformat()] = float(a.get("close") or a.get("adjValue"))
    return dict(sorted(out.items()))
