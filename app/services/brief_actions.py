"""Brief buttons (K5 k5-task-buttons): "done / not relevant / postpone" under tasks.

Under the brief message there is a row of buttons for each numbered task
(``pipes_cards.render(buttons=...)`` → ``keyboard``). A press arrives here by
``getUpdates`` long-polling: a webhook needs a public address, while Recall lives
on a home PC behind Tailscale without Funnel. So a press made while the PC sleeps
waits on the Telegram side for up to 24 h and is applied after wake-up; an older one
is lost (a line about this is at the bottom of the brief).

A change goes only through ``commitments.apply_action``/``undo_action`` (the journal
``action_item_events``) or, for the daily MCP summary, ``telegram_chats.undo_event`` (the
journal ``tg_chat_events``). A press is accepted only from the chat ``TELEGRAM_BOT_CHAT_ID``.

``callback_data`` carries everything needed (≤64 bytes, no state on our side):
    ``a:<n>:<id>:<action>`` — action on a task (done|cancelled|postpone:1|7|30) or a
                            decision on a proposal (approve|reject, the Yes / No row)
    ``p:<n>:<id>``       — show the deadline choice
    ``b:<n>:<id>``       — back to the three buttons
    ``u:<n>:<event>``    — undo an event
    ``m:<n>:t|c:<event>`` — undo an MCP action from the daily summary: a task event
                            (``commitments.undo_action``) or a chat journal event
                            (``telegram_chats.undo_event``)
    ``x``                — nothing (the caption after an action)
"""
from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from typing import Optional

from app.core.i18n import server_lang, tr
from app.services import commitments, tg_bot

logger = logging.getLogger(__name__)

#: 3 buttons per task, the Telegram limit is 100 per message.
MAX_ITEMS = 33
MAX_BUTTONS = 100
#: MCP-summary undo kinds → the letter in ``m:<n>:<letter>:<ref>``.
_UNDO_KINDS = {"task": "t", "chat": "c"}
POLL_TIMEOUT = 15            # < tg_bot.TIMEOUT: long-poll must not run into the HTTP timeout
_DONE_KEYS = {"done": "done", "cancelled": "cancelled", "postpone:1": "postpone1",
              "postpone:7": "postpone7", "postpone:30": "postpone30",
              "approve": "approve", "reject": "reject"}


def _t(key: str, lang: Optional[str] = None, **vars) -> str:
    """Button/reply text in the language of the setting at assembly/press time."""
    return tr("telegram.btn." + key, lang=lang or server_lang(), **vars)


def footer(lang: Optional[str] = None) -> str:
    return _t("footer", lang)


def __getattr__(name: str):
    if name == "FOOTER":          # compatibility: the constant became language-dependent
        return footer()
    raise AttributeError(name)


def _done_label(action: str, lang: Optional[str] = None) -> Optional[str]:
    key = _DONE_KEYS.get(action)
    return _t("label." + key, lang) if key else None


def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def task_row(n: int, item_id: int) -> list[dict]:
    return [_btn(f"{n} ✅", f"a:{n}:{item_id}:done"),
            _btn("🚫", f"a:{n}:{item_id}:cancelled"),
            _btn("⏭", f"p:{n}:{item_id}")]


def proposal_row(n: int, item_id: int, lang: Optional[str] = None) -> list[dict]:
    """A task proposal (ADR-056): Yes -> ``approve`` (status ``open``), No -> ``reject``."""
    return [_btn(f"{n} {_t('yes', lang)}", f"a:{n}:{item_id}:approve"),
            _btn(_t("no", lang), f"a:{n}:{item_id}:reject")]


def postpone_row(n: int, item_id: int, lang: Optional[str] = None) -> list[dict]:
    return [_btn(f"{n} {_t('tomorrow', lang)}", f"a:{n}:{item_id}:postpone:1"),
            _btn(_t("plusWeek", lang), f"a:{n}:{item_id}:postpone:7"),
            _btn(_t("plusMonth", lang), f"a:{n}:{item_id}:postpone:30"),
            _btn("↩", f"b:{n}:{item_id}")]


