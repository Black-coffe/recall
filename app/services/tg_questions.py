"""Питання без відповіді (Волна 5.3).

«Прийшло в Telegram і лишилось без відповіді» — остання волна роадмапу. План
казав: «питання до мене, після якого немає мого повідомлення в треді (потребує
`tg_reply_to`)». Замір зняв обидві опори плану і лишив третю.

**`tg_reply_to` не працює** — 24 записи з 3961 (0.6%), колонка forward-only з
Волни 4. Де вона є, там їй віримо; будувати на ній не можна.

**Локальна модель тут не суддя.** Перевірено на 14 живих питаннях: 7B назвала
«@johndoe тобі вдасться долучитись?» — питанням НЕ до власника (вона не знає,
що цей нік і є власник), в інших випадках вигадувала адресата («адресовано
Julia Bondarenko», якої в нитці немає) і копіювала формулювання прямо з промпта.
Влучань 1 з 14, причому промах саме на тому випадку, заради якого волна робилась.
Тому адресата визначає СТРУКТУРА, а не модель: @нік власника або приватний чат.
Групове питання без адресата (233 з 294 кандидатів) свідомо НЕ показуємо — ані
структура, ані 7B не можуть сказати, чи чекають відповіді від власника, а
здогадка тут коштує довіри до всього списку.

**«Немає моєї репліки в нитці» — теж хибний тест.** З 10 явно адресованих питань
у 4 власник відповів через 0.0–0.4 години, але відповідь лягла в СУСІДНЮ нитку
(нитки розмічені моделлю). Тому присутність шукаємо в межах ЧАТУ у вікні
`reply_window_h`: писав у чаті після питання — значить бачив. Це свідомо м'який
критерій: назвати забутим те, на що людина відповіла, гірше, ніж пропустити.

Ціна проходу — нуль: ані Claude, ані Ollama, лише SQL. Тому нічого не
персиститься і не протухає: список рахується на запит.

CLI:
    python -m app.services.tg_questions list
    python -m app.services.tg_questions list --days 90 --window 48
    python -m app.services.tg_questions whoami
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from app.db.connection import get_db_connection

logger = logging.getLogger(__name__)

# Вікно відповіді: писав у цьому чаті протягом стількох годин після питання —
# вважаємо, що бачив. 24 год, бо питання ввечері нормально відповісти зранку.
REPLY_WINDOW_H = float(os.environ.get("TG_QUESTIONS_WINDOW_H", "24"))
# Вікно ширше, ніж у зводі задач (там 30 днів), бо питання без відповіді часом НЕ
# розсмоктується: на живому архіві всі шість висять по 2–4 місяці. Список
# короткий за побудовою (адресат має бути доведений), тож ширина його не заливає.
DEFAULT_DAYS = int(os.environ.get("TG_QUESTIONS_DAYS", "90"))

_SELF_CACHE = Path(__file__).resolve().parents[2] / "data" / "tg_self.local.json"

# Query-рядок посилання — не питання: `?igsh=`, `?from=search` дали 77 хибних
# кандидатів з 574, і модель слухняно судила інстаграм-лінки.
_URL_RE = re.compile(r"https?://\S+|www\.\S+|t\.me/\S+")
_HANDLE_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_]{3,31})")


# ============================================================
# Хто такий «я»
# ============================================================

def _listener_status(timeout: float = 5.0) -> Optional[dict]:
    """Спитати слухача, під ким він залогінений (він єдиний тримає сесію)."""
    try:
        from telegram_common import CONTROL_TOKEN_HEADER, control_token
    except ImportError:
        return None
    host = os.environ.get("TELEGRAM_CONTROL_HOST", "127.0.0.1")
    port = os.environ.get("TELEGRAM_CONTROL_PORT", "5051")
    req = urllib.request.Request(f"http://{host}:{port}/status",
                                 headers={CONTROL_TOKEN_HEADER: control_token()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}").get("me") or None
    except (urllib.error.URLError, OSError, ValueError) as e:
        logger.debug("[tg_questions] слухач недоступний: %s", e)
        return None


def _derive_from_private_chats(db_path: str) -> Optional[dict]:
    """Запасний шлях: у чаті 1:1 власник — той відправник, що НЕ є назвою чату.

    Працює лише якщо моніториться хоч одна лічка. Ім'я без ніка й без id, тож
    @згадки цим шляхом не ловляться — про це чесно каже `source`.
    """
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT tg_sender, COUNT(DISTINCT tg_chat_id) chats, COUNT(*) n "
            "FROM transcriptions WHERE source_type='telegram' AND deleted_at IS NULL "
            "AND tg_chat_id > 0 AND tg_sender <> tg_chat_title "
            "GROUP BY tg_sender ORDER BY chats DESC, n DESC LIMIT 1").fetchall()
    if not rows:
        return None
    return {"id": None, "name": rows[0]["tg_sender"], "username": None,
            "source": "private_chats"}


def resolve_self(db_path: str, *, refresh: bool = False) -> Optional[dict]:
    """Хто власник архіву: {'id','name','username','source'} або None.

    Порядок: env (явно задано людиною) → слухач (авторитет, він тримає сесію) →
    кеш минулого опитування → виведення з лічок. Кеш потрібен, бо проходи й MCP
    працюють і тоді, коли слухач вимкнений.

    Здогадка «власник — той, хто присутній у найбільшій кількості чатів» НЕ
    використовується: на живих даних вона показує на двох інших людей (16 чатів
    проти 12 у власника).
    """
    name = os.environ.get("TELEGRAM_SELF_NAME", "").strip()
    if name:
        user = os.environ.get("TELEGRAM_SELF_USERNAME", "").strip().lstrip("@") or None
        return {"id": None, "name": name, "username": user, "source": "env"}

    # Кеш без ніка — неповний, а не готовий: саме такий лишає старий слухач,
    # який ще не вміє віддавати username. Мовчки прийняти його означало б
    # назавжди вимкнути детект по @згадці і не сказати про це нікому.
    cached_ok = False
    if _SELF_CACHE.exists():
        try:
            cached_ok = bool(json.loads(_SELF_CACHE.read_text(encoding="utf-8")).get("username"))
        except (OSError, ValueError):
            cached_ok = False

    if refresh or not cached_ok:
        me = _listener_status()
        if me and me.get("name"):
            me = {"id": me.get("id"), "name": me["name"],
                  "username": (me.get("username") or None), "source": "listener"}
            try:
                _SELF_CACHE.parent.mkdir(parents=True, exist_ok=True)
                _SELF_CACHE.write_text(json.dumps(me, ensure_ascii=False), encoding="utf-8")
            except OSError as e:      # кеш — зручність, не умова роботи
                logger.debug("[tg_questions] кеш не записався: %s", e)
            return me

    if _SELF_CACHE.exists():
        try:
            cached = json.loads(_SELF_CACHE.read_text(encoding="utf-8"))
            if cached.get("name"):
                cached["source"] = "cache"
                return cached
        except (OSError, ValueError) as e:
            logger.debug("[tg_questions] кеш не прочитався: %s", e)

    return _derive_from_private_chats(db_path)


# ============================================================
# Детектор
# ============================================================

def is_question(text: Optional[str]) -> bool:
    """Питальний знак у тексті, а не в query-рядку посилання."""
    return "?" in _URL_RE.sub(" ", text or "")


def _handles(text: Optional[str]) -> set:
    return {h.lower() for h in _HANDLE_RE.findall(_URL_RE.sub(" ", text or ""))}


def addressed_to_me(row: Any, me: dict) -> Optional[str]:
    """Чому це питання САМЕ до власника: 'private' | 'mention' | None.

    None означає «невідомо», а не «ні»: групове питання без звернення може бути
    і до власника — просто довести це нічим, тож у список воно не йде.
    """
    if (row["tg_chat_id"] or 0) > 0:
        return "private"
    user = (me.get("username") or "").lower()
    if user and user in _handles(row["transcript_text"]):
        return "mention"
    return None


def open_questions(db_path: str, *, days: int = DEFAULT_DAYS,
                   window_h: float = REPLY_WINDOW_H, chat_id: Optional[int] = None,
                   limit: int = 20, today: Optional[datetime] = None) -> dict:
    """Питання до власника, після яких він не зʼявився в чаті.

    Повертає {'me', 'questions', 'skipped_group'}: `skipped_group` — скільки
    питань лишилось поза списком через невідомого адресата (це не нуль і має
    бути видно, інакше список читається як «більше нічого немає»).
    """
    me = resolve_self(db_path)
    if not me:
        return {"me": None, "questions": [], "skipped_group": 0,
                "reason": "невідомо, хто власник архіву: слухач вимкнений і "
                          "немає ні TELEGRAM_SELF_NAME, ні приватних чатів"}

    now = today or datetime.now()
    since = (now - timedelta(days=days)).date().isoformat()

    sql = ("SELECT id, tg_chat_id, tg_chat_title, tg_sender, tg_date, tg_link, "
           "tg_message_id, tg_thread_id, tg_reply_to, transcript_text "
           "FROM transcriptions WHERE source_type='telegram' AND deleted_at IS NULL "
           "AND tg_sender <> ? AND tg_date >= ? AND transcript_text LIKE '%?%'")
    params: list = [me["name"], since]
    if chat_id is not None:
        sql += " AND tg_chat_id = ?"
        params.append(chat_id)
    sql += " ORDER BY tg_date DESC"

    out, skipped = [], 0
    with get_db_connection(db_path) as conn:
        for r in conn.execute(sql, params).fetchall():
            if not is_question(r["transcript_text"]):
                continue
            why = addressed_to_me(r, me)
            if not why:
                skipped += 1
                continue

            # Пряма відповідь реплаєм — найсильніший доказ, коли він є (0.6%).
            replied = conn.execute(
                "SELECT 1 FROM transcriptions WHERE tg_sender = ? AND deleted_at IS NULL "
                "AND tg_reply_to = ? LIMIT 1", (me["name"], r["tg_message_id"])).fetchone()
            if replied:
                continue

            # Присутність у ЧАТІ, а не в нитці: нитки розмічені моделлю, і
            # відповідь регулярно лягає в сусідню (заміряно: 4 випадки з 10
            # мали відповідь через 0.0–0.4 год у тому ж чаті).
            nxt = conn.execute(
                "SELECT MIN(tg_date) AS d FROM transcriptions WHERE tg_sender = ? "
                "AND deleted_at IS NULL AND tg_chat_id = ? AND tg_date > ?",
                (me["name"], r["tg_chat_id"], r["tg_date"])).fetchone()["d"]
            gap_h = None
            if nxt:
                try:
                    gap_h = (datetime.fromisoformat(nxt)
                             - datetime.fromisoformat(r["tg_date"])).total_seconds() / 3600
                except ValueError:                 # дата в чужому форматі — не мовчимо
                    logger.debug("[tg_questions] дата не розібралась: %r", nxt)
                    gap_h = None
                if gap_h is not None and gap_h <= window_h:
                    continue

            out.append({
                "transcription_id": r["id"], "chat": r["tg_chat_title"],
                "chat_id": r["tg_chat_id"], "msg_id": r["tg_message_id"],
                "asked_by": r["tg_sender"], "asked_at": r["tg_date"],
                "question": " ".join((r["transcript_text"] or "").split())[:400],
                "addressed": why, "link": r["tg_link"],
                "thread_id": r["tg_thread_id"],
                "silence_h": round(gap_h, 1) if gap_h is not None else None,
            })
            if len(out) >= limit:
                break

    return {"me": {k: me.get(k) for k in ("name", "username", "source")},
            "questions": out, "skipped_group": skipped, "days": days,
            "window_h": window_h}


# ============================================================
# CLI
# ============================================================

def main(argv: Optional[list] = None) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    except ImportError:
        pass

    from config import Config
    default_db = str(Config.BASE_DIR / Config.DATABASE)

    p = argparse.ArgumentParser(prog="tg_questions",
                                description="Питання без відповіді (Волна 5.3).")
    p.add_argument("--db", default=default_db)
    sub = p.add_subparsers(dest="command", required=True)

    q = sub.add_parser("list", help="Питання до мене, на які я не відповів")
    q.add_argument("--days", type=int, default=DEFAULT_DAYS)
    q.add_argument("--window", type=float, default=REPLY_WINDOW_H,
                   help="Годин присутності в чаті, які вважати відповіддю")
    q.add_argument("--chat", type=int, default=None)
    q.add_argument("--limit", type=int, default=20)

    w = sub.add_parser("whoami", help="Кого архів вважає власником і звідки це відомо")
    w.add_argument("--refresh", action="store_true", help="Перепитати слухача")

    args = p.parse_args(argv)
    if args.command == "whoami":
        print(json.dumps(resolve_self(args.db, refresh=args.refresh),
                         ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(open_questions(args.db, days=args.days, window_h=args.window,
                                    chat_id=args.chat, limit=args.limit),
                     ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
