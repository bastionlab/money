"""Subscriptions = recurring withdrawals to the same payee at a steady rhythm and a similar amount."""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, timedelta

from . import notify, store
from .ai import norm_payee
from .config import NOT_SPENDING

PERIODS = [("weekly", 7, 2), ("monthly", 30.4, 4), ("quarterly", 91, 8), ("yearly", 365, 20)]


def _period(gaps: list[int]):
    med = statistics.median(gaps)
    for name, days, slack in PERIODS:
        if abs(med - days) <= slack and all(abs(g - days) <= slack * 2 for g in gaps[-3:]):
            return name, days
    return None, None


def detect(splits: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for s in splits:
        if s["type"] != "withdrawal" or (s.get("category_name") or "") in NOT_SPENDING:
            continue
        key = norm_payee(s.get("destination_name") or "") or norm_payee(s.get("description") or "")
        if key and key not in ("unbekannt",):
            groups[key].append(s)
    found = []
    today = date.today()
    for key, rows in groups.items():
        rows.sort(key=lambda r: r["date"])
        days = sorted({date.fromisoformat(r["date"][:10]) for r in rows})
        if len(days) < 2:
            continue
        gaps = [(b - a).days for a, b in zip(days, days[1:])]
        name, per = _period(gaps)
        if not name or (name != "yearly" and len(days) < 3):
            continue
        amounts = [float(r["amount"]) for r in rows]
        last, prev = amounts[-1], amounts[-2]
        # a supermarket you visit monthly is not a subscription: the amounts wander
        if statistics.pstdev(amounts[-4:]) > 0.15 * statistics.mean(amounts[-4:]) and abs(last - prev) > 0.15 * prev:
            continue
        nxt = days[-1] + timedelta(days=round(per))
        state = "active" if today <= nxt + timedelta(days=round(per * 0.5) + 5) else "ended"
        found.append({"key": key, "payee": rows[-1].get("destination_name") or key, "category": rows[-1].get("category_name"),
                      "period": name, "amount": round(last, 2), "prev_amount": round(prev, 2),
                      "last_date": days[-1].isoformat(), "next_date": nxt.isoformat(), "count": len(days), "state": state})
    return found


def update(splits: list[dict]) -> dict:
    """Upsert detections; push a note for new subscriptions and price changes."""
    found = detect(splits)
    new = changed = 0
    with store.connect() as con:
        old = {r["key"]: dict(r) for r in con.execute("SELECT * FROM subs")}
        first_run = not old
        for f in found:
            o = old.get(f["key"])
            stamp = store.now()
            if not o:
                new += 1
                con.execute("INSERT INTO subs (key,payee,category,period,amount,prev_amount,last_date,next_date,count,first_seen,changed,state) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (f["key"], f["payee"], f["category"], f["period"], f["amount"], f["prev_amount"], f["last_date"],
                             f["next_date"], f["count"], stamp, None, f["state"]))
                if not first_run and f["state"] == "active":
                    notify.send(f"Neues Abo erkannt: <b>{f['payee']}</b> {f['amount']:.2f} € / {f['period']}")
                continue
            price_moved = o["last_date"] != f["last_date"] and abs(f["amount"] - (o["amount"] or 0)) >= 0.01
            if price_moved:
                changed += 1
                notify.send(f"Preisänderung: <b>{f['payee']}</b> {o['amount']:.2f} € → {f['amount']:.2f} €")
            con.execute("UPDATE subs SET payee=?,category=?,period=?,amount=?,prev_amount=?,last_date=?,next_date=?,count=?,state=?,"
                        "changed=COALESCE(?,changed) WHERE key=?",
                        (f["payee"], f["category"], f["period"], f["amount"], o["amount"] if price_moved else o["prev_amount"],
                         f["last_date"], f["next_date"], f["count"], f["state"], stamp if price_moved else None, f["key"]))
        gone = set(old) - {f["key"] for f in found}
        for k in gone:
            con.execute("UPDATE subs SET state='ended' WHERE key=?", (k,))
    return {"subs": len(found), "new": new, "changed": changed}
