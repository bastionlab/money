"""Pushes for things that need a tap (consent renewals, TR login) or a look (subs, unusual charges, month report).
A Discord webhook once you paste one on the dashboard; else Telegram (own bot, else the deals bot)."""
from __future__ import annotations

import logging
import re

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
    dc = secrets().get("discord") or {}
    return {"discord": bool(dc.get("url")), "discord_name": dc.get("name"), "own": bool(own.get("token")), "bot": own.get("bot"), "chat": bool(own.get("chat_id")),
            "fallback": bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)}


DISCORD_HOOK = re.compile(r"^https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$")


def setup_discord(url: str) -> dict:
    s = secrets()
    if not url:  # empty = back to Telegram
        s.pop("discord", None)
        save_secrets(s)
        return view()
    if not DISCORD_HOOK.match(url):
        raise ValueError("Das ist keine Discord-Webhook-URL")
    r = httpx.get(url, timeout=15)
    if r.status_code != 200:
        raise ValueError("Discord kennt diesen Webhook nicht")
    s["discord"] = {"url": url, "name": r.json().get("name")}
    save_secrets(s)
    return view()


def _markdown(html: str) -> str:
    """The messages are written as Telegram HTML; Discord speaks markdown."""
    t = re.sub(r"</?b>", "**", html)
    t = re.sub(r"</?i>", "*", t)
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


def send(text: str) -> bool:
    dc = (secrets().get("discord") or {}).get("url")
    if dc:
        return _send_discord(dc, text)
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
