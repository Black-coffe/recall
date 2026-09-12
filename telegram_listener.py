#!/usr/bin/env python
"""Phase 17B — слухач реального Telegram-акаунта (MTProto/Telethon).

ОКРЕМИЙ процес (не частина Flask). Тонкий: ловить нові повідомлення у
моніторених чатах, СКАЧУЄ медіа на диск і POST'ить у Flask
(/api/telegram/ingest) — уся важка робота (parse/OCR/whisper/embeddings/Claude)
там, де моделі вже в пам'яті GPU. Слухач НЕ вантажить моделей.

Запуск (після telegram_login.py):
    .venv\\Scripts\\python.exe telegram_listener.py            # слухати
    .venv\\Scripts\\python.exe telegram_listener.py list       # показати чати (id+назва)
    .venv\\Scripts\\python.exe telegram_listener.py enable  <chat_id>
    .venv\\Scripts\\python.exe telegram_listener.py disable <chat_id>
    .venv\\Scripts\\python.exe telegram_listener.py status     # що слухається

Які чати слухати — таблиця tg_monitored_chats (enabled=1). У Phase 17C цим
керуватиме UI Recall; зараз — CLI enable/disable. Набір моніторених чатів
оновлюється на льоту (refresh кожні 30с), тож вмикати/вимикати можна без
перезапуску слухача.
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

# Windows-консоль: UTF-8 для кирилиці/емодзі імен чатів.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# T8.3 (Волна 4): окремий процес (не app.py) — свій файловий лог, ІНША назва
# файлу (telegram_listener.log), ніж app.py (whisper_ui.log). Причина, чому
# спільний whisper_ui.log був вимкнений навмисно — app/core/logger.py:46-53
# (коміти 84accf8/baf0cda): другий RotatingFileHandler на ОДНОМУ файлі з двох
# процесів → rollover одного впирається у файл, зайнятий іншим (WinError 32 на
# Windows). Окремий файл на процес прибирає конфлікт повністю (rollover свого
# файлу цей процес ніхто інший не тримає відкритим). logging.raiseExceptions =
# False лишаємо як другий рубіж — той самий anti-WinError32 патерн, що і в
# app.py/mcp_server.py, на випадок паралельного запуску двох listener'ів.
logging.raiseExceptions = False
_TG_LOG_FORMAT = "%(asctime)s %(levelname)s [tg-listener] %(message)s"
_tg_log_formatter = logging.Formatter(fmt=_TG_LOG_FORMAT, datefmt="%H:%M:%S")

_tg_stream_handler = logging.StreamHandler()
_tg_stream_handler.setFormatter(_tg_log_formatter)
_tg_handlers = [_tg_stream_handler]
try:
    _tg_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telegram_listener.log")
    _tg_file_handler = logging.handlers.RotatingFileHandler(
        _tg_log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8", delay=True,
    )
    _tg_file_handler.setFormatter(_tg_log_formatter)
    _tg_handlers.append(_tg_file_handler)
except OSError:
    # Не валимо слухача, якщо файл лога недоступний — консольний лог лишається.
    pass

logging.basicConfig(level=logging.INFO, handlers=_tg_handlers)
logger = logging.getLogger("tg_listener")

REFRESH_SECONDS = 30
STICKER_SKIP = True  # стікери/гіфи без сенсу для RAG — пропускаємо


# ============================================================
# Конфіг / БД
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
        logger.error("Немає TELEGRAM_API_ID / TELEGRAM_API_HASH у .env")
        sys.exit(1)
    api_base = f"http://127.0.0.1:{getattr(cfg, 'PORT', 5050)}"
    return {
        "api_id": api_id,
        "api_hash": api_hash,
        # BASE_DIR-абсолютний шлях, cwd-незалежний; явний env і так врахований
        # у config.py:269 — окремий os.environ.get тут дублював/обходив його.
        "session": str(cfg.TELEGRAM_SESSION),
        "media_dir": str(getattr(cfg, "TELEGRAM_MEDIA_DIR", "telegram_media")),
        "db_path": os.path.abspath(str(getattr(cfg, "DATABASE", "whisper_history.db"))),
        "ingest_url": f"{api_base}/api/telegram/ingest",
        # T2.6: app.py лишається єдиним писарем у SQLite — cmd_toggle (CLI
        # enable/disable) POST'ить сюди замість прямого INSERT/UPDATE.
        "chats_toggle_url": f"{api_base}/api/telegram/chats/toggle",
        # Волна 4: архів більше не пишеться один раз назавжди.
        "edited_url": f"{api_base}/api/telegram/edited",
        "deleted_url": f"{api_base}/api/telegram/deleted",
        "control_host": getattr(cfg, "TELEGRAM_CONTROL_HOST", "127.0.0.1"),
        "control_port": int(getattr(cfg, "TELEGRAM_CONTROL_PORT", 5051)),
        # Вікно діалогів. 200 упиралось у стелю (dialogs_seen == limit), і тоді
        # «чату немає серед діалогів» неможливо відрізнити від «не вліз у вікно» —
        # саме на цьому /coverage не міг винести вердикт по двох чатах.
        "dialog_limit": int(os.environ.get("TELEGRAM_DIALOG_LIMIT", "500")),
        # Throttle backfill проти flood-wait (сек між повідомленнями).
        "backfill_delay": float(os.environ.get("TELEGRAM_BACKFILL_DELAY", "0.5")),
        # Недоставлене в Flask (app.py перезапускався) — сюди, а не в нікуди.
        "deadletter": os.path.join(_BASE_DIR, "telegram_deadletter.jsonl"),
        # Догонка після простою: стан «коли востаннє догоняли» + вимикач + дебаунс.
        "catchup_state": os.path.join(_BASE_DIR, "telegram_catchup.json"),
        "catchup_enabled": os.environ.get("TELEGRAM_CATCHUP", "1").strip().lower()
                           not in ("0", "false", "no"),
        "catchup_debounce_min": float(os.environ.get("TELEGRAM_CATCHUP_DEBOUNCE_MIN", "15")),
    }


def _db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # T2.3: явний busy_timeout — listener пише в ту саму WAL-БД, що й app.py;
    # без нього конкурентна транзакція дає миттєвий `database is locked`.
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
        logger.warning("Не зміг прочитати tg_monitored_chats: %s", e)
        return set()


# ============================================================
# Маршрутизація повідомлення → payload для Flask
# ============================================================

def _detect_kind(msg) -> str | None:
    """Тип повідомлення для ingest. None = пропустити (стікер/службове)."""
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


def _chat_link(chat, chat_id: int, msg_id: int) -> str | None:
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
    """Повідомлення, яке не вдалось віддати Flask, лягає у dead-letter, а не зникає."""
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as e:
        # Останній рубіж: навіть файл не пишеться — гучно в лог, бо це вже втрата.
        logger.error("[deadletter] НЕ ЗБЕРЕГЛОСЬ (chat=%s msg=%s): %s",
                     payload.get("chat_id"), payload.get("message_id"), e)


def _post_ingest_reliable(url: str, payload: dict, deadletter: str) -> bool:
    """POST в ingest із ретраями; провал → dead-letter, ніколи не мовчазна втрата.

    Причина: раніше `_post_ingest` кидав виняток, handler його логував
    (telegram_listener.py: except у handler) — і повідомлення зникало назавжди.
    app.py недоступний рівно в ті моменти, коли він перезапускається, тобто
    регулярно. Це — окреме джерело втрат, незалежне від простою слухача, і саме
    його не бачив аудит: у розривах id воно виглядає так само, як вимкнена машина.
    """
    delays = (1, 3, 9)
    for attempt, pause in enumerate(delays, 1):
        try:
            _post_ingest(url, payload)
            if attempt > 1:
                logger.info("[ingest] доставлено з %d-ї спроби (chat=%s msg=%s)",
                            attempt, payload.get("chat_id"), payload.get("message_id"))
            return True
        except Exception as e:
            if attempt == len(delays):
                logger.warning("[ingest] не доставлено після %d спроб (%s) → dead-letter "
                               "(chat=%s msg=%s)", attempt, e,
                               payload.get("chat_id"), payload.get("message_id"))
                _deadletter_append(deadletter, payload)
                return False
            logger.info("[ingest] спроба %d/%d не вдалась (%s) — повтор через %ds",
                        attempt, len(delays), e, pause)
            time.sleep(pause)
    return False


def _replay_deadletter(cfg: dict) -> None:
    """Спробувати віддати те, що колись не доїхало. Викликається на старті, коли
    app.py вже точно піднятий. Те, що не пішло знову, лишається у файлі."""
    path = cfg["deadletter"]
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            pending = [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError) as e:
        logger.error("[deadletter] не зміг прочитати %s: %s", path, e)
        return
    if not pending:
        return

    logger.info("[deadletter] повторна доставка: %d повідомлень", len(pending))
    still_failing = []
    for payload in pending:
        try:
            _post_ingest(cfg["ingest_url"], payload)
        except Exception as e:
            logger.info("[deadletter] знову не пішло (chat=%s msg=%s): %s",
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
        logger.warning("[deadletter] не зміг оновити %s: %s", path, e)
    logger.info("[deadletter] доставлено %d, лишилось %d",
                len(pending) - len(still_failing), len(still_failing))


def _post_json(url: str, payload: dict, timeout: int = 15) -> dict:
    """POST з shared-secret, повертає розпарсений JSON. Кидає виняток при
    мережевій помилці/не-2xx — виклик відповідає за retry/деградацію."""
    from telegram_common import CONTROL_TOKEN_HEADER, control_token
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 CONTROL_TOKEN_HEADER: control_token()})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


async def _ingest_message(chat, msg, cfg) -> str | None:
    """Побудувати payload з повідомлення → скачати медіа → POST у Flask ingest.
    СПІЛЬНЕ для real-time (handler) і backfill (iter_messages). chat — вже
    резолвлена сутність (щоб не резолвити на кожне повідомлення). Дедуп робить
    сам ingest (chat_id,message_id), тож перетин real-time/backfill безпечний.
    Returns kind якщо відправлено, None якщо пропущено (стікер/порожнє/без медіа)."""
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
        # Волна 4: сигнали, які Telegram дає безкоштовно, а ми викидали.
        # reply_to — ТОЧНА нитка (краще за будь-яке вгадування по часу);
        # sender_id — стабільний ключ автора (імʼя людина міняє);
        # grouped_id — альбом; edit_date — єдиний спосіб побачити правку.
        "sender_id": getattr(sender, "id", None),
        "reply_to": getattr(msg, "reply_to_msg_id", None),
        "grouped_id": getattr(msg, "grouped_id", None),
        "edit_date": edit_date.isoformat() if edit_date else None,
    }
    text_body = (msg.message or "").strip()
    if kind == "text":
        payload["text"] = text_body
    else:
        payload["caption"] = text_body
        # Скачуємо медіа на диск; Flask читає по локальному шляху (та сама машина).
        path = await msg.download_media(file=cfg["media_dir"])
        if not path:
            logger.warning("Не вдалось скачати медіа (chat=%s msg=%s)", chat_id, msg.id)
            return None
        payload["file_path"] = os.path.abspath(path)

    delivered = await asyncio.to_thread(
        _post_ingest_reliable, cfg["ingest_url"], payload, cfg["deadletter"])
    return kind if delivered else None


# ============================================================
# Control-API (Phase 17C): Flask питає слухача (лише він тримає сесію).
#   GET  /status   → живий? хто залогінений? які чати моніторяться
#   GET  /dialogs  → список діалогів акаунта (для вибору галочками в UI)
#   POST /backfill → догрузка історії чату (реалізація — Phase 17D)
# Захист: той самий shared-secret header, що й ingest.
# ============================================================

async def _ctl_status(client, get_monitored):
    me = await client.get_me()
    # username — не косметика: питання «@нік, зробиш?» адресоване власнику
    # структурно, і це єдиний спосіб це довести (Волна 5.3). Ім'я в тексті
    # пишуть як завгодно, нік — рівно один.
    return {"me": {"id": me.id, "name": _sender_name(me),
                   "username": getattr(me, "username", None)},
            "monitored": sorted(get_monitored())}


def _migrated_to(ent) -> int | None:
    """Чат мігрував у супергрупу? Тоді chat_id змінюється на -100…, а старий стає
    недоступним — слухач мовчки глухне, бо в tg_monitored_chats лишається мертвий id.
    Кандидати на це: чат обірвався різко і не оживає (Волна 1 роадмапу)."""
    target = getattr(ent, "migrated_to", None)
    if target is None:
        return None
    try:
        from telethon import utils
        return utils.get_peer_id(target)
    except Exception as e:
        logger.debug("не зміг розгорнути migrated_to для %s: %s", getattr(ent, "id", "?"), e)
        return None


async def _ctl_dialogs(client, cfg, get_monitored):
    """Діалоги акаунта + ЖИВИЙ курсор кожного (Волна 1 «Бачити»).

    last_message_* беруться з того самого проходу iter_dialogs (Dialog.message —
    останнє повідомлення діалогу), тож звірка «архів проти Telegram» не коштує
    жодного зайвого запиту і не потребує другої Telethon-сесії."""
    enabled = get_monitored()
    out = []
    async for d in client.iter_dialogs(limit=cfg["dialog_limit"]):
        ent = d.entity
        last = d.message
        out.append({
            "id": d.id, "title": d.name, "type": _chat_type_of(ent, d),
            "username": getattr(ent, "username", None),
            "monitored": d.id in enabled,
            "last_message_id": getattr(last, "id", None),
            "last_message_date": (last.date.isoformat()
                                  if last is not None and getattr(last, "date", None) else None),
            "unread_count": getattr(d, "unread_count", None),
            "archived": bool(getattr(d, "archived", False)),
            "migrated_to": _migrated_to(ent),
        })
    # limit віддаємо разом зі списком: без нього «чату немає серед діалогів»
    # неможливо відрізнити від «чат не вліз у вікно останніх N діалогів».
    return {"dialogs": out, "limit": cfg["dialog_limit"]}


async def _backfill_task(client, chat_id: int, limit: int, cfg):
    """Догрузка історії чату через iter_messages. Fire-and-forget (планується на
    loop, HTTP-відповідь не чекає завершення — може тривати хвилини). Throttle
    між повідомленнями + обробка FloodWaitError проти бана. Дедуп — в ingest."""
    from telethon.errors import FloodWaitError
    try:
        chat = await client.get_entity(chat_id)
    except Exception as e:
        logger.error("[backfill] не зміг резолвити чат %s: %s", chat_id, e)
        return
    title = _chat_title(chat, chat_id)
    delay = cfg.get("backfill_delay", 0.5)
    logger.info("[backfill] старт «%s» (limit=%s, delay=%.2fs)", title, limit, delay)
    sent = 0
    # Ітеруємо вручну: FloodWaitError прилітає з ГРАНИЦІ генератора (__anext__,
    # коли Telethon тягне наступну порцію історії), а не з тіла циклу. У версії
    # з `async for ... : try:` він летів повз except, а задача — fire-and-forget,
    # тож виняток нікуди не прокидався: backfill помирав мовчки, чат лишався
    # залитим наполовину, і в логу просто не зʼявлялось «завершено».
    it = client.iter_messages(chat, limit=limit).__aiter__()
    while True:
        try:
            msg = await it.__anext__()
        except StopAsyncIteration:
            break
        except FloodWaitError as e:
            logger.warning("[backfill] flood-wait %ss на вибірці історії — чекаю", e.seconds)
            await asyncio.sleep(e.seconds + 1)
            continue
        except Exception as e:
            logger.error("[backfill] вибірка історії «%s» обірвалась: %s", title, e)
            break

        try:
            if await _ingest_message(chat, msg, cfg):
                sent += 1
        except FloodWaitError as e:
            # Раніше тут було sleep + continue, тобто сон І ВТРАТА повідомлення.
            logger.warning("[backfill] flood-wait %ss на msg %s — чекаю і ПОВТОРЮЮ",
                           e.seconds, getattr(msg, "id", "?"))
            await asyncio.sleep(e.seconds + 1)
            try:
                if await _ingest_message(chat, msg, cfg):
                    sent += 1
            except Exception as e2:
                logger.error("[backfill] msg %s не вдалось і після паузи: %s",
                             getattr(msg, "id", "?"), e2)
        except Exception as e:
            logger.error("[backfill] msg %s помилка: %s", getattr(msg, "id", "?"), e)
        await asyncio.sleep(delay)
    logger.info("[backfill] завершено «%s»: відправлено %d повідомлень", title, sent)


async def _ctl_repair(client, chat_id: int, ids: list[int], cfg) -> dict:
    """Точковий ремонт дірок: тягнемо КОНКРЕТНІ id і інжестимо знайдене (Волна 3).

    Чому не backfill за limit: get_messages(ids=…) віддає None на місці кожного
    видаленого/недоступного повідомлення, тож «втрачено слухачем» нарешті
    відрізняється від «видалено автором». Послідовний прохід цього не вміє —
    для нього дірка в id завжди виглядає однаково.

    Telethon сам ріже список по 100 (_MAX_CHUNK_SIZE), але ми ріжемо своїми
    пачками теж: тоді FloodWait повторює лише свою пачку, а не весь ремонт.

    FloodWait НЕ пропускає повідомлення (регрес _backfill_task, telegram_listener
    :301 — там sleep + continue, тобто сон і втрата), а повторює його.
    """
    from telethon.errors import FloodWaitError
    stats = {"requested": len(ids), "found": 0, "deleted": 0,
             "ingested": 0, "skipped": 0, "failed": 0}
    try:
        chat = await client.get_entity(chat_id)
    except Exception as e:
        logger.error("[repair] не зміг резолвити чат %s: %s", chat_id, e)
        return {**stats, "error": "chat_unresolved"}

    delay = cfg.get("backfill_delay", 0.5)
    logger.info("[repair] «%s»: %d id на перевірку", _chat_title(chat, chat_id), len(ids))

    for start in range(0, len(ids), 100):
        batch = ids[start:start + 100]
        msgs = None
        for attempt in range(3):
            try:
                msgs = await client.get_messages(chat, ids=batch)
                break
            except FloodWaitError as e:
                logger.warning("[repair] flood-wait %ss на пачці %d — чекаю",
                               e.seconds, start // 100)
                await asyncio.sleep(e.seconds + 1)
            except Exception as e:
                logger.error("[repair] пачка %d впала: %s", start // 100, e)
                break
        if msgs is None:
            stats["failed"] += len(batch)
            continue

        for msg in msgs:
            if msg is None:
                stats["deleted"] += 1      # видалено автором — це не наша втрата
                continue
            stats["found"] += 1
            try:
                kind = await _ingest_message(chat, msg, cfg)
                stats["ingested" if kind else "skipped"] += 1
            except FloodWaitError as e:
                logger.warning("[repair] flood-wait %ss на msg %s — чекаю і ПОВТОРЮЮ",
                               e.seconds, msg.id)
                await asyncio.sleep(e.seconds + 1)
                try:
                    kind = await _ingest_message(chat, msg, cfg)
                    stats["ingested" if kind else "skipped"] += 1
                except Exception as e2:
                    logger.error("[repair] msg %s не вдалось і після паузи: %s", msg.id, e2)
                    stats["failed"] += 1
            except Exception as e:
                logger.error("[repair] msg %s помилка: %s", msg.id, e)
                stats["failed"] += 1
            await asyncio.sleep(delay)

    logger.info("[repair] «%s» завершено: %s", _chat_title(chat, chat_id), stats)
    return stats


def _watermarks(db_path: str, chat_ids: set[int]) -> dict[int, dict]:
    """Скільки архів уже знає по кожному чату — ВИВОДИМО з даних, а не зберігаємо.

    Колонка tg_monitored_chats.last_message_id лишається мертвою свідомо: окремий
    курсор довелось би комусь писати (а писар у БД лише app.py), він розʼїхався б
    з реальністю при кожній частковій невдачі, і його все одно не можна скинути
    на виході — app.py гасить слухача через terminate(), тобто без atexit і flush.
    MAX(tg_date) завжди правдивий за побудовою.

    Дата, а не min_id: id послідовні лише в супергрупах (-100…), а це 4 чати з 19.

    Разом з датою віддаємо id того самого повідомлення: offset_date у Telethon
    ВКЛЮЧНИЙ, тож без нього догонка щоразу перетягує вже відоме останнє
    повідомлення — дедуп його відкидає, але медіафайл встигає скачатись ще раз
    (на живих даних той самий PDF лежав у telegram_media у семи копіях).
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
        logger.warning("[catchup] не зміг прочитати watermark: %s", e)
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
        logger.debug("[catchup] не зміг зберегти стан: %s", e)


