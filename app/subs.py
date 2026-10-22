"""Subscriptions = recurring withdrawals to the same payee at a steady rhythm and a similar amount."""
from __future__ import annotations

import json
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


PER_DAYS = {name: days for name, days, _ in PERIODS}


def guess_period(days: list[date]) -> str:
    if len(days) < 2:
        return "monthly"
    gaps = [(b - a).days for a, b in zip(days, days[1:])]
    name, _ = _period(gaps)
    if name:
        return name
    med = statistics.median(gaps)
    return min(PER_DAYS, key=lambda k: abs(PER_DAYS[k] - med))


def pkey_of(s: dict) -> str:
    other = s.get("destination_name") if s["type"] == "withdrawal" else s.get("source_name")
    return norm_payee(other or "") or norm_payee(s.get("description") or "")


def matches(sub: dict, splits: list[dict]) -> list[dict]:
    """Bookings that belong to a hand-picked subscription: same payee key, amount within 25% (a price step).
    Income ('in') looks at deposits, and at one account only when it was picked from one (Taschengeld on DKB,
    not the odd PayPal from the same person)."""
    want = "deposit" if sub.get("kind") == "in" else "withdrawal"
    acct = sub.get("match_account")
    out = []
    for s in splits:
        if s["type"] != want or pkey_of(s) != sub["pkey"]:
            continue
        if acct and (s.get("destination_name") if want == "deposit" else s.get("source_name")) != acct:
            continue
        if sub.get("match_amount") and abs(float(s["amount"]) - sub["match_amount"]) > 0.25 * sub["match_amount"]:
            continue
        out.append(s)
    return sorted(out, key=lambda s: s["date"])


def share_rows(sub: dict, splits: list[dict]) -> list[tuple[dict, dict]]:
    """Deposits that are a friend's part of this Abo: same payer, amount within 40% of what they pay."""
    shares = json.loads(sub.get("shares") or "[]")
    out = []
    for s in splits:
        if s["type"] != "deposit":
            continue
        k = pkey_of(s)
        for sh in shares:
            if sh["pkey"] == k and abs(float(s["amount"]) - sh["amount"]) <= 0.4 * sh["amount"]:
                out.append((s, sh))
                break
    return out


def share_view(sub: dict, splits: list[dict]) -> list[dict]:
    """Per payer: latest part and whether they still pay (a payment within ~1.5 periods)."""
    per = PER_DAYS.get(sub.get("period") or "monthly", 30.4)
    last: dict[str, dict] = {}
    for s, sh in share_rows(sub, splits):
        if sh["pkey"] not in last or s["date"] > last[sh["pkey"]]["date"]:
            last[sh["pkey"]] = s
    out = []
    for sh in json.loads(sub.get("shares") or "[]"):
        s = last.get(sh["pkey"])
        d = s["date"][:10] if s else None
        active = bool(d) and (date.today() - date.fromisoformat(d)).days <= per * 1.5 + 5
        out.append({"pkey": sh["pkey"], "payee": sh.get("payee") or sh["pkey"], "amount": round(float(s["amount"]), 2) if s else sh["amount"],
                    "last_date": d, "active": active})
    return out


def refresh_manual(con, sub: dict, splits: list[dict]) -> dict | None:
    """Recompute last/next charge and price of a hand-picked subscription from the bookings."""
    rows = matches(sub, splits)
    if not rows:
        return None
    per = PER_DAYS[sub["period"]]
    last = rows[-1]
    amt = round(float(last["amount"]), 2)
    prev = round(float(rows[-2]["amount"]), 2) if len(rows) > 1 else amt
    if sub.get("kind") == "in" and not sub.get("match_amount"):
        # a salary paid in parts (Abschlag + Abrechnung): the usual month total of the last 3 finished months
        by_month: dict[str, float] = defaultdict(float)
        for r in rows:
            by_month[r["date"][:7]] += float(r["amount"])
        done = [v for k, v in sorted(by_month.items()) if k < date.today().strftime("%Y-%m")][-3:] or list(by_month.values())
        if sub["period"] == "monthly":
            amt = prev = round(sum(done) / len(done), 2)
    last_day = date.fromisoformat(last["date"][:10])
    nxt = last_day + timedelta(days=round(per))
    if sub["period"] == "monthly":  # same day next month (Taschengeld on the 1st stays on the 1st)
        y, m = divmod(last_day.month, 12)
        nxt = date(last_day.year + y, m + 1, min(last_day.day, 28 if m + 1 == 2 else 30 if m + 1 in (4, 6, 9, 11) else 31))
    state = "active" if date.today() <= nxt + timedelta(days=round(per * 0.5) + 5) else "ended"
    moved = sub.get("kind") != "in" and sub.get("last_date") and sub["last_date"] != last_day.isoformat() \
        and abs(amt - (sub.get("amount") or amt)) >= 0.01
    if moved:
        notify.send(f"Preisänderung: <b>{sub.get('name') or sub['payee']}</b> {sub['amount']:.2f} € → {amt:.2f} €")
    con.execute("UPDATE subs SET amount=?, prev_amount=?, last_date=?, next_date=?, count=?, state=?, match_amount=?, "
                "changed=COALESCE(?,changed) WHERE key=?",
                (amt, sub["amount"] if moved else prev, last_day.isoformat(), nxt.isoformat(), len(rows), state,
                 amt if sub.get("kind") != "in" or sub.get("match_amount") else None,
                 store.now() if moved else None, sub["key"]))
    return {"amount": amt, "last_date": last_day.isoformat(), "next_date": nxt.isoformat(), "state": state}


def update(splits: list[dict]) -> dict:
    """Upsert detections; push a note for new subscriptions and price changes."""
    with store.connect() as con:
        manual = [dict(r) for r in con.execute("SELECT * FROM subs WHERE manual=1")]
        for m in manual:
            refresh_manual(con, m, splits)
    # a hand-picked subscription wins over the detector for the same payee and price
    found = [f for f in detect(splits) if not any(
        m.get("kind") != "in" and m["pkey"] == f["key"] and abs(f["amount"] - (m["match_amount"] or f["amount"])) <= 0.25 * f["amount"] for m in manual)]
    new = changed = 0
    with store.connect() as con:
        old = {r["key"]: dict(r) for r in con.execute("SELECT * FROM subs WHERE manual=0")}
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
