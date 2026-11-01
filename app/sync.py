"""One sync run (systemd timer, or 'Jetzt synchronisieren'): pull every source into Firefly, categorize,
pair own-account transfers, snapshot the portfolio, refresh subscriptions, and nag before consents expire."""
from __future__ import annotations

import hashlib
import re
import json
import logging
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

from . import ai, cashback, depot, eb, firefly, insights, notify, store, subs, tr
from .config import HISTORY_DAYS, REDIRECT_URL, REMIND_DAYS

log = logging.getLogger("sync")


def _since(last: float | None, slack_days: int) -> date:
    if not last:
        return date.today() - timedelta(days=HISTORY_DAYS)
    return datetime.fromtimestamp(last).date() - timedelta(days=slack_days)


def _book(c, con, acc: dict, rows: list[dict], new: list[dict]) -> int:
    """rows: {key, date, amount, payee, purpose, category?}. Skips anything already booked."""
    n = 0
    for r in rows:
        if con.execute("SELECT 1 FROM seen WHERE key=?", (r["key"],)).fetchone():
            continue
        r["payee"] = ai.merchant(r["payee"], r["purpose"])
        try:
            gid, jid, cat = firefly.add(c, account_id=acc["firefly_id"], amount=r["amount"], day=r["date"],
                                        payee=r["payee"], description=r["purpose"] or r["payee"],
                                        external_id=r["key"][:250], notes=r.get("notes", ""), category=r.get("category"))
        except firefly.Duplicate:
            con.execute("INSERT OR IGNORE INTO seen VALUES (?,?,?)", (r["key"], None, store.now()))
            continue
        con.execute("INSERT OR IGNORE INTO seen VALUES (?,?,?)", (r["key"], jid, store.now()))
        if r.get("fund"):  # the DKB debit that paid this PayPal purchase is a top-up, not a second purchase
            f = r["fund"]
            firefly.set_category(c, f["group_id"], f["transaction_journal_id"], "Umbuchung")
            try:
                firefly.add(c, account_id=acc["firefly_id"], amount=float(f["amount"]), day=f["date"][:10], payee=f["source_name"] or "Bank",
                            description=f"PayPal-Aufladung für {r['payee']}", external_id=(r["key"] + ":fund")[:250], category="Umbuchung")
            except firefly.Duplicate:
                pass
        con.commit()
        n += 1
        if not cat:
            new.append(r | {"group_id": gid, "journal_id": jid})
    return n


def _eb_rows(uid: str, txs: list[dict]) -> list[dict]:
    rows = []
    for t in txs:
        if t.get("status") not in (None, "BOOK"):
            continue  # pending lines change id/amount when they book; take them once booked
        amt = float(t["transaction_amount"]["amount"])
        if t.get("credit_debit_indicator") == "DBIT":
            amt = -abs(amt)
        party = (t.get("creditor") if amt < 0 else t.get("debtor")) or {}
        purpose = " ".join(t.get("remittance_information") or []).strip()
        ref = t.get("entry_reference") or t.get("transaction_id") or hashlib.sha1(json.dumps(
            [t.get("booking_date"), amt, party.get("name"), purpose], sort_keys=True).encode()).hexdigest()[:20]
        rows.append({"key": f"eb:{uid}:{ref}", "date": t.get("booking_date") or t.get("value_date") or t.get("transaction_date"),
                     "amount": amt, "payee": (party.get("name") or purpose[:60] or "").strip(), "purpose": purpose})
    return rows


PAYPAL_DEBIT = re.compile(r"paypal|\bPP\.\d|ihr einkauf bei", re.I)


