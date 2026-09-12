"""Recall MCP server — read-first міст до Recall по MCP.

Окремий процес (за зразком telegram_listener.py). Віддає Claude (Code / Desktop)
read-first ядро read-only інструментів (точна кількість — жива, з реєстру FastMCP,
див. ресурс `recall://about`): пошук/читання архіву, граф сутностей, задачі,
статистика, ask-archive/research та status-only знімки live-підсистем
(recording/copilot/telegram/jobs). **Гібрид:** частина тулзів читає персистентні
дані напряму з SQLite (`get_db_connection`/`app.services.research`, працюють БЕЗ
запущеного app.py); решта (пошук/RAG/статуси) — httpx-проксі на запущений
app.py (реюз готових ендпоінтів, нуль дублювання, і щоб не вантажити важкі
e5/torch у stdio-процес) — цим потрібен запущений app.py. Повний розподіл —
у ресурсі `recall://about` і docs/MCP_SETUP.md. **Стратегічне рішення власника
(02.07.2026): MCP — read-first.** Усі create/update/delete та керування залізом
(запис мікрофона, копілот, Telegram-чати) чи платні/довгі Claude-виклики
(enrich/polish/summarize/transcribe/ingest/backfill) прибрані з MCP — це робить
UI Recall. Нові write-тулзи свідомо не додаються. Деталі підключення/env —
docs/MCP_SETUP.md.

Транспорти:
  * stdio (default) — для Claude Desktop / Claude Code (клієнт сам спавнить процес,
    без мережі/ключа). Саме його реєструє `claude mcp add`;
  * http            — Streamable HTTP на localhost, гейт статичним ключем (Bearer).

Ключ: env RECALL_MCP_KEY (згенеруй: python -c "import secrets;print(secrets.token_urlsafe(32))").
У http-режимі без ключа сервер НЕ стартує (захист від випадкової публічності).

Запуск:
  .venv/Scripts/python.exe mcp_server.py                   # stdio (default)
  .venv/Scripts/python.exe mcp_server.py --transport http  # http://127.0.0.1:5060/mcp (треба RECALL_MCP_KEY)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Optional

import logging
import logging.handlers
# stdio-mcp_server спавниться клієнтом і живе коротко: НЕ тримаємо спільний
# whisper_ui.log відкритим (інакше блокуємо rollover у app.py → WinError 32 спам) і
# глушимо самі logging-помилки. Має стояти ДО імпорту app.* (там лінивий setup_logger).
os.environ.setdefault("RECALL_LOG_TO_FILE", "0")
logging.raiseExceptions = False

# T8.3 (Волна 4): цей процес досі НЕ мав жодного логера (лише raiseExceptions=False
# вище) — падіння без нагляду лишалось непоміченим. Додаємо ЛЕГКИЙ (stdlib-only,
# без app.core.logger.setup_logger і без важких torch/e5-імпортів — search_archive
# їх і далі вантажить ліниво) файловий лог у ВЛАСНИЙ файл mcp_server.log, окремий
# від app.py (whisper_ui.log) і telegram_listener.py (telegram_listener.log) —
# та сама причина, що й там: спільний файл з кількох процесів → rollover-конфлікт
# на Windows (WinError 32). КРИТИЧНО: жодного handler'а на stdout — stdio-транспорт
# MCP використовує stdout для JSON-RPC, будь-який лог туди ламає протокол. Пишемо
# лише у файл (stderr теж не займаємо, щоб не заважати клієнтському парсингу).
try:
    _MCP_LOG_PATH = Path(__file__).resolve().parent / "mcp_server.log"
    _mcp_file_handler = logging.handlers.RotatingFileHandler(
        _MCP_LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8", delay=True,
    )
    _mcp_file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [mcp_server pid=%(process)d] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.getLogger().addHandler(_mcp_file_handler)
    logging.getLogger().setLevel(logging.INFO)
    # Приглушуємо галасливі сторонні логери (той самий список, що app.py) —
    # інакше httpx-проксі-виклики роздують mcp_server.log per-request.
    for _noisy in ("httpx", "httpcore", "urllib3", "anthropic", "huggingface_hub", "filelock"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)
except OSError:
    # Не валимо stdio-сервер, якщо файл лога недоступний — MCP-протокол
    # важливіший за діагностику.
    pass

logger = logging.getLogger("mcp_server")

try:
    from dotenv import load_dotenv
    # Глобальний (user-scope) конектор стартує з будь-якого cwd — вантажимо .env
    # з каталогу самого скрипта, а не з поточної директорії.
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

from config import Config
from app.db.connection import get_db_connection

import httpx
from fastmcp import FastMCP

# Важкі ML-залежності (retrieval → numpy/torch для e5-ембеддингів) імпортуються
# ЛІНИВО всередині search_archive, щоб stdio-сервер стартував миттєво — інакше
# health-check Claude Code впирається в таймаут, поки вантажиться torch.

# --- Шляхи (резолвимо абсолютно — процес стартує з кореня репо) ----------------
BASE_DIR: Path = Config.BASE_DIR
DB_PATH: str = str(BASE_DIR / Config.DATABASE)
# Папки, де лежать оригінали — лише з них дозволяємо читати файли (anti-traversal).
_ALLOWED_DIRS = [
    (BASE_DIR / Config.DOCUMENTS_FOLDER).resolve(),
    (BASE_DIR / Config.UPLOAD_FOLDER).resolve(),
    (BASE_DIR / Config.YOUTUBE_FOLDER).resolve(),
    (BASE_DIR / getattr(Config, "TELEGRAM_MEDIA_DIR", "telegram_media")).resolve(),
]

_TEXT_DOC_TYPES = {"md", "txt", "csv"}


# --- HTTP-проксі до живого Flask (app.py) --------------------------------------
# Гібридна архітектура: читання persisted-даних і пошук — напряму з SQLite (нижче);
# усі ЗАПИСИ та live/AI-дії проксіюємо на запущений app.py — реюз готової валідації,
# джобів, ембеддингів. Localhost-only, без CORS-обмежень для server-to-server.
API_URL: str = os.environ.get("RECALL_API_URL", "http://127.0.0.1:5050").rstrip("/")
# Аварійний вимикач усіх записів (на читання не впливає). За замовчуванням — увімкнено.
_WRITE_DISABLED: bool = (
    os.environ.get("RECALL_MCP_READONLY", "").strip().lower() in ("1", "true", "yes", "on")
)


def _api(method: str, path: str, *, params: Optional[dict] = None,
         body: Optional[dict] = None, write: bool = False,
         timeout: float = 60.0) -> dict:
    """Виклик ендпоінта Recall (localhost:5050). Реюз готової логіки замість
    дублювання. write=True → зміна стану (блокується в read-only режимі).
    Ніколи не кидає — повертає {"error": ...}, якщо app.py не запущений / HTTP-збій,
    щоб інструмент деградував грейсфул, а не падав."""
    if write and _WRITE_DISABLED:
        return {"error": "MCP у read-only режимі (RECALL_MCP_READONLY встановлено). "
                         "Прибери цю змінну середовища, щоб дозволити записи."}
    url = f"{API_URL}{path}"
    try:
        resp = httpx.request(method, url, params=params, json=body, timeout=timeout)
    except httpx.ConnectError:
        return {"error": f"Recall (app.py) не запущений на {API_URL}. "
                         f"Запусти '.venv/Scripts/python.exe app.py' і повтори дію."}
    except httpx.TimeoutException:
        return {"error": f"Таймаут {timeout:g}s від {path} — операція могла бути надто довгою "
                         f"(для довгих джобів використовуй start+poll, а не блокуючий виклик)."}
    except httpx.HTTPError as e:
        return {"error": f"HTTP-помилка до {path}: {e}"}
    return _handle_resp(resp)


def _handle_resp(resp: "httpx.Response") -> dict:
    """Розбір відповіді: JSON або сирий текст; >=400 → проброс серверної помилки."""
    try:
        data = resp.json()
    except ValueError:
        return {"status_code": resp.status_code, "text": resp.text[:4000]}
    if resp.status_code >= 400:
        if isinstance(data, dict):
            data.setdefault("error", f"HTTP {resp.status_code}")
            data["status_code"] = resp.status_code
            return data
        return {"error": f"HTTP {resp.status_code}", "body": data}
    return data


def _sse_collect(path: str, body: Optional[dict] = None,
                 timeout: float = 600.0) -> dict:
    """Споживає SSE-ендпоінт (формат event:/data:) до кінця і повертає
    {'events':[...], 'done': <payload>|None, 'error': <msg>|None}. Для довгих
    стрімів (research/summary), бо MCP сам не стрімить — дочитуємо на боці проксі."""
    url = f"{API_URL}{path}"
    events: list = []
    cur_event: Optional[str] = None
    data_lines: list = []
    try:
        with httpx.stream("POST", url, json=body, timeout=timeout) as resp:
            if resp.status_code >= 400:
                resp.read()
                return {"error": f"HTTP {resp.status_code}", "events": []}
            for line in resp.iter_lines():
                if line == "":
                    if cur_event is not None or data_lines:
                        raw = "\n".join(data_lines)
                        try:
                            payload = json.loads(raw) if raw else {}
                        except ValueError:
                            payload = {"raw": raw}
                        events.append({"event": cur_event, "data": payload})
                    cur_event, data_lines = None, []
                elif line.startswith("event:"):
                    cur_event = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
    except httpx.ConnectError:
        return {"error": f"Recall (app.py) не запущений на {API_URL}.", "events": []}
    except httpx.HTTPError as e:
        return {"error": f"SSE-помилка до {path}: {e}", "events": []}
    done = next((e["data"] for e in events if e["event"] == "done"), None)
    err = next((e["data"].get("error") for e in events
                if e["event"] == "error" and isinstance(e["data"], dict)), None)
    return {"events": events, "done": done, "error": err}


# --- Авторизація ---------------------------------------------------------------
def _build_auth():
    """StaticTokenVerifier на один ключ власника. None → без авторизації (stdio)."""
    key = os.environ.get("RECALL_MCP_KEY")
    if not key:
        return None
    StaticTokenVerifier = None
    for path in ("fastmcp.server.auth.providers.static",
                 "fastmcp.server.auth.providers.jwt"):
        try:
            mod = __import__(path, fromlist=["StaticTokenVerifier"])
            StaticTokenVerifier = getattr(mod, "StaticTokenVerifier")
            break
        except (ImportError, AttributeError):
            continue
    if StaticTokenVerifier is None:
        raise RuntimeError("FastMCP StaticTokenVerifier недоступний — онови fastmcp")
    return StaticTokenVerifier(
        tokens={key: {"client_id": "recall-owner", "scopes": ["recall"]}},
        required_scopes=["recall"],
    )


# --- DEBUG: опційний лог викликів тулзів (вмикається env RECALL_MCP_DEBUG_LOG) --
# 1/true → logs/mcp_calls.log; або явний шлях. За замовч. ВИМКНЕНО (нуль-оверхед).
# Тимчасовий інструмент відладки MCP — пише рядок на кожен tools/call + handshake.
import time as _time
from fastmcp.server.middleware.middleware import Middleware as _Middleware


def _debug_log_path() -> Optional[str]:
    raw = os.environ.get("RECALL_MCP_DEBUG_LOG", "").strip()
    if not raw:
        return None
    if raw.lower() in ("1", "true", "yes", "on"):
        return str(BASE_DIR / "logs" / "mcp_calls.log")
    return raw


class _CallLogMiddleware(_Middleware):
    """Логує кожен виклик тула: час, ім'я, аргументи (обрізані), ok/exc, мс."""

    def __init__(self, path: str) -> None:
        self._path = path
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        except OSError:
            pass

    def _w(self, line: str) -> None:
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    async def on_initialize(self, context, call_next):
        self._w(f"{_time.strftime('%H:%M:%S')} ── initialize (handshake)")
        return await call_next(context)

    async def on_call_tool(self, context, call_next):
        name = getattr(context.message, "name", "?")
        try:
            sargs = json.dumps(getattr(context.message, "arguments", None),
                               ensure_ascii=False)[:600]
        except Exception:
            sargs = str(getattr(context.message, "arguments", None))[:600]
        # START — пишемо ДО виклику, щоб зависання було видно (START без пари OK/EXC).
        self._w(f"{_time.strftime('%H:%M:%S')} →   START {name} args={sargs}")
        t0 = _time.perf_counter()
        try:
            res = await call_next(context)
            ms = (_time.perf_counter() - t0) * 1000
            self._w(f"{_time.strftime('%H:%M:%S')} OK  {name} {ms:.0f}ms")
            return res
        except Exception as e:
            ms = (_time.perf_counter() - t0) * 1000
            self._w(f"{_time.strftime('%H:%M:%S')} EXC {name} {ms:.0f}ms :: {type(e).__name__}: {e}")
            raise


