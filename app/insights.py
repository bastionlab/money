"""Things worth a look that aren't a ledger total: unusual charges, net worth over time, the month-end report."""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta

from . import firefly, notify, store
from .ai import norm_payee
from .config import NOT_SPENDING

# groceries and rides vary by nature; a 3x Ullrich basket is not worth a ping
CALM = {"Lebensmittel", "Restaurant & Lieferdienst", "Bargeld", "Mobilität"}


def pkey(s: dict) -> str:
    other = s.get("destination_name") if s["type"] == "withdrawal" else s.get("source_name")
    return norm_payee(other or "") or norm_payee(s.get("description") or "")


def anomalies(splits: list[dict], start: date, end: date) -> list[dict]:
    """Withdrawals in [start, end] that stand out: far above what that payee usually costs, or charged twice the same day."""
    by_payee: dict[str, list[dict]] = defaultdict(list)
    for s in splits:
        if s["type"] == "withdrawal" and s.get("category_name") not in NOT_SPENDING:
            by_payee[pkey(s)].append(s)
    ok = set(store.get("anomaly_ok", []) or [])
    out = []
    for key, rows in by_payee.items():
        if not key or key.startswith(("paypal", "klarna")):  # a middleman's lines mix every shop
            continue
        rows.sort(key=lambda s: s["date"])
        seen_day: dict[tuple, dict] = {}
        for i, s in enumerate(rows):
            d = date.fromisoformat(s["date"][:10])
            amt = float(s["amount"])
            jid = s["transaction_journal_id"]
            dup = seen_day.get((s["date"][:10], round(amt, 2)))
            seen_day[(s["date"][:10], round(amt, 2))] = s
            if not (start <= d <= end) or jid in ok:
                continue
            if dup and amt >= 5:
                out.append({"journal_id": jid, "group_id": s["group_id"], "date": s["date"][:10], "amount": -amt,
                            "payee": s.get("destination_name"), "category": s.get("category_name"), "kind": "duplicate",
                            "reason": f"zweimal {amt:.2f} € am selben Tag"})
                continue
            before = [float(r["amount"]) for r in rows[:i]]
            if len(before) < 4 or s.get("category_name") in CALM:
                continue
            med = statistics.median(before[-12:])
            if amt >= 20 and amt > max(3 * med, med + 30):
                out.append({"journal_id": jid, "group_id": s["group_id"], "date": s["date"][:10], "amount": -amt,
                            "payee": s.get("destination_name"), "category": s.get("category_name"), "kind": "high",
                            "reason": f"sonst ~{med:.2f} €, jetzt {amt:.2f} €"})
    return sorted(out, key=lambda x: x["date"], reverse=True)


def notify_new_anomalies(splits: list[dict]) -> int:
    """Ping once per unusual charge booked in the last 10 days."""
    sent = set(store.get("anomaly_sent", []) or [])
    new = [a for a in anomalies(splits, date.today() - timedelta(days=10), date.today()) if a["journal_id"] not in sent]
    for a in new:
        icon = "⚠️" if a["kind"] == "duplicate" else "📈"
        notify.send(f"{icon} Auffällig: <b>{a['payee']}</b> {abs(a['amount']):.2f} € ({a['date'][8:10]}.{a['date'][5:7]}.) – {a['reason']}")
        sent.add(a["journal_id"])
    store.put("anomaly_sent", sorted(sent)[-2000:])
    return len(new)


def networth(c) -> dict:
    """Daily balance of every imported account (walked back from Firefly's current balance through its bookings)
    plus the depot value -- cash and depot over time."""
    with store.connect() as con:
        accs = [dict(r) for r in con.execute("SELECT key,name,firefly_id FROM accounts WHERE firefly_id IS NOT NULL")]
    start = date.today() - timedelta(days=430)
    sp = firefly.splits(c, start, date.today() + timedelta(days=1))
    days = [start + timedelta(days=i) for i in range((date.today() - start).days + 1)]
    series: dict[str, list[float]] = {}
    for a in accs:
        bal = firefly.account_balance(c, a["firefly_id"])
        delta: dict[str, float] = defaultdict(float)
        for s in sp:
            amt = float(s["amount"])
            if s.get("source_id") == a["firefly_id"]:
                delta[s["date"][:10]] -= amt
            if s.get("destination_id") == a["firefly_id"]:
                delta[s["date"][:10]] += amt
        vals = []
        for d in reversed(days):
            vals.append(round(bal, 2))
            bal -= delta.get(d.isoformat(), 0.0)
        series[a["name"]] = list(reversed(vals))
    depot = {d: v for d, v, _ in (store.get("depot") or {}).get("total", [])}
    dep, last = [], 0.0
    for d in days:
        last = depot.get(d.isoformat(), last)
        dep.append(round(last, 2))
    # before an account's first booking its balance is unknown, not 0: start once every account with money has begun
    firsts = []
    for a in accs:
        mine = [s["date"][:10] for s in sp if a["firefly_id"] in (s.get("source_id"), s.get("destination_id"))]
        if mine:
            i0 = next(i for i, d in enumerate(days) if d.isoformat() >= min(mine))
            v = series[a["name"]]
            v[:i0] = [v[i0]] * i0  # before its first booking: assume it held what it started with
            if abs(v[i0]) > 1000:  # only a big account's unknown start would bend the curve
                firsts.append(min(mine))
    begin = max(firsts) if firsts else days[0].isoformat()
    first = next((i for i, d in enumerate(days) if d.isoformat() >= begin), 0)
    return {"days": [d.isoformat() for d in days[first:]], "accounts": {k: v[first:] for k, v in series.items()},
            "depot": dep[first:]}


MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"]


def _eur(n: float) -> str:
    return f"{n:,.0f} €".replace(",", ".")


def month_report(month: str) -> str:
    from .api import _ledger  # the same numbers the page shows
    L = _ledger(month)
    y, m = map(int, month.split("-"))
    start = date(y, m, 1)
    end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    saldo = L["income"] - L["spending"]
    lines = [f"📊 <b>{MONTHS[m - 1]} {y}</b>",
             f"Ein {_eur(L['income'])} · Aus {_eur(L['spending'])} · Saldo <b>{'+' if saldo >= 0 else '−'}{_eur(abs(saldo))}</b>"]
    if L.get("avg_spending"):
        diff = L["spending"] - L["avg_spending"]
        lines.append(f"Ausgaben {'über' if diff > 0 else 'unter'} Ø der 3 Vormonate: {'+' if diff > 0 else '−'}{_eur(abs(diff))}")
    if L["income"] > 0:
        lines.append(f"Sparquote {100 * saldo / L['income']:.0f} %")
    top = L["categories"][:3]
    if top:
        lines.append("Top: " + ", ".join(f"{c['name']} {_eur(c['amount'])}" for c in top))
    big = sorted((t for t in L["transactions"] if t["amount"] < 0 and t["category"] not in NOT_SPENDING), key=lambda t: t["amount"])[:3]
    if big:
        lines.append("Größte: " + ", ".join(f"{t['alias'] or t['payee']} {_eur(-t['amount'])}" for t in big))
    with store.connect() as con:
        subs = [dict(r) for r in con.execute("SELECT * FROM subs WHERE dismissed=0 AND state='active' AND COALESCE(kind,'out')='out'")]
    per = {"weekly": 4.33, "monthly": 1, "quarterly": 1 / 3, "yearly": 1 / 12}
    lines.append(f"Abos: {_eur(sum(x['amount'] * per[x['period']] for x in subs))} / Monat")
    t0 = datetime.combine(start, datetime.min.time()).timestamp()
    t1 = datetime.combine(end + timedelta(days=1), datetime.min.time()).timestamp()
    moved = [x for x in subs if x.get("changed") and t0 <= x["changed"] < t1]
    new = [x for x in subs if x.get("first_seen") and t0 <= x["first_seen"] < t1]
    if new:
        lines.append("Neue Abos: " + ", ".join(x.get("name") or x["payee"] for x in new))
    if moved:
        lines.append("Teurer: " + ", ".join(f"{x.get('name') or x['payee']} {x['prev_amount']:.2f} → {x['amount']:.2f} €" for x in moved))
    with firefly.client() as c:
        sp = firefly.splits(c, start - timedelta(days=400), end)
        nw = networth(c)
    odd = anomalies(sp, start, end)
    if odd:
        lines.append(f"Auffällig: {len(odd)} – " + ", ".join(f"{a['payee']} {abs(a['amount']):.0f} €" for a in odd[:3]))
    days = nw["days"]
    if days:
        def worth(i):
            return sum(v[i] for v in nw["accounts"].values()) + nw["depot"][i]
        i_end = max(i for i, d in enumerate(days) if d <= end.isoformat())
        i_start = next((i for i, d in enumerate(days) if d >= start.isoformat()), 0)
        i_prev = max(0, i_start - 1)
        lines.append(f"Vermögen: {_eur(worth(i_end))} ({'+' if worth(i_end) >= worth(i_prev) else '−'}{_eur(abs(worth(i_end) - worth(i_prev)))} im Monat)")
    return "\n".join(lines)


def maybe_month_report() -> bool:
    """In the first days of a month, once: last month's report."""
    today = date.today()
    if today.day > 5:
        return False
    prev = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    if store.get("month_report_sent") == prev:
        return False
    if notify.send(month_report(prev)):
        store.put("month_report_sent", prev)
        return True
    return False
