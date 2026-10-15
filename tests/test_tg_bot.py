"""K4 story 03: бот доставки труб."""
from __future__ import annotations

import json
import urllib.parse

import pytest

from app.services import tg_bot
from app.services.tg_bot import BotError

TOKEN = "123456:секретний-токен"


class Fake:
    def __init__(self, reply=None, exc=None):
        self.calls, self.reply, self.exc = [], reply or {"ok": True, "result": {}}, exc

    def __call__(self, url, data):
        self.calls.append((url, urllib.parse.parse_qs(data.decode()) if data else None))
        if self.exc:
            raise self.exc
        return json.dumps(self.reply).encode()


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_BOT_CHAT_ID", "777")


def test_short_text_one_request_to_configured_chat(configured):
    f = Fake()
    assert tg_bot.send("Звід: усе гаразд.", transport=f) == 1
    url, params = f.calls[0]
    assert url.endswith("/sendMessage") and params["chat_id"] == ["777"]
    assert params["text"] == ["Звід: усе гаразд."]


def test_long_text_split_by_lines_and_limit(configured):
    f = Fake()
    text = "\n".join(f"• задача номер {i} для Олени" for i in range(400))
    n = tg_bot.send(text, transport=f)
    assert n == len(f.calls) > 1
    sent = [p["text"][0] for _, p in f.calls]
    assert all(len(s) <= 4096 for s in sent)
    assert "\n".join(sent) == text
    assert {p["chat_id"][0] for _, p in f.calls} == {"777"}


def test_split_single_huge_line():
    parts = tg_bot.split_text("я" * 9000)
    assert [len(p) for p in parts] == [4096, 4096, 808]


def test_not_configured_does_not_call(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_BOT_CHAT_ID", "")
    f = Fake()
    with pytest.raises(BotError, match="не налаштований"):
        tg_bot.send("текст", transport=f)
    assert f.calls == []


def test_errors_never_carry_token(configured):
    f = Fake(exc=OSError(f"connect https://api.telegram.org/bot{TOKEN}/sendMessage refused"))
    with pytest.raises(BotError) as ei:
        tg_bot.send("текст", transport=f)
    assert TOKEN not in str(ei.value) and "OSError" in str(ei.value)
    f = Fake(reply={"ok": False, "description": "Bad Request: chat not found"})
    with pytest.raises(BotError, match="chat not found"):
        tg_bot.send("текст", transport=f)


def test_whoami_lists_chats(configured):
    f = Fake(reply={"ok": True, "result": [
        {"message": {"chat": {"id": 777, "type": "private", "first_name": "Андрій"}}},
        {"message": {"chat": {"id": 777, "type": "private", "first_name": "Андрій"}}},
    ]})
    assert tg_bot.whoami(transport=f) == [{"chat_id": 777, "type": "private", "name": "Андрій"}]


def test_whoami_cli_without_token(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    assert tg_bot._cli(["whoami"]) == 1
    assert "@BotFather" in capsys.readouterr().out


def test_reply_markup_goes_only_on_last_part(configured):
    """K5: кнопки брифінгу — під останньою частиною, щоб нумерація читалась над ними."""
    f = Fake()
    kb = {"inline_keyboard": [[{"text": "1 ✅", "callback_data": "a:1:5:done"}]]}
    text = "\n\n".join(f"🏷 <b>Блок {i}</b>\n" + "задача Олени " * 60 for i in range(12))
    n = tg_bot.send(text, reply_markup=kb, transport=f)
    assert n > 1
    marks = [p.get("reply_markup") for _, p in f.calls]
    assert marks[:-1] == [None] * (n - 1)
    assert json.loads(marks[-1][0]) == kb


def test_whoami_asks_for_messages_despite_button_filter(configured):
    """Слухач кнопок просить лише callback_query, Telegram це запамʼятовує — whoami скидає фільтр."""
    f = Fake(reply={"ok": True, "result": []})
    tg_bot.whoami(transport=f)
    _, params = f.calls[0]
    assert "message" in json.loads(params["allowed_updates"][0])
