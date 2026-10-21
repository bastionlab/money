"""Telegram pushes for things that need a tap (consent renewals, TR login) or a look (subs, unusual charges, month report).
Its own bot once you paste a token on the dashboard; until then through the deals bot."""
from __future__ import annotations

import logging

import httpx

from .config import TELEGRAM_CHAT_ID, TELEGRAM_TOKEN, save_secrets, secrets

log = logging.getLogger("notify")


def target() -> tuple[str, str, bool]:
    """(token, chat id, own bot?)"""
    own = secrets().get("telegram") or {}
    if own.get("token") and own.get("chat_id"):
        return own["token"], str(own["chat_id"]), True
    return TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, False


def find_chat(token: str) -> str | None:
    """The private chat that last wrote to the bot (you send it /start once)."""
    try:
        r = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15).json()
    except httpx.HTTPError:
        return None
    for u in reversed(r.get("result", [])):
        chat = (u.get("message") or u.get("my_chat_member") or {}).get("chat") or {}
        if chat.get("type") == "private":
            return str(chat["id"])
    return None


def setup_own(token: str) -> dict:
    me = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15).json()
    if not me.get("ok"):
        raise ValueError("Telegram kennt diesen Bot-Token nicht")
    s = secrets()
    s["telegram"] = {"token": token, "bot": me["result"].get("username"), "chat_id": find_chat(token)}
    save_secrets(s)
    return view()


def view() -> dict:
    own = secrets().get("telegram") or {}
    if own.get("token") and not own.get("chat_id"):
        chat = find_chat(own["token"])
        if chat:
            s = secrets()
            s["telegram"]["chat_id"] = chat
            save_secrets(s)
            own["chat_id"] = chat
    return {"own": bool(own.get("token")), "bot": own.get("bot"), "chat": bool(own.get("chat_id")),
            "fallback": bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)}


def send(text: str) -> bool:
    token, chat, own = target()
    if not (token and chat):
        return False
    try:
        r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=15,
                       json={"chat_id": chat, "text": ("" if own else "💶 ") + text, "parse_mode": "HTML",
                             "disable_web_page_preview": True})
        return r.status_code == 200
    except httpx.HTTPError as e:
        log.warning("telegram: %s", e)
        return False