_dbg_path = _debug_log_path()
_mw = [_CallLogMiddleware(_dbg_path)] if _dbg_path else None
mcp = FastMCP(name="Recall Archive", auth=_build_auth(), middleware=_mw)


# --- helpers -------------------------------------------------------------------
def _row(r) -> dict:
    return {k: r[k] for k in r.keys()}


def _name_key(name: Optional[str]) -> str:
    """Ключ згортання імені сутності: «AcmeCorp» і «Acmecorp» — одне.

    Рахуємо в Python, а не в SQL: SQLite `LOWER` не згортає кирилицю, тож
    порівняння в запиті мовчки вважало б «Алгоритм» і «алгоритм» різними
    (той самий граблі, що й у фільтрі власника задач).

    Дужковий хвіст НЕ зрізаємо, хоч це й здається природним («Промінвест (хаб)»
    → «Промінвест»). У людей дужка — це РОЗРІЗНЮВАЧ, а не шум: у графі живуть
    «Юлія» (213 зустрічей), «Юлія (Willow)» і «Юлія (Ділова англійська)» — різні
    жінки. Зрізання склеювало їх в одну і додавало до досьє «разом 218
    зустрічей» — рівно та брехня, проти якої весь цей механізм. Ціна відмови:
    «Промінвест (хаб)» лишається окремою від «Промінвест». Так і має бути — це
    здогад, а не факт, а тезки тут показуються як факт.

    Апостроф беремо той, який у даних справді є: 9 імен пишуться через U+02BC
    (ʼ) і жодного через U+2019 (’). Крапку згортаємо — «Aegis.UA» і
    «Aegis UA» це одне.
    """
    s = (name or "").casefold().strip()
    return re.sub(r"[\s\-_.'`ʼ’«»\"]+", "", s)


def _same_name_rows(conn, entity_id: int, name: str) -> list[dict]:
    """Інші рядки графа з тим самим імʼям — включно з ІНШИМ типом.

    Збагачення заводить сутність окремо в кожному типі, тому один проєкт живе
    як `Acmecorp[project]` і `AcmeCorp[org]`: досьє однієї строки показує
    частину історії і виглядає як уся (у цій парі — 114 і 78 звʼязків при 190
    у групі). Таких груп на архіві понад сотня.

    Свідомо НЕ зливаємо: merge переносить написання в аліаси і вже одного разу
    зламав споживачів (`memory/entity-merge-moves-names-to-aliases`). Тулз має
    не переписувати граф, а не брехати про нього — тому просто показуємо решту
    групи і суму. Кандидатів на злиття дає `entity_dedup` (CLI), і він цей клас
    не бачить за побудовою: порівнює лише в межах одного типу.
    """
    key = _name_key(name)
    if not key:
        return []
    rows = conn.execute(
        "SELECT id, type, canonical_name, mention_count, meeting_count FROM entities "
        "WHERE id <> ?", (int(entity_id),)).fetchall()
    return [_row(r) for r in rows if _name_key(r["canonical_name"]) == key]


def _safe_original_path(file_path: Optional[str]) -> Optional[Path]:
    """Резолв шляху до оригіналу з anti-traversal: лише всередині дозволених папок."""
    if not file_path:
        return None
    p = Path(file_path)
    if not p.is_absolute():
        p = BASE_DIR / p
    try:
        p = p.resolve()
    except OSError:
        return None
    for base in _ALLOWED_DIRS:
        try:
            p.relative_to(base)
            return p
        except ValueError:
            continue
    return None


# ============================== TOOLS =========================================
# Ядро читання архіву — direct-DB (get_db_connection/app.services.research),
# працює навіть без запущеного app.py.
# ============================================================
# Коментарі власника (Волна 5)
# ============================================================
#
# Read-only, як і решта тулзів: рішення «MCP → read-first» (02.07.2026) лишається
# чинним, і write-тулза `add_comment` свідомо НЕ додається — коментар пишеться
# з інтерфейсу Recall, а Claude його лише читає.
#
# Прямий SQL, а не проксі на app.py: запит дешевий (індекс по target), важких
# моделей не потребує, а stdio-сервер не має піднімати torch (урок
# mcp-stdio-no-heavy-models).

_COMMENT_COLS = ("id", "kind", "body", "pinned", "anchor_time", "author",
                 "source", "created_at", "target_type", "target_id", "target_key")


