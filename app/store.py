"""Local state next to the ledger: bank links, sync log, imported-tx index, portfolio snapshots, subscriptions."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager

from .config import DB, STATE

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (          -- one Enable Banking consent (one bank login)
  session_id TEXT PRIMARY KEY, aspsp TEXT NOT NULL, valid_until TEXT, created REAL, reminded REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS accounts (       -- every account we import into its own Firefly asset account
  key TEXT PRIMARY KEY,                      -- 'eb:<uid>' or 'tr:cash'
  source TEXT NOT NULL, name TEXT NOT NULL, iban TEXT, currency TEXT DEFAULT 'EUR',
  session_id TEXT, firefly_id TEXT, balance REAL, balance_at REAL, last_sync REAL, error TEXT
);
CREATE TABLE IF NOT EXISTS seen (           -- provider tx id -> Firefly journal, so re-polls never double-book
  key TEXT PRIMARY KEY, firefly_id TEXT, at REAL
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, started REAL, finished REAL, ok INTEGER, summary TEXT
);
CREATE TABLE IF NOT EXISTS portfolio (      -- one row per day, last snapshot of the day wins
  day TEXT PRIMARY KEY, value REAL, cash REAL, invested REAL, positions TEXT
);
CREATE TABLE IF NOT EXISTS subs (
  key TEXT PRIMARY KEY, payee TEXT, category TEXT, period TEXT, amount REAL, prev_amount REAL,
  last_date TEXT, next_date TEXT, count INTEGER, first_seen REAL, changed REAL, state TEXT, dismissed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""
# subscriptions you add yourself (picked from bookings): pinned, matched by payee key + amount, never auto-ended away
MIGRATE_SUBS = [("manual", "INTEGER DEFAULT 0"), ("pkey", "TEXT"), ("match_amount", "REAL"), ("name", "TEXT"),
                # kind 'in' = recurring income (Gehalt, Taschengeld), matched on deposits, optionally only on one account;
                # shares = JSON [{pkey, payee, amount}]: friends paying their part of this Abo, netted against it
                ("kind", "TEXT DEFAULT 'out'"), ("match_account", "TEXT"), ("shares", "TEXT")]


@contextmanager
def connect():
    STATE.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(subs)")}
    for col, decl in MIGRATE_SUBS:
        if col not in cols:
            con.execute(f"ALTER TABLE subs ADD COLUMN {col} {decl}")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def get(key: str, default=None):
    with connect() as con:
        r = con.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return json.loads(r["v"]) if r else default


def put(key: str, value) -> None:
    with connect() as con:
        con.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, json.dumps(value)))


def now() -> float:
    return time.time()