def done_row(n: int, action: str, event_id: int, due_date: Optional[str] = None,
             lang: Optional[str] = None) -> list[dict]:
    label = _done_label(action, lang) or action
    if action.startswith("postpone:") and due_date:
        label = _t("postponedTo", lang, date=f"{due_date[8:10]}.{due_date[5:7]}")
    return [_btn(f"{n} {label}", "x"), _btn(_t("undo", lang), f"u:{n}:{event_id}")]


def undo_row(n: int, kind: str, ref: int, lang: Optional[str] = None) -> list[dict]:
    """An MCP-summary line: undo a task event (``t``) or a chat journal event (``c``)."""
    return [_btn(f"{n} {_t('undo.button', lang)}", f"m:{n}:{_UNDO_KINDS[kind]}:{ref}")]


def keyboard(item_ids: list[int], undo_items: Optional[list[dict]] = None, *,
             proposal_ids: frozenset = frozenset()) -> Optional[dict]:
    """Numbering from 1 in display order; None — there are no buttons.

    An id in ``proposal_ids`` gets the Yes / No row instead of done / not relevant / postpone.
    ``undo_items`` (``[{n, kind, ref}]`` from the MCP summary) add one row each, numbered
    after the tasks, within Telegram's 100 buttons per message."""
    rows = [proposal_row(n, i) if i in proposal_ids else task_row(n, i)
            for n, i in enumerate(item_ids[:MAX_ITEMS], 1)]
    room = MAX_BUTTONS - 3 * len(rows)
    rows += [undo_row(int(u["n"]), u["kind"], int(u["ref"]))
             for u in (undo_items or [])[:max(0, room)] if u.get("kind") in _UNDO_KINDS]
    return {"inline_keyboard": rows} if rows else None


# ------------------------------------------------------------------ press

class _UndoRefused(ValueError):
    """A chat undo the journal refused; the message is the localized callback text."""


def _undo_chat_event(db_path: str, event_id: int, actor: str, lang: str) -> None:
    """``telegram_chats.undo_event`` with its refusals turned into callback text."""
    from app.services import privacy, telegram_chats
    try:
        telegram_chats.undo_event(db_path, event_id, source="bot", actor=actor)
    except LookupError as e:
        raise _UndoRefused(_t("undo.notFound", lang, id=event_id)) from e
    except ValueError as e:
        key = "undo.already" if str(e) == "undone" else "undo.created" if str(e) == "created" else None
        raise _UndoRefused(_t(key, lang) if key else str(e)) from e
    except telegram_chats.UndoNotLast as e:
        raise _UndoRefused(_t("undo.notLast", lang)) from e
    except (telegram_chats.PrivateCategoryChange, privacy.PrivacyError) as e:
        raise _UndoRefused(_t("undo.private", lang)) from e


