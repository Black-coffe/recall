"""Бот доставки труб (K4): окремий Telegram-бот, лише в чат власника.

Не плутати зі слухачем ``telegram_listener.py``: той — MTProto-АКАУНТ власника
(Telethon), цей — Bot API з токеном від @BotFather. Бот шле ЛИШЕ в
``TELEGRAM_BOT_CHAT_ID``; адресата викликач не передає взагалі.

HTTP — стандартний ``urllib``, без нової бібліотеки. Повідомлення довше
4096 символів ріжеться по рядках. Токен — частина URL Bot API, тому жодна
помилка звідси не несе ні URL, ні токена (``BotError`` уже очищений).

CLI:
    python -m app.services.tg_bot whoami     # chat id після /start боту
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from app.core import settings as _settings

logger = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4096
TIMEOUT = 20.0


class BotError(RuntimeError):
    """Збій бота; повідомлення без токена."""


#: (url, data | None) → тіло відповіді. Підмінюється в тестах.
Transport = Callable[[str, Optional[bytes]], bytes]


def _urllib_transport(url: str, data: Optional[bytes]) -> bytes:
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded"} if data else {})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 — фіксований https
        return resp.read()


def _token() -> str:
    return _settings.env("TELEGRAM_BOT_TOKEN").strip()


def _chat_id() -> str:
    return _settings.env("TELEGRAM_BOT_CHAT_ID").strip()


def is_configured() -> bool:
    return bool(_token() and _chat_id())


def _call(method: str, params: Optional[dict] = None, *,
          transport: Optional[Transport] = None) -> dict:
    token = _token()
    if not token:
        raise BotError("нема TELEGRAM_BOT_TOKEN")
    url = API.format(token=token, method=method)
    data = urllib.parse.urlencode(params).encode() if params else None
    try:
        body = (transport or _urllib_transport)(url, data)
        out = json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise BotError(f"Bot API {method}: HTTP {e.code}") from None
    except Exception as e:  # noqa: BLE001 — будь-що інше теж без URL і токена
        raise BotError(f"Bot API {method}: {type(e).__name__}: "
                       f"{str(e).replace(token, '***')}"[:300]) from None
    if not out.get("ok"):
        raise BotError(f"Bot API {method}: {out.get('description', 'відмова')}"[:300])
    return out


def split_text(text: str, limit: int = MAX_LEN) -> list[str]:
    """Ріже по рядках; рядок довший за ліміт — по символах."""
    parts: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        cand = f"{cur}\n{line}" if cur else line
        if len(cand) > limit:
            parts.append(cur)
            cur = line
        else:
            cur = cand
    if cur or not parts:
        parts.append(cur)
    return parts


def send(text: str, *, transport: Optional[Transport] = None) -> int:
    """Надіслати текст у чат власника. → кількість повідомлень. Збій → BotError."""
    chat = _chat_id()
    if not chat or not _token():
        raise BotError("бот не налаштований: потрібні TELEGRAM_BOT_TOKEN і TELEGRAM_BOT_CHAT_ID")
    parts = split_text(text)
    for part in parts:
        _call("sendMessage", {"chat_id": chat, "text": part,
                              "disable_web_page_preview": "true"}, transport=transport)
    return len(parts)


def whoami(*, transport: Optional[Transport] = None) -> list[dict]:
    """Чати, що писали боту (з getUpdates): звідси береться TELEGRAM_BOT_CHAT_ID."""
    out = _call("getUpdates", transport=transport)
    seen: dict = {}
    for upd in out.get("result", []):
        msg = upd.get("message") or upd.get("my_chat_member") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            seen[chat["id"]] = {"chat_id": chat["id"], "type": chat.get("type"),
                                "name": chat.get("title") or " ".join(
                                    x for x in (chat.get("first_name"), chat.get("last_name")) if x)
                                or chat.get("username")}
    return list(seen.values())


def _cli(argv: list[str]) -> int:
    if argv[:1] != ["whoami"]:
        print("використання: python -m app.services.tg_bot whoami")
        return 2
    if not _token():
        print("Нема TELEGRAM_BOT_TOKEN у .env. Створи бота в @BotFather, впиши токен, "
              "напиши боту /start і повтори.")
        return 1
    try:
        chats = whoami()
    except BotError as e:
        print(f"Не вдалося спитати Telegram: {e}")
        return 1
    if not chats:
        print("Бот ще нікого не бачив. Напиши йому /start у Telegram і повтори.")
        return 1
    for c in chats:
        print(f"TELEGRAM_BOT_CHAT_ID={c['chat_id']}   # {c['type']}: {c['name']}")
    return 0


if __name__ == "__main__":
    import sys
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    raise SystemExit(_cli(sys.argv[1:]))
