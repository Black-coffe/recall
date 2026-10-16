"""Summaries of units of meaning with provenance: TG thread + `unit_summary_line()` (Wave B).

**What already existed.** A call/document gets a summary in `transcriptions.summary_json`
via `enrichment.enrich_transcription` (Claude, 1 call per record). A TG thread is
also a unit of meaning (Wave 4.5), but `tg_threads.summary TEXT` (a column from v32)
was neither written nor read by anyone.

**What this module does.**
1. `summarize_thread` — one Claude call per thread: one paragraph <=600 characters,
   provenance (ids of source messages, time, model, the `summary_msgs` watermark).
   A thread with short text (<300 characters of raw text) is not worth a model
   call — `summary_model='verbatim'`.
2. `backfill` — an offline pass over threads without a summary, `--dry-run` counts the volume and
   cost (`pricing.estimate_cost`) and writes nothing.
3. `stats` — summary coverage of the three kinds of units of meaning (calls/documents/
   threads), to control "backfill to 100%".
4. `unit_summary_line(conn, transcription_id)` — contract C1 of the Wave B plan: a single
   summary line (<=200 characters, first sentence, no line breaks) for ANY
   record — Telegram takes the summary of its thread, the rest `summary_json.summary`.
   The consumer is the chunk prefix (`embeddings.build_context_prefix`, story 05).
   **SQL + stdlib only** — no torch/anthropic at module level, so that
   `embeddings.py` can import this function from the stdio MCP too.

**What the model sees.** The same approach as in `tg_tasks.extract_thread`: a preview of
each message (not the thread's continuous text — the biggest thread is 337 thousand
characters, continuous text would show the model the first few thousand characters of
one forwarded document instead of the shape of the conversation), with a prompt character ceiling.

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

# Raw thread text shorter than this — a model summary is not worth a call,
# the thread itself is already its own summary.
VERBATIM_CHAR_LIMIT = 300
# Ceiling on the length of a summary written to the DB (one paragraph of prose).
SUMMARY_LINE_LIMIT = 600
# Ceiling on the length of `unit_summary_line()` (contract C1 — line 2 of the chunk prefix).
UNIT_LINE_LIMIT = 200

# Preview per message in the prompt + a character ceiling for the whole prompt. The same
# numbers and the same compromise as in tg_tasks.py: the preview shows the shape of the conversation,
# continuous text would show the first N characters of one attached document.
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
# Reading a thread
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
    """Thread messages in chronological order (the first `limit`)."""
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
    """Messages -> numbered input for Claude, with a prompt character ceiling."""
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
# Claude call
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
# Summary of one thread
# ============================================================

def summarize_thread(conn, thread_id: int, *, model: Optional[str] = None,
                     dry_run: bool = False) -> dict:
    """Thread summary: one paragraph <=600 characters + provenance.

    A short thread (<300 characters of raw text) does not call the model:
    `summary = text`, `summary_model = 'verbatim'`.
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
        # Soft degradation (like tg_tasks.extract_thread): a failure of one call
        # does not break the pass, the thread stays without a summary -> a candidate next time.
        logger.warning("[summaries] thread %s: Claude failure: %s", thread_id, e)
        return {"thread_id": thread_id, "status": "failed", "error": str(e)}

    text = " ".join(text.split())[:SUMMARY_LINE_LIMIT]
    if not text:
        return {"thread_id": thread_id, "status": "empty"}
    _write_summary(conn, thread_id, text, used_model, source_ids, head["msg_count"])
    return {"thread_id": thread_id, "status": "ok", "model": used_model, "chars": prompt_chars}


# ============================================================
# Backfill
# ============================================================

def backfill_candidates(db_path: str, *, force: bool = False,
                        limit: Optional[int] = None) -> list[int]:
    """Threads without a summary (or all, if force)."""
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
        # Backfill is impossible without a key, but that is not a user error: the rest of
        # the archive (search, call cards) works as it did.
        logger.warning("[summaries] backfill skipped: no ANTHROPIC_API_KEY")
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
# Summary coverage (calls / documents / threads)
# ============================================================

_CALL_SOURCE_TYPES = ("file", "youtube", "recording")


def _pack(row) -> dict:
    total = row["total"] or 0
    done = row["with_summary"] or 0
    pct = round(done / total * 100, 1) if total else 0.0
    return {"total": total, "with_summary": done, "pct": pct}


def stats(db_path: str) -> dict:
    """Summary coverage for three kinds of units of meaning, excluding deleted and duplicates."""
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
# unit_summary_line — contract C1 (consumer: chunk prefix, story 05)
# ============================================================

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")


def _first_sentence(text: Optional[str]) -> Optional[str]:
    flat = " ".join((text or "").split())
    if not flat:
        return None
    sentence = _SENTENCE_SPLIT_RE.split(flat, maxsplit=1)[0].strip()
    return sentence or None


def unit_summary_line(conn, transcription_id: int) -> Optional[str]:
    """A single summary line for any record — <=200 characters, first sentence,
    no line breaks; `None` if there is no summary.

    telegram -> the THREAD summary (`tg_threads.summary` by `tg_thread_id`);
    otherwise -> `transcriptions.summary_json.summary`.

    SQL only — no model and no torch, safe to import from the stdio MCP
    (`embeddings.py`, story 05).
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
                                description="Summaries of TG threads with provenance (Wave B).")
    p.add_argument("--db", default=default_db)
    # --dry-run must work from anywhere on the line (as in enrichment.py/
    # dedup_audio.py): the top-level flag goes to a separate dest, the final value is the OR of both.
    p.add_argument("--dry-run", dest="dry_run_pre", action="store_true")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true",
                        help="Write nothing, only estimate volume/cost")
    common.add_argument("--model", default=None,
                        help="Override the Claude model (default — models.get_default_model())")
    common.add_argument("--limit", type=int, default=None)
    common.add_argument("--force", action="store_true", help="Re-summarize already summarized threads")

    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("backfill", parents=[common],
                   help="Claude summary for threads without a summary")
    sub.add_parser("stats", help="Summary coverage: calls/documents/threads")

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