def handle(cq: dict, *, db_path: str, transport=None) -> Optional[str]:
    """One ``callback_query`` → a DB change + a new keyboard. → reply text (for tests)."""
    lang = server_lang()          # language — at the moment of the press
    msg = cq.get("message") or {}
    chat = str((msg.get("chat") or {}).get("id", ""))
    data = str(cq.get("data") or "")
    if not chat or chat != tg_bot._chat_id():
        logger.warning("Brief buttons: press from a foreign chat — rejected")
        denied = _t("noAccess", lang)
        _answer(cq, denied, transport)
        return denied
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
            new_row = postpone_row(n, iid, lang) if kind == "p" else task_row(n, iid)
        elif kind == "a" and len(parts) == 4:
            n, iid, action = int(parts[1]), int(parts[2]), parts[3]
            res = commitments.apply_action(db_path, iid, action, source="bot", actor=actor)
            new_row = done_row(n, action, res["event_id"], res.get("due_date"), lang)
            reply = _done_label(action, lang) or _t("ok", lang)
            logger.info("Brief buttons: task %s → %s (event %s)", iid, action, res["event_id"])
        elif kind == "u" and len(parts) == 3:
            n, eid = int(parts[1]), int(parts[2])
            res = commitments.undo_action(db_path, eid, source="bot", actor=actor)
            # An undone decision puts the proposal back: Yes / No again, not the task row.
            new_row = (proposal_row(n, res["item_id"], lang) if res["status"] == "proposed"
                       else task_row(n, res["item_id"]))
            reply = _t("undone", lang)
            logger.info("Brief buttons: event %s undone", eid)
        elif kind == "m" and len(parts) == 4 and parts[2] in ("t", "c"):
            n, eid = int(parts[1]), int(parts[3])
            if parts[2] == "t":
                commitments.undo_action(db_path, eid, source="bot", actor=actor)
            else:
                _undo_chat_event(db_path, eid, actor, lang)
            new_row = [_btn(f"{n} {_t('undo.done', lang)}", "x")]
            reply = _t("undone", lang)
            logger.info("Brief buttons: MCP %s event %s undone",
                        "task" if parts[2] == "t" else "chat", eid)
        else:
            reply = _t("unknown", lang)
    except (LookupError, ValueError) as e:
        reply = str(e)[:180]
        logger.info("Brief buttons: %s — %s", data, reply)
    if new_row is not None:
        _replace_row(msg, n, new_row, transport)
    _answer(cq, reply, transport)
    return reply


#: (chat, message_id) → the keyboard WE set last. Presses that waited
#: while the PC slept arrive in a batch, and each carries the INITIAL keyboard too — building
#: from it would return the previous press's row to three buttons (and lose
#: its ↩). So the base is our last edit, and the keyboard from the press is used only if
#: we have not changed this message yet (for example, after a restart).
_KEYBOARDS: "OrderedDict[tuple, list]" = OrderedDict()
_KEYBOARDS_MAX = 200
_kb_lock = threading.Lock()


def _row_number(row: list[dict]) -> Optional[str]:
    """Task number of a row: every row (task/postpone/done) starts with "N …"."""
    text = str((row[0] if row else {}).get("text", ""))
    return text.split(" ", 1)[0] or None


def _replace_row(msg: dict, n: int, new_row: list[dict], transport) -> None:
    """Replace the row of task N. The row is found by NUMBER, not by the pressed button:
    after the first edit of the same task (two late presses in a row) the button is gone,
    while the DB change is already there — the row must show it together with its ↩ (round 2 review)."""
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
        logger.warning("Brief buttons: keyboard not updated (%s)", e)
        return
    with _kb_lock:
        _KEYBOARDS[key] = rows
        _KEYBOARDS.move_to_end(key)
        while len(_KEYBOARDS) > _KEYBOARDS_MAX:
            _KEYBOARDS.popitem(last=False)


def _answer(cq: dict, text: str, transport) -> None:
    """A late press (PC slept) — Telegram no longer accepts the answer; the change is not undone by this."""
    try:
        params = {"callback_query_id": cq.get("id")}
        if text:
            params["text"] = text
        tg_bot._call("answerCallbackQuery", params, transport=transport)
    except tg_bot.BotError as e:
        logger.info("Brief buttons: reply to the press not accepted (%s)", e)


# ------------------------------------------------------------------ thread

class ButtonPoller:
    """Daemon thread: ``getUpdates`` long-polling for ``callback_query`` only."""

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
            except Exception as e:  # noqa: BLE001 — one broken press does not kill the thread
                logger.warning("Brief buttons: press not processed (%s)", e, exc_info=True)
        return n

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as e:  # noqa: BLE001 — network/409: wait and try again
                logger.warning("Brief buttons: getUpdates failed (%s)", e)
                self._stop.wait(self.backoff)

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="brief-buttons", daemon=True)
        self._thread.start()
        logger.info("Brief buttons: listening for presses")

    def stop(self):
        self._stop.set()