async def _catchup(client, cfg, monitored: set[int]) -> dict:
    """Догрузити те, що прийшло, поки слухач не працював (Волна 2).

    Слухач — дочірній процес app.py, тож «слухач 24/7» неможливий без app.py 24/7:
    вимкнена на ніч машина = мовчазна дірка. На живих даних це 30 втрачених
    буднів за 8 місяців, зі зміщенням у вечір і вихідні.

    Сутності беремо з ОДНОГО проходу iter_dialogs, а не get_entity на кожен чат:
    резолв пірів — найбільш flood-небезпечна частина, 1-2 виклики замість 18.

    Дебаунс: app.py стартує десятки разів на день під час розробки
    (_maybe_launch_telegram_listener на кожен запуск), і без нього догонка
    гнала б 18 запитів щоразу.
    """
    from telethon.errors import FloodWaitError

    stats = {"chats": 0, "skipped_debounce": 0, "ingested": 0, "failed": 0, "failed_chats": []}
    if not cfg["catchup_enabled"]:
        logger.info("[catchup] вимкнено (TELEGRAM_CATCHUP=0)")
        return stats
    if not monitored:
        return stats

    marks = _watermarks(cfg["db_path"], monitored)
    state = _catchup_state_load(cfg["catchup_state"])
    now = datetime.now(timezone.utc)
    debounce = timedelta(minutes=cfg["catchup_debounce_min"])

    # Один прохід діалогів → і сутності, і фільтр «чат взагалі доступний».
    entities: dict[int, object] = {}
    try:
        async for d in client.iter_dialogs(limit=cfg["dialog_limit"]):
            if d.id in monitored:
                entities[d.id] = d.entity
    except Exception as e:
        logger.warning("[catchup] не зміг перелічити діалоги: %s", e)
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
                    # Чат без жодного запису — це первинна догрузка історії, свідоме
                    # рішення власника (обсяг невідомий), а не автоматичний ремонт.
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
                        logger.warning("[catchup] flood-wait %ss — чекаю", e.seconds)
                        await asyncio.sleep(e.seconds + 1)
                        continue
                    except Exception as e:
                        logger.warning("[catchup] вибірка для %s обірвалась: %s", chat_id, e)
                        break

                    # offset_date включний → перше повідомлення і є наш watermark.
                    # Пропускаємо ДО завантаження медіа, інакше кожен старт створює
                    # ще одну копію того самого файлу в telegram_media.
                    if known_id is not None and getattr(msg, "id", None) == known_id:
                        continue

                    try:
                        if await _ingest_message(entity, msg, cfg):
                            got += 1
                    except Exception as e:
                        logger.error("[catchup] msg %s помилка: %s", getattr(msg, "id", "?"), e)
                        stats["failed"] += 1
                    await asyncio.sleep(delay)

                state[str(chat_id)] = now.isoformat()
                stats["chats"] += 1
                stats["ingested"] += got
                if got:
                    logger.info("[catchup] «%s»: догружено %d з %s",
                                _chat_title(entity, chat_id), got, mark["date"][:16])
            except Exception as e:
                # Межа одного чату: збій тут коштує рівно цього чату — решта
                # чатів і збереження стану мають відбутись попри цю помилку.
                stats["failed_chats"].append(chat_id)
                try:
                    title = _chat_title(entity, chat_id)
                except Exception:
                    title = str(chat_id)
                logger.error("[catchup] «%s» (id=%s) обвалив чат: %s",
                             title, chat_id, e, exc_info=True)
    finally:
        _catchup_state_save(cfg["catchup_state"], state)

    logger.info("[catchup] завершено: %s", stats)
    return stats


