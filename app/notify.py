"""Pushes for things that need a tap (consent renewals, TR login) or a look (subs, unusual charges, month report).
Discord: #money, or #alerts for logins/consents/failed syncs. The dashboard keeps the webhooks (Metrics → Discord)."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import httpx

log = logging.getLogger("notify")

HOOKS = Path("/etc/discord.json")  # written by the dashboard agent: {"money": url, "alerts": url, …}


def _hook(channel: str) -> str | None:
    try:
        return json.loads(HOOKS.read_text()).get(channel) or None
    except (OSError, ValueError):
        return None


def view() -> dict:
    return {"money": bool(_hook("money")), "alerts": bool(_hook("alerts"))}


def _markdown(html: str) -> str:
    """The messages are written with a few HTML tags; Discord speaks markdown."""
    t = re.sub(r"</?b>", "**", html)
    t = re.sub(r"</?i>", "*", t)
    t = re.sub(r"</?code>", "`", t)
    t = re.sub(r'<a href="([^"]+)">([^<]*)</a>', r"[\2](\1)", t)
    t = re.sub(r"<[^>]+>", "", t)
    return t.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def send(text: str, channel: str = "money") -> bool:
    """channel: "money" for things to look at, "alerts" for things that need fixing (logins, consents, failed syncs)"""
    url = _hook(channel) or _hook("money")
    if not url:
        return False
    body = _markdown(("💶 " if channel == "alerts" else "") + text)
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