def _paypal_funding(c, con, acc: dict, rows: list[dict]) -> None:
    """PayPal purchases are paid by a bank debit ('… PP.6299.PP … Ihr Einkauf bei Google') that is already booked.
    Pair each new PayPal purchase with that debit (same amount, bank books it 0-7 days later): the purchase keeps
    the debit's category, and the debit becomes a transfer to PayPal -- so it is counted once."""
    todo = [r for r in rows if r["amount"] < 0 and not con.execute("SELECT 1 FROM seen WHERE key=?", (r["key"],)).fetchone()]
    if not todo:
        return
    days = [date.fromisoformat(r["date"]) for r in todo]
    pool = [s for s in firefly.splits(c, min(days) - timedelta(days=3), max(days) + timedelta(days=10))
            if s["type"] == "withdrawal" and s["source_id"] != acc["firefly_id"] and s.get("category_name") != "Umbuchung"
            and PAYPAL_DEBIT.search(f"{s.get('description') or ''} {s.get('notes') or ''}")]
    for r, d in zip(todo, days):
        best = None
        for s in pool:
            gap = (date.fromisoformat(s["date"][:10]) - d).days
            if abs(float(s["amount"]) - abs(r["amount"])) < 0.005 and -2 <= gap <= 7 and (best is None or abs(gap) < best[0]):
                best = (abs(gap), s)
        if not best:  # partly paid from the PayPal balance: the bank debit is smaller, but names the same shop
            first = (ai.norm_payee(r["payee"]).split() or [""])[0]
            for s in pool:
                gap = (date.fromisoformat(s["date"][:10]) - d).days
                if first and (ai.norm_payee(s.get("destination_name") or "").split() or [""])[0] == first \
                        and float(s["amount"]) < abs(r["amount"]) and -2 <= gap <= 7 and (best is None or abs(gap) < best[0]):
                    best = (abs(gap), s)
        if best:
            s = best[1]
            pool.remove(s)
            r["fund"] = s
            if s.get("category_name") and s["category_name"] != "Sonstiges":
                r["category"] = s["category_name"]


def pair_late_paypal(c) -> int:
    """The bank debit can book AFTER the PayPal purchase was already synced (purchase 28th, 'Ihr Einkauf bei' debit 29th),
    so _paypal_funding never saw it. Pair such leftovers now: debit -> Umbuchung + top-up into PayPal, purchase counted once."""
    with store.connect() as con:
        pp = {r["firefly_id"] for r in con.execute("SELECT firefly_id FROM accounts WHERE lower(name) LIKE 'paypal%'")}
    if not pp:
        return 0
    sp = firefly.splits(c, date.today() - timedelta(days=30), date.today())
    debits = [s for s in sp if s["type"] == "withdrawal" and s["source_id"] not in pp and s.get("category_name") != "Umbuchung"
              and PAYPAL_DEBIT.search(f"{s.get('description') or ''} {s.get('notes') or ''}")]
    buys = [s for s in sp if s["type"] == "withdrawal" and s["source_id"] in pp and s.get("category_name") != "Umbuchung"]
    topups = [s for s in sp if (s.get("description") or "").startswith("PayPal-Aufladung")]
    n = 0
    for s in debits:
        d = date.fromisoformat(s["date"][:10])
        first = (ai.norm_payee(s.get("destination_name") or "").split() or [""])[0]
        best = None
        for b in buys:
            gap = (d - date.fromisoformat(b["date"][:10])).days
            bd = date.fromisoformat(b["date"][:10])
            if not first or (ai.norm_payee(b.get("destination_name") or "").split() or [""])[0] != first:
                continue
            if float(s["amount"]) > float(b["amount"]) + 0.005 or not -2 <= gap <= 7:
                continue
            if any(t["description"] == f"PayPal-Aufladung für {b.get('destination_name')}" and abs((date.fromisoformat(t["date"][:10]) - bd).days) <= 9
                   for t in topups):
                continue  # this purchase already has its debit
            if best is None or abs(gap) < best[0]:
                best = (abs(gap), b)
        if not best:
            continue
        b = best[1]
        buys.remove(b)
        try:
            firefly.add(c, account_id=b["source_id"], amount=float(s["amount"]), day=s["date"][:10], payee=s.get("source_name") or "Bank",
                        description=f"PayPal-Aufladung für {b.get('destination_name')}", external_id=("late:" + s["transaction_journal_id"])[:250],
                        category="Umbuchung")
        except firefly.Duplicate:
            continue
        if s.get("category_name") and s["category_name"] not in ("Sonstiges", "Umbuchung") and b.get("category_name") in (None, "Sonstiges"):
            firefly.set_category(c, b["group_id"], b["transaction_journal_id"], s["category_name"])
        firefly.set_category(c, s["group_id"], s["transaction_journal_id"], "Umbuchung")
        n += 1
    return n


