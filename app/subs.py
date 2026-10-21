"""Subscriptions = recurring withdrawals to the same payee at a steady rhythm and a similar amount."""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import date, timedelta

from . import notify, store
from .ai import norm_payee
from .config import NOT_SPENDING

# a bakery or a Döner place you visit regularly is a habit, not a subscription
NEVER_SUBS = {"Lebensmittel", "Restaurant & Lieferdienst", "Bargeld"}
PERIODS = [("weekly", 7, 2), ("monthly", 30.4, 4), ("quarterly", 91, 8), ("yearly", 365, 20)]


def _period(gaps: list[int]):
    """Fixed rhythm, allowing skipped cycles (Netflix paused a month): each gap counts as k periods."""
    for name, days, slack in PERIODS:
        ks = [max(1, round(g / days)) for g in gaps]
        norm = [g / k for g, k in zip(gaps, ks)]
        if sum(ks) > 2 * len(gaps):  # more skipped than paid: not a rhythm
            continue
        if abs(statistics.median(norm) - days) <= slack and all(abs(n - days) <= slack * 2 for n in norm[-4:]):
            return name, days
    return None, None


def detect(splits: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for s in splits:
        if s["type"] != "withdrawal" or (s.get("category_name") or "") in NOT_SPENDING | NEVER_SUBS:
            continue
        key = norm_payee(s.get("destination_name") or "") or norm_payee(s.get("description") or "")
        if key and key not in ("unbekannt",):
            groups[key].append(s)
    found = []
    today = date.today()
    for key, rows in groups.items():
        rows.sort(key=lambda r: r["date"])
        # the recurring charge is the one close to the latest amount; one-off purchases at the same
        # payee (a Netflix gift card, a second Google bill) and old prices further back drop out
        recent = [round(float(r["amount"]), 2) for r in rows[-6:]]
        mode, n = Counter(recent).most_common(1)[0]
        last_amt = mode if n > 1 else recent[-1]
        chain = [r for r in rows if abs(float(r["amount"]) - last_amt) <= 0.2 * last_amt]
        # walk back through price steps (Google 20.99 -> 23.99 -> 27.99): an older charge joins while it is
        # within 20% of the oldest one kept so far and its month has no charge yet
        months = {r["date"][:7] for r in chain}
        for r in reversed([r for r in rows if r not in chain]):
            oldest = min(chain, key=lambda c: c["date"])
            if r["date"] < oldest["date"] and r["date"][:7] not in months and \
                    abs(float(r["amount"]) - float(oldest["amount"])) <= 0.2 * float(oldest["amount"]):
                chain.append(r)
                months.add(r["date"][:7])
        chain.sort(key=lambda r: r["date"])
        days = sorted({date.fromisoformat(r["date"][:10]) for r in chain})
        if len(days) < 2:
            continue
        gaps = [(b - a).days for a, b in zip(days, days[1:])]
        name, per = _period(gaps)
        if not name or (name != "yearly" and len(days) < 3):
            continue
        chain = [r for r in chain if r["date"][:10] in {d.isoformat() for d in days}]
        amounts = [float(r["amount"]) for r in chain]
        last, prev = amounts[-1], amounts[-2]
        # a bakery you pass every week is not a subscription: weekly needs a fixed price
        if name == "weekly" and statistics.pstdev(amounts[-4:]) > 0.05 * statistics.mean(amounts[-4:]):
            continue
        nxt = days[-1] + timedelta(days=round(per))
        state = "active" if today <= nxt + timedelta(days=round(per * 0.5) + 5) else "ended"
        found.append({"key": key, "payee": chain[-1].get("destination_name") or key, "category": chain[-1].get("category_name"),
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
