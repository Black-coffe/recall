"""Зобовʼязання з переписки (Волна 5.1).

Волна 0 поставила гард: Claude-картку на `source_type='telegram'` не рахуємо —
на однорядковику «Ок» вона марна, а на живих даних ще й платна. Але задачі в
переписці роздаються реально, просто не в одному повідомленні: «надішлю до
пʼятниці» — це відповідь на прохання, яке прозвучало трьома репліками вище.
Після ниток (Волна 4.5) одиниця сенсу є, тож гард знімається вибірково.

**Каскад.** Локальний триаж (7B, $0, ~4 с на нитку) відсіює нитки без
домовленостей; Claude витягує структуру лише з відібраних. Це той самий
local-first, що в копілоті: дешеве вирішує «чи варто», дороге — «що саме».

**Чому не регекс.** План волни говорив «маркери обіцянки + топ-8 відправників +
довжина». Замір: список маркерів («треба», «надішлю», «чекаю», …) спрацьовує
на живих даних з 545 — це не фільтр, а половина архіву. На пілоті з ниток
регекс і 7B розійшлись у 13 випадках, і мав рацію переважно не регекс: «треба»
всередині «треба визнати» — не зобовʼязання, а «Давай я зараз уточню» —
зобовʼязання без жодного маркера зі списку.

**Що бачить модель.** Прев'ю кожного повідомлення, а не суцільний текст нитки:
найбільша нитка — 337 тис. символів, і це один документ, надісланий у чат.
Обрізання суцільного тексту показало б моделі перші 6 тис. символів документа
замість розмови; прев'ю по повідомленнях показує форму розмови цілком.

**Куди пишеться задача.** У `action_items` з `transcription_id` того
повідомлення, де зобовʼязання прозвучало (модель повертає номер) — звідси
безкоштовно виходить 5.2: у задачі є `tg_link`, автор і чат, тобто видно не
лише «що», а й «куди написати». `source='tg_thread'` відрізняє ці рядки від
успадкованих карток дзвінків: повторний прогін стирає лише свої.

CLI:
    python -m app.services.tg_tasks triage --dry-run
    python -m app.services.tg_tasks triage --chat -100123 --limit 50
    python -m app.services.tg_tasks extract --dry-run
    python -m app.services.tg_tasks extract --limit 10
    python -m app.services.tg_tasks stats
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

from app.db.connection import get_db_connection
from app.services import commitments

logger = logging.getLogger(__name__)


# --- Триаж (локальна модель) ---------------------------------------------
# 7B, а не 32B: замір Волни 4.5.2 показав, що 32B не влазить у 24 GB поруч із
# e5 і reranker'ом і йде в CPU (один чат > 300 с проти 8 с у 7B). Триаж — це
# рішення «так/ні», найдешевша задача з усіх, які тут є.
TRIAGE_MODEL = os.environ.get("TG_TASKS_TRIAGE_MODEL", "qwen2.5:7b-instruct")
TRIAGE_TIMEOUT = float(os.environ.get("TG_TASKS_TRIAGE_TIMEOUT", "180"))

# Прев'ю повідомлення для триажу. 400 символів вистачає, щоб побачити
# домовленість; документ на 88 тис. символів показується головою і відсівається
# як документ, чим він і є.
TRIAGE_PREVIEW = int(os.environ.get("TG_TASKS_TRIAGE_PREVIEW", "400"))
TRIAGE_MAX_MSGS = int(os.environ.get("TG_TASKS_TRIAGE_MAX_MSGS", "40"))

# --- Витяг (Claude) ------------------------------------------------------
TASKS_MODEL = os.environ.get("TG_TASKS_MODEL") or None
TASKS_EFFORT = os.environ.get("TG_TASKS_EFFORT", "low")
# Витягу потрібно більше тексту, ніж триажу (у прев'ю на 400 символів строк
# може не влізти), але не весь документ: стеля на повідомлення + на нитку.
TASKS_PREVIEW = int(os.environ.get("TG_TASKS_PREVIEW", "1500"))
TASKS_MAX_CHARS = int(os.environ.get("TG_TASKS_MAX_CHARS", "24000"))

SOURCE = "tg_thread"

_TRIAGE_SYSTEM = (
    "Ти читаєш фрагмент робочої переписки в месенджері. Визнач, чи є в ньому "
    "зобовʼязання: хтось бере на себе конкретну дію, доручає конкретну дію "
    "іншій людині, або сторони узгоджують строк виконання конкретної дії. "
    "Обмін думками, новини, пересилання матеріалів, привітання, обговорення "
    "без домовленості про дію — це НЕ зобовʼязання. "
    # Мову задаємо явно: 7B зсувається на російську в україномовних чатах
    # (спіймано у Волні 4.5.2).
    "Пояснення пиши УКРАЇНСЬКОЮ. Відповідай лише JSON."
)

_TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "has_commitment": {"type": "boolean"},
        "evidence": {"type": "string"},
    },
    "required": ["has_commitment", "evidence"],
}

VERDICT_YES = "commitment"
VERDICT_NO = "none"


# ============================================================
# Читання ниток
# ============================================================

_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# Рядок «null» проходив як справжня роль у Волні 4.5.2 (18 учасників отримали
# роль "null"). Перевірки на непустоту мало — потрібен фільтр заглушок.
_PLACEHOLDERS = {"null", "none", "n/a", "na", "-", "—", "невідомо", "unknown", "?"}


def _clean_owner(value: Any) -> Optional[str]:
    s = str(value or "").strip()
    return s if s and s.lower() not in _PLACEHOLDERS else None


def _preview(text: Optional[str], limit: int) -> str:
    s = " ".join((text or "").split())
    return s if len(s) <= limit else s[:limit] + " …"

def thread_messages(conn, thread_id: int, *, limit: int = TRIAGE_MAX_MSGS) -> list:
    """Повідомлення нитки у хронологічному порядку (перші `limit`)."""
    return conn.execute(
        "SELECT id, tg_sender, tg_date, tg_link, transcript_text "
        "FROM transcriptions WHERE tg_thread_id = ? AND deleted_at IS NULL "
        "ORDER BY tg_date, id LIMIT ?",
        (thread_id, limit),
    ).fetchall()


def _thread_head(conn, thread_id: int) -> Optional[dict]:
    row = conn.execute(
        "SELECT th.id, th.label, th.chat_id, th.msg_count, th.triage, th.triage_msgs, "
        "th.tasks_at, (SELECT tg_chat_title FROM transcriptions "
        "  WHERE tg_chat_id = th.chat_id AND tg_chat_title IS NOT NULL LIMIT 1) AS chat_title "
        "FROM tg_threads th WHERE th.id = ?",
        (thread_id,),
    ).fetchone()
    return dict(row) if row else None


def _triage_text(messages: list) -> str:
    lines = []
    for m in messages:
        text = _preview(m["transcript_text"], TRIAGE_PREVIEW)
        if not text:
            continue
        lines.append(f"{m['tg_sender'] or '?'}: {text}")
    return "\n".join(lines)


# ============================================================
# Крок 1: триаж локальною моделлю
# ============================================================

def triage_candidates(db_path: str, *, chat_id: Optional[int] = None,
                      force: bool = False, limit: Optional[int] = None) -> list[int]:
    """Нитки, яким потрібен вердикт: ще не триажені або підросли після вердикту.

    Водяний знак — `triage_msgs`: нитка, що не змінилась, не витрачає модель.
    """
    sql = ("SELECT id FROM tg_threads WHERE msg_count > 0 "
           "AND (? OR triage IS NULL OR triage_msgs IS NULL OR triage_msgs < msg_count)")
    params: list = [1 if force else 0]
    if chat_id is not None:
        sql += " AND chat_id = ?"
        params.append(chat_id)
    sql += " ORDER BY last_date DESC, id DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    with get_db_connection(db_path) as conn:
        return [r["id"] for r in conn.execute(sql, params).fetchall()]


def triage_thread(db_path: str, thread_id: int, *, model: Optional[str] = None) -> dict:
    """Вердикт по одній нитці. Повертає {'thread_id','verdict','evidence'}."""
    from app.services import local_llm

    with get_db_connection(db_path) as conn:
        head = _thread_head(conn, thread_id)
        if not head:
            return {"thread_id": thread_id, "status": "not_found"}
        messages = thread_messages(conn, thread_id)

    text = _triage_text(messages)
    if not text.strip():
        return {"thread_id": thread_id, "status": "empty"}

    used = model or TRIAGE_MODEL
    try:
        data = local_llm.generate_json(
            f"Фрагмент переписки (нитка «{head.get('label') or '?'}»):\n\n{text}",
            schema=_TRIAGE_SCHEMA, system=_TRIAGE_SYSTEM, model=used,
            max_tokens=200, timeout=TRIAGE_TIMEOUT,
        )["data"]
    except Exception as e:
        # М'яка деградація: одна нитка без вердикту не валить прохід. Вердикт
        # НЕ пишемо — нитка лишається кандидатом на наступний раз.
        logger.warning("[tg_tasks] триаж нитки %s не вдався: %s", thread_id, e)
        return {"thread_id": thread_id, "status": "failed", "error": str(e)}

    verdict = VERDICT_YES if data.get("has_commitment") else VERDICT_NO
    with get_db_connection(db_path) as conn:
        # Нитка, що підросла після витягу, має бути порахована заново: нові
        # повідомлення — це нові домовленості (або скасування старих). SQLite
        # рахує праву частину SET по СТАРОМУ рядку, тож `triage_msgs` тут ще
        # старий, а `tasks_at` скидається лише коли нитка справді виросла.
        conn.execute(
            "UPDATE tg_threads SET triage = ?, triage_at = CURRENT_TIMESTAMP, "
            "triage_model = ?, "
            "tasks_at = CASE WHEN triage_msgs IS NULL OR triage_msgs < msg_count "
            "                THEN NULL ELSE tasks_at END, "
            "triage_msgs = msg_count WHERE id = ?",
            (verdict, used, thread_id))
        conn.commit()
    return {"thread_id": thread_id, "status": "ok", "verdict": verdict,
            "evidence": str(data.get("evidence") or "")[:300]}


def triage_all(db_path: str, *, dry_run: bool = True, chat_id: Optional[int] = None,
               force: bool = False, limit: Optional[int] = None,
               model: Optional[str] = None,
               progress_cb: Optional[Any] = None) -> dict:
    ids = triage_candidates(db_path, chat_id=chat_id, force=force, limit=limit)
    if dry_run:
        return {"dry_run": True, "candidates": len(ids)}

    from app.services import local_llm
    ok, reason = local_llm.availability(force=True)
    if not ok:
        # Без Ollama триаж неможливий, але це не помилка користувача: решта
        # архіву (пошук, картки дзвінків) працює як працювала.
        logger.warning("[tg_tasks] триаж пропущено: %s", reason)
        return {"skipped": True, "reason": reason, "candidates": len(ids)}

    stat = {"triaged": 0, "commitment": 0, "none": 0, "failed": 0, "candidates": len(ids)}
    for i, tid in enumerate(ids, 1):
        res = triage_thread(db_path, tid, model=model)
        if res.get("status") != "ok":
            stat["failed"] += 1
        else:
            stat["triaged"] += 1
            stat["commitment" if res["verdict"] == VERDICT_YES else "none"] += 1
        if progress_cb:
            progress_cb({"done": i, "total": len(ids), **stat})
    return stat


# ============================================================
# Крок 2: витяг задач (Claude) по відібраних нитках
# ============================================================

def extract_candidates(db_path: str, *, chat_id: Optional[int] = None,
                       force: bool = False, limit: Optional[int] = None) -> list[int]:
    """Нитки з вердиктом «є зобовʼязання», яким ще не рахували задачі."""
    sql = f"SELECT id FROM tg_threads WHERE triage = '{VERDICT_YES}' AND (? OR tasks_at IS NULL)"
    params: list = [1 if force else 0]
    if chat_id is not None:
        sql += " AND chat_id = ?"
        params.append(chat_id)
    sql += " ORDER BY last_date DESC, id DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    with get_db_connection(db_path) as conn:
        return [r["id"] for r in conn.execute(sql, params).fetchall()]


def _tasks_payload(messages: list) -> list[dict]:
    """Повідомлення → нумерований вхід для Claude (з датою для розрахунку строків)."""
    out, total = [], 0
    for n, m in enumerate(messages, 1):
        text = _preview(m["transcript_text"], TASKS_PREVIEW)
        if not text:
            continue
        out.append({"n": n, "sender": m["tg_sender"] or "?",
                    "date": str(m["tg_date"] or "")[:10], "text": text})
        total += len(text)
        if total >= TASKS_MAX_CHARS:
            break
    return out


def extract_thread(db_path: str, thread_id: int, *, model: Optional[str] = None,
                   effort: str = TASKS_EFFORT) -> dict:
    """Витягти задачі з однієї нитки і записати їх у `action_items`."""
    from app.services import text_polishing

    with get_db_connection(db_path) as conn:
        head = _thread_head(conn, thread_id)
        if not head:
            return {"thread_id": thread_id, "status": "not_found"}
        messages = thread_messages(conn, thread_id, limit=TRIAGE_MAX_MSGS)

    payload = _tasks_payload(messages)
    if not payload:
        return {"thread_id": thread_id, "status": "empty"}

    try:
        res = text_polishing.extract_chat_tasks(
            payload, chat_title=head.get("chat_title"), thread_label=head.get("label"),
            model=model or TASKS_MODEL, effort=effort,
        )
    except Exception as e:
        # Як у _enrich_card: збій одного витягу не валить прохід, нитка
        # лишається без `tasks_at` і потрапить у наступний.
        logger.warning("[tg_tasks] витяг нитки %s не вдався: %s", thread_id, e)
        return {"thread_id": thread_id, "status": "retry_needed", "error": str(e)}

    # Нумерація в промпті — це enumerate по `messages`, тож зворотний
    # перерахунок номера в id архіву має йти рівно тією ж послідовністю.
    id_by_n = {n: m["id"] for n, m in enumerate(messages, 1)}
    # Куди чіпляти задачу, якщо модель не вказала повідомлення: останнє в нитці —
    # там, де розмова стоїть зараз, а не там, де вона почалась.
    fallback_id = messages[-1]["id"] if messages else None
    date_by_id = {m["id"]: str(m["tg_date"] or "")[:10] for m in messages}

    saved = 0
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        # Ідемпотентність: стираємо ЛИШЕ свої рядки цієї нитки. Задачі, що
        # прийшли з Claude-картки дзвінка (source IS NULL), не чіпаємо — вони
        # не наші, і 152 таких рядки в архіві вже є.
        c.execute(
            "DELETE FROM action_items WHERE source = ? AND transcription_id IN "
            "(SELECT id FROM transcriptions WHERE tg_thread_id = ?)",
            (SOURCE, thread_id))

        for t in res.get("tasks", []):
            task = str(t.get("task") or "").strip()
            if not task:
                continue
            n = t.get("msg")
            tx_id = id_by_n.get(n) if isinstance(n, int) else None
            if tx_id is None:
                tx_id = fallback_id
            if tx_id is None:
                continue
            owner = _clean_owner(t.get("owner"))
            raw_due = t.get("due")
            raw_due = str(raw_due).strip() if raw_due else None
            anchor = date_by_id.get(tx_id)
            due_date, due_prec = commitments.parse_due(raw_due, anchor)
            model_due = t.get("due_date")
            if isinstance(model_due, str) and _ISO_RE.fullmatch(model_due.strip()):
                due_date = model_due.strip()
                due_prec = due_prec or commitments.P_DAY
            c.execute(
                "INSERT INTO action_items (transcription_id, task, owner_name, "
                "due, due_date, due_precision, status, source) "
                "VALUES (?, ?, ?, ?, ?, ?, 'open', ?)",
                (tx_id, task, owner, raw_due, due_date, due_prec, SOURCE))
            saved += 1

        c.execute("UPDATE tg_threads SET tasks_at = CURRENT_TIMESTAMP, tasks_model = ? "
                  "WHERE id = ?", (res.get("model") or "", thread_id))
        conn.commit()

    logger.info("[tg_tasks] нитка %s: задач=%d (in=%d out=%d)", thread_id, saved,
                res.get("input_tokens", 0), res.get("output_tokens", 0))
    return {"thread_id": thread_id, "status": "ok", "tasks": saved,
            "input_tokens": res.get("input_tokens", 0),
            "output_tokens": res.get("output_tokens", 0)}


def extract_all(db_path: str, *, dry_run: bool = True, chat_id: Optional[int] = None,
                force: bool = False, limit: Optional[int] = None,
                model: Optional[str] = None, effort: str = TASKS_EFFORT,
                progress_cb: Optional[Any] = None) -> dict:
    ids = extract_candidates(db_path, chat_id=chat_id, force=force, limit=limit)
    if dry_run:
        return {"dry_run": True, "candidates": len(ids)}

    from app.services import text_polishing
    if not text_polishing.is_available():
        logger.warning("[tg_tasks] витяг пропущено: немає ANTHROPIC_API_KEY")
        return {"skipped": True, "reason": "no_api_key", "candidates": len(ids)}

    stat = {"threads": 0, "tasks": 0, "failed": 0, "candidates": len(ids),
            "input_tokens": 0, "output_tokens": 0}
    for i, tid in enumerate(ids, 1):
        res = extract_thread(db_path, tid, model=model, effort=effort)
        if res.get("status") != "ok":
            stat["failed"] += 1
        else:
            stat["threads"] += 1
            stat["tasks"] += res.get("tasks", 0)
            stat["input_tokens"] += res.get("input_tokens", 0)
            stat["output_tokens"] += res.get("output_tokens", 0)
        if progress_cb:
            progress_cb({"done": i, "total": len(ids), **stat})

    # Власника звʼязуємо з графом тим самим проходом, що й для дзвінків
    # (Трек 1): інакше задача з чату не потрапить у зріз по людині.
    stat["owners_linked"] = commitments.link_owners(db_path).get("linked", 0)
    return stat


# ============================================================
# Стан
# ============================================================

def stats(db_path: str) -> dict:
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS threads, "
            "SUM(CASE WHEN triage IS NOT NULL THEN 1 ELSE 0 END) AS triaged, "
            f"SUM(CASE WHEN triage = '{VERDICT_YES}' THEN 1 ELSE 0 END) AS commitment, "
            "SUM(CASE WHEN tasks_at IS NOT NULL THEN 1 ELSE 0 END) AS extracted "
            "FROM tg_threads WHERE msg_count > 0").fetchone()
        tasks = conn.execute(
            "SELECT COUNT(*) AS n, SUM(CASE WHEN due_date IS NOT NULL THEN 1 ELSE 0 END) AS dated, "
            "SUM(CASE WHEN owner_name IS NOT NULL THEN 1 ELSE 0 END) AS owned, "
            "SUM(CASE WHEN owner_entity_id IS NOT NULL THEN 1 ELSE 0 END) AS linked "
            "FROM action_items WHERE source = ?", (SOURCE,)).fetchone()
    out = {k: (row[k] or 0) for k in ("threads", "triaged", "commitment", "extracted")}
    out["stale_triage"] = len(triage_candidates(db_path))
    out["tasks"] = {"total": tasks["n"] or 0, "with_date": tasks["dated"] or 0,
                    "with_owner": tasks["owned"] or 0, "owner_linked": tasks["linked"] or 0}
    return out


# ============================================================
# CLI
# ============================================================

def _print(res: Any) -> int:
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    return 0


def main(argv: Optional[list] = None) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    except ImportError:
        pass

    from config import Config
    default_db = str(Config.BASE_DIR / Config.DATABASE)

    p = argparse.ArgumentParser(prog="tg_tasks",
                                description="Зобовʼязання з переписки (Волна 5.1).")
    p.add_argument("--db", default=default_db)
    sub = p.add_subparsers(dest="command", required=True)

    t = sub.add_parser("triage", help="Локальний вердикт: чи є в нитці домовленість")
    t.add_argument("--dry-run", action="store_true")
    t.add_argument("--chat", type=int, default=None)
    t.add_argument("--force", action="store_true", help="Переоцінити вже триажені")
    t.add_argument("--limit", type=int, default=None)
    t.add_argument("--model", default=None)

    e = sub.add_parser("extract", help="Claude-витяг задач по відібраних нитках")
    e.add_argument("--dry-run", action="store_true")
    e.add_argument("--chat", type=int, default=None)
    e.add_argument("--force", action="store_true", help="Перерахувати вже пораховані")
    e.add_argument("--limit", type=int, default=None)
    e.add_argument("--model", default=None)
    e.add_argument("--effort", default=TASKS_EFFORT, choices=("low", "medium", "high"))

    sub.add_parser("stats", help="Стан триажу і витягу")

    args = p.parse_args(argv)

    def _tick(info: dict) -> None:
        sys.stderr.write(f"\r  {info['done']}/{info['total']}")
        sys.stderr.flush()

    if args.command == "triage":
        res = triage_all(args.db, dry_run=args.dry_run, chat_id=args.chat,
                         force=args.force, limit=args.limit, model=args.model,
                         progress_cb=None if args.dry_run else _tick)
        sys.stderr.write("\n")
        return _print(res)
    if args.command == "extract":
        res = extract_all(args.db, dry_run=args.dry_run, chat_id=args.chat,
                          force=args.force, limit=args.limit, model=args.model,
                          effort=args.effort, progress_cb=None if args.dry_run else _tick)
        sys.stderr.write("\n")
        return _print(res)
    return _print(stats(args.db))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
