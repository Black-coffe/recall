#!/usr/bin/env python
"""Phase 17B — listener for a real Telegram account (MTProto/Telethon).

A SEPARATE process (not part of Flask). Thin: catches new messages in the
monitored chats, DOWNLOADS media to disk and POSTs to Flask
(/api/telegram/ingest) — all the heavy work (parse/OCR/whisper/embeddings/Claude)
lives there, where the models are already in GPU memory. The listener loads NO models.

Run (after telegram_login.py):
    .venv\\Scripts\\python.exe telegram_listener.py            # listen
    .venv\\Scripts\\python.exe telegram_listener.py list       # show chats (id+title)
    .venv\\Scripts\\python.exe telegram_listener.py enable  <chat_id>
    .venv\\Scripts\\python.exe telegram_listener.py disable <chat_id>
    .venv\\Scripts\\python.exe telegram_listener.py status     # what is being listened to

Which chats to listen to is the tg_monitored_chats table (enabled=1). In Phase 17C the Recall UI
will manage it; for now it is the CLI enable/disable. The set of monitored chats
is refreshed on the fly (refresh every 30s), so you can enable/disable without
restarting the listener.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import sqlite3
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Windows console: UTF-8 for Cyrillic/emoji in chat names.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# T8.3 (Wave 4): a separate process (not app.py) — its own log file, a DIFFERENT file
# name (telegram_listener.log) from app.py (whisper_ui.log). The reason the
# shared whisper_ui.log was disabled on purpose — app/core/logger.py:46-53
# (commits 84accf8/baf0cda): a second RotatingFileHandler on ONE file from two
# processes → one's rollover runs into a file held by the other (WinError 32 on
# Windows). A separate file per process removes the conflict entirely (nobody else holds
# this process's own file open during rollover). logging.raiseExceptions =
# False stays as a second line of defence — the same anti-WinError32 pattern as in
# app.py/mcp_server.py, in case two listeners are started in parallel.
logging.raiseExceptions = False
_TG_LOG_FORMAT = "%(asctime)s %(levelname)s [tg-listener] %(message)s"
_tg_log_formatter = logging.Formatter(fmt=_TG_LOG_FORMAT, datefmt="%H:%M:%S")

_tg_stream_handler = logging.StreamHandler()
_tg_stream_handler.setFormatter(_tg_log_formatter)
_tg_handlers = [_tg_stream_handler]

# Under pytest this module is only imported (tests/test_telegram.py, at
# module level — i.e. already at the collection stage) — it is not the live listener, and
# a file handler here would write into the owner's telegram_listener.log next to
# real records (that was the bug: a line from a test was taken for the end of a
# live catchup). The settings registry already knows PYTEST_CURRENT_TEST as
# an external variable (app/core/settings.py IGNORED_ENV_NAMES), but pytest sets
# exactly this variable only for the test execution phase (setup/call/teardown),
# not for collection — at the moment of this import it is still None. pytest puts "pytest" in
# sys.modules immediately at startup, before collecting tests, so that is
# the reliable indicator here. The console handler stays under pytest too,
# but pytest has already hung its own handlers on the root, so this basicConfig() is a
# no-op and the StreamHandler is not attached at all (measured); `capsys` therefore does not
# see listener lines, `caplog` does — via propagation to the root.
if "pytest" not in sys.modules:
    try:
        _tg_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telegram_listener.log")
        _tg_file_handler = logging.handlers.RotatingFileHandler(
            _tg_log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8", delay=True,
        )
        _tg_file_handler.setFormatter(_tg_log_formatter)
        _tg_handlers.append(_tg_file_handler)
    except OSError:
        # Do not crash the listener if the log file is unavailable — the console log remains.
        pass

logging.basicConfig(level=logging.INFO, handlers=_tg_handlers)
logger = logging.getLogger("tg_listener")

REFRESH_SECONDS = 30
STICKER_SKIP = True  # stickers/gifs are meaningless for RAG — skip them


# ============================================================
# Config / DB
# ============================================================

def _load_cfg():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    from config import get_config
    cfg = get_config()
    api_id = int(os.environ.get("TELEGRAM_API_ID", "0") or "0")
    api_hash = os.environ.get("TELEGRAM_API_HASH", "")
    if not api_id or not api_hash:
        logger.error("TELEGRAM_API_ID / TELEGRAM_API_HASH missing in .env")
        sys.exit(1)
    api_base = f"http://127.0.0.1:{getattr(cfg, 'PORT', 5050)}"
    return {
        "api_id": api_id,
        "api_hash": api_hash,
        # BASE_DIR-absolute path, cwd-independent; an explicit env is already honoured
        # in config.py:269 — a separate os.environ.get here duplicated/bypassed it.
        "session": str(cfg.TELEGRAM_SESSION),
        "media_dir": str(getattr(cfg, "TELEGRAM_MEDIA_DIR", "telegram_media")),
        "db_path": os.path.abspath(str(getattr(cfg, "DATABASE", "whisper_history.db"))),
        "ingest_url": f"{api_base}/api/telegram/ingest",
        # T2.6: app.py stays the only writer to SQLite — cmd_toggle (CLI
        # enable/disable) POSTs here instead of a direct INSERT/UPDATE.
        "chats_toggle_url": f"{api_base}/api/telegram/chats/toggle",
        # Wave 4: the archive is no longer written once and for all.
        "edited_url": f"{api_base}/api/telegram/edited",
        "deleted_url": f"{api_base}/api/telegram/deleted",
        "control_host": getattr(cfg, "TELEGRAM_CONTROL_HOST", "127.0.0.1"),
        "control_port": int(getattr(cfg, "TELEGRAM_CONTROL_PORT", 5051)),
        # Dialog window. 200 hit the ceiling (dialogs_seen == limit), and then
        # «the chat is not among the dialogs» cannot be told from «did not fit the window» —
        # exactly why /coverage could not deliver a verdict on two chats.
        "dialog_limit": int(os.environ.get("TELEGRAM_DIALOG_LIMIT", "500")),
        # Throttle backfill against flood-wait (seconds between messages).
        "backfill_delay": float(os.environ.get("TELEGRAM_BACKFILL_DELAY", "0.5")),
        # Undelivered to Flask (app.py was restarting) goes here, not into nothing.
        "deadletter": os.path.join(_BASE_DIR, "telegram_deadletter.jsonl"),
        # Catch-up after downtime: the state «when we last caught up» + a switch + a debounce.
        "catchup_state": os.path.join(_BASE_DIR, "telegram_catchup.json"),
        "catchup_enabled": os.environ.get("TELEGRAM_CATCHUP", "1").strip().lower()
                           not in ("0", "false", "no"),
        "catchup_debounce_min": float(os.environ.get("TELEGRAM_CATCHUP_DEBOUNCE_MIN", "15")),
    }


def _db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # T2.3: explicit busy_timeout — the listener writes to the same WAL DB as app.py;
    # without it a concurrent transaction gives an immediate `database is locked`.
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _enabled_chat_ids(db_path: str) -> set[int]:
    try:
        with _db(db_path) as conn:
            rows = conn.execute(
                "SELECT chat_id FROM tg_monitored_chats WHERE enabled = 1"
            ).fetchall()
        return {int(r["chat_id"]) for r in rows}
    except Exception as e:
        logger.warning("Could not read tg_monitored_chats: %s", e)
        return set()


# ============================================================
# Routing a message → payload for Flask
# ============================================================

# tg-media-policy-03: a video sent as a «file» (document), Telegram often
# leaves without a mime_type or with an erased/mismatched one — DocumentAttributeVideo
# catches compressed video and round notes, but not this case. The extension from
# DocumentAttributeFilename is the last signal before the document silently
# goes into a full download.
_VIDEO_EXTENSIONS = (
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".wmv", ".flv", ".m4v", ".3gp", ".mpeg", ".mpg",
)


def _doc_filename(doc) -> str:
    """Document file name. Telethon puts it in attributes
    (DocumentAttributeFilename), not in a flat document attribute."""
    for attr in getattr(doc, "attributes", None) or []:
        name = getattr(attr, "file_name", None)
        if name:
            return name
    return ""


def _detect_kind(msg) -> str | None:
    """Message type for ingest. None = skip (sticker/service)."""
    if getattr(msg, "voice", None):
        return "voice"
    if getattr(msg, "video_note", None) or getattr(msg, "video", None) or getattr(msg, "gif", None):
        return "video"
    if getattr(msg, "audio", None):
        return "audio"
    if getattr(msg, "photo", None):
        return "photo"
    if STICKER_SKIP and getattr(msg, "sticker", None):
        return None
    doc = getattr(msg, "document", None)
    if doc:
        mime = (getattr(doc, "mime_type", "") or "").lower()
        if mime.startswith("image/"):
            return "photo"
        if mime.startswith("video/"):
            return "video"
        if mime.startswith("audio/"):
            return "audio"
        if _doc_filename(doc).lower().endswith(_VIDEO_EXTENSIONS):
            return "video"
        return "document"
    if (msg.message or "").strip():
        return "text"
    return None


def _sender_name(sender) -> str | None:
    if sender is None:
        return None
    name = " ".join(filter(None, [getattr(sender, "first_name", None),
                                  getattr(sender, "last_name", None)]))
    if name:
        return name
    return getattr(sender, "title", None) or (
        f"@{sender.username}" if getattr(sender, "username", None) else None)


def _chat_link(chat, chat_id: int | None, msg_id: int) -> str | None:
    if chat_id is None:
        # Telethon returns None when peer_id has none of the fields
        # (a rare but real case) — earlier this gave a link or
        # None through comparison with int, now we behave the same way explicitly.
        return None
    if chat_id > 0:
        # Personal chat (positive chat_id = User): even if the counterpart
        # has a username, https://t.me/<username>/<msg_id> is a
        # link to a post in a public group/channel, not to a message in
        # a private conversation. No link format exists for such a message
        # (core.telegram.org/api/links).
        return None
    uname = getattr(chat, "username", None)
    if uname:
        return f"https://t.me/{uname}/{msg_id}"
    s = str(chat_id)
    if s.startswith("-100"):
        return f"https://t.me/c/{s[4:]}/{msg_id}"
    return None


def _chat_title(chat, chat_id: int) -> str:
    return (getattr(chat, "title", None)
            or " ".join(filter(None, [getattr(chat, "first_name", None),
                                      getattr(chat, "last_name", None)]))
            or f"chat {chat_id}")


def _post_ingest(url: str, payload: dict) -> None:
    from telegram_common import CONTROL_TOKEN_HEADER, control_token
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 CONTROL_TOKEN_HEADER: control_token()})
    with urllib.request.urlopen(req, timeout=120) as resp:
        resp.read()


def _deadletter_append(path: str, payload: dict) -> None:
    """A message that could not be handed to Flask goes to the dead-letter, not vanishing."""
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as e:
        # Last line of defence: even the file is not written — loud in the log, as this is already a loss.
        logger.error("[deadletter] NOT SAVED (chat=%s msg=%s): %s",
                     payload.get("chat_id"), payload.get("message_id"), e)


def _post_ingest_reliable(url: str, payload: dict, deadletter: str) -> bool:
    """POST to ingest with retries; failure → dead-letter, never a silent loss.

    Reason: earlier `_post_ingest` raised an exception, the handler logged it
    (telegram_listener.py: except in the handler) — and the message vanished forever.
    app.py is unavailable exactly when it restarts, i.e.
    regularly. This is a separate source of losses, independent of listener downtime, and it is
    the one the audit did not see: in id gaps it looks the same as a switched-off machine.
    """
    delays = (1, 3, 9)
    for attempt, pause in enumerate(delays, 1):
        try:
            _post_ingest(url, payload)
            if attempt > 1:
                logger.info("[ingest] delivered on attempt %d (chat=%s msg=%s)",
                            attempt, payload.get("chat_id"), payload.get("message_id"))
            return True
        except Exception as e:
            if attempt == len(delays):
                logger.warning("[ingest] not delivered after %d attempts (%s) → dead-letter "
                               "(chat=%s msg=%s)", attempt, e,
                               payload.get("chat_id"), payload.get("message_id"))
                _deadletter_append(deadletter, payload)
                return False
            logger.info("[ingest] attempt %d/%d failed (%s) — retry in %ds",
                        attempt, len(delays), e, pause)
            time.sleep(pause)
    return False


def _replay_deadletter(cfg: dict) -> None:
    """Try to deliver what once did not arrive. Called at startup, when
    app.py is definitely up. What fails again stays in the file."""
    path = cfg["deadletter"]
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            pending = [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError) as e:
        logger.error("[deadletter] could not read %s: %s", path, e)
        return
    if not pending:
        return

    logger.info("[deadletter] redelivery: %d messages", len(pending))
    still_failing = []
    for payload in pending:
        try:
            _post_ingest(cfg["ingest_url"], payload)
        except Exception as e:
            logger.info("[deadletter] failed again (chat=%s msg=%s): %s",
                        payload.get("chat_id"), payload.get("message_id"), e)
            still_failing.append(payload)
    try:
        if still_failing:
            with open(path, "w", encoding="utf-8") as f:
                for payload in still_failing:
                    f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        else:
            os.remove(path)
    except OSError as e:
        logger.warning("[deadletter] could not update %s: %s", path, e)
    logger.info("[deadletter] delivered %d, %d left",
                len(pending) - len(still_failing), len(still_failing))


def _post_json(url: str, payload: dict, timeout: int = 15) -> dict:
    """POST with the shared secret, returns the parsed JSON. Raises on
    a network error/non-2xx — the caller is responsible for retry/degradation."""
    from telegram_common import CONTROL_TOKEN_HEADER, control_token
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 CONTROL_TOKEN_HEADER: control_token()})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


async def _ingest_message(chat, msg, cfg) -> str | None:
    """Build the payload from a message → download media → POST to Flask ingest.
    SHARED by real-time (handler) and backfill (iter_messages). chat is an already
    resolved entity (so as not to resolve on every message). Dedup is done by
    ingest itself (chat_id,message_id), so real-time/backfill overlap is safe.
    Returns kind if sent, None if skipped (sticker/empty/no media)."""
    kind = _detect_kind(msg)
    if kind is None:
        return None
    chat_id = msg.chat_id
    try:
        sender = await msg.get_sender()
    except Exception:
        sender = None
    edit_date = getattr(msg, "edit_date", None)
    payload = {
        "kind": kind,
        "chat_id": chat_id,
        "chat_title": _chat_title(chat, chat_id),
        "sender": _sender_name(sender),
        "message_id": msg.id,
        "date": msg.date.isoformat() if msg.date else None,
        "link": _chat_link(chat, chat_id, msg.id),
        # Wave 4: signals that Telegram gives for free and we threw away.
        # reply_to is the EXACT thread (better than any guessing by time);
        # sender_id is a stable author key (a person changes their name);
        # grouped_id is an album; edit_date is the only way to see an edit.
        "sender_id": getattr(sender, "id", None),
        "reply_to": getattr(msg, "reply_to_msg_id", None),
        "grouped_id": getattr(msg, "grouped_id", None),
        "edit_date": edit_date.isoformat() if edit_date else None,
    }
    text_body = (msg.message or "").strip()
    if kind == "text":
        payload["text"] = text_body
    elif kind == "video":
        # Owner's decision (tg-media-policy-01): video is NEVER downloaded —
        # neither to the listener's disk nor further. We set no file_path in the payload at all;
        # ingest itself leaves a stub row with a link (app/blueprints/telegram.py).
        payload["caption"] = text_body
    else:
        payload["caption"] = text_body
        # Download media to disk; Flask reads by local path (same machine).
        path = await msg.download_media(file=cfg["media_dir"])
        if not path:
            logger.warning("Could not download media (chat=%s msg=%s)", chat_id, msg.id)
            return None
        payload["file_path"] = os.path.abspath(path)

    delivered = await asyncio.to_thread(
        _post_ingest_reliable, cfg["ingest_url"], payload, cfg["deadletter"])
    return kind if delivered else None


# ============================================================
# Control-API (Phase 17C): Flask asks the listener (only it holds the session).
#   GET  /status   → alive? who is logged in? which chats are monitored
#   GET  /dialogs  → list of the account's dialogs (for picking with checkboxes in the UI)
#   POST /backfill → load a chat's history (implementation — Phase 17D)
# Protection: the same shared-secret header as ingest.
# ============================================================

def _ctl_err(key: str) -> str:
    """Control-API error text for the UI, in the server language (lazy import: no Flask at listener import)."""
    from app.core.i18n import tr
    return tr(key)

async def _ctl_status(client, get_monitored):
    me = await client.get_me()
    # username is not cosmetic: a question «@handle, will you do it?» is addressed to the owner
    # structurally, and this is the only way to prove it (Wave 5.3). A name in the text
    # is written any way at all, a handle is exactly one.
    return {"me": {"id": me.id, "name": _sender_name(me),
                   "username": getattr(me, "username", None)},
            "monitored": sorted(get_monitored())}


def _migrated_to(ent) -> int | None:
    """Did the chat migrate to a supergroup? Then chat_id changes to -100…, and the old one becomes
    unavailable — the listener silently goes deaf, because a dead id stays in tg_monitored_chats.
    Candidates: the chat broke off abruptly and does not revive (Wave 1 of the roadmap)."""
    target = getattr(ent, "migrated_to", None)
    if target is None:
        return None
    try:
        from telethon import utils
        return utils.get_peer_id(target)
    except Exception as e:
        logger.debug("could not unfold migrated_to for %s: %s", getattr(ent, "id", "?"), e)
        return None


def _identity_fields(ent) -> dict:
    """Who this is — so that a person can be recognised and found in the chat list
    (name, surname, phone). Everything from the entity iter_dialogs already returned:
    no request per chat. Groups and channels have no such attributes → None.
    phone in Telethon is digits without «+», None if hidden by privacy.
    deleted: a deleted account arrives without a name, and without the flag the row in the UI
    would be a nameless «Chat»."""
    return {
        "first_name": getattr(ent, "first_name", None),
        "last_name": getattr(ent, "last_name", None),
        "phone": getattr(ent, "phone", None),
        "bot": bool(getattr(ent, "bot", False)),
        "deleted": bool(getattr(ent, "deleted", False)),
    }


async def _ctl_dialogs(client, cfg, get_monitored):
    """The account's dialogs + each one's LIVE cursor (Wave 1 «See»).

    last_message_* come from the same iter_dialogs pass (Dialog.message is
    the dialog's last message), so the «archive vs Telegram» check costs
    not a single extra request and needs no second Telethon session."""
    enabled = get_monitored()
    out = []
    async for d in client.iter_dialogs(limit=cfg["dialog_limit"]):
        ent = d.entity
        last = d.message
        out.append({
            "id": d.id, "title": d.name, "type": _chat_type_of(ent, d),
            "username": getattr(ent, "username", None),
            **_identity_fields(ent),
            "monitored": d.id in enabled,
            "last_message_id": getattr(last, "id", None),
            "last_message_date": (last.date.isoformat()
                                  if last is not None and getattr(last, "date", None) else None),
            "unread_count": getattr(d, "unread_count", None),
            "archived": bool(getattr(d, "archived", False)),
            "migrated_to": _migrated_to(ent),
        })
    # We return limit together with the list: without it «the chat is not among the dialogs»
    # cannot be told from «the chat did not fit the window of the last N dialogs».
    return {"dialogs": out, "limit": cfg["dialog_limit"]}


async def _backfill_task(client, chat_id: int, limit: int, cfg):
    """Backfill a chat's history through iter_messages. Fire-and-forget (scheduled on
    the loop, the HTTP response does not wait for completion — it can take minutes). Throttle
    between messages + FloodWaitError handling against a ban. Dedup is in ingest."""
    from telethon.errors import FloodWaitError
    try:
        chat = await client.get_entity(chat_id)
    except Exception as e:
        logger.error("[backfill] could not resolve chat %s: %s", chat_id, e)
        return
    title = _chat_title(chat, chat_id)
    delay = cfg.get("backfill_delay", 0.5)
    logger.info("[backfill] start «%s» (limit=%s, delay=%.2fs)", title, limit, delay)
    sent = 0
    # We iterate manually: FloodWaitError arrives from the generator BOUNDARY (__anext__,
    # when Telethon pulls the next portion of history), not from the loop body. In the version
    # with `async for ... : try:` it flew past the except, and the task is fire-and-forget,
    # so the exception propagated nowhere: backfill died silently, the chat stayed
    # half-filled, and the log simply lacked «completed».
    it = client.iter_messages(chat, limit=limit).__aiter__()
    while True:
        try:
            msg = await it.__anext__()
        except StopAsyncIteration:
            break
        except FloodWaitError as e:
            logger.warning("[backfill] flood-wait %ss on history fetch — waiting", e.seconds)
            await asyncio.sleep(e.seconds + 1)
            continue
        except Exception as e:
            logger.error("[backfill] history fetch of «%s» broke off: %s", title, e)
            break

        try:
            if await _ingest_message(chat, msg, cfg):
                sent += 1
        except FloodWaitError as e:
            # Earlier there was sleep + continue here, i.e. sleeping AND LOSING the message.
            logger.warning("[backfill] flood-wait %ss on msg %s — waiting and RETRYING",
                           e.seconds, getattr(msg, "id", "?"))
            await asyncio.sleep(e.seconds + 1)
            try:
                if await _ingest_message(chat, msg, cfg):
                    sent += 1
            except Exception as e2:
                logger.error("[backfill] msg %s failed even after the pause: %s",
                             getattr(msg, "id", "?"), e2)
        except Exception as e:
            logger.error("[backfill] msg %s error: %s", getattr(msg, "id", "?"), e)
        await asyncio.sleep(delay)
    logger.info("[backfill] completed «%s»: sent %d messages", title, sent)


async def _ctl_repair(client, chat_id: int, ids: list[int], cfg) -> dict:
    """Targeted gap repair: fetch SPECIFIC ids and ingest what is found (Wave 3).

    Why not backfill by limit: get_messages(ids=…) returns None in place of every
    deleted/unavailable message, so «lost by the listener» finally
    differs from «deleted by the author». A sequential pass cannot do this —
    to it a gap in ids always looks the same.

    Telethon itself cuts the list by 100 (_MAX_CHUNK_SIZE), but we cut with our own
    batches too: then FloodWait repeats only its own batch, not the whole repair.

    FloodWait does NOT skip a message (regression of _backfill_task, telegram_listener
    :301 — sleep + continue there, i.e. sleeping and loss), but repeats it.
    """
    from telethon.errors import FloodWaitError
    stats = {"requested": len(ids), "found": 0, "deleted": 0,
             "ingested": 0, "skipped": 0, "failed": 0}
    try:
        chat = await client.get_entity(chat_id)
    except Exception as e:
        logger.error("[repair] could not resolve chat %s: %s", chat_id, e)
        return {**stats, "error": "chat_unresolved"}

    delay = cfg.get("backfill_delay", 0.5)
    logger.info("[repair] «%s»: %d ids to check", _chat_title(chat, chat_id), len(ids))

    for start in range(0, len(ids), 100):
        batch = ids[start:start + 100]
        msgs = None
        for attempt in range(3):
            try:
                msgs = await client.get_messages(chat, ids=batch)
                break
            except FloodWaitError as e:
                logger.warning("[repair] flood-wait %ss on batch %d — waiting",
                               e.seconds, start // 100)
                await asyncio.sleep(e.seconds + 1)
            except Exception as e:
                logger.error("[repair] batch %d failed: %s", start // 100, e)
                break
        if msgs is None:
            stats["failed"] += len(batch)
            continue

        for msg in msgs:
            if msg is None:
                stats["deleted"] += 1      # deleted by the author — not our loss
                continue
            stats["found"] += 1
            try:
                kind = await _ingest_message(chat, msg, cfg)
                stats["ingested" if kind else "skipped"] += 1
            except FloodWaitError as e:
                logger.warning("[repair] flood-wait %ss on msg %s — waiting and RETRYING",
                               e.seconds, msg.id)
                await asyncio.sleep(e.seconds + 1)
                try:
                    kind = await _ingest_message(chat, msg, cfg)
                    stats["ingested" if kind else "skipped"] += 1
                except Exception as e2:
                    logger.error("[repair] msg %s failed even after the pause: %s", msg.id, e2)
                    stats["failed"] += 1
            except Exception as e:
                logger.error("[repair] msg %s error: %s", msg.id, e)
                stats["failed"] += 1
            await asyncio.sleep(delay)

    logger.info("[repair] «%s» completed: %s", _chat_title(chat, chat_id), stats)
    return stats


def _watermarks(db_path: str, chat_ids: set[int]) -> dict[int, dict]:
    """How much the archive already knows per chat — DERIVED from data, not stored.

    The column tg_monitored_chats.last_message_id stays dead on purpose: a separate
    cursor would have to be written by someone (and the only DB writer is app.py), it would drift
    from reality on every partial failure, and it still cannot be reset
    on exit — app.py kills the listener via terminate(), i.e. without atexit and flush.
    MAX(tg_date) is true by construction.

    A date, not min_id: ids are sequential only in supergroups (-100…), and that is 4 chats of 19.

    Together with the date we return the id of that same message: offset_date in Telethon
    is INCLUSIVE, so without it the catch-up re-pulls the already known last
    message every time — dedup rejects it, but the media file manages to be downloaded again
    (in live data the same PDF lay in telegram_media in seven copies).
    """
    if not chat_ids:
        return {}
    marks: dict[int, dict] = {}
    try:
        with _db(db_path) as conn:
            ph = ",".join("?" * len(chat_ids))
            rows = conn.execute(
                f"SELECT tg_chat_id AS cid, tg_date AS last, tg_message_id AS mid "
                f"FROM transcriptions WHERE tg_chat_id IN ({ph}) AND tg_date IS NOT NULL "
                f"GROUP BY tg_chat_id HAVING tg_date = MAX(tg_date)",
                list(chat_ids)).fetchall()
        marks = {int(r["cid"]): {"date": r["last"], "msg_id": r["mid"]}
                 for r in rows if r["last"]}
    except Exception as e:
        logger.warning("[catchup] could not read the watermark: %s", e)
    return marks


def _catchup_state_load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _catchup_state_save(path: str, state: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError as e:
        logger.debug("[catchup] could not save the state: %s", e)


async def _catchup(client, cfg, monitored: set[int]) -> dict:
    """Catch up on what arrived while the listener was not running (Wave 2).

    The listener is a child process of app.py, so a «24/7 listener» is impossible without app.py 24/7:
    a machine switched off for the night = a silent gap. In live data that is 30 lost
    working days over 8 months, skewed to evenings and weekends.

    We take entities from ONE iter_dialogs pass, not get_entity per chat:
    resolving peers is the most flood-prone part, 1-2 calls instead of 18.

    Debounce: app.py starts dozens of times a day during development
    (_maybe_launch_telegram_listener on every start), and without it the catch-up
    would run 18 requests every time.
    """
    from telethon.errors import FloodWaitError

    stats = {"chats": 0, "skipped_debounce": 0, "ingested": 0, "failed": 0, "failed_chats": []}
    if not cfg["catchup_enabled"]:
        logger.info("[catchup] disabled (TELEGRAM_CATCHUP=0)")
        return stats
    if not monitored:
        return stats

    marks = _watermarks(cfg["db_path"], monitored)
    state = _catchup_state_load(cfg["catchup_state"])
    now = datetime.now(timezone.utc)
    debounce = timedelta(minutes=cfg["catchup_debounce_min"])

    # One dialog pass → both entities and the «is the chat available at all» filter.
    entities: dict[int, object] = {}
    try:
        async for d in client.iter_dialogs(limit=cfg["dialog_limit"]):
            if d.id in monitored:
                entities[d.id] = d.entity
    except Exception as e:
        logger.warning("[catchup] could not list dialogs: %s", e)
        return stats

    delay = cfg.get("backfill_delay", 0.5)
    try:
        for chat_id, entity in entities.items():
            try:
                last_run = state.get(str(chat_id))
                if last_run:
                    try:
                        if now - datetime.fromisoformat(last_run) < debounce:
                            stats["skipped_debounce"] += 1
                            continue
                    except ValueError:
                        pass

                mark = marks.get(chat_id)
                if not mark:
                    # A chat with no record at all is the initial history load, a deliberate
                    # decision of the owner (volume unknown), not an automatic repair.
                    state[str(chat_id)] = now.isoformat()
                    continue
                try:
                    since = datetime.fromisoformat(mark["date"])
                except (ValueError, TypeError):
                    continue
                known_id = mark.get("msg_id")

                got = 0
                it = client.iter_messages(entity, offset_date=since, reverse=True).__aiter__()
                while True:
                    try:
                        msg = await it.__anext__()
                    except StopAsyncIteration:
                        break
                    except FloodWaitError as e:
                        logger.warning("[catchup] flood-wait %ss — waiting", e.seconds)
                        await asyncio.sleep(e.seconds + 1)
                        continue
                    except Exception as e:
                        logger.warning("[catchup] fetch for %s broke off: %s", chat_id, e)
                        break

                    # offset_date is inclusive → the first message is our watermark.
                    # We skip BEFORE downloading media, otherwise every start creates
                    # one more copy of the same file in telegram_media.
                    if known_id is not None and getattr(msg, "id", None) == known_id:
                        continue

                    try:
                        if await _ingest_message(entity, msg, cfg):
                            got += 1
                    except Exception as e:
                        logger.error("[catchup] msg %s error: %s", getattr(msg, "id", "?"), e)
                        stats["failed"] += 1
                    await asyncio.sleep(delay)

                state[str(chat_id)] = now.isoformat()
                stats["chats"] += 1
                stats["ingested"] += got
                if got:
                    logger.info("[catchup] «%s»: caught up %d of %s",
                                _chat_title(entity, chat_id), got, mark["date"][:16])
            except Exception as e:
                # One chat's boundary: a failure here costs exactly this chat — the rest of the
                # chats and the state saving must happen despite this error.
                stats["failed_chats"].append(chat_id)
                try:
                    title = _chat_title(entity, chat_id)
                except Exception:
                    title = str(chat_id)
                logger.error("[catchup] «%s» (id=%s) brought the chat down: %s",
                             title, chat_id, e, exc_info=True)
    finally:
        _catchup_state_save(cfg["catchup_state"], state)

    logger.info("[catchup] completed: %s", stats)
    return stats


def _start_control_api(client, loop, cfg, get_monitored):
    """ThreadingHTTPServer in a daemon thread; handlers schedule coroutines on
    the Telethon loop via run_coroutine_threadsafe (because the client lives in the loop)."""
    from telegram_common import CONTROL_TOKEN_HEADER, control_token
    token = control_token()

    def _run_coro(coro, timeout):
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)

    class Handler(BaseHTTPRequestHandler):
        def _auth(self):
            return self.headers.get(CONTROL_TOKEN_HEADER) == token

        def _send(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass  # do not clutter the listener console with HTTP logs

        def do_GET(self):
            if not self._auth():
                return self._send(403, {"error": "forbidden"})
            try:
                if self.path == "/status":
                    return self._send(200, _run_coro(_ctl_status(client, get_monitored), 30))
                if self.path.split("?")[0] == "/dialogs":
                    return self._send(200, _run_coro(_ctl_dialogs(client, cfg, get_monitored), 90))
            except Exception as e:
                return self._send(500, {"error": str(e)})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._auth():
                return self._send(403, {"error": "forbidden"})
            if self.path.split("?")[0] == "/repair":
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads((self.rfile.read(length).decode("utf-8") or "{}"))
                except Exception:
                    body = {}
                ids = body.get("ids") or []
                if body.get("chat_id") is None or not ids:
                    return self._send(400, {"error": _ctl_err("svc_telegram.listener.err.repairArgs")})
                # Synchronous (unlike backfill): the batch is bounded by the caller,
                # and the repair stats are exactly what it is run for.
                try:
                    res = _run_coro(_ctl_repair(client, int(body["chat_id"]),
                                                [int(i) for i in ids], cfg), 900)
                    return self._send(200, res)
                except Exception as e:
                    logger.error("[repair] failed: %s", e, exc_info=True)
                    return self._send(500, {"error": "repair failed"})

            if self.path.split("?")[0] == "/backfill":
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads((self.rfile.read(length).decode("utf-8") or "{}"))
                except Exception:
                    body = {}
                if body.get("chat_id") is None:
                    return self._send(400, {"error": _ctl_err("svc_telegram.listener.err.chatIdRequired")})
                chat_id = int(body["chat_id"])
                limit = int(body.get("limit", 200))
                # Fire-and-forget: backfill is long, we do not hold the HTTP connection.
                asyncio.run_coroutine_threadsafe(
                    _backfill_task(client, chat_id, limit, cfg), loop)
                return self._send(200, {"started": True, "chat_id": chat_id, "limit": limit})
            return self._send(404, {"error": "not found"})

    srv = ThreadingHTTPServer((cfg["control_host"], cfg["control_port"]), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True, name="tg-control-api").start()
    logger.info("Control-API: http://%s:%d (status/dialogs/backfill)",
                cfg["control_host"], cfg["control_port"])
    return srv


# ============================================================
# Run
# ============================================================

async def run_listener(cfg: dict):
    from telethon import TelegramClient, events

    os.makedirs(cfg["media_dir"], exist_ok=True)
    monitored = _enabled_chat_ids(cfg["db_path"])
    logger.info("Start. Monitoring %d chat(s): %s", len(monitored), sorted(monitored) or "—")
    logger.info("Ingest → %s", cfg["ingest_url"])

    client = TelegramClient(cfg["session"], cfg["api_id"], cfg["api_hash"])

    async def refresh_loop():
        nonlocal monitored
        while True:
            await asyncio.sleep(REFRESH_SECONDS)
            new = _enabled_chat_ids(cfg["db_path"])
            if new != monitored:
                added, removed = new - monitored, monitored - new
                monitored = new
                logger.info("Chat set updated (+%s / -%s) → %d active",
                            sorted(added) or "—", sorted(removed) or "—", len(monitored))

    @client.on(events.MessageEdited())
    async def on_edited(event):
        """An edit in a chat → update the text in the archive (Wave 4)."""
        try:
            if event.chat_id not in monitored:
                return
            msg = event.message
            text = (msg.message or "").strip()
            if not text:
                return                       # media edit without a caption — nothing to update
            edit_date = getattr(msg, "edit_date", None)
            await asyncio.to_thread(_post_json, cfg["edited_url"], {
                "chat_id": event.chat_id, "message_id": msg.id, "text": text,
                "edit_date": edit_date.isoformat() if edit_date else None,
            })
            logger.info("→ edit in «%s» (msg=%s)", _chat_title(await event.get_chat(),
                                                                event.chat_id), msg.id)
        except Exception as e:
            logger.error("Edit handling error: %s", e, exc_info=True)

    @client.on(events.MessageDeleted())
    async def on_deleted(event):
        """A deletion in a chat → softly remove from the archive (Wave 4).

        In personal chats Telegram does not always say WHERE the deletion happened
        (event.chat_id None) — then we do nothing: guessing by message_id alone
        is dangerous, ids in different chats overlap."""
        try:
            if event.chat_id is None or event.chat_id not in monitored:
                return
            ids = list(event.deleted_ids or [])
            if not ids:
                return
            await asyncio.to_thread(_post_json, cfg["deleted_url"],
                                    {"chat_id": event.chat_id, "message_ids": ids})
        except Exception as e:
            logger.error("Deletion handling error: %s", e, exc_info=True)

    @client.on(events.NewMessage())
    async def handler(event):
        try:
            if event.chat_id not in monitored:
                return
            chat = await event.get_chat()
            kind = await _ingest_message(chat, event.message, cfg)
            if kind:
                logger.info("→ %s from «%s» (msg=%s)", kind,
                            _chat_title(chat, event.chat_id), event.message.id)
        except Exception as e:
            logger.error("Message handling error: %s", e, exc_info=True)

    async with client:
        asyncio.create_task(refresh_loop())
        # Control-API for Flask: the lambda sees the current `monitored` (refresh_loop
        # reassigns it in this same scope).
        try:
            _start_control_api(client, asyncio.get_running_loop(), cfg, lambda: monitored)
        except OSError as e:
            logger.warning("Control-API did not come up (port busy?): %s", e)
        me = await client.get_me()
        logger.info("Logged in as %s (id=%s). Listening… Ctrl+C to stop.",
                    _sender_name(me), me.id)

        # First we deliver what we already caught but did not deliver (app.py was
        # unavailable), then we catch up on what we missed while not running.
        # Both in the background: the listener must start accepting NEW messages immediately,
        # without waiting for the end of the catch-up.
        async def _startup_recovery():
            await asyncio.to_thread(_replay_deadletter, cfg)
            try:
                await _catchup(client, cfg, set(monitored))
            except Exception as e:
                logger.error("[catchup] failed: %s", e, exc_info=True)

        asyncio.create_task(_startup_recovery())
        await client.run_until_disconnected()


# ============================================================
# CLI: list / enable / disable / status
# ============================================================

async def cmd_list(cfg: dict, limit: int):
    from telethon import TelegramClient
    client = TelegramClient(cfg["session"], cfg["api_id"], cfg["api_hash"])
    async with client:
        enabled = _enabled_chat_ids(cfg["db_path"])
        print(f"{'EN':<3} {'chat_id':>15}  {'type':<9} title")
        print("-" * 70)
        async for d in client.iter_dialogs(limit=limit):
            ent = d.entity
            ctype = ("channel" if getattr(ent, "broadcast", False) else
                     "group" if (d.is_group or getattr(ent, "megagroup", False)) else
                     "user" if d.is_user else "chat")
            mark = "[x]" if d.id in enabled else "[ ]"
            print(f"{mark} {d.id:>15}  {ctype:<9} {d.name}")
    print("\nEnable: telegram_listener.py enable <chat_id>")


def _chat_type_of(ent, dialog=None) -> str:
    if getattr(ent, "broadcast", False):
        return "channel"
    if getattr(ent, "megagroup", False) or (dialog and dialog.is_group):
        return "group"
    if getattr(ent, "first_name", None) is not None:
        return "user"
    return "chat"


async def cmd_toggle(cfg: dict, chat_id: int, enable: bool):
    """Enable/disable monitoring of a chat. Resolves the title/type via Telethon
    (for UX), the write to tg_monitored_chats itself goes THROUGH Flask (T2.6:
    POST /api/telegram/chats/toggle), not by direct SQL from this process. app.py
    stays the only SQLite writer — an invariant kept by the rest of the
    listener (all TG messages also go through /api/telegram/ingest).

    Degradation when app.py is unavailable: 3 attempts with a 2s pause (it may still be
    coming up), then a clear error to stderr + exit(1) — the command does NOT
    fail silently and does NOT try to write to the DB directly as a fallback."""
    from telethon import TelegramClient
    title = username = ctype = None
    try:
        client = TelegramClient(cfg["session"], cfg["api_id"], cfg["api_hash"])
        async with client:
            ent = await client.get_entity(chat_id)
            title = _chat_title(ent, chat_id)
            username = getattr(ent, "username", None)
            ctype = _chat_type_of(ent)
    except Exception as e:
        logger.warning("Could not resolve chat %s (continuing without a title): %s", chat_id, e)

    payload = {"chat_id": chat_id, "enabled": bool(enable), "title": title,
               "username": username, "chat_type": ctype}

    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            res = await asyncio.to_thread(_post_json, cfg["chats_toggle_url"], payload)
            if not res.get("success"):
                raise RuntimeError(res.get("error") or "unknown error from app.py")
            print(f"{'Enabled' if enable else 'Disabled'}: {title or chat_id} ({chat_id})")
            return
        except Exception as e:
            last_err = e
            if attempt < 3:
                logger.warning(
                    "Attempt %d/3 to write chat %s to app.py failed (%s) — retry in 2s",
                    attempt, chat_id, e,
                )
                await asyncio.sleep(2)

    logger.error("Could not save chat %s: app.py unavailable (%s)", chat_id, last_err)
    print(
        f"ERROR: could not save chat {chat_id} — app.py unavailable ({last_err}).\n"
        f"Make sure the application is running (.venv/Scripts/python.exe app.py) and retry the command.",
        file=sys.stderr,
    )
    sys.exit(1)


def cmd_status(cfg: dict):
    with _db(cfg["db_path"]) as conn:
        rows = conn.execute(
            "SELECT chat_id, title, chat_type, enabled, category_id "
            "FROM tg_monitored_chats ORDER BY enabled DESC, title"
        ).fetchall()
    if not rows:
        print("No chats configured. First: telegram_listener.py list")
        return
    print(f"{'EN':<3} {'chat_id':>15}  {'type':<9} {'cat':>4}  title")
    print("-" * 70)
    for r in rows:
        mark = "[x]" if r["enabled"] else "[ ]"
        cat = r["category_id"] if r["category_id"] is not None else "—"
        print(f"{mark} {r['chat_id']:>15}  {r['chat_type'] or '?':<9} {str(cat):>4}  {r['title'] or ''}")


def main() -> int:
    p = argparse.ArgumentParser(description="Telegram listener for Recall (Phase 17B)")
    sub = p.add_subparsers(dest="cmd")
    pl = sub.add_parser("list", help="show the account's chats (id + title)")
    pl.add_argument("--limit", type=int, default=100)
    pe = sub.add_parser("enable", help="enable a chat for listening")
    pe.add_argument("chat_id", type=int)
    pd = sub.add_parser("disable", help="disable a chat")
    pd.add_argument("chat_id", type=int)
    sub.add_parser("status", help="what is being listened to now")
    args = p.parse_args()

    cfg = _load_cfg()
    try:
        from telethon import TelegramClient  # noqa: F401
    except ImportError:
        logger.error("Telethon is not installed: .venv\\Scripts\\python.exe -m pip install telethon")
        return 1

    if args.cmd == "list":
        asyncio.run(cmd_list(cfg, args.limit))
    elif args.cmd == "enable":
        asyncio.run(cmd_toggle(cfg, args.chat_id, True))
    elif args.cmd == "disable":
        asyncio.run(cmd_toggle(cfg, args.chat_id, False))
    elif args.cmd == "status":
        cmd_status(cfg)
    else:
        try:
            asyncio.run(run_listener(cfg))
        except KeyboardInterrupt:
            logger.info("Stopped by the user.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
