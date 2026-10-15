"""Кнопки брифінгу (K5 k5-task-buttons): «зроблено / неактуально / перенести» під задачами.

Під повідомленням брифінгу — ряд кнопок на кожну пронумеровану задачу
(``pipes_cards.render(buttons=...)`` → ``keyboard``). Натиск приходить сюди
long-polling-ом ``getUpdates``: webhook потребує публічної адреси, а Recall живе
на домашньому ПК за Tailscale без Funnel. Тому натиск, зроблений, поки ПК спить,
чекає на боці Telegram до 24 год і застосовується після пробудження; старший
губиться (рядок про це — внизу брифінгу).

Зміна йде лише через ``commitments.apply_action``/``undo_action`` (журнал
``action_item_events``), MCP лишається read-only. Натиск приймається лише з чату
``TELEGRAM_BOT_CHAT_ID``.

``callback_data`` несе все потрібне (≤64 байти, без стану на нашому боці):
    ``a:<n>:<id>:<дія>`` — дія над задачею (done|cancelled|postpone:1|7|30)
    ``p:<n>:<id>``       — показати вибір терміну
    ``b:<n>:<id>``       — назад до трьох кнопок
    ``u:<n>:<event>``    — скасувати подію
    ``x``                — нічого (підпис після дії)
"""
from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from typing import Optional

from app.services import commitments, tg_bot

logger = logging.getLogger(__name__)

#: 3 кнопки на задачу, ліміт Telegram — 100 на повідомлення.
MAX_ITEMS = 33
POLL_TIMEOUT = 15            # < tg_bot.TIMEOUT: long-poll не має впертись у таймаут HTTP
FOOTER = "Кнопки спрацьовують, коли увімкнений домашній ПК."

_DONE_LABEL = {"done": "✅ зроблено", "cancelled": "🚫 неактуально",
               "postpone:1": "⏭ на завтра", "postpone:7": "⏭ на тиждень",
               "postpone:30": "⏭ на місяць"}


def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def task_row(n: int, item_id: int) -> list[dict]:
    return [_btn(f"{n} ✅", f"a:{n}:{item_id}:done"),
            _btn("🚫", f"a:{n}:{item_id}:cancelled"),
            _btn("⏭", f"p:{n}:{item_id}")]


def postpone_row(n: int, item_id: int) -> list[dict]:
    return [_btn(f"{n} завтра", f"a:{n}:{item_id}:postpone:1"),
            _btn("+тиждень", f"a:{n}:{item_id}:postpone:7"),
            _btn("+місяць", f"a:{n}:{item_id}:postpone:30"),
            _btn("↩", f"b:{n}:{item_id}")]


def done_row(n: int, action: str, event_id: int, due_date: Optional[str] = None) -> list[dict]:
    label = _DONE_LABEL.get(action, action)
    if action.startswith("postpone:") and due_date:
        label = f"⏭ до {due_date[8:10]}.{due_date[5:7]}"
    return [_btn(f"{n} {label}", "x"), _btn("↩ скасувати", f"u:{n}:{event_id}")]


def keyboard(item_ids: list[int]) -> Optional[dict]:
    """Нумерація з 1 у порядку показу; None — кнопок нема."""
    rows = [task_row(n, i) for n, i in enumerate(item_ids[:MAX_ITEMS], 1)]
    return {"inline_keyboard": rows} if rows else None


# ------------------------------------------------------------------ натиск

def handle(cq: dict, *, db_path: str, transport=None) -> Optional[str]:
    """Один ``callback_query`` → зміна в БД + нова клавіатура. → текст відповіді (для тестів)."""
    msg = cq.get("message") or {}
    chat = str((msg.get("chat") or {}).get("id", ""))
    data = str(cq.get("data") or "")
    if not chat or chat != tg_bot._chat_id():
        logger.warning("Кнопки брифінгу: натиск з чужого чату — відмова")
        _answer(cq, "Немає доступу", transport)
        return "Немає доступу"
    sender = cq.get("from") or {}
    actor = f"tg:{sender.get('id')}" + (f" @{sender['username']}" if sender.get("username") else "")
    parts = data.split(":", 3)
    kind = parts[0]
    new_row: Optional[list[dict]] = None
    reply = ""
    try:
        if kind == "x":
            pass
        elif kind in ("p", "b") and len(parts) == 3:
            n, iid = int(parts[1]), int(parts[2])
            new_row = postpone_row(n, iid) if kind == "p" else task_row(n, iid)
        elif kind == "a" and len(parts) == 4:
            n, iid, action = int(parts[1]), int(parts[2]), parts[3]
            res = commitments.apply_action(db_path, iid, action, source="bot", actor=actor)
            new_row = done_row(n, action, res["event_id"], res.get("due_date"))
            reply = _DONE_LABEL.get(action, "готово")
            logger.info("Кнопки брифінгу: задача %s → %s (подія %s)", iid, action, res["event_id"])
        elif kind == "u" and len(parts) == 3:
            n, eid = int(parts[1]), int(parts[2])
            res = commitments.undo_action(db_path, eid, source="bot", actor=actor)
            new_row = task_row(n, res["item_id"])
            reply = "↩ скасовано"
            logger.info("Кнопки брифінгу: подію %s скасовано", eid)
        else:
            reply = "Невідома кнопка"
    except (LookupError, ValueError) as e:
        reply = str(e)[:180]
        logger.info("Кнопки брифінгу: %s — %s", data, reply)
    if new_row is not None:
        _replace_row(msg, n, new_row, transport)
    _answer(cq, reply, transport)
    return reply


