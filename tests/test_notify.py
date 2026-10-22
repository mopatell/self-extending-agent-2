"""Telegram notifier: payloads, chat discovery, and token redaction. No real network."""

import io
import json
import urllib.error

import pytest

from sea import notify


class FakeResponse:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture(monkeypatch, body: dict):
    """Patch urlopen to record the request and return `body`."""
    seen = {}

    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        seen["payload"] = json.loads(req.data.decode())
        return FakeResponse(body)

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    return seen


def test_send_telegram_posts_message(monkeypatch):
    monkeypatch.setattr(notify.settings, "telegram_bot_token", "Tok123")
    monkeypatch.setattr(notify.settings, "telegram_chat_id", "555")
    seen = _capture(monkeypatch, {"ok": True, "result": {}})

    notify.send_telegram("hello")

    assert seen["url"] == "https://api.telegram.org/botTok123/sendMessage"
    assert seen["payload"]["chat_id"] == "555"
    assert seen["payload"]["text"] == "hello"


def test_send_without_chat_raises(monkeypatch):
    monkeypatch.setattr(notify.settings, "telegram_bot_token", "Tok")
    monkeypatch.setattr(notify.settings, "telegram_chat_id", "")
    with pytest.raises(notify.TelegramError, match="chat id"):
        notify.send_telegram("hi")


def test_send_without_token_raises(monkeypatch):
    monkeypatch.setattr(notify.settings, "telegram_bot_token", "")
    with pytest.raises(notify.TelegramError, match="token"):
        notify.send_telegram("hi")


def test_discover_chat_returns_latest_private_chat(monkeypatch):
    monkeypatch.setattr(notify.settings, "telegram_bot_token", "Tok")
    updates = {
        "ok": True,
        "result": [
            {"message": {"chat": {"id": 111, "first_name": "Old"}}},
            {"message": {"chat": {"id": 222, "username": "sohail"}}},
        ],
    }
    _capture(monkeypatch, updates)

    chat = notify.discover_chat()
    assert chat is not None
    assert chat.chat_id == "222"
    assert chat.name == "sohail"


def test_discover_chat_none_when_no_updates(monkeypatch):
    monkeypatch.setattr(notify.settings, "telegram_bot_token", "Tok")
    _capture(monkeypatch, {"ok": True, "result": []})
    assert notify.discover_chat() is None


def test_api_error_is_token_redacted(monkeypatch):
    monkeypatch.setattr(notify.settings, "telegram_bot_token", "SECRET")
    monkeypatch.setattr(notify.settings, "telegram_chat_id", "5")

    def boom(req, timeout=0):
        # A real Telegram error body echoes the request path, which carries the token.
        body = io.BytesIO(b'{"description":"bot SECRET blocked"}')
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, body)

    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    with pytest.raises(notify.TelegramError) as e:
        notify.send_telegram("hi")
    assert "SECRET" not in str(e.value)
    assert "<token>" in str(e.value)
