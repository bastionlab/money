"""Pushes for things that need a tap (consent renewals, TR login) or a look (subs, unusual charges, month report).
Discord (#money, or #alerts for logins/consents/failures) once the dashboard has the webhooks; else Telegram."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

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
    return {"discord": bool(_hook("money")), "discord_alerts": bool(_hook("alerts")), "own": bool(own.get("token")), "bot": own.get("bot"), "chat": bool(own.get("chat_id")),
            "fallback": bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)}


HOOKS = Path("/etc/discord.json")  # written by the dashboard agent: {"money": url, "alerts": url, …}


def _hook(channel: str) -> str | None:
    try:
        return json.loads(HOOKS.read_text()).get(channel) or None
    except (OSError, ValueError):
        return None


def _markdown(html: str) -> str:
    """The messages are written as Telegram HTML; Discord speaks markdown."""
    t = re.sub(r"</?b>", "**", html)
    t = re.sub(r"</?i>", "*", t)
    t = re.sub(r"</?code>", "`", t)
    t = re.sub(r'<a href="([^"]+)">([^<]*)</a>', r"[\2](\1)", t)
    t = re.sub(r"<[^>]+>", "", t)
    return t.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _send_discord(url: str, text: str) -> bool:
    body = _markdown(text)
    try:
        for i in range(0, len(body), 1900):  # 2000 chars per message
            r = httpx.post(url, timeout=15, json={"content": body[i:i + 1900], "allowed_mentions": {"parse": []}, "flags": 4})  # 4 = no link previews
            if r.status_code >= 300:
                log.warning("discord: %s %s", r.status_code, r.text[:200])
                return False
        return True
    except httpx.HTTPError as e:
        log.warning("discord: %s", e)
        return False


def send(text: str, channel: str = "money") -> bool:
    """channel: "money" for things to look at, "alerts" for things that need fixing (logins, consents, failed syncs)"""
    dc = _hook(channel) or _hook("money")
    if dc:
        return _send_discord(dc, ("💶 " if channel == "alerts" else "") + text)
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