#: (chat, message_id) → клавіатура, яку МИ поставили останньою. Натиски, що чекали,
#: поки ПК спав, приходять пачкою, і кожен несе ще ПОЧАТКОВУ клавіатуру — будувати
#: від неї означало б повернути ряд попереднього натиску до трьох кнопок (і загубити
#: його ↩). Тому основа — наша остання правка, а клавіатура з натиску — лише якщо
#: цього повідомлення ми ще не міняли (наприклад, після рестарту).
_KEYBOARDS: "OrderedDict[tuple, list]" = OrderedDict()
_KEYBOARDS_MAX = 200
_kb_lock = threading.Lock()


def _row_number(row: list[dict]) -> Optional[str]:
    """Номер задачі ряду: кожен ряд (task/postpone/done) починається з «N …»."""
    text = str((row[0] if row else {}).get("text", ""))
    return text.split(" ", 1)[0] or None


def _replace_row(msg: dict, n: int, new_row: list[dict], transport) -> None:
    """Замінити ряд задачі N. Ряд шукається за НОМЕРОМ, а не за натиснутою кнопкою:
    після першої правки тієї ж задачі (два пізні натиски поспіль) кнопки вже нема,
    а зміна в БД уже є — ряд мусить її показати разом зі своїм ↩ (ревʼю раунду 2)."""
    key = (str((msg.get("chat") or {}).get("id")), msg.get("message_id"))
    with _kb_lock:
        rows = _KEYBOARDS.get(key)
    if rows is None:
        rows = ((msg.get("reply_markup") or {}).get("inline_keyboard")) or []
    idx = next((i for i, row in enumerate(rows) if _row_number(row) == str(n)), None)
    if idx is None:
        return
    rows = [*rows[:idx], new_row, *rows[idx + 1:]]
    try:
        tg_bot._call("editMessageReplyMarkup", {
            "chat_id": msg["chat"]["id"], "message_id": msg.get("message_id"),
            "reply_markup": json.dumps({"inline_keyboard": rows}, ensure_ascii=False)},
            transport=transport)
    except tg_bot.BotError as e:
        logger.warning("Кнопки брифінгу: клавіатуру не оновлено (%s)", e)
        return
    with _kb_lock:
        _KEYBOARDS[key] = rows
        _KEYBOARDS.move_to_end(key)
        while len(_KEYBOARDS) > _KEYBOARDS_MAX:
            _KEYBOARDS.popitem(last=False)


def _answer(cq: dict, text: str, transport) -> None:
    """Пізній натиск (ПК спав) — Telegram уже не приймає відповідь; зміна від цього не скасовується."""
    try:
        params = {"callback_query_id": cq.get("id")}
        if text:
            params["text"] = text
        tg_bot._call("answerCallbackQuery", params, transport=transport)
    except tg_bot.BotError as e:
        logger.info("Кнопки брифінгу: відповідь на натиск не прийнято (%s)", e)


# ------------------------------------------------------------------ потік

class ButtonPoller:
    """Daemon-потік: long-polling ``getUpdates`` лише для ``callback_query``."""

    def __init__(self, *, db_path: str, transport=None, backoff: float = 10.0):
        self.db_path = db_path
        self.transport = transport
        self.backoff = backoff
        self.offset: Optional[int] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def poll_once(self, timeout: int = POLL_TIMEOUT) -> int:
        params = {"timeout": timeout, "allowed_updates": json.dumps(["callback_query"])}
        if self.offset is not None:
            params["offset"] = self.offset
        out = tg_bot._call("getUpdates", params, transport=self.transport)
        n = 0
        for upd in out.get("result", []):
            self.offset = int(upd["update_id"]) + 1
            cq = upd.get("callback_query")
            if not cq:
                continue
            n += 1
            try:
                handle(cq, db_path=self.db_path, transport=self.transport)
            except Exception as e:  # noqa: BLE001 — один зламаний натиск не валить потік
                logger.warning("Кнопки брифінгу: натиск не оброблено (%s)", e, exc_info=True)
        return n

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as e:  # noqa: BLE001 — мережа/409: чекаємо й пробуємо знову
                logger.warning("Кнопки брифінгу: getUpdates не вдався (%s)", e)
                self._stop.wait(self.backoff)

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="brief-buttons", daemon=True)
        self._thread.start()
        logger.info("Кнопки брифінгу: слухаю натиски")

    def stop(self):
        self._stop.set()
