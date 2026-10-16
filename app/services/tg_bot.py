"""Delivery bot for pipes (K4): a separate Telegram bot, only to the owner's chat.

Not to be confused with the listener ``telegram_listener.py``: that one is the owner's MTProto ACCOUNT
(Telethon), this one is the Bot API with a token from @BotFather. The bot sends ONLY to
``TELEGRAM_BOT_CHAT_ID``; the caller does not pass a recipient at all.

HTTP is the standard ``urllib``, no new library. The text goes with
``parse_mode=HTML`` (K5 cards). A message longer than 4096 characters is cut at
empty lines — block boundaries — so no part breaks a tag. The token is part of the Bot API URL, so no
error from here carries either the URL or the token (``BotError`` is already sanitized).

CLI:
    python -m app.services.tg_bot whoami     # chat id after /start to the bot
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
    """Bot failure; the message has no token."""


#: (url, data | None) → response body. Replaced in tests.
Transport = Callable[[str, Optional[bytes]], bytes]


def _urllib_transport(url: str, data: Optional[bytes]) -> bytes:
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded"} if data else {})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 — fixed https
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
        raise BotError("no TELEGRAM_BOT_TOKEN")
    url = API.format(token=token, method=method)
    data = urllib.parse.urlencode(params).encode() if params else None
    try:
        body = (transport or _urllib_transport)(url, data)
        out = json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise BotError(f"Bot API {method}: HTTP {e.code}") from None
    except Exception as e:  # noqa: BLE001 — anything else is also without URL and token
        raise BotError(f"Bot API {method}: {type(e).__name__}: "
                       f"{str(e).replace(token, '***')}"[:300]) from None
    if not out.get("ok"):
        raise BotError(f"Bot API {method}: {out.get('description', 'rejected')}"[:300])
    return out


def _split_lines(text: str, limit: int) -> list[str]:
    """Cuts by lines; a line longer than the limit — by characters."""
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


def split_text(text: str, limit: int = MAX_LEN) -> list[str]:
    """Cuts by paragraphs (an empty line is a block boundary); a paragraph longer than the limit — by lines."""
    parts: list[str] = []
    cur = ""
    for para in text.split("\n\n"):
        if len(para) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.extend(_split_lines(para, limit))
            continue
        cand = f"{cur}\n\n{para}" if cur else para
        if len(cand) > limit:
            parts.append(cur)
            cur = para
        else:
            cur = cand
    if cur or not parts:
        parts.append(cur)
    return parts


def send(text: str, *, parse_mode: Optional[str] = "HTML",
         reply_markup: Optional[dict] = None,
         transport: Optional[Transport] = None) -> int:
    """Send text to the owner's chat. → number of messages. Failure → BotError.

    ``parse_mode`` is HTML by default: everything the pipes send is already escaped
    (``pipes_cards``, the failure notification). ``None`` — plain text.
    ``reply_markup`` (K5 brief buttons) is attached to the LAST part."""
    chat = _chat_id()
    if not chat or not _token():
        raise BotError("bot not configured: TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_CHAT_ID are required")
    parts = split_text(text)
    for i, part in enumerate(parts):
        params = {"chat_id": chat, "text": part, "disable_web_page_preview": "true"}
        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_markup and i == len(parts) - 1:
            params["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        _call("sendMessage", params, transport=transport)
    return len(parts)


def whoami(*, transport: Optional[Transport] = None) -> list[dict]:
    """Chats that wrote to the bot (from getUpdates): TELEGRAM_BOT_CHAT_ID is taken from here."""
    # The button listener (brief_actions) asks only for callback_query, and Telegram remembers
    # that filter — so here we explicitly return messages, otherwise /start is not visible.
    out = _call("getUpdates", {"allowed_updates": json.dumps(
        ["message", "my_chat_member", "callback_query"])}, transport=transport)
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
        print("usage: python -m app.services.tg_bot whoami")
        return 2
    if not _token():
        print("No TELEGRAM_BOT_TOKEN in .env. Create a bot in @BotFather, put the token in, "
              "send the bot /start and repeat.")
        return 1
    try:
        chats = whoami()
    except BotError as e:
        print(f"Failed to ask Telegram: {e}")
        return 1
    if not chats:
        print("The bot has not seen anyone yet. Send it /start in Telegram and repeat.")
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
