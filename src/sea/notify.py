"""Outbound notifications. Telegram only, for now.

Runs on the host with urllib (like http_get in tools/builtin.py), so it adds no dependency
and never goes near the sandbox. The bot token is a secret: it lives in .env, is masked in the
settings UI, and is kept out of logs and events (see redact()).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from sea.config import settings

API = "https://api.telegram.org"


class TelegramError(RuntimeError):
    """A Telegram API call failed. The message is already token-redacted."""


@dataclass
class Chat:
    chat_id: str
    name: str


def _token(token: str | None) -> str:
    token = (token or settings.telegram_bot_token or "").strip()
    if not token:
        raise TelegramError("no Telegram bot token set (TELEGRAM_BOT_TOKEN)")
    return token


def redact(text: str, token: str | None = None) -> str:
    """Strip the bot token out of any string before it is logged or stored."""
    token = (token or settings.telegram_bot_token or "").strip()
    return text.replace(token, "<token>") if token else text


def _call(method: str, token: str, payload: dict, timeout: int = 15) -> dict:
    url = f"{API}/bot{token}/{method}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode(errors="replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise TelegramError(redact(f"Telegram {method} failed: HTTP {e.code} {detail}", token)) from e
    except urllib.error.URLError as e:
        raise TelegramError(redact(f"Telegram {method} failed: {e.reason}", token)) from e
    if not body.get("ok"):
        raise TelegramError(redact(f"Telegram {method} rejected: {body.get('description')}", token))
    return body["result"]


def send_telegram(text: str, *, token: str | None = None, chat_id: str | None = None) -> None:
    """Send a message. Raises TelegramError (token-redacted) on failure."""
    token = _token(token)
    chat_id = (chat_id or settings.telegram_chat_id or "").strip()
    if not chat_id:
        raise TelegramError("no Telegram chat id set (TELEGRAM_CHAT_ID)")
    _call(
        "sendMessage",
        token,
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
    )


def discover_chat(token: str | None = None) -> Chat | None:
    """Find the chat to notify from the bot's recent updates: the user must have messaged the
    bot at least once. Returns the most recent private chat, or None if there are no updates."""
    token = _token(token)
    updates = _call("getUpdates", token, {"limit": 100, "timeout": 0})
    for update in reversed(updates):
        msg = update.get("message") or update.get("channel_post")
        chat = (msg or {}).get("chat")
        if chat and chat.get("id") is not None:
            name = chat.get("username") or chat.get("title") or chat.get("first_name") or ""
            return Chat(str(chat["id"]), name)
    return None
