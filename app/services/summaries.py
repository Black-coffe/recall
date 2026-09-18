"""Сводки одиниць сенсу з провенансом: TG-нитка + `unit_summary_line()` (Волна B).

**Що вже було.** Дзвінок/документ отримує сводку в `transcriptions.summary_json`
через `enrichment.enrich_transcription` (Claude, 1 виклик на запис). TG-нитка —
теж одиниця сенсу (Волна 4.5), але `tg_threads.summary TEXT` (колонка з v32)
ніхто не писав і не читав.

**Що робить цей модуль.**
1. `summarize_thread` — один Claude-виклик на нитку: один абзац ≤600 символів,
   провенанс (id повідомлень-джерел, час, модель, водяний знак `summary_msgs`).
   Нитка з коротким текстом (<300 символів сирого тексту) не варта виклику
   моделі — `summary_model='verbatim'`.
2. `backfill` — офлайн-прохід по нитках без сводки, `--dry-run` рахує обсяг і
   вартість (`pricing.estimate_cost`), нічого не пише.
3. `stats` — покриття сводками трьох типів одиниць сенсу (дзвінки/документи/
   нитки), для контролю "бекфіл до 100%".
4. `unit_summary_line(conn, transcription_id)` — контракт C1 плану Волни B: єдиний
   рядок сводки (≤200 символів, перше речення, без переносів) для БУДЬ-ЯКОГО
   запису — телеграм бере сводку своєї нитки, решта — `summary_json.summary`.
   Споживач — префікс чанка (`embeddings.build_context_prefix`, історія 05).
   **Лише SQL + stdlib** — без torch/anthropic на модульному рівні, щоб
   `embeddings.py` міг імпортувати цю функцію і зі stdio-MCP.

**Що бачить модель.** Той самий підхід, що в `tg_tasks.extract_thread`: превʼю
кожного повідомлення (не суцільний текст нитки — найбільша нитка 337 тис.
символів, суцільний текст показав би моделі перші кілька тисяч символів
одного надісланого документа замість форми розмови), стеля символів промпта.

CLI:
    python -m app.services.summaries backfill --dry-run
    python -m app.services.summaries backfill --model claude-haiku-4-5-20251001 --limit 20
    python -m app.services.summaries stats
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

logger = logging.getLogger(__name__)

# Сирий текст нитки коротший за це — сводка моделі не варта виклику,
# сама нитка вже і є своєю сводкою.
VERBATIM_CHAR_LIMIT = 300
# Стеля довжини сводки, що пишеться в БД (один абзац прози).
SUMMARY_LINE_LIMIT = 600
# Стеля довжини `unit_summary_line()` (контракт C1 — рядок 2 префікса чанка).
UNIT_LINE_LIMIT = 200

# Превʼю на повідомлення в промпті + стеля прсимволів усього промпта. Ті самі
# числа й той самий компроміс, що в tg_tasks.py: превʼю показує форму розмови,
# суцільний текст показав би перші N символів одного вкладеного документа.
SUMMARY_PREVIEW = int(os.environ.get("TG_SUMMARY_PREVIEW", "800"))
SUMMARY_MAX_CHARS = int(os.environ.get("TG_SUMMARY_MAX_CHARS", "20000"))
SUMMARY_MAX_MSGS = int(os.environ.get("TG_SUMMARY_MAX_MSGS", "200"))
SUMMARY_MODEL_ENV = os.environ.get("TG_SUMMARY_MODEL") or None

_SUMMARY_SYSTEM = (
    "Ти читаєш фрагмент робочої переписки в месенджері (нумеровані повідомлення). "
    "Напиши ОДИН абзац прози (не більше 600 символів), що узагальнює суть "
    "розмови: про що йшлося і до чого дійшли. Пиши мовою переписки. Без "
    "вступних фраз («Ця нитка про», «У цій розмові»), без заголовка, без "
    "лапок і списків — лише сам абзац."
)


# ============================================================
# Читання нитки
# ============================================================

def _thread_head(conn, thread_id: int) -> Optional[dict]:
    row = conn.execute(
        "SELECT th.id, th.label, th.chat_id, th.msg_count, "
        "(SELECT tg_chat_title FROM transcriptions "
        " WHERE tg_chat_id = th.chat_id AND tg_chat_title IS NOT NULL LIMIT 1) AS chat_title "
        "FROM tg_threads th WHERE th.id = ?",
        (thread_id,),
    ).fetchone()
    return dict(row) if row else None


def _thread_messages(conn, thread_id: int, *, limit: int = SUMMARY_MAX_MSGS) -> list:
    """Повідомлення нитки в хронологічному порядку (перші `limit`)."""
    return conn.execute(
        "SELECT id, tg_sender, tg_date, transcript_text "
        "FROM transcriptions WHERE tg_thread_id = ? AND deleted_at IS NULL "
        "ORDER BY tg_date, id LIMIT ?",
        (thread_id, limit),
    ).fetchall()


def _preview(text: Optional[str], limit: int) -> str:
    s = " ".join((text or "").split())
    return s if len(s) <= limit else s[:limit] + " …"


def _payload(messages: list) -> list[dict]:
    """Повідомлення → нумерований вхід для Claude, з стелею символів промпта."""
    out, total = [], 0
    for n, m in enumerate(messages, 1):
        text = _preview(m["transcript_text"], SUMMARY_PREVIEW)
        if not text:
            continue
        out.append({"n": n, "sender": m["tg_sender"] or "?",
                    "date": str(m["tg_date"] or "")[:10], "text": text})
        total += len(text)
        if total >= SUMMARY_MAX_CHARS:
            break
    return out


# ============================================================
# Claude-виклик
# ============================================================

def _call_summary(payload: list[dict], head: dict, model: str) -> tuple[str, str]:
    from app.services import text_polishing
    from app.services.claude_retry import call_with_retry
    from app.services import models as _models

    client = text_polishing._get_client()
    lines = []
    for p in payload:
        date = f" · {p['date']}" if p.get("date") else ""
        lines.append(f"[{p['n']}] {p.get('sender') or '?'}{date}: {p['text']}")
    head_line = "Нитка переписки"
    if head.get("label"):
        head_line += f" «{head['label']}»"
    if head.get("chat_title"):
        head_line += f" (чат: {head['chat_title']})"
    user_message = f"{head_line}.\n\n<thread>\n" + "\n".join(lines) + "\n</thread>"

    kwargs = dict(
        model=model,
        max_tokens=400,
        system=[{
            "type": "text",
            "text": _SUMMARY_SYSTEM,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_message}],
    )
    if _models.supports_adaptive_thinking(model):
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": "low"}

    def _do():
        with client.with_options(timeout=180.0).messages.stream(**kwargs) as stream:
            return stream.get_final_message()

    final = call_with_retry(_do, what="tg-thread-summary")
    text = "".join(b.text for b in final.content if b.type == "text").strip()
    return text, final.model


def _default_model() -> str:
    from app.services import models
    return SUMMARY_MODEL_ENV or models.get_default_model()


def _estimate_cost(model: str, tokens_in: int) -> float:
    from app.services import pricing
    return pricing.estimate_cost(model, tokens_in, 0)


def _write_summary(conn, thread_id: int, text: str, model: str,
                   source_ids: list[int], msg_count: int) -> None:
    conn.execute(
        "UPDATE tg_threads SET summary = ?, summary_at = CURRENT_TIMESTAMP, "
        "summary_model = ?, summary_source_ids_json = ?, summary_msgs = ? WHERE id = ?",
        (text, model, json.dumps(source_ids, ensure_ascii=False), msg_count, thread_id))
    conn.commit()


# ============================================================
# Сводка однієї нитки
# ============================================================

def summarize_thread(conn, thread_id: int, *, model: Optional[str] = None,
                     dry_run: bool = False) -> dict:
    """Сводка нитки: один абзац ≤600 символів + провенанс.

    Коротка нитка (<300 символів сирого тексту) не викликає модель:
    `summary = текст`, `summary_model = 'verbatim'`.
    """
    head = _thread_head(conn, thread_id)
    if not head:
        return {"thread_id": thread_id, "status": "not_found"}
    messages = _thread_messages(conn, thread_id)
    texts = [m["transcript_text"] for m in messages if m["transcript_text"]]
    if not texts:
        return {"thread_id": thread_id, "status": "empty"}

    raw_total = sum(len(t) for t in texts)

    if raw_total < VERBATIM_CHAR_LIMIT:
        if dry_run:
            return {"thread_id": thread_id, "status": "dry_run", "model": "verbatim",
                    "chars": raw_total, "tokens_est": 0, "cost_est_usd_input_only": 0.0}
        verbatim_text = " ".join(" ".join(t.split()) for t in texts).strip()[:SUMMARY_LINE_LIMIT]
        all_ids = [m["id"] for m in messages if m["transcript_text"]]
        _write_summary(conn, thread_id, verbatim_text, "verbatim", all_ids, head["msg_count"])
        return {"thread_id": thread_id, "status": "ok", "model": "verbatim", "chars": raw_total}

    payload = _payload(messages)
    if not payload:
        return {"thread_id": thread_id, "status": "empty"}
    prompt_chars = sum(len(p["text"]) for p in payload)
    eff_model = model or _default_model()

    if dry_run:
        tokens_est = prompt_chars // 4
        return {"thread_id": thread_id, "status": "dry_run", "model": eff_model,
                "chars": prompt_chars, "tokens_est": tokens_est,
                "cost_est_usd_input_only": _estimate_cost(eff_model, tokens_est)}

    id_by_n = {n: m["id"] for n, m in enumerate(messages, 1)}
    source_ids = [id_by_n[p["n"]] for p in payload if p["n"] in id_by_n]

    try:
        text, used_model = _call_summary(payload, head, eff_model)
    except Exception as e:
        # М'яка деградація (як tg_tasks.extract_thread): збій одного виклику
        # не валить прохід, нитка лишається без summary → кандидат наступного разу.
        logger.warning("[summaries] нитка %s: збій Claude: %s", thread_id, e)
        return {"thread_id": thread_id, "status": "failed", "error": str(e)}

    text = " ".join(text.split())[:SUMMARY_LINE_LIMIT]
    if not text:
        return {"thread_id": thread_id, "status": "empty"}
    _write_summary(conn, thread_id, text, used_model, source_ids, head["msg_count"])
    return {"thread_id": thread_id, "status": "ok", "model": used_model, "chars": prompt_chars}


# ============================================================
# Бекфіл
# ============================================================

def backfill_candidates(db_path: str, *, force: bool = False,
                        limit: Optional[int] = None) -> list[int]:
    """Нитки без сводки (або всі, якщо force)."""
    sql = "SELECT id FROM tg_threads WHERE msg_count > 0 AND (? OR summary IS NULL)"
    params: list = [1 if force else 0]
    sql += " ORDER BY last_date DESC, id DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    with get_db_connection(db_path) as conn:
        return [r["id"] for r in conn.execute(sql, params).fetchall()]


def backfill(db_path: str, *, model: Optional[str] = None, dry_run: bool = True,
            limit: Optional[int] = None, force: bool = False,
            progress_cb: Optional[Any] = None) -> dict:
    ids = backfill_candidates(db_path, force=force, limit=limit)
    eff_model = model or _default_model()

    if dry_run:
        total_chars = tokens_est = 0
        cost_est = 0.0
        with get_db_connection(db_path) as conn:
            for tid in ids:
                info = summarize_thread(conn, tid, model=eff_model, dry_run=True)
                if info.get("status") == "dry_run":
                    total_chars += info.get("chars", 0)
                    tokens_est += info.get("tokens_est", 0)
                    cost_est += info.get("cost_est_usd_input_only", 0.0)
        return {"dry_run": True, "candidates": len(ids), "model": eff_model,
                "total_chars": total_chars, "tokens_est": tokens_est,
                "cost_est_usd_input_only": round(cost_est, 6)}

    from app.services import text_polishing
    if not text_polishing.is_available():
        # Без ключа бекфіл неможливий, але це не помилка користувача: решта
        # архіву (пошук, картки дзвінків) працює як працювала.
        logger.warning("[summaries] бекфіл пропущено: немає ANTHROPIC_API_KEY")
        return {"skipped": True, "reason": "no_api_key", "candidates": len(ids)}

    stat = {"threads": 0, "verbatim": 0, "failed": 0, "candidates": len(ids)}
    for i, tid in enumerate(ids, 1):
        with get_db_connection(db_path) as conn:
            res = summarize_thread(conn, tid, model=eff_model, dry_run=False)
        if res.get("status") != "ok":
            stat["failed"] += 1
        else:
            stat["threads"] += 1
            if res.get("model") == "verbatim":
                stat["verbatim"] += 1
        if progress_cb:
            progress_cb({"done": i, "total": len(ids), **stat})
    return stat


# ============================================================
# Покриття сводками (дзвінки / документи / нитки)
# ============================================================

_CALL_SOURCE_TYPES = ("file", "youtube", "recording")


def _pack(row) -> dict:
    total = row["total"] or 0
    done = row["with_summary"] or 0
    pct = round(done / total * 100, 1) if total else 0.0
    return {"total": total, "with_summary": done, "pct": pct}


def stats(db_path: str) -> dict:
    """Покриття сводками для трьох типів одиниць сенсу, без видалених і дублів."""
    with get_db_connection(db_path) as conn:
        calls = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN summary_json IS NOT NULL THEN 1 ELSE 0 END) AS with_summary "
            "FROM transcriptions WHERE deleted_at IS NULL AND duplicate_of IS NULL "
            f"AND source_type IN ({','.join('?' * len(_CALL_SOURCE_TYPES))})",
            _CALL_SOURCE_TYPES,
        ).fetchone()
        documents = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN summary_json IS NOT NULL THEN 1 ELSE 0 END) AS with_summary "
            "FROM transcriptions WHERE deleted_at IS NULL AND duplicate_of IS NULL "
            "AND source_type = 'document'"
        ).fetchone()
        threads = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN summary IS NOT NULL THEN 1 ELSE 0 END) AS with_summary "
            "FROM tg_threads WHERE msg_count > 0"
        ).fetchone()
    return {"calls": _pack(calls), "documents": _pack(documents), "threads": _pack(threads)}


# ============================================================
# unit_summary_line — контракт C1 (споживач: префікс чанка, історія 05)
# ============================================================

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")


def _first_sentence(text: Optional[str]) -> Optional[str]:
    flat = " ".join((text or "").split())
    if not flat:
        return None
    sentence = _SENTENCE_SPLIT_RE.split(flat, maxsplit=1)[0].strip()
    return sentence or None


def unit_summary_line(conn, transcription_id: int) -> Optional[str]:
    """Один рядок сводки для будь-якого запису — ≤200 символів, перше речення,
    без переносів; `None`, якщо сводки нема.

    telegram → сводка НИТКИ (`tg_threads.summary` по `tg_thread_id`);
    інакше → `transcriptions.summary_json.summary`.

    Лише SQL — без моделі й torch, безпечно імпортувати зі stdio-MCP
    (`embeddings.py`, історія 05).
    """
    row = conn.execute(
        "SELECT source_type, tg_thread_id, summary_json FROM transcriptions WHERE id = ?",
        (transcription_id,),
    ).fetchone()
    if not row:
        return None

    text: Optional[str] = None
    if row["source_type"] == "telegram" and row["tg_thread_id"] is not None:
        th = conn.execute(
            "SELECT summary FROM tg_threads WHERE id = ?", (row["tg_thread_id"],)
        ).fetchone()
        text = th["summary"] if th else None
    else:
        raw = row["summary_json"]
        if raw:
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                data = None
            if isinstance(data, dict):
                text = data.get("summary")

    sentence = _first_sentence(text)
    if not sentence:
        return None
    return sentence[:UNIT_LINE_LIMIT]


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

    p = argparse.ArgumentParser(prog="summaries",
                                description="Сводки TG-ниток з провенансом (Волна B).")
    p.add_argument("--db", default=default_db)
    # --dry-run має рятувати з будь-якого місця рядка (як у enrichment.py/
    # dedup_audio.py): верхній флаг у окремий dest, фінальне значення — OR обох.
    p.add_argument("--dry-run", dest="dry_run_pre", action="store_true")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true",
                        help="Нічого не писати, лише оцінка обсягу/вартості")
    common.add_argument("--model", default=None,
                        help="Override моделі Claude (дефолт — models.get_default_model())")
    common.add_argument("--limit", type=int, default=None)
    common.add_argument("--force", action="store_true", help="Пересвести вже посумовані нитки")

    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("backfill", parents=[common],
                   help="Сводка Claude для ниток без summary")
    sub.add_parser("stats", help="Покриття сводками: дзвінки/документи/нитки")

    args = p.parse_args(argv)
    args.dry_run = getattr(args, "dry_run", False) or args.dry_run_pre

    def _tick(info: dict) -> None:
        sys.stderr.write(f"\r  {info['done']}/{info['total']}")
        sys.stderr.flush()

    if args.command == "backfill":
        res = backfill(args.db, model=args.model, dry_run=args.dry_run,
                       limit=args.limit, force=args.force,
                       progress_cb=None if args.dry_run else _tick)
        if not args.dry_run:
            sys.stderr.write("\n")
        return _print(res)
    return _print(stats(args.db))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
