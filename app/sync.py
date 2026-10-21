"""One sync run (systemd timer, or 'Jetzt synchronisieren'): pull every source into Firefly, categorize,
pair own-account transfers, snapshot the portfolio, refresh subscriptions, and nag before consents expire."""
from __future__ import annotations

import hashlib
import json
import logging
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

from . import ai, eb, firefly, notify, store, subs, tr
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
        rows.append({"key": f"eb:{uid}:{ref}", "date": t.get("booking_date") or t.get("value_date"),
                     "amount": amt, "payee": (party.get("name") or purpose[:60] or "").strip(), "purpose": purpose})
    return rows


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
                rows = _eb_rows(uid, eb.transactions(uid, _since(a["last_sync"], 10).isoformat()))
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
    if not t:
        t = _tr_relogin()
    if not t:
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
    except Exception as e:  # noqa: BLE001
        log.warning("tr: %s", e)
        with store.connect() as con:
            con.execute("UPDATE accounts SET error=? WHERE key='tr:cash'", (str(e)[:300],))
        summary["Trade Republic"] = f"error: {str(e)[:80]}"


def _tr_relogin():
    """Cookie expired: at most once a day and only while you're likely awake, ask for one tap in the TR app."""
    hour = datetime.now().hour
    last = store.get("tr_auto_login", 0)
    if not (9 <= hour < 22) or time.time() - last < 20 * 3600:
        return None
    store.put("tr_auto_login", time.time())
    try:
        return tr.unattended_login(lambda: notify.send(
            "Trade Republic möchte einen neuen Login – bitte jetzt in der TR-App bestätigen (2 Min)."))
    except Exception as e:  # noqa: BLE001
        log.info("tr relogin: %s", e)
        notify.send(f"Trade-Republic-Login nicht bestätigt. Neu anmelden: {REDIRECT_URL}")
        return None


def categorize(c, new: list[dict]) -> int:
    if not new:
        return 0
    # what you (or earlier runs) decided for the same payee wins over asking the model
    memory: dict[str, Counter] = defaultdict(Counter)
    for s in firefly.splits(c, date.today() - timedelta(days=HISTORY_DAYS), date.today()):
        cat = s.get("category_name")
        if cat and cat != "Sonstiges":
            other = s.get("destination_name") if s["type"] == "withdrawal" else s.get("source_name")
            memory[ai.norm_payee(other or "")][cat] += 1
    todo = []
    for r in new:
        k = ai.norm_payee(r["payee"]) or ai.norm_payee(r["purpose"])
        if k and memory.get(k):
            r["category"] = memory[k].most_common(1)[0][0]
        else:
            todo.append(r)
    for r, cat in zip(todo, ai.categorize(todo)):
        r["category"] = cat
    # the model judges line by line and can call one AWS bill "Software" and the next "Lebensmittel":
    # within one run, every payee gets its majority category
    votes: dict[str, Counter] = defaultdict(Counter)
    for r in todo:
        votes[ai.norm_payee(r["payee"])][r["category"]] += 1
    for r in todo:
        k = ai.norm_payee(r["payee"])
        if k and sum(votes[k].values()) > 1:
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
    sp = [s for s in firefly.splits(c, date.today() - timedelta(days=45), date.today())
          if s.get("category_name") != "Investments" and s["type"] in ("withdrawal", "deposit")]
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
            notify.send(f"{l['aspsp']}: Kontozugriff läuft am {l['valid_until'][:10]} ab – einmal neu verbinden: {REDIRECT_URL}")
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
            summary["transfers"] = pair_transfers(c)
            summary |= subs.update(firefly.splits(c, date.today() - timedelta(days=HISTORY_DAYS), date.today()))
        remind_consents()
    except Exception as e:  # noqa: BLE001
        log.exception("sync failed")
        summary["error"] = str(e)[:300]
        ok = 0
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
    sys.exit(0)