def _start_control_api(client, loop, cfg, get_monitored):
    """ThreadingHTTPServer у daemon-потоці; хендлери шедулять корутини на
    Telethon-loop через run_coroutine_threadsafe (бо клієнт живе в loop)."""
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
            pass  # не засмічуємо консоль слухача HTTP-логами

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
                    return self._send(400, {"error": "потрібні chat_id та непорожній ids"})
                # Синхронно (на відміну від backfill): пачка обмежена викликачем,
                # і саме стата ремонту — те, заради чого його запускають.
                try:
                    res = _run_coro(_ctl_repair(client, int(body["chat_id"]),
                                                [int(i) for i in ids], cfg), 900)
                    return self._send(200, res)
                except Exception as e:
                    logger.error("[repair] впав: %s", e, exc_info=True)
                    return self._send(500, {"error": "repair failed"})

            if self.path.split("?")[0] == "/backfill":
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads((self.rfile.read(length).decode("utf-8") or "{}"))
                except Exception:
                    body = {}
                if body.get("chat_id") is None:
                    return self._send(400, {"error": "chat_id обов'язковий"})
                chat_id = int(body["chat_id"])
                limit = int(body.get("limit", 200))
                # Fire-and-forget: backfill довгий, не тримаємо HTTP-зʼєднання.
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
    logger.info("Старт. Моніторю %d чат(ів): %s", len(monitored), sorted(monitored) or "—")
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
                logger.info("Оновлено набір чатів (+%s / -%s) → %d активних",
                            sorted(added) or "—", sorted(removed) or "—", len(monitored))

    @client.on(events.MessageEdited())
    async def on_edited(event):
        """Правка в чаті → оновити текст у архіві (Волна 4)."""
        try:
            if event.chat_id not in monitored:
                return
            msg = event.message
            text = (msg.message or "").strip()
            if not text:
                return                       # правка медіа без підпису — нічого оновлювати
            edit_date = getattr(msg, "edit_date", None)
            await asyncio.to_thread(_post_json, cfg["edited_url"], {
                "chat_id": event.chat_id, "message_id": msg.id, "text": text,
                "edit_date": edit_date.isoformat() if edit_date else None,
            })
            logger.info("→ правка в «%s» (msg=%s)", _chat_title(await event.get_chat(),
                                                                event.chat_id), msg.id)
        except Exception as e:
            logger.error("Помилка обробки правки: %s", e, exc_info=True)

    @client.on(events.MessageDeleted())
    async def on_deleted(event):
        """Видалення в чаті → мʼяко прибрати з архіву (Волна 4).

        В особистих чатах Telegram не завжди повідомляє, ДЕ саме видалено
        (event.chat_id None) — тоді нічого не робимо: гадати по одному лише
        message_id небезпечно, id у різних чатах перетинаються."""
        try:
            if event.chat_id is None or event.chat_id not in monitored:
                return
            ids = list(event.deleted_ids or [])
            if not ids:
                return
            await asyncio.to_thread(_post_json, cfg["deleted_url"],
                                    {"chat_id": event.chat_id, "message_ids": ids})
        except Exception as e:
            logger.error("Помилка обробки видалення: %s", e, exc_info=True)

    @client.on(events.NewMessage())
    async def handler(event):
        try:
            if event.chat_id not in monitored:
                return
            chat = await event.get_chat()
            kind = await _ingest_message(chat, event.message, cfg)
            if kind:
                logger.info("→ %s з «%s» (msg=%s)", kind,
                            _chat_title(chat, event.chat_id), event.message.id)
        except Exception as e:
            logger.error("Помилка обробки повідомлення: %s", e, exc_info=True)

    async with client:
        asyncio.create_task(refresh_loop())
        # Control-API для Flask: lambda бачить актуальний `monitored` (refresh_loop
        # переприсвоює його у цьому ж scope).
        try:
            _start_control_api(client, asyncio.get_running_loop(), cfg, lambda: monitored)
        except OSError as e:
            logger.warning("Control-API не піднявся (порт зайнятий?): %s", e)
        me = await client.get_me()
        logger.info("Залогінено як %s (id=%s). Слухаю… Ctrl+C для зупинки.",
                    _sender_name(me), me.id)

        # Спочатку віддаємо те, що вже ловили, але не доставили (app.py був
        # недоступний), потім догоняємо те, що пропустили, поки не працювали.
        # Обидва — у фоні: слухач має почати приймати НОВІ повідомлення одразу,
        # не чекаючи кінця догонки.
        async def _startup_recovery():
            await asyncio.to_thread(_replay_deadletter, cfg)
            try:
                await _catchup(client, cfg, set(monitored))
            except Exception as e:
                logger.error("[catchup] впав: %s", e, exc_info=True)

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
        print(f"{'EN':<3} {'chat_id':>15}  {'тип':<9} назва")
        print("-" * 70)
        async for d in client.iter_dialogs(limit=limit):
            ent = d.entity
            ctype = ("channel" if getattr(ent, "broadcast", False) else
                     "group" if (d.is_group or getattr(ent, "megagroup", False)) else
                     "user" if d.is_user else "chat")
            mark = "[x]" if d.id in enabled else "[ ]"
            print(f"{mark} {d.id:>15}  {ctype:<9} {d.name}")
    print("\nУвімкнути: telegram_listener.py enable <chat_id>")


