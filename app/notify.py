"""Telegram pushes through the deals bot, for things that need a tap (consent renewals, TR login) or a look (subs)."""
from __future__ import annotations

import logging

import httpx

from .config import TELEGRAM_CHAT_ID, TELEGRAM_TOKEN

log = logging.getLogger("notify")


def send(text: str) -> None:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        httpx.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", timeout=15,
                   json={"chat_id": TELEGRAM_CHAT_ID, "text": "💶 " + text, "parse_mode": "HTML",
                         "disable_web_page_preview": True})
    except httpx.HTTPError as e:
        log.warning("telegram: %s", e)