def sync_eb(c, summary: dict, new: list[dict]) -> None:
    if not eb.configured():
        return
    with store.connect() as con:
        accts = [dict(r) for r in con.execute("SELECT * FROM accounts WHERE source='eb'")]
        links = {r["session_id"]: dict(r) for r in con.execute("SELECT * FROM links")}
        for a in accts:
            link = links.get(a["session_id"])
            if not link or (link["valid_until"] and link["valid_until"] < datetime.now(timezone.utc).isoformat()):
                con.execute("UPDATE accounts SET error=? WHERE key=?", ("Zugriff abgelaufen – neu verbinden", a["key"]))
                continue
            uid = a["key"][3:]
            try:
                bal = eb.balance(uid)
                rows = [r for r in _eb_rows(uid, eb.transactions(uid, _since(a["last_sync"], 10).isoformat())) if r["date"]]
                if a["name"].lower().startswith("paypal"):
                    _paypal_funding(c, con, a, rows)
                n = _book(c, con, a, rows, new)
                con.execute("UPDATE accounts SET balance=?, balance_at=?, last_sync=?, error=NULL WHERE key=?",
                            (bal, store.now(), store.now(), a["key"]))
                summary[a["name"]] = n
                if not a["last_sync"] and rows and bal is not None:
                    _opening(c, a, bal, min(r["date"] for r in rows))
            except Exception as e:  # noqa: BLE001 - one bank failing must not stop the others
                log.warning("%s: %s", a["name"], e)
                con.execute("UPDATE accounts SET error=? WHERE key=?", (str(e)[:300], a["key"]))
                summary[a["name"]] = f"error: {str(e)[:80]}"


def _opening(c, acc: dict, bank_balance: float, earliest: str) -> None:
    """First import: back-solve the opening balance so Firefly's balance matches the bank's."""
    booked = firefly.account_balance(c, acc["firefly_id"])
    firefly.set_opening_balance(c, acc["firefly_id"], round(bank_balance - booked, 2),
                                date.fromisoformat(earliest) - timedelta(days=1))


def sync_tr(c, summary: dict, new: list[dict]) -> None:
    if not tr.configured():
        return
    with store.connect() as con:
        a = con.execute("SELECT * FROM accounts WHERE key='tr:cash'").fetchone()
        if not a:
            fid = firefly.ensure_asset(c, "Trade Republic", None)
            con.execute("INSERT INTO accounts (key,source,name,firefly_id) VALUES ('tr:cash','tr','Trade Republic',?)", (fid,))
            a = con.execute("SELECT * FROM accounts WHERE key='tr:cash'").fetchone()
        a = dict(a)
    t = tr.session()
    if not t:   # TR caps a login at 24 h; no auto prompt, you log in from Konten when you want fresh data
        with store.connect() as con:
            con.execute("UPDATE accounts SET error=? WHERE key='tr:cash'", ("Login nötig – in der TR-App bestätigen", ))
        summary["Trade Republic"] = "needs login"
        return
    try:
        pf = tr.portfolio(t)
        since = _since(a["last_sync"], 7)
        txs = tr.transactions(t, datetime.combine(since, datetime.min.time()).timestamp())
        rows = [{"key": f"tr:{x['id']}", "date": x["date"], "amount": x["amount"], "payee": x["payee"],
                 "purpose": x["purpose"] or str(x["kind"]), "notes": str(x["kind"]),
                 "category": "Investments" if x["invest"] else None} for x in txs]
        with store.connect() as con:
            n = _book(c, con, a, rows, new)
            con.execute("INSERT INTO portfolio VALUES (?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET value=excluded.value,"
                        "cash=excluded.cash, invested=excluded.invested, positions=excluded.positions",
                        (date.today().isoformat(), pf["value"], pf["cash"], pf["invested"], json.dumps(pf["positions"])))
            con.execute("UPDATE accounts SET balance=?, balance_at=?, last_sync=?, error=NULL WHERE key='tr:cash'",
                        (pf["cash"], store.now(), store.now()))
        if not a["last_sync"] and rows:
            _opening(c, a, pf["cash"], min(r["date"] for r in rows))
        summary["Trade Republic"] = n
        try:
            summary["depot"] = depot.rebuild(t, pf["positions"])
        except Exception as e:  # noqa: BLE001 - the chart history is a nice-to-have
            log.warning("depot history: %s", e)
            summary["depot"] = f"error: {str(e)[:80]}"
    except Exception as e:  # noqa: BLE001
        log.warning("tr: %s", e)
        with store.connect() as con:
            con.execute("UPDATE accounts SET error=? WHERE key='tr:cash'", (str(e)[:300],))
        summary["Trade Republic"] = f"error: {str(e)[:80]}"