def _chat_type_of(ent, dialog=None) -> str:
    if getattr(ent, "broadcast", False):
        return "channel"
    if getattr(ent, "megagroup", False) or (dialog and dialog.is_group):
        return "group"
    if getattr(ent, "first_name", None) is not None:
        return "user"
    return "chat"


async def cmd_toggle(cfg: dict, chat_id: int, enable: bool):
    """Увімкнути/вимкнути моніторинг чату. Резолвить назву/тип через Telethon
    (для UX), сам запис у tg_monitored_chats — ЧЕРЕЗ Flask (T2.6:
    POST /api/telegram/chats/toggle), а не прямим SQL з цього процесу. app.py
    лишається єдиним писарем у SQLite — інваріант, що його дотримується решта
    слухача (усі TG-повідомлення теж ідуть через /api/telegram/ingest).

    Деградація коли app.py недоступний: 3 спроби з паузою 2с (може ще
    піднімається), потім зрозуміла помилка в stderr + exit(1) — команда НЕ
    падає мовчки і НЕ намагається писати в БД напряму як фолбек."""
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
        logger.warning("Не зміг резолвити чат %s (продовжую без назви): %s", chat_id, e)

    payload = {"chat_id": chat_id, "enabled": bool(enable), "title": title,
               "username": username, "chat_type": ctype}

    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            res = await asyncio.to_thread(_post_json, cfg["chats_toggle_url"], payload)
            if not res.get("success"):
                raise RuntimeError(res.get("error") or "невідома помилка від app.py")
            print(f"{'Увімкнено' if enable else 'Вимкнено'}: {title or chat_id} ({chat_id})")
            return
        except Exception as e:
            last_err = e
            if attempt < 3:
                logger.warning(
                    "Спроба %d/3 запису чату %s у app.py не вдалась (%s) — повтор через 2с",
                    attempt, chat_id, e,
                )
                await asyncio.sleep(2)

    logger.error("Не вдалось зберегти чат %s: app.py недоступний (%s)", chat_id, last_err)
    print(
        f"ПОМИЛКА: не вдалось зберегти чат {chat_id} — app.py недоступний ({last_err}).\n"
        f"Переконайтесь що застосунок запущено (.venv/Scripts/python.exe app.py) і повторіть команду.",
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
        print("Немає налаштованих чатів. Спочатку: telegram_listener.py list")
        return
    print(f"{'EN':<3} {'chat_id':>15}  {'тип':<9} {'cat':>4}  назва")
    print("-" * 70)
    for r in rows:
        mark = "[x]" if r["enabled"] else "[ ]"
        cat = r["category_id"] if r["category_id"] is not None else "—"
        print(f"{mark} {r['chat_id']:>15}  {r['chat_type'] or '?':<9} {str(cat):>4}  {r['title'] or ''}")


def main() -> int:
    p = argparse.ArgumentParser(description="Telegram listener для Recall (Phase 17B)")
    sub = p.add_subparsers(dest="cmd")
    pl = sub.add_parser("list", help="показати чати акаунта (id + назва)")
    pl.add_argument("--limit", type=int, default=100)
    pe = sub.add_parser("enable", help="увімкнути чат для слухання")
    pe.add_argument("chat_id", type=int)
    pd = sub.add_parser("disable", help="вимкнути чат")
    pd.add_argument("chat_id", type=int)
    sub.add_parser("status", help="що зараз слухається")
    args = p.parse_args()

    cfg = _load_cfg()
    try:
        from telethon import TelegramClient  # noqa: F401
    except ImportError:
        logger.error("Telethon не встановлено: .venv\\Scripts\\python.exe -m pip install telethon")
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
            logger.info("Зупинено користувачем.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
