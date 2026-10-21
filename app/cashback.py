"""Cash at the till: a supermarket card payment of 205,98 € is 5,98 € groceries + 200 € cash-back.
Left whole it makes groceries look like 1.500 €/month, so the cash part becomes its own Bargeld booking
and the original line keeps only the shopping. Undo with `python -m app.cashback undo`."""
from __future__ import annotations

import logging
import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta

from . import ai, firefly
from .config import HISTORY_DAYS

log = logging.getLogger("cashback")

PAYEE = "Bargeld an der Kasse"
MARK = "Cashback abgetrennt:"
MIN, MAX, STEP, CAP = 100.0, 250.0, 50, 200  # German stores pay out at most 200 € and people pick round amounts


def split(c) -> int:
    sp = [s for s in firefly.splits(c, date.today() - timedelta(days=HISTORY_DAYS + 30), date.today())
          if s["type"] == "withdrawal" and s.get("category_name") == "Lebensmittel"]
    # only shops where you usually spend little: a big weekly shop at the same Rewe is not cash
    usual: dict[str, list[float]] = defaultdict(list)
    for s in sp:
        if float(s["amount"]) < MIN:
            usual[ai.norm_payee(s.get("destination_name") or "")].append(float(s["amount"]))
    n = 0
    for s in sp:
        amt = float(s["amount"])
        small = usual.get(ai.norm_payee(s.get("destination_name") or ""), [])
        if not (MIN <= amt < MAX) or "debitkarte" not in (s.get("description") or "").lower():
            continue
        if MARK in (s.get("notes") or "") or len(small) < 3 or statistics.median(small) >= 40:
            continue
        cash = min(CAP, int(amt // STEP) * STEP)
        rest = round(amt - cash, 2)
        if rest < 0.01:
            continue
        try:
            firefly.add(c, account_id=s["source_id"], amount=-cash, day=s["date"][:10], payee=PAYEE,
                        description=f"Bargeld an der Kasse bei {s.get('destination_name') or 'Supermarkt'}",
                        external_id=f"cashback:{s['transaction_journal_id']}", category="Bargeld",
                        notes=f"aus Buchung {s['transaction_journal_id']} ({amt:.2f} €)")
        except firefly.Duplicate:
            pass
        notes = ((s.get("notes") or "") + f"\n{MARK} {cash:.2f} € Bargeld, Original {amt:.2f} €").strip()
        firefly.update(c, s["group_id"], s["transaction_journal_id"], amount=rest, notes=notes)
        n += 1
    return n


def undo(c) -> int:
    """Put every split charge back to its original amount and delete the cash bookings."""
    n = 0
    for s in firefly.splits(c, date.today() - timedelta(days=HISTORY_DAYS + 60), date.today()):
        notes = s.get("notes") or ""
        if (s.get("external_id") or "").startswith("cashback:"):
            firefly.delete(c, s["group_id"])
        elif MARK in notes:
            orig = float(notes.split("Original ")[-1].split(" €")[0])
            keep = "\n".join(l for l in notes.splitlines() if MARK not in l)
            firefly.update(c, s["group_id"], s["transaction_journal_id"], amount=orig, notes=keep)
            n += 1
    return n


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    with firefly.client() as cl:
        print(undo(cl) if sys.argv[1:] == ["undo"] else split(cl))