OWN_PARTIES = re.compile(r"paypal|trade republic|dkb|deutsche kreditbank", re.I)


def _own_names() -> set[str]:
    """Account holder names ('DKB Erika Mustermann' -> 'erika mustermann') to tell your own transfers apart."""
    with store.connect() as con:
        names = [r["name"] for r in con.execute("SELECT name FROM accounts")]
    return {" ".join(n.lower().split()[1:3]) for n in names if len(n.split()) >= 3}


def _is_own(payee: str, own: set[str]) -> bool:
    p = (payee or "").lower()
    return bool(OWN_PARTIES.search(p)) or any(o and o in p for o in own)


def own_transfer(payee: str, purpose: str, own: set[str]) -> bool:
    """Money to/from yourself: your own name as the other side (DKB 'Erika Mustermann' ↔ TR), or a bare PayPal
    top-up/payout ('PAYPAL INSTANT TRANSFER', TR 'Gesendet' to PayPal) -- not a PayPal purchase ('Ihr Einkauf bei X')."""
    p = (payee or "").lower()
    if any(o and o in p for o in own):
        return True
    return ai.norm_payee(payee) in ("paypal", "pay pal") and "einkauf bei" not in (purpose or "").lower()


def categorize(c, new: list[dict]) -> int:
    if not new:
        return 0
    # what you (or earlier runs) decided for the same payee wins over asking the model
    own = _own_names()
    # money in and money out are remembered apart: an employer's payments in are Gehalt even if a refund to it was not
    memory: dict[tuple, Counter] = defaultdict(Counter)
    for s in firefly.splits(c, date.today() - timedelta(days=HISTORY_DAYS), date.today()):
        cat = s.get("category_name")
        other = s.get("destination_name") if s["type"] == "withdrawal" else s.get("source_name")
        if cat and cat not in ("Sonstiges", "Umbuchung") and not own_transfer(other, "", own):
            memory[(s["type"] == "deposit", ai.norm_payee(other or ""))][cat] += 1
    rules = store.get("payee_rules", {}) or {}  # set from the dashboard: "all bookings with this payee"
    todo = []
    for r in new:
        if own_transfer(r["payee"], r["purpose"], own):
            r["category"] = "Umbuchung"
            continue
        rule = rules.get(f"{'deposit' if r['amount'] > 0 else 'withdrawal'}:{ai.norm_payee(r['payee']) or ai.norm_payee(r['purpose'])}")
        if rule:
            r["category"] = rule
            continue
        k = (r["amount"] > 0, ai.norm_payee(r["payee"]) or ai.norm_payee(r["purpose"]))
        if k[1] and memory.get(k):
            r["category"] = memory[k].most_common(1)[0][0]
        else:
            todo.append(r)
    for r, cat in zip(todo, ai.categorize(todo)):
        # Umbuchung means between your own accounts; the model also says it for an employer's "Abschlag/Vorschuss"
        if cat == "Umbuchung" and not _is_own(r["payee"], own):
            cat = "Sonstige Einnahmen" if r["amount"] > 0 else "Sonstiges"
        r["category"] = cat
    # the model judges line by line and can call one AWS bill "Software" and the next "Lebensmittel":
    # within one run, every payee gets its majority category
    votes: dict[tuple, Counter] = defaultdict(Counter)
    for r in todo:
        votes[(r["amount"] > 0, ai.norm_payee(r["payee"]))][r["category"]] += 1
    for r in todo:
        k = (r["amount"] > 0, ai.norm_payee(r["payee"]))
        if k[1] and sum(votes[k].values()) > 1:
            r["category"] = votes[k].most_common(1)[0][0]
    for r in new:
        try:
            firefly.set_category(c, r["group_id"], r["journal_id"], r["category"])
        except Exception as e:  # noqa: BLE001
            log.warning("category: %s", e)
    return len(new)