def _comments_for(conn, target_type: str, target_id) -> list[dict]:
    """Коментарі однієї картки. Порожньо на старій БД без міграції v37 —
    відсутність шару не має валити читання транскрипту."""
    col = "target_key" if target_type == "recording_session" else "target_id"
    try:
        rows = conn.execute(
            f"SELECT {', '.join(_COMMENT_COLS)} FROM comments "
            f"WHERE target_type = ? AND {col} = ? AND deleted_at IS NULL "
            f"ORDER BY pinned DESC, COALESCE(anchor_time, 1e18), created_at",
            (target_type, target_id)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [_row(r) for r in rows]


@mcp.tool
def list_comments(target_type: Optional[str] = None, target_id: Optional[str] = None,
                  kind: Optional[str] = None, days: Optional[int] = None,
                  limit: int = 50) -> list:
    """Коментарі власника — його власні уточнення, виправлення й акценти щодо
    записів архіву.

    Це найточніший шар архіву: транскрипт фіксує, що ПРОЗВУЧАЛО (з помилками
    розпізнавання, обмовками, недомовками), а коментар — що власник свідомо
    ЗАФІКСУВАВ як правду. Тому при конфлікті вір коментарю, а не транскрипту.

    Args:
        target_type: звузити до типу картки — transcription | audio_download |
            action_item | entity | speaker | category | tg_thread |
            recording_session.
        target_id: конкретна картка (для recording_session — рядок `rec_...`).
            Разом із target_type.
        kind: correction (виправляє сказане) | decision | note | context |
            question. `correction` — найважливіший зріз: це місця, де запис
            вводить в оману.
        days: лише за останні N днів.
        limit: скільки повернути (за замовчуванням 50).

    Returns: список коментарів із провенансом — до якої картки, якого типу,
    коли написано, і `target_name` (назва запису), щоб не робити ще один виклик.
    """
    sql = (f"SELECT {', '.join('c.' + col for col in _COMMENT_COLS)}, "
           f"t.source_name AS target_name "
           f"FROM comments c "
           f"LEFT JOIN transcriptions t ON c.target_type = 'transcription' "
           f"AND t.id = c.target_id "
           f"WHERE c.deleted_at IS NULL")
    params: list = []
    if target_type:
        sql += " AND c.target_type = ?"
        params.append(target_type)
        if target_id is not None:
            col = "c.target_key" if target_type == "recording_session" else "c.target_id"
            sql += f" AND {col} = ?"
            params.append(target_id)
    if kind:
        sql += " AND c.kind = ?"
        params.append(kind)
    if days:
        sql += " AND c.created_at >= datetime('now', ?)"
        params.append(f"-{int(days)} days")
    # id як тай-брейкер: created_at має роздільність в одну секунду, і без
    # нього рівні за часом поверталися б у порядку rowid — тобто зворотно.
    sql += " ORDER BY c.created_at DESC, c.id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    with get_db_connection(DB_PATH) as conn:
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []       # стара БД без міграції v37
    return [_row(r) for r in rows]


@mcp.tool
def search_archive(query: str, top_k: int = 8,
                   category_id: Optional[int] = None, explain: bool = False) -> dict:
    """Гібридний (вектор e5 + FTS5 BM25) пошук по ВСЬОМУ архіву дзвінків/документів/
    Telegram. Повертає чанки з провенансом (джерело, дата, спікер, таймкод/сторінка,
    transcription_id, chunk_id) і релевантністю. category_id — звузити до напрямку.
    Це основний інструмент: знайди тут, далі тягни деталі через get_transcript.

    У видачі можуть бути чанки з `source_type='comment'` — це коментарі власника
    про запис (уточнення/виправлення/акценти), а не репліки з розмови. Вони мають
    ВИЩИЙ пріоритет за транскрипт: при суперечності правильний коментар, і
    `comment_kind='correction'` прямо скасовує відповідне місце запису. Поле
    `target_label` каже, до чого саме написано коментар. explain=True — додати
    розбір релевантності (bm25/dense/rrf/recency/rerank, джерело) до кожного чанка."""
    # Проксі на app.py (тепла e5 вже в його пам'яті) — НЕ вантажити torch/e5 у stdio-
    # процес MCP-сервера: важка нативна ініціалізація в stdio псує JSON-RPC (запис у
    # stdout) і конкурує за GPU з app.py (faster-whisper/Qwen) → зависання назавжди.
    # Той самий шлях, що ask_archive/search_history → GET /api/memory/search.
    params: dict = {"q": query, "k": int(top_k)}
    if category_id is not None:
        params["category_id"] = int(category_id)
    if explain:
        params["explain"] = True
    res = _api("GET", "/api/memory/search", params=params)
    chunks = res.get("chunks") or []
    return {
        "query": res.get("query", query),
        "vector_available": res.get("vector_available"),
        "count": len(chunks),
        "chunks": chunks,
    }


@mcp.tool
def get_transcript(transcription_id: int, include_segments: bool = False) -> dict:
    """Повний транскрипт/текст запису або документа + метадані (джерело, мова, дата,
    напрямок, summary). include_segments=True — додати посегментну розбивку з
    таймкодами/спікерами (для аудіо).

    Для Telegram додається `thread` — уся нитка розмови, до якої належить це
    повідомлення (Волна 4.5). Окрема репліка в переписці часто нечитабельна
    («Ок», «а скільки там?»), тож без нитки відповідь довелось би вгадувати.

    `comments` — коментарі власника до цього запису, якщо вони є. Це НЕ репліки
    з розмови, а речення, написані про неї вже після: уточнення, виправлення,
    акценти. Вони мають ВИЩИЙ пріоритет за сам транскрипт — якщо коментар
    суперечить тексту запису, правильний коментар. Тип `correction` прямо
    скасовує відповідне місце транскрипту."""
    with get_db_connection(DB_PATH) as conn:
        r = conn.execute(
            "SELECT id, created_at, source_type, source_name, language, model_used, "
            "category_id, meeting_date, doc_type, original_filename, page_count, "
            "summary_json, transcript_text, polished_text, segments, "
            # Провенанс Telegram: без (tg_chat_id, tg_message_id) знахідку не
            # відкрити в треді, без tg_sender не сказати ХТО це написав.
            "tg_chat_id, tg_chat_title, tg_sender, tg_message_id, tg_date, tg_link, "
            "tg_reply_to, tg_thread_id, tg_thread_src "
            "FROM transcriptions WHERE id = ?", (int(transcription_id),)).fetchone()
        if not r:
            return {"error": f"transcription {transcription_id} не знайдено"}
        # Нитка (Волна 4.5): окреме TG-повідомлення поза розмовою часто
        # нечитабельне («Ок», «а скільки там?»). Віддаємо нитку цілком, щоб не
        # довелось вгадувати контекст за сусідніми id.
        thread = None
        if r["tg_thread_id"]:
            th = conn.execute("SELECT id, label, msg_count, first_date, last_date, status "
                              "FROM tg_threads WHERE id = ?", (r["tg_thread_id"],)).fetchone()
            msgs = conn.execute(
                "SELECT id, tg_message_id, tg_date, tg_sender, transcript_text "
                "FROM transcriptions WHERE tg_thread_id = ? AND deleted_at IS NULL "
                "ORDER BY tg_date LIMIT 60", (r["tg_thread_id"],)).fetchall()
            if th and len(msgs) > 1:
                thread = {**_row(th), "messages": [_row(m) for m in msgs]}
        comments = _comments_for(conn, "transcription", int(transcription_id))
    d = _row(r)
    if thread:
        d["thread"] = thread
    if comments:
        d["comments"] = comments
    if d.get("summary_json"):
        try:
            d["summary"] = json.loads(d.pop("summary_json"))
        except (ValueError, TypeError):
            d.pop("summary_json", None)
    segs = d.pop("segments", None)
    if include_segments and segs:
        try:
            d["segments"] = json.loads(segs)
        except (ValueError, TypeError):
            pass
    return d


@mcp.tool
def get_original_file(transcription_id: int, max_chars: int = 20000) -> dict:
    """Метадані оригіналу файлу запису/документа + (для текстових: md/txt/csv) вміст
    оригіналу з диска. Для бінарних (pdf/docx/аудіо) повертає шлях/тип/розмір —
    розпарсений текст уже доступний через get_transcript."""
    with get_db_connection(DB_PATH) as conn:
        r = conn.execute(
            "SELECT id, source_type, doc_type, original_filename, file_path, byte_size "
            "FROM transcriptions WHERE id = ?", (int(transcription_id),)).fetchone()
    if not r:
        return {"error": f"transcription {transcription_id} не знайдено"}
    d = _row(r)
    p = _safe_original_path(d.get("file_path"))
    d["original_available"] = bool(p and p.exists())
    if p and p.exists():
        d["resolved_path"] = str(p)
        if (d.get("doc_type") or "").lower() in _TEXT_DOC_TYPES:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                d["content"] = text[:int(max_chars)]
                d["content_truncated"] = len(text) > int(max_chars)
            except OSError as e:
                d["read_error"] = str(e)
    return d


@mcp.tool
def list_recent(limit: int = 20, source_type: Optional[str] = None,
                category_id: Optional[int] = None) -> list:
    """Останні записи архіву (id, дата, тип джерела, назва, мова, напрямок, розмір
    тексту). source_type: recording|youtube|document|telegram|file. Для огляду
    «що є свіжого» перед точковим пошуком."""
    sql = ("SELECT id, created_at, source_type, source_name, language, category_id, "
           "doc_type, meeting_date, tg_chat_id, tg_chat_title, tg_sender, tg_message_id, "
           "length(transcript_text) AS text_len FROM transcriptions")
    where, params = [], []
    if source_type:
        where.append("source_type = ?"); params.append(source_type)
    if category_id is not None:
        where.append("category_id = ?"); params.append(int(category_id))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"; params.append(min(int(limit), 200))
    with get_db_connection(DB_PATH) as conn:
        return [_row(r) for r in conn.execute(sql, params)]


@mcp.tool
def grep_archive(pattern: str, regex: bool = False, ignore_case: bool = True,
                 context: int = 1, limit: int = 20, source_type: Optional[str] = None,
                 transcription_id: Optional[int] = None, days: Optional[int] = None) -> dict:
    """Буквальний або regex-пошук ТОЧНОГО РЯДКА по чанках архіву, з ±context сусідніми
    чанками. Обирай ЦЕЙ інструмент, а не `search_archive`, коли треба знайти РІВНО
    ЦЕЙ РЯДОК: ID запису, суму («1 200 000»), @нік, номер договору, точну назву —
    усе, що FTS5-токенізація і семантичний ембединг розбивають на частини або
    ховають за близьким за змістом, але неточним збігом. `search_archive` —
    навпаки, коли треба знайти ЗА ЗМІСТОМ, а не за точним написанням.

    Порядок видачі — за датою зустрічі, НЕ за релевантністю (тут немає ні
    вектора, ні BM25, ні reranker); рядки без дати — останні. `truncated=True`
    має три різні причини (`truncated_reason`), і рецепт різний:
    - `"limit"` — весь архів переглянуто, збігів більше, ніж `limit`
      (`match_total` каже скільки саме); підніми `limit` або звузь фільтрами
      (`source_type`/`transcription_id`/`days`), щоб побачити решту.
    - `"max_scan"` або `"deadline"` — скан зупинився ДОСТРОКОВО, не дійшовши
      кінця таблиці; підняти `limit` НІЧОГО не дасть, бо збіги за межею
      переглянутого просто не переглядались. `match_total` тоді — кількість
      серед переглянутого, не в усьому архіві. Відповідь несе
      `arbitrary_scan_caveat`: без `ORDER BY` у SQL переглянутий префікс —
      ДОВІЛЬНИЙ щодо дати, а не гарантовано найновіші записи. Дієва порада тут
      не `limit` (у тулзи немає параметра `max_scan`, аби розширити скан) —
      звузь `days`/`source_type`/`transcription_id`, щоб переглянутий префікс
      покривав менший, точніший зріз архіву.
    Обмеження: пошук іде по `chunks`, не по повному тексту транскрипції —
    рядок, розрізаний швом чанкування навпіл, не знайдеться
    (`chunk_boundary_caveat` у відповіді).
    Автономний: працює без запущеного app.py (пряме читання SQLite)."""
    from app.services.archive_grep import grep
    try:
        return grep(DB_PATH, pattern, regex=regex, ignore_case=ignore_case,
                    context=context, limit=limit, source_type=source_type,
                    transcription_id=transcription_id, days=days)
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool
def list_categories() -> list:
    """Напрямки/категорії архіву (Фонд, Особисте, AI, …) + кількість записів — для
    звуження пошуку через category_id."""
    with get_db_connection(DB_PATH) as conn:
        return [_row(r) for r in conn.execute(
            "SELECT c.id, c.name, c.slug, "
            "(SELECT COUNT(*) FROM transcriptions t WHERE t.category_id=c.id) AS items "
            "FROM categories c ORDER BY c.sort_order, c.name")]


@mcp.tool
def list_entities(type: Optional[str] = None, query: Optional[str] = None,
                  limit: int = 50) -> list:
    """Сутності графа пам'яті (people/projects/orgs/topics) з кількістю згадок/
    зустрічей. type — person|project|org|topic; query — підрядок назви/псевдоніма.

    `same_name_ids` на рядку означає, що це імʼя заведене в графі ще раз — майже
    завжди під іншим типом («Acmecorp» як project і як org). Числа рядка тоді
    описують лише його частину історії; підсумок по групі дає `get_entity`
    (він рахує по звʼязках, бо колонки-агрегати відстають). Фільтр `type` ці
    рядки РОЗДІЛЯЄ: `type='org'` віддасть рядок «AcmeCorp» із 32 зустрічами,
    тоді як уся група має 190."""
    sql = ("SELECT id, type, canonical_name, role, mention_count, meeting_count "
           "FROM entities")
    where, params = [], []
    if type:
        where.append("type = ?"); params.append(type)
    if query:
        where.append("(normalized_name LIKE ? OR id IN "
                     "(SELECT entity_id FROM entity_aliases WHERE normalized_alias LIKE ?))")
        like = f"%{query.lower()}%"; params += [like, like]
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY mention_count DESC LIMIT ?"; params.append(min(int(limit), 200))
    with get_db_connection(DB_PATH) as conn:
        out = [_row(r) for r in conn.execute(sql, params)]
        # Один прохід по графу (≈5 тис. рядків) замість запиту на кожен рядок.
        by_key: dict[str, list[int]] = {}
        for r in conn.execute("SELECT id, canonical_name FROM entities"):
            k = _name_key(r["canonical_name"])
            if k:
                by_key.setdefault(k, []).append(r["id"])
    for d in out:
        twins = [i for i in by_key.get(_name_key(d.get("canonical_name")), [])
                 if i != d.get("id")]
        if twins:
            d["same_name_ids"] = twins
    return out


@mcp.tool
def get_entity(entity_id: int) -> dict:
    """Досьє сутності: псевдоніми, зустрічі (де згадувалась, з salience) і пов'язані
    задачі (action items)."""
    with get_db_connection(DB_PATH) as conn:
        e = conn.execute("SELECT * FROM entities WHERE id = ?", (int(entity_id),)).fetchone()
        if not e:
            return {"error": f"entity {entity_id} не знайдено"}
        d = _row(e)
        d["aliases"] = [r["alias"] for r in conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = ?", (int(entity_id),))]
        d["meetings"] = [_row(r) for r in conn.execute(
            "SELECT me.transcription_id, t.source_name, t.meeting_date, me.salience, "
            "me.role_in_meeting FROM meeting_entities me "
            "JOIN transcriptions t ON t.id = me.transcription_id "
            "WHERE me.entity_id = ? ORDER BY me.salience DESC LIMIT 50", (int(entity_id),))]
        d["action_items"] = [_row(r) for r in conn.execute(
            "SELECT id, task, due, status, transcription_id FROM action_items "
            "WHERE owner_entity_id = ? ORDER BY id DESC LIMIT 50", (int(entity_id),))]
        same = _same_name_rows(conn, int(entity_id), e["canonical_name"])
        if same:
            # Підсумок рахуємо по звʼязках, а не складанням колонок `entities`.
            # Дві причини: рядки ділять спільні зустрічі (сума їх задвоїла б), а
            # самі агрегати протухають — у «AcmeCorp» колонка каже 32 при 78
            # реальних звʼязках, тож складання давало 134 там, де насправді 190.
            ids = [int(entity_id)] + [r["id"] for r in same]
            ph = ",".join("?" * len(ids))
            # Згортаємо спочатку по зустрічі, і лише потім підсумовуємо. Проста
            # SUM задвоювала б: коли один запис звʼязаний і з project-рядком, і
            # з org-рядком того самого імені, ті самі згадки в тексті рахувались
            # би двічі (на парі Acmecorp — 4 згадки з 1271). Беремо MAX по
            # зустрічі: скільки разів імʼя там прозвучало, а не скільки рядків
            # графа на нього дивляться.
            agg = conn.execute(
                f"SELECT COUNT(*) AS meetings, COALESCE(SUM(m), 0) AS mentions FROM ("
                f"  SELECT transcription_id, MAX(mention_count) AS m "
                f"  FROM meeting_entities WHERE entity_id IN ({ph}) "
                f"  GROUP BY transcription_id)", ids).fetchone()
            d["same_name_entities"] = same
            d["totals_across_same_name"] = {
                "entities": len(ids),
                "meeting_count": agg["meetings"],
                "mention_count": agg["mentions"],
                "note": ("те саме імʼя заведено в графі кілька разів (частіше — під різними "
                         "типами); числа цього досьє стосуються ЛИШЕ цього рядка, а тут — "
                         "порахований по звʼязках підсумок групи (спільні зустрічі не "
                         "задвоєні)"),
            }
    return d


@mcp.tool
def list_action_items(window: str = "this_week", owner: Optional[str] = None,
                      category_id: Optional[int] = None, status: str = "open",
                      limit: int = 50) -> dict:
    """Задачі/домовленості з дзвінків І З ПЕРЕПИСКИ за ЧАСОВИМ ВІКНОМ і виконавцем.

    window: this_week | next_week | overdue | soon (14 днів) | no_date | all.
    `this_week` — це «ще встигаємо»: рахується від СЬОГОДНІ до кінця тижня, а не
    від понеділка, тож вікна не перетинаються і те саме не читається двічі.
    owner — підрядок імені виконавця («Юлія» знайде і «Юля», якщо їх злито в графі).
    category_id — звузити до напрямку. status: open | stale | done | cancelled | all
    (`stale` — задачі зі старих зустрічей без майбутнього терміну: не видалені,
    але прибрані зі зводів). Дублі однієї задачі з різних зустрічей приховані.

    Повертає і сиру фразу терміну (`due_raw`, як прозвучало), і обчислену дату
    (`due_date` + `due_precision`) — якщо дата виглядає дивно, дивись на сиру фразу.
    Так само з виконавцем: `owner` — канонічне імʼя з графа (зливає «Юля»/«Юлія»/
    «Julia Bondarenko»), `owner_said` — як його назвали в самій задачі. Розходження
    цих двох полів означає криву звʼязку в графі, а не два різні завдання.

    Задача, що прозвучала в Telegram (`source='tg_thread'`), несе адресу:
    `chat` — у якому чаті, `said_by` — хто це сказав, `chat_id`+`msg_id` — точна
    адреса повідомлення. Це відповідь на «куди написати», а не лише «що зробити».
    `link` (t.me) буває не завжди: у legacy-групах Telegram посилання на
    повідомлення не існує — адресою там служить саме пара id.

    Повертає `{items, coverage}`. `coverage` — знаменник під тими ж фільтрами:
    вікна за датою бачать лише задачі з `due_date`, а він є у чверті задач. Тому
    «цього тижня — 5» означає «5 із датованих», а не «справ майже немає»;
    `coverage.undated` каже, скільки задач у жодне вікно за датою не потрапить
    (вони у `window='no_date'`). Не переказуй порожнє вікно як «все спокійно»,
    не подивившись на знаменник.

    `coverage.window_matched` — скільки задач у ЦЬОМУ вікні насправді, а
    `truncated` — чи обрізав `limit` видачу. Коли `truncated=true`, перелічені
    рядки не є всім вікном: підвищ `limit` або звузь фільтр, перш ніж робити
    висновок «ось усе, що є».
    """
    from app.services import commitments
    items = commitments.list_commitments(
        DB_PATH, window=window, owner=owner, category_id=category_id,
        status=status, limit=limit)
    matched = commitments.count_commitments(
        DB_PATH, window=window, owner=owner, category_id=category_id, status=status)
    coverage = commitments.commitments_coverage(
        DB_PATH, owner=owner, category_id=category_id, status=status)
    # Скільки в цьому вікні НАСПРАВДІ і чи обрізано видачу. Без цього знаменник
    # біля обрізаного списку шкодив: «total 1700» поруч із 50 рядками читається
    # як «ось усе вікно, решта 1650 деінде».
    coverage["window_matched"] = matched
    coverage["truncated"] = matched > len(items)
    return {"items": items, "coverage": coverage}


@mcp.tool
def list_dropped_commitments(days: int = 30, limit: int = 20, min_age_days: int = 14) -> list:
    """Обіцянки, що тихо померли: задача прозвучала, а ПІСЛЯ тієї зустрічі тема
    більше не спливала в жодному джерелі (дзвінки, Telegram, документи).

    days — за який період назад брати задачі-кандидати; min_age_days — карантин,
    щоб учорашні домовленості не рахувались «забутими» просто тому, що після них
    ще нічого не було. Відповідає на «що ми упустили з фокусу за останній місяць».

    Беруться лише обіцянки з РОЗМОВ (дзвінок, зустріч, Telegram, завантажений
    аудіофайл). Задачі з документів і ютуб-роликів сюди не потрапляють: у PDF
    інвесторам «обіцянки» дають чужі компанії, і вони справді не спливають у
    наших розмовах — бо ніколи й не були нашими.

    Тему задачі впізнають два НАЙРІДКІСНІШІ її слова, порахованих по архіву
    станом на ту зустріч. Найдовші слова для цього не годяться: у 15 задачах зі
    200 «жива» доводилась лише загальним дієсловом — «Продовжувати роботу по
    ендавменту BUEF» вважалась спливлою, бо в архіві 509 разів трапляється
    «стратегії».

    З однієї зустрічі береться щонайбільше 3 задачі: прослуханий курс — теж
    запис розмови, і його конспект інакше займає шосту частину списку.

    Список НЕ детермінований на живому архіві, і так і має бути: щойно в архів
    приходить повідомлення, яке зачіпає тему задачі, задача перестає бути
    загубленою і зникає зі списку."""
    from app.services import commitments
    return commitments.dropped_commitments(DB_PATH, days=days, limit=limit,
                                           min_age_days=min_age_days)


@mcp.tool
def list_stale_topics(days: int = 30, min_meetings: int = 3, limit: int = 15) -> list:
    """Теми/проєкти/організації, які активно обговорювались (≥ min_meetings зустрічей),
    а потім зникли з розмов на понад `days` днів. Друга половина питання
    «що випало з фокуса» — на рівні напрямів, а не окремих задач.

    Кандидатів дає граф сутностей, але мовчання підтверджується ТЕКСТОМ: граф
    покриває лише чверть Telegram-архіву, тож тема, яку щодня пишуть у чаті, для
    нього «замовкла» ще на останньому дзвінку. `last_seen_source` каже, на чому
    стоїть дата: `graph` — назви немає в пізніших текстах, `text` — вона там є
    (тоді береться текстова дата). Точна форма назви: відмінки FTS не ловить,
    тож помилка йде в бік «мовчить», а не «жива»."""
    from app.services import commitments
    return commitments.stale_topics(DB_PATH, days=days, min_meetings=min_meetings, limit=limit)


@mcp.tool
def weekly_digest(owner: Optional[str] = None, days_back: int = 30, limit: int = 15) -> dict:
    """Понеділковий звід одним викликом: що на цьому тижні, що протерміновано,
    що без терміну, які обіцянки загубились і які теми зникли з розмов.

    owner — звузити до конкретної людини («що на мені» / «що на Юлії»).

    `coverage` у відповіді — знаменник до трьох перших відер: вони фільтрують за
    `due_date`, який має чверть задач. Переказуючи звід, називай і його, інакше
    «на тиждень пʼять справ» звучить як повна картина замість чверті.

    `corrections` — коментарі-виправлення власника за період: місця, де запис
    виявився неправдивим і його поправили. Це найдорожче відро зводу: доки таке
    виправлення не враховане, кожна відповідь по тому запису лишається хибною."""
    from app.services import commitments
    return commitments.weekly_digest(DB_PATH, owner=owner, days_back=days_back, limit=limit)


@mcp.tool
def list_open_questions(days: int = 90, limit: int = 20, window_h: float = 24.0,
                        chat_id: Optional[int] = None) -> dict:
    """Питання з Telegram, адресовані ВЛАСНИКУ архіву, на які він так і не відповів.

    Відповідає на «що в мене висить у переписці». Показуються ЛИШЕ ті питання,
    де адресат доведений структурно: приватний чат (там більше нікого) або
    @згадка ніка власника. Групове питання без звернення в список НЕ потрапляє —
    скільки таких відсіяно, видно в `skipped_group`. Це свідомо: довести, що
    групове «а можемо на 15:00?» чекає саме власника, нічим.

    `window_h` — скільки годин присутності власника в ТОМУ Ж чаті після питання
    вважати відповіддю (за замовчуванням доба). Присутність рахується по чату, а
    не по нитці: нитки розмічені моделлю, і відповідь регулярно лягає в сусідню.
    `silence_h` у відповіді — через скільки годин власник узагалі зʼявився в чаті
    (null = більше не писав там жодного разу).

    Не плутати з `get_chat_context(chat_id).open_questions`: там — питання, що
    висять у ЧАТІ за версією локальної моделі (будь до кого), тут — доведено до
    власника і без моделі взагалі.

    Потребує знання, хто власник: береться від слухача Telegram (кешується), або
    задається `TELEGRAM_SELF_NAME`/`TELEGRAM_SELF_USERNAME`. Якщо невідомо —
    повертає порожній список і причину в `reason`."""
    from app.services import tg_questions
    return tg_questions.open_questions(DB_PATH, days=days, limit=limit,
                                       window_h=window_h, chat_id=chat_id)


@mcp.tool
def research_export(terms: str, category_id: Optional[int] = None) -> str:
    """Зібрати ВСІ згадки терміна/бренду/сутності з усіх джерел у markdown (оригінальні
    цитати з провенансом, без AI). terms — кома/пробіл-розділені терміни."""
    try:
        from app.services import research
    except Exception as e:  # pragma: no cover
        return f"research-модуль недоступний: {e}"
    collected = research.collect(DB_PATH, terms, category_id=category_id)
    try:
        return research.render_originals_md(collected, "")
    except TypeError:
        return json.dumps(collected, ensure_ascii=False, indent=2)


# ====================== AI НА ВИМОГУ (Claude, HTTP-проксі на app.py) ==========

@mcp.tool
def ask_archive(question: str, k: int = 12, category_id: Optional[int] = None,
                project: Optional[str] = None, model: Optional[str] = None,
                explain: bool = False) -> dict:
    """RAG «Запитай архів»: питання → відповідь Claude ТІЛЬКИ з архіву + цитати [n].

    k — скільки чанків у контекст (1–20; дефолт 12 за замірами eval-харнеса:
    на k=8 частина правильних джерел стоїть одразу за межею зрізу —
    recall@8 67.9% проти recall@12 82.1%). Два рівні звуження:
      * category_id — напрямок (грубий шар; «Робота» покриває ~більша частина архіву,
        тож сам по собі майже не звужує);
      * project — назва проєкту або людини («Acmecorp», «Ковальчука», «Адам»):
        шукає лише в записах, де ця сутність згадана. Саме це рятує від
        змішування напрямків. Кілька назв — через кому. Невідома назва не
        обнуляє пошук, а просто не звужує.

    Для природномовних питань («що ми вирішили по X?»); для точкового пошуку
    фрагментів бери search_archive. Нічого не змінює (працює і в read-only).
    explain=True — додати розбір релевантності (bm25/dense/rrf/recency/rerank,
    джерело) до цитованих чанків."""
    body = {"question": question, "k": int(k), "category_id": category_id,
            "project": project, "model": model}
    if explain:
        body["explain"] = True
    return _api("POST", "/api/memory/ask", body=body, write=False, timeout=180.0)


@mcp.tool
def suggest_category(transcription_id: int) -> dict:
    """k-NN підказка напрямку для запису (по ембеддингах сусідів). Нічого не
    змінює — лише пропозиція (suggestion=None + reason, якщо cold-start).
    Застосування напрямку — дія в UI Recall (MCP тут read-only)."""
    return _api("GET", f"/api/memory/transcriptions/{int(transcription_id)}/suggest-category")


@mcp.tool
def research_preview(q: Optional[str] = None, entity_id: Optional[int] = None,
                     category_id: Optional[int] = None) -> dict:
    """Швидкий прев'ю згадок терміна/бренду/сутності по всьому архіву (0 токенів,
    без AI). Вкажи q (підрядок) АБО entity_id. Для повного дампу цитат —
    research_export."""
    params: dict = {}
    if q is not None:
        params["q"] = q
    if entity_id is not None:
        params["entity_id"] = int(entity_id)
    if category_id is not None:
        params["category_id"] = int(category_id)
    return _api("GET", "/api/research/preview", params=params)


@mcp.tool
def research_summary(q: Optional[str] = None, entity_id: Optional[int] = None,
                     category_id: Optional[int] = None,
                     model: Optional[str] = None) -> dict:
    """Зведений структурований звіт по терміну/сутності (map-reduce Claude по
    витягнутих фрагментах архіву). Дорожче за research_preview (платні токени),
    але дає готовий markdown. Вкажи q АБО entity_id. Стан не змінює."""
    body: dict = {}
    if q is not None:
        body["q"] = q
    if entity_id is not None:
        body["entity_id"] = int(entity_id)
    if category_id is not None:
        body["category_id"] = int(category_id)
    if model is not None:
        body["model"] = model
    res = _sse_collect("/api/research/summary", body, timeout=600.0)
    if res.get("error"):
        return {"error": res["error"]}
    done = res.get("done") or {}
    return {
        "markdown": done.get("markdown"),
        "model": done.get("model"),
        "stats": done.get("stats"),
        "cost": done.get("cost"),
        "tokens": {"input": done.get("input_tokens"), "output": done.get("output_tokens")},
    }


# ====================== БАГАТШІ ЧИТАННЯ, як в UI (HTTP-проксі на app.py) ======

@mcp.tool
def search_history(search: Optional[str] = None, source_type: Optional[str] = None,
                   language: Optional[str] = None, category_id: Optional[int] = None,
                   speaker_id: Optional[int] = None, tg_chat: Optional[str] = None,
                   tg_sender: Optional[str] = None, page: int = 1,
                   per_page: int = 20) -> dict:
    """Список/пошук записів архіву з фільтрами (FTS по тексту через search) і
    пагінацією — повноцінний пошук+фасети як в UI Архіву.

    Фільтри: source_type, language, category_id, speaker_id, а для Telegram —
    tg_chat (назва чату, підрядок) і tg_sender (автор, підрядок). Саме ця пара
    дає хронологічне читання чату: `source_type='telegram'` + `tg_chat='Acmecorp'`
    поверне переписку цього чату по порядку, а не розсипом однорядковиків."""
    params: dict = {"page": int(page), "per_page": int(per_page)}
    for key, val in (("search", search), ("source_type", source_type),
                     ("language", language), ("category_id", category_id),
                     ("speaker_id", speaker_id), ("tg_chat", tg_chat),
                     ("tg_sender", tg_sender)):
        if val is not None:
            params[key] = val
    return _api("GET", "/api/history", params=params)


@mcp.tool
def get_archive_stats() -> dict:
    """Агрегати архіву: кількості транскриптів за джерелом, типи сутностей,
    статуси задач тощо (як на дашборді)."""
    return _api("GET", "/api/memory/stats")


@mcp.tool
def list_speakers(q: Optional[str] = None, limit: int = 50) -> dict:
    """Глобальні спікери (+ автокомпліт через q).

    Це ГОЛОСИ з діаризації дзвінків — не всі люди архіву. Авторів переписки
    (35 імен на повідомлень) тут немає: їх дає `get_speaker_stats`
    у `tg_senders`."""
    params: dict = {"limit": int(limit)}
    if q is not None:
        params["q"] = q
    return _api("GET", "/api/speakers", params=params)


@mcp.tool
def get_speaker_stats() -> dict:
    """Хто скільки говорив і хто скільки писав — ДВА РІЗНІ шари в одній відповіді.

    `speakers` — голоси з діаризації: записів, секунд мовлення, слів, діапазон
    дат. Покриває лише дзвінки й записи (на архіві — частина записів).
    `tg_senders` — автори переписки: повідомлень, у скількох чатах, перша й
    остання поява. Секунд і слів мовлення там немає й бути не може.

    **Одиниці шарів не додаються**: 40 тисяч секунд і 872 повідомлення не
    зводяться в «активність». Хочеш порівняти людей — порівнюй усередині шару.

    Спільна людина видна через `entity_id`, проставлений в обох списках: у
    діаризації вона «Слава Верес», у переписці «Veres Viacheslav», і однакове
    id означає, що це знає ГРАФ. Прочерк означає, що граф цього написання не
    знає, — не роби з двох схожих імен один висновок самотужки.

    `coverage` каже, скільки записів має діаризацію і скільки відправників
    привʼязано до графа. Без нього список голосів читався як увесь архів,
    хоча описує 3% його."""
    return _api("GET", "/api/speakers/stats")


@mcp.tool
def get_speaker_timeline(speaker_id: int, days: int = 30) -> dict:
    """Активність спікера по днях за останні N днів.

    Рахується з діаризованих сегментів, тобто це активність ГОЛОСОМ. Тиша тут
    не означає, що людина мовчала: вона могла весь місяць писати в чатах —
    переписка в цей таймлайн не входить взагалі."""
    return _api("GET", f"/api/speakers/{int(speaker_id)}/timeline",
                params={"days": int(days)})


@mcp.tool
def list_bookmarks(transcription_id: int) -> dict:
    """Закладки конкретного транскрипту."""
    return _api("GET", f"/api/transcription/{int(transcription_id)}/bookmarks")


@mcp.tool
def list_saved_searches() -> dict:
    """Збережені пошуки (назва + збережений об'єкт фільтрів + лічильник)."""
    return _api("GET", "/api/saved-searches")


# ====================== LIVE / ФОНОВІ ЗАДАЧІ (status-only) ====================
# Опитування черги фонових задач (transcribe/download/backfill/import/finalize)
# і status-знімки live-підсистем: запис (Windows WASAPI mic/loopback), ко-пілот
# дзвінка (Qwen-локально + Claude-верифікація, 503 якщо COPILOT_ENABLED=False),
# Telegram-слухач (Telethon, 503 якщо процес офлайн). Керування (старт/стоп/
# зміна стану) прибрано з MCP (read-first) — ці дії робить UI Recall; тут лише
# перегляд поточного стану.

@mcp.tool
def get_job_status(job_id: str) -> dict:
    """Стан фонової задачі з черги (transcribe/download/backfill/import/finalize):
    queued|running|completed|failed|cancelled + метадані."""
    return _api("GET", f"/api/jobs/{job_id}")


@mcp.tool
def get_active_recording() -> dict:
    """Поточна активна сесія запису (для re-attach) + за що чіплятись, щоб
    дивитись у дзвінок наживо (mcp-live-call-03): `copilot_session_id`
    (None, якщо ко-пілот для цього запису не стартував), `copilot_active`
    (сесія ко-пілота ще 'active', не 'ended'), `live_transcript_available`
    (те саме поле `available`, що віддає `get_live_transcript` — не
    передивляємось сюди по `state.is_active`: доступність live-прев'ю
    вирішує live-воркер, а не статус сесії запису, знахідка історії 01)."""
    data = _api("GET", "/api/recordings/active")
    if not isinstance(data, dict) or not data.get("active"):
        return data
    sid = data.get("session_id")
    data["copilot_session_id"] = None
    data["copilot_active"] = False
    data["live_transcript_available"] = None
    if sid:
        row = None
        try:
            with get_db_connection(DB_PATH) as conn:
                row = conn.execute(
                    "SELECT id, status FROM copilot_sessions WHERE recording_session_id = ? "
                    "ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
        except sqlite3.Error as e:
            # Деградуємо, як і решта тулзи: БД заблокована довше busy_timeout —
            # не причина обвалити відповідь про активний запис, який app.py
            # вже підтвердив.
            logger.debug("[mcp] get_active_recording: copilot_sessions недоступна: %s", e)
        if row:
            data["copilot_session_id"] = row["id"]
            data["copilot_active"] = (row["status"] == "active")
        lt = get_live_transcript(session_id=sid)
        if isinstance(lt, dict) and "available" in lt:
            data["live_transcript_available"] = lt["available"]
    return data


# Потолок подій копілота за один виклик тулзи (окремий від _LIVE_TRANSCRIPT_LIMIT=400
# у app/blueprints/recording.py — обидва існують по контракту C4, plan.md, значення
# НЕ збігаються: події копілота важчі за сегмент транскрипту).
_LIVE_COPILOT_LIMIT = 200

# Види подій copilot_events — коментар колонки kind у app/db/migrations.py (v19).
# Джерело для валідації kinds=... і для повного тесту (жоден вид не пропущений).
_COPILOT_EVENT_KINDS = frozenset({
    "topic_shift", "topic_return", "retrieval", "insight_local", "insight_verified",
    "escalation", "operator_action", "usage", "safety_sweep",
})


def _operator_saw(kind: str, payload: dict, event_id: int, verdict_by_ref: dict) -> Optional[bool]:
    """Виведений (а не сирий) ознака «оператор реально побачив цю картку».

    `payload.get("shown")` сам по собі бреше у режимі verified_only
    (app/services/copilot/config.py): там `insight_local` персистується з
    `shown=False` ДО вердикту Claude, а показана картка йде лише в SSE
    (`_publish("copilot_insight", ...)` у app/services/copilot/worker.py) —
    подія в БД так і лишається з `shown=False`, хоча оператор її бачив.

    Ланцюжок `ref_event_id`/вердикт закриває це: якщо для `insight_local` є
    пізніша подія `insight_verified` з `payload.ref_event_id == id` і
    `verdict == "real"`, картку таки показали (з верифікованим текстом).

    `insight_verified` сама по собі буває ДВОХ форм з тим самим kind:
    - запис вердикту ескалації (`ref_event_id` у payload, БЕЗ `shown`) — це
      метадані про іншу подію, не картка сама по собі → None, не False;
    - картка safety-sweep (`_emit_insight` з kind_event="insight_verified") —
      несе власний `shown`, як insight_local.
    """
    if kind == "insight_local":
        if verdict_by_ref.get(event_id) == "real":
            return True
        shown = payload.get("shown")
        return bool(shown) if shown is not None else None
    if kind == "insight_verified" and "ref_event_id" in payload:
        return None
    return payload.get("shown")


@mcp.tool
def get_live_transcript(session_id: Optional[str] = None, since_seq: Optional[int] = None) -> dict:
    """Прокси на C1 (`GET /api/recording/<sid>/live-transcript`) — снапшот
    live-прев'ю сегментів запису, що триває (RAM-only, зникає після finalize —
    тоді бери `get_transcript`). `session_id=None` → активна сесія запису.

    `since_seq` — **єдиний спосіб опитування**: поверне лише сегменти з
    `seq > since_seq` (номер спільний для обох доріжок mic/system, росте в
    порядку фактичного append'у — це гарантує, що жодна доріжка не
    загубиться). Бери `next_seq` з відповіді ДОСЛІВНО і передавай його як
    `since_seq` наступного виклику — не рахуй максимум сам за вже отриманими
    сегментами: порожній опит навмисно лишає `next_seq` незмінним, щоб
    усічені (`truncated`) сегменти не пропали.

    Фільтру по часу (`since_sec`) тут навмисно немає: доріжки транскрибуються
    незалежно, і опитування по часу губить сегменти повільнішої доріжки.
    Потребує запущеного app.py."""
    if not session_id:
        active = _api("GET", "/api/recordings/active")
        if not isinstance(active, dict) or not active.get("active"):
            return {
                "success": True, "session_id": None, "available": False,
                "reason": (active.get("error") if isinstance(active, dict) else None)
                          or "немає активної сесії запису",
                "is_active": False, "status": None, "elapsed_seconds": 0.0,
                "segments": [], "count": 0, "last_sec": None, "next_seq": None,
                "truncated": False,
            }
        session_id = active.get("session_id")
    params: dict = {}
    if since_seq is not None:
        params["since_seq"] = int(since_seq)
    return _api("GET", f"/api/recording/{session_id}/live-transcript", params=params or None)


@mcp.tool
def get_live_copilot(session_id: Optional[str] = None, since_event_id: Optional[int] = None,
                      kinds: Optional[str] = None) -> dict:
    """Direct-DB, ПОВНИЙ потік подій ко-пілота живого дзвінка — включно з
    притишеними бюджетом уваги і неверифікованими `insight_local` (нічого не
    фільтрується тут: рішення, що показувати, лишається за агентом, non-goal
    цієї історії — не звужувати на боці тулзи).

    `session_id` — id сесії ЗАПИСУ (`rec_...`, як у `get_live_transcript`), НЕ
    `copilot_session_id`. `None` → та сама активна сесія запису, що й у
    `get_live_transcript` (через `/api/recordings/active`, коли app.py живий —
    'active': false там означає ЩО НЕМАЄ активного запису, а не «шукай щось
    старе в БД»). Якщо app.py недоступний, тулза лишається direct-DB: фолбек
    на найновішу `copilot_sessions` зі статусом 'active' у БД — і тоді
    зависла сесія впалого процесу МОЖЕ трапитись, про що каже поле
    `session_resolution` у відповіді.
    `since_event_id` — лише події з `id >` заданого (інкрементальний опит).
    `kinds` — опційний CSV-фільтр (`"insight_local,escalation"`); порожньо —
    усі види. Невідомий вид — явна відмова з переліком `known_kinds`, а не
    тиха порожня видача.

    Кожна подія несе розгорнутий `payload` + два top-level поля:
    - `shown` — СИРЕ значення `payload.shown` на момент запису в БД
      (`None` для видів, де показ картки не застосовний: topic_shift,
      topic_return, retrieval, usage, operator_action, escalation, і для
      `insight_verified`-запису вердикту, див. нижче). У режимі verified_only
      (типовий, `app/services/copilot/config.py`) це поле БРЕШЕ для
      `insight_local`: подія персистується з `shown=False` ДО вердикту
      Claude, а показана картка йде лише в SSE — БД цей момент не оновлює.
    - `operator_saw` — ВИВЕДЕНИЙ, правдивий показник «оператор це бачив»,
      побудований по ланцюжку `ref_event_id`/`verdict`: якщо для
      `insight_local` пізніше зʼявилась `insight_verified` з
      `ref_event_id == id` і `verdict == 'real'`, картку таки показали
      (з верифікованим текстом) — `operator_saw=True` навіть якщо `shown`
      каже `False`. Значення двох форм `insight_verified` різні: запис
      вердикту ескалації (є `ref_event_id`, немає `shown`) сам по собі не
      картка → `None`; картка safety-sweep (є `shown`, немає `ref_event_id`)
      — як `insight_local`. Ланцюжок рахується в межах ОДНОГО виклику: якщо
      верифікація прийшла вже ПІСЛЯ обрізки/курсора цієї сторінки, значення
      лишається таким, яким було на момент виклику — best-effort, не гарантія.

    Потолок 200 подій за виклик (SQL `LIMIT`, без розбору зайвого в Python),
    при обрізці `truncated: true` — гортай далі через `since_event_id` = id
    останньої отриманої події.

    Працює БЕЗ запущеного app.py для явного `session_id`; для `session_id=None`
    йде необовʼязковий виклик `/api/recordings/active` — його відсутність не
    валить тулзу, лише вимикає точне розв'язання на користь фолбека з БД."""
    session_resolution = None
    if not session_id:
        active = _api("GET", "/api/recordings/active")
        if isinstance(active, dict) and "error" not in active:
            if not active.get("active"):
                return {
                    "success": True, "available": False,
                    "reason": "немає активної сесії запису",
                    "session_id": None, "copilot_session_id": None,
                    "events": [], "count": 0, "truncated": False,
                }
            session_id = active.get("session_id")
        else:
            session_resolution = (
                "app.py недоступний — сесію взято фолбеком з БД (найновіша "
                "copilot_sessions зі статусом 'active'), могла лишитись від "
                "впалого процесу")

    with get_db_connection(DB_PATH) as conn:
        if session_id:
            csess = conn.execute(
                "SELECT * FROM copilot_sessions WHERE recording_session_id = ? "
                "ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
        else:
            csess = conn.execute(
                "SELECT * FROM copilot_sessions WHERE status = 'active' "
                "ORDER BY id DESC LIMIT 1").fetchone()
        if csess is None:
            return {
                "success": True, "available": False,
                "reason": (f"ко-пілот для сесії {session_id} не запускався" if session_id
                           else "активної сесії ко-пілота не знайдено"),
                "session_id": session_id, "copilot_session_id": None,
                "events": [], "count": 0, "truncated": False,
            }
        csess = _row(csess)
        cs_id = csess["id"]
        kind_list = [k.strip() for k in kinds.split(",") if k.strip()] if kinds else None
        if kind_list:
            unknown = sorted(set(kind_list) - _COPILOT_EVENT_KINDS)
            if unknown:
                return {
                    "success": False,
                    "error": f"невідомі kinds: {', '.join(unknown)}",
                    "known_kinds": sorted(_COPILOT_EVENT_KINDS),
                }
        query = ("SELECT id, ts_wall, ts_offset_sec, kind, topic_id, source, confidence, "
                 "payload_json, tokens_in, tokens_out, operator_action FROM copilot_events "
                 "WHERE copilot_session_id = ?")
        params: list = [cs_id]
        if since_event_id is not None:
            query += " AND id > ?"
            params.append(int(since_event_id))
        if kind_list:
            query += f" AND kind IN ({','.join('?' * len(kind_list))})"
            params.extend(kind_list)
        query += " ORDER BY id LIMIT ?"
        params.append(_LIVE_COPILOT_LIMIT + 1)
        rows = conn.execute(query, params).fetchall()

    truncated = len(rows) > _LIVE_COPILOT_LIMIT
    if truncated:
        rows = rows[:_LIVE_COPILOT_LIMIT]

    events = []
    verdict_by_ref: dict = {}
    for r in rows:
        e = _row(r)
        raw = e.pop("payload_json")
        try:
            payload = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            payload = {}
        e["payload"] = payload
        e["shown"] = payload.get("shown")
        events.append(e)
        if e["kind"] == "insight_verified" and "ref_event_id" in payload:
            verdict_by_ref[payload["ref_event_id"]] = payload.get("verdict")
    for e in events:
        e["operator_saw"] = _operator_saw(e["kind"], e["payload"], e["id"], verdict_by_ref)

    result = {
        "success": True, "available": True, "reason": None,
        "session_id": csess.get("recording_session_id"),
        "copilot_session_id": cs_id,
        "events": events, "count": len(events), "truncated": truncated,
    }
    if session_resolution:
        result["session_resolution"] = session_resolution
    return result


@mcp.tool
def ask_live(question: str, scope: str = "both", session_id: Optional[str] = None) -> dict:
    """Прокси на C2 (`POST /api/copilot/live-ask`) — питання агента → локальна
    модель (Ollama, $0, без Claude) → відповідь із живого транскрипту і/або
    архіву. Семантично читання: нічого не пише в БД, у сесію запису чи у
    віджет оператора (A2, plan.md).

    `scope`: `'call'` — лише живий транскрипт | `'archive'` — лише архів |
    `'both'` (за замовч.) — обидва. `session_id=None` → активна сесія запису
    (розв'язується на боці app.py). Недоступність Ollama — `available: false`
    + `reason` у відповіді, не HTTP-помилка. Потребує запущеного app.py."""
    return _api("POST", "/api/copilot/live-ask",
                body={"question": question, "session_id": session_id, "scope": scope})


@mcp.tool
def copilot_availability() -> dict:
    """Чи ввімкнено ко-пілот + статус локальної LLM (Ollama)."""
    return _api("GET", "/api/copilot/availability")


@mcp.tool
def telegram_status() -> dict:
    """Чи живий процес-слухач Telegram + статус логіну."""
    return _api("GET", "/api/telegram/status")


@mcp.tool
def telegram_coverage() -> dict:
    """Наскільки архів відстає від живого Telegram — по кожному моніторенему чату.

    Бери це ПЕРШИМ, коли пошук у Telegram-частині архіву нічого не дав: відповідь
    «в архіві цього немає» чесна лише тоді, коли чат не відстає. Поля: коли чат
    востаннє потрапляв в архів (archived_last_date), коли там востаннє писали
    (live_last_date), відставання в днях (lag_days) і непрочитані (unread_count).

    status: ok | behind (у Telegram новіше, ніж в архіві) | never_ingested |
    migrated (chat_id змінився після переїзду в супергрупу — слухач глухне) |
    not_listed (немає серед діалогів: вийшли/видалено/поза лімітом) | unknown
    (слухач офлайн — жива половина недоступна, архівна лишається).

    behind_messages рахується лише для супергруп (-100…): в решті чатів
    message_id з глобальної послідовності акаунта. Дірки в id можуть бути
    стікерами і службовими повідомленнями, які інжест свідомо не бере."""
    return _api("GET", "/api/telegram/coverage", timeout=90.0)


@mcp.tool
def get_chat_context(chat_id: int) -> dict:
    """Досьє Telegram-чату: хто там, поточні теми, що висить без відповіді, до
    чого дійшли (Волна 4.5.2).

    Бери це ПЕРЕД тим, як питати архів про робочий чат: пошук віддає окремі
    репліки, а тут — стан справ цілком. Особливо для «що я пропустив у X»,
    «хто за що відповідає» і «які питання висять».

    Поля:
      * participants — з ТВЕРДИМИ числами (скільки написав, коли зʼявився і
        писав востаннє): їх рахує SQL, модель їх не переписує. `role` — опис
        від локальної моделі, може бути порожній;
      * topics — відкриті НИТКИ розмов (не чат цілком: у робочому чаті
        одночасно йдуть кілька проєктів);
      * open_questions / decisions — з посиланням на thread_id, звідки взято;
      * messages_seen / updated_at — станом на коли досьє. Оновлюється за
        порогом, тож свіжі повідомлення можуть бути ще не враховані;
      * summary=None і model=None разом означають, що локальна модель була
        недоступна і є лише тверда частина — це не порожній чат.

    chat_id бери з list_recent / search_history (поле tg_chat_id) або з
    telegram_coverage."""
    from app.services import tg_chat_context
    got = tg_chat_context.get_context(DB_PATH, int(chat_id))
    if not got:
        return {"error": f"досьє для чату {chat_id} ще не побудоване",
                "hint": "python -m app.services.tg_chat_context refresh "
                        f"--chat {chat_id} --force"}
    # «Оновлюється за порогом» — правда, але непридатна для рішення: читач не
    # знає, це відставання на два повідомлення чи на двісті. Рахуємо різницю.
    if got.get("updated_at"):
        with get_db_connection(DB_PATH) as conn:
            got["messages_after_profile"] = conn.execute(
                "SELECT COUNT(*) FROM transcriptions WHERE tg_chat_id = ? "
                "AND created_at > ? AND deleted_at IS NULL",
                (int(chat_id), got["updated_at"])).fetchone()[0]
    return got


# ============================== RESOURCES =====================================
# Поділ на direct-DB/проксі — довідковий текст для людини; він НЕ визначає, що
# зареєстровано. Джерело істини — реєстр FastMCP (`_registered_tool_names`);
# тест ловить розбіжність, якщо ці два бакети розійдуться з реєстром.
_ABOUT_DIRECT_DB = (
    "get_transcript", "get_original_file", "list_recent", "list_categories",
    "list_entities", "get_entity", "list_action_items", "list_dropped_commitments",
    "list_stale_topics", "weekly_digest", "list_open_questions", "research_export",
    "get_chat_context", "list_comments", "grep_archive", "get_live_copilot",
)
_ABOUT_PROXY = (
    "search_archive", "ask_archive", "suggest_category", "research_preview",
    "research_summary", "search_history", "get_archive_stats", "list_speakers",
    "get_speaker_stats", "get_speaker_timeline", "list_bookmarks", "list_saved_searches",
    "get_job_status", "get_active_recording", "copilot_availability", "telegram_status",
    "telegram_coverage", "get_live_transcript", "ask_live",
)


def _registered_tool_names() -> list[str]:
    """Імена тулзів, реально зареєстрованих у FastMCP — не хардкод, а реєстр:
    підкладена нова тулза зміниться тут сама, без правки `about()`."""
    tools = asyncio.run(mcp.list_tools())
    return sorted(t.name for t in tools)


@mcp.resource("recall://about")
def about() -> str:
    """Огляд системи + жива статистика архіву (щоб Claude розумів, що під рукою)."""
    with get_db_connection(DB_PATH) as conn:
        n_tx = conn.execute("SELECT COUNT(*) FROM transcriptions").fetchone()[0]
        n_ch = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        by_src = {r["source_type"]: r["c"] for r in conn.execute(
            "SELECT source_type, COUNT(*) c FROM transcriptions GROUP BY source_type")}
        cats = [r["name"] for r in conn.execute("SELECT name FROM categories ORDER BY sort_order")]
    n_tools = len(_registered_tool_names())
    return (
        f"# Recall — локальний RAG-архів дзвінків (MCP: read-first ядро, {n_tools} тулзів)\n\n"
        "Flask-додаток, дані локально. Джерела: записи дзвінків (діаризація), YouTube, "
        "документи (PDF/DOCX/…), Telegram. Пошук: e5-ембеддинги (1024-dim) + FTS5, "
        "гібрид RRF з recency/diversity.\n\n"
        f"## Статистика\n- транскриптів: {n_tx}\n- чанків (векторизовано): {n_ch}\n"
        f"- за джерелом: {json.dumps(by_src, ensure_ascii=False)}\n"
        f"- напрямки: {', '.join(cats)}\n\n"
        "## Як користуватись (усе нижче — лише читання, стан не змінює)\n"
        "1. `search_archive(query)` — головний пошук (поверне chunk'и з transcription_id);\n"
        "2. `get_transcript(id)`/`get_original_file(id)` — повний текст + summary/оригінал;\n"
        "   `search_history(...)` — список/фасети як в UI; `list_recent`;\n"
        "3. `list_entities`/`get_entity` — граф людей/проєктів/тем; `get_speaker_stats`/`get_speaker_timeline`;\n"
        "4. **Зобовʼязання:** `weekly_digest()` — звід одним викликом («що на мені цього тижня»); "
        "`list_action_items(window=this_week|next_week|overdue|no_date|all, owner=…)`; "
        "`list_dropped_commitments(days)` — обіцянки без сліду; `list_stale_topics(days)` — теми, "
        "що зникли з розмов; `list_open_questions(days)` — питання з Telegram особисто до власника, "
        "на які він не відповів. `research_preview`/`research_export`/`research_summary` — згадки терміна;\n"
        "5. `list_categories`/`list_speakers`/`list_bookmarks`/`list_saved_searches`/`get_archive_stats`;\n"
        "6. `ask_archive(question)` — RAG-відповідь з цитатами; `suggest_category` — k-NN підказка (без застосування);\n"
        "7. **Live/фонові статуси:** `get_job_status`, `get_active_recording`, `copilot_availability`, "
        "`telegram_status`; `list_comments` — власні уточнення й виправлення поверх записів;\n"
        "8. **Живий дзвінок:** `get_live_transcript`/`ask_live` — прев'ю транскрипту й питання до нього, "
        "поки дзвінок триває; `get_live_copilot` — повний потік подій ко-пілота (включно з притишеними "
        "бюджетом і неверифікованими), фільтрація — на боці агента.\n\n"
        "## Автономність (працює без запущеного app.py?)\n"
        f"**Direct-DB, автономні ({len(_ABOUT_DIRECT_DB)}):** "
        + ", ".join(f"`{n}`" for n in _ABOUT_DIRECT_DB) +
        " — читають SQLite напряму, app.py НЕ потрібен.\n"
        f"**HTTP-проксі на app.py ({len(_ABOUT_PROXY)}):** "
        + ", ".join(f"`{n}`" for n in _ABOUT_PROXY) +
        " — потребують запущеного `.venv/Scripts/python.exe app.py` "
        "(інакше повертають зрозумілу помилку замість падіння).\n\n"
        "**Стратегічне рішення власника: MCP — read-first.** Створення/зміна/видалення записів, керування "
        "залізом (запис мікрофона, копілот, Telegram-чати) і платні/довгі Claude-виклики "
        "(enrich/polish/summarize/transcribe/ingest/backfill) через MCP недоступні — ці дії робить UI Recall.\n"
    )


@mcp.resource("recall://transcript/{transcription_id}")
def transcript_resource(transcription_id: str) -> str:
    """Повний текст транскрипту як ресурс (для підтяжки в чат)."""
    d = get_transcript(int(transcription_id))  # reuse tool logic
    if "error" in d:
        return d["error"]
    head = f"# {d.get('source_name')} (#{d.get('id')}, {d.get('source_type')}, {d.get('meeting_date') or d.get('created_at')})\n\n"
    return head + (d.get("transcript_text") or "")


@mcp.resource("recall://entity/{entity_id}")
def entity_resource(entity_id: str) -> str:
    """Досьє сутності як ресурс."""
    return json.dumps(get_entity(int(entity_id)), ensure_ascii=False, indent=2)


# ============================== MAIN ==========================================
def main() -> None:
    ap = argparse.ArgumentParser(description="Recall MCP server")
    ap.add_argument("--transport", default="stdio", choices=["http", "stdio"])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "5060")))
    args = ap.parse_args()

    if args.transport == "http" and not os.environ.get("RECALL_MCP_KEY"):
        sys.stderr.write(
            "ВІДМОВА: http-режим без RECALL_MCP_KEY = відкритий доступ.\n"
            "Згенеруй ключ: python -c \"import secrets;print(secrets.token_urlsafe(32))\"\n"
            "і додай у .env: RECALL_MCP_KEY=<ключ>. Або запусти --transport stdio.\n")
        sys.exit(2)

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
