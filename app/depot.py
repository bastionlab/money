"""Depot history the way the TR app draws it: holdings over time (rebuilt from every trade) × the instrument's
price history. Anchored at today's real positions, walking back through the trades, so the last point always
matches the account even if TR's timeline misses an old trade."""
from __future__ import annotations

import logging
import re
from bisect import bisect_right
from datetime import date

from . import store, tr

log = logging.getLogger("depot")


def _label(info: dict, dup: bool) -> str:
    """Two 'Core MSCI World USD (Acc)' (iShares + Amundi) need the issuer to be told apart."""
    if not dup:
        return info["short"]
    issuer = (info["name"].split() or [""])[0]
    return f"{issuer} {re.sub(r' USD| EUR|\(Acc\)|\(Dist\)', '', info['short']).strip()}".strip()


def rebuild(t, positions: list[dict]) -> dict:
    trades = tr.trades(t)
    isins = list(dict.fromkeys([p["isin"] for p in positions] + [x["isin"] for x in trades]))
    infos = store.get("instruments", {}) or {}
    for i in isins:
        if i not in infos:
            try:
                infos[i] = tr.instrument(t, i)
            except Exception as e:  # noqa: BLE001
                log.warning("instrument %s: %r", i, e)
    store.put("instruments", infos)
    for i in isins:  # not cached: try again next run
        infos.setdefault(i, {"short": next((p["name"] for p in positions if p["isin"] == i), i), "name": "", "type": "", "exchange": "LSX"})
    shorts = [infos[i]["short"] for i in isins]
    out: dict = {"positions": {}, "total": []}
    today = date.today().isoformat()
    per: dict[str, dict[str, tuple[float, float, float]]] = {}
    for i in isins:
        try:
            px = tr.closes(t, i, infos[i].get("exchange") or "LSX")
        except Exception as e:  # noqa: BLE001
            log.warning("prices %s: %r", i, e)
            continue
        if not px:
            continue
        pos = next((p for p in positions if p["isin"] == i), None)
        shares = pos["shares"] if pos else 0.0
        invested = pos["buyin"] if pos else 0.0
        mine = [x for x in trades if x["isin"] == i]
        days = sorted(set(px) | {x["day"] for x in mine} | ({today} if pos else set()))
        pdays = list(px)
        # walk back from today: before each trade you held fewer (bought) or more (sold) shares
        rows: dict[str, tuple[float, float, float]] = {}
        k = len(mine) - 1
        for d in reversed(days):
            while k >= 0 and mine[k]["day"] > d:
                x = mine[k]
                if x["shares"] > 0:
                    invested -= -x["amount"] if x["amount"] < 0 else 0
                else:  # a sale took avg-cost × shares out of the cost basis
                    avg = invested / shares if shares > 1e-9 else abs(x["amount"]) / abs(x["shares"])
                    invested += avg * abs(x["shares"])
                shares -= x["shares"]
                k -= 1
            if shares < 1e-6:
                shares, invested = 0.0, max(invested, 0.0)
                if d < (mine[0]["day"] if mine else today):
                    break
            if d == today and pos:
                val = pos["value"]
            else:
                j = bisect_right(pdays, d) - 1
                if j < 0:
                    break
                val = shares * px[pdays[j]]
            rows[d] = (round(shares, 6), round(val, 2), round(max(invested, 0.0), 2))
        series = sorted(rows.items())
        # drop leading zero-holding days (before the first buy)
        while series and series[0][1][0] == 0:
            series.pop(0)
        if not series:
            continue
        per[i] = dict(series)
        info = infos[i]
        last_px = px[pdays[-1]]
        out["positions"][i] = {
            "isin": i, "label": _label(info, shorts.count(info["short"]) > 1), "name": info["name"], "type": info["type"],
            "shares": shares if not pos else pos["shares"], "price": round(pos["value"] / pos["shares"], 4) if pos and pos["shares"] else last_px,
            "trades": [{"day": x["day"], "shares": x["shares"], "amount": x["amount"]} for x in mine][-60:],
            "series": [[d, v[1], v[2]] for d, v in series]}
    # total: every day any position has a point, each position carried forward to that day
    all_days = sorted({d for s in per.values() for d in s})
    last: dict[str, tuple] = {}
    for d in all_days:
        for i, s in per.items():
            if d in s:
                last[i] = s[d]
        out["total"].append([d, round(sum(v[1] for v in last.values()), 2), round(sum(v[2] for v in last.values()), 2)])
    out["built"] = store.now()
    store.put("depot", out)
    return {"positions": len(out["positions"]), "days": len(out["total"])}