def pair_transfers(c) -> int:
    """Money moving between your own accounts shows up twice (DKB −50, TR +50): mark both 'Umbuchung'."""
    with store.connect() as con:
        own = {r["firefly_id"] for r in con.execute("SELECT firefly_id FROM accounts WHERE firefly_id IS NOT NULL")}
    sp = [s for s in firefly.splits(c, date.today() - timedelta(days=HISTORY_DAYS + 30), date.today())
          if s.get("category_name") != "Investments" and s["type"] in ("withdrawal", "deposit")]
    # PayPal top-ups were already paired with their bank debit when booked; never pair them again
    sp = [s for s in sp if not (s.get("description") or "").startswith("PayPal-Aufladung")]
    names = _own_names()
    outs = [s for s in sp if s["type"] == "withdrawal" and s["source_id"] in own]
    ins = [s for s in sp if s["type"] == "deposit" and s["destination_id"] in own]
    used, n = set(), 0
    for o in outs:
        od = date.fromisoformat(o["date"][:10])
        for i in ins:
            if i["transaction_journal_id"] in used or i["destination_id"] == o["source_id"]:
                continue
            if o.get("category_name") == i.get("category_name") == "Umbuchung":
                continue
            # same amount by chance (a 10 € card payment vs 10 € from a friend) is not a transfer: one side must name you or a bank
            if not (_is_own(o.get("destination_name"), names) or _is_own(i.get("source_name"), names)):
                continue
            if abs(float(i["amount"]) - float(o["amount"])) < 0.005 and abs((date.fromisoformat(i["date"][:10]) - od).days) <= 4:
                used.add(i["transaction_journal_id"])
                for s in (o, i):
                    firefly.set_category(c, s["group_id"], s["transaction_journal_id"], "Umbuchung")
                n += 1
                break
    return n


def remind_consents() -> None:
    soon = (datetime.now(timezone.utc) + timedelta(days=REMIND_DAYS)).isoformat()
    with store.connect() as con:
        for l in con.execute("SELECT * FROM links WHERE valid_until < ? AND reminded < ?", (soon, time.time() - 86400)):
            notify.send(f"{l['aspsp']}: Kontozugriff läuft am {l['valid_until'][:10]} ab – einmal neu verbinden{': ' + REDIRECT_URL if REDIRECT_URL else ''}", "alerts")
            con.execute("UPDATE links SET reminded=? WHERE session_id=?", (time.time(), l["session_id"]))


def run() -> dict:
    started = time.time()
    summary: dict = {}
    new: list[dict] = []
    ok = 1
    try:
        with firefly.client() as c:
            firefly.ensure_categories(c)
            sync_eb(c, summary, new)
            sync_tr(c, summary, new)
            summary["categorized"] = categorize(c, new)
            summary["paypal_late"] = pair_late_paypal(c)
            summary["transfers"] = pair_transfers(c)
            summary["cashback"] = cashback.split(c)
            all_splits = firefly.splits(c, date.today() - timedelta(days=HISTORY_DAYS), date.today())
            summary |= subs.update(all_splits)
            summary["unusual"] = insights.notify_new_anomalies(all_splits)
        remind_consents()
        try:
            summary["report"] = insights.maybe_month_report()
        except Exception as e:  # noqa: BLE001
            log.warning("month report: %s", e)
    except Exception as e:  # noqa: BLE001
        log.exception("sync failed")
        summary["error"] = str(e)[:300]
        ok = 0
        if not store.get("sync_fail_sent"):  # once per outage, an all-clear when it works again
            store.put("sync_fail_sent", notify.send(f"Sync fehlgeschlagen: <code>{summary['error'][:200]}</code>", "alerts"))
    if ok and store.get("sync_fail_sent"):
        store.put("sync_fail_sent", False)
        notify.send("✅ Sync läuft wieder", "alerts")
    with store.connect() as con:
        con.execute("INSERT INTO runs (started,finished,ok,summary) VALUES (?,?,?,?)",
                    (started, time.time(), ok, json.dumps(summary)))
        con.execute("DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT 200)")
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "pytr", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    print(json.dumps(run(), ensure_ascii=False))
    # a bank linked or 'sync now' tapped while this run was busy: go again so it isn't left for the next timer
    for _ in range(2):
        if not store.get("sync_again"):
            break
        store.put("sync_again", False)
        print(json.dumps(run(), ensure_ascii=False))
    sys.exit(0)
