"""Commitments from conversations (Wave 5.1).

Wave 0 put up a guard: we do not count a Claude card for `source_type='telegram'` —
on a one-liner «Ок» it is useless, and on live data it is also paid. But tasks in
conversations are really handed out, just not in a single message: «надішлю до
пʼятниці» is a reply to a request voiced three messages above.
After threads (Wave 4.5) a unit of meaning exists, so the guard is lifted selectively.

**Cascade.** A local triage (7B, $0, ~4 s per thread) filters out threads without
agreements; Claude extracts structure only from the selected ones. This is the same
local-first as in the copilot: the cheap model decides «is it worth it», the expensive one «what exactly».

**Why not a regex.** The wave plan said «promise markers + top-8 senders +
length». Measurement: the marker list («треба», «надішлю», «чекаю», …) fires
on some threads— that is not a filter, it is half the archive. On a pilot of 40 threads
the regex and the 7B disagreed in 13 cases, and mostly the regex was not right: «треба»
inside «треба визнати» is not a commitment, while «Давай я зараз уточню» is a
commitment without any marker from the list.

**What the model sees.** A preview of each message, not the thread's continuous text:
the largest thread is 337 thousand characters, and that is one document sent to a chat.
Truncating the continuous text would show the model the first 6 thousand characters of the document
instead of the conversation; per-message previews show the whole shape of the conversation.

**Where the task is written.** Into `action_items` with the `transcription_id` of the
message where the commitment was voiced (the model returns the number) — this gives 5.2
for free: the task has `tg_link`, author and chat, so it shows not only
«what» but also «where to write». `source='tg_thread'` distinguishes these rows from
inherited call cards: a re-run erases only its own.

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
from app.services import commitments, task_proposals

logger = logging.getLogger(__name__)


# --- Triage (local model) ------------------------------------------------
# 7B, not 32B: the Wave 4.5.2 measurement showed the 32B does not fit in 24 GB next to
# e5 and the reranker and goes to CPU (one chat > 300 s versus 8 s for the 7B). Triage is
# a yes/no decision, the cheapest task of all that exist here.
TRIAGE_MODEL = os.environ.get("TG_TASKS_TRIAGE_MODEL", "qwen2.5:7b-instruct")
TRIAGE_TIMEOUT = float(os.environ.get("TG_TASKS_TRIAGE_TIMEOUT", "180"))

# Message preview for triage. 400 characters is enough to see
# an agreement; an 88-thousand-character document is shown by its head and filtered out
# as a document, which is what it is.
TRIAGE_PREVIEW = int(os.environ.get("TG_TASKS_TRIAGE_PREVIEW", "400"))
TRIAGE_MAX_MSGS = int(os.environ.get("TG_TASKS_TRIAGE_MAX_MSGS", "40"))

# --- Extraction (Claude) -------------------------------------------------
TASKS_MODEL = os.environ.get("TG_TASKS_MODEL") or None
TASKS_EFFORT = os.environ.get("TG_TASKS_EFFORT", "low")
# Extraction needs more text than triage (in a 400-character preview the deadline
# may not fit), but not the whole document: a cap per message + per thread.
TASKS_PREVIEW = int(os.environ.get("TG_TASKS_PREVIEW", "1500"))
TASKS_MAX_CHARS = int(os.environ.get("TG_TASKS_MAX_CHARS", "24000"))

SOURCE = "tg_thread"

_TRIAGE_SYSTEM = (
    "Ти читаєш фрагмент робочої переписки в месенджері. Визнач, чи є в ньому "
    "зобовʼязання: хтось бере на себе конкретну дію, доручає конкретну дію "
    "іншій людині, або сторони узгоджують строк виконання конкретної дії. "
    "Обмін думками, новини, пересилання матеріалів, привітання, обговорення "
    "без домовленості про дію — це НЕ зобовʼязання. "
    # The language is set explicitly: the 7B drifts to Russian in Ukrainian-language chats
    # (caught in Wave 4.5.2).
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
# Reading threads
# ============================================================

_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# The string «null» passed as a real role in Wave 4.5.2 (18 participants got the
# role "null"). A non-empty check is not enough — a placeholder filter is needed.
_PLACEHOLDERS = {"null", "none", "n/a", "na", "-", "—", "невідомо", "unknown", "?"}


def _clean_owner(value: Any) -> Optional[str]:
    s = str(value or "").strip()
    return s if s and s.lower() not in _PLACEHOLDERS else None


def _preview(text: Optional[str], limit: int) -> str:
    s = " ".join((text or "").split())
    return s if len(s) <= limit else s[:limit] + " …"

def thread_messages(conn, thread_id: int, *, limit: int = TRIAGE_MAX_MSGS) -> list:
    """Thread messages in chronological order (the first `limit`)."""
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
# Step 1: triage by the local model
# ============================================================

def triage_candidates(db_path: str, *, chat_id: Optional[int] = None,
                      force: bool = False, limit: Optional[int] = None) -> list[int]:
    """Threads that need a verdict: not yet triaged or grown since the verdict.

    The watermark is `triage_msgs`: an unchanged thread does not spend the model.
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
    """A verdict for one thread. Returns {'thread_id','verdict','evidence'}."""
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
        # Soft degradation: one thread without a verdict does not fail the pass. We do NOT
        # write a verdict — the thread stays a candidate for next time.
        logger.warning("[tg_tasks] triage of thread %s failed: %s", thread_id, e)
        return {"thread_id": thread_id, "status": "failed", "error": str(e)}

    verdict = VERDICT_YES if data.get("has_commitment") else VERDICT_NO
    with get_db_connection(db_path) as conn:
        # A thread that grew after extraction must be counted anew: new
        # messages are new agreements (or cancellations of old ones). SQLite
        # evaluates the right side of SET against the OLD row, so `triage_msgs` here is still
        # old, and `tasks_at` is reset only when the thread really grew.
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
        # Without Ollama triage is impossible, but this is not a user error: the rest of
        # the archive (search, call cards) works as it did.
        logger.warning("[tg_tasks] triage skipped: %s", reason)
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
# Step 2: task extraction (Claude) over the selected threads
# ============================================================

def extract_candidates(db_path: str, *, chat_id: Optional[int] = None,
                       force: bool = False, limit: Optional[int] = None) -> list[int]:
    """Threads with a «has commitment» verdict whose tasks have not yet been counted."""
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
    """Messages → numbered input for Claude (with a date for computing deadlines)."""
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
    """Extract tasks from one thread and write them to `action_items`."""
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
            owner_names=task_proposals.owner_names(db_path),
        )
    except Exception as e:
        # As in _enrich_card: one failed extraction does not fail the pass, the thread
        # stays without `tasks_at` and gets into the next one.
        logger.warning("[tg_tasks] extraction of thread %s failed: %s", thread_id, e)
        return {"thread_id": thread_id, "status": "retry_needed", "error": str(e)}

    # Numbering in the prompt is an enumerate over `messages`, so the reverse
    # conversion of a number into an archive id must follow exactly the same sequence.
    id_by_n = {n: m["id"] for n, m in enumerate(messages, 1)}
    # Where to attach the task if the model named no message: the last one in the thread —
    # where the conversation is now, not where it started.
    fallback_id = messages[-1]["id"] if messages else None
    date_by_id = {m["id"]: str(m["tg_date"] or "")[:10] for m in messages}

    saved = 0
    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        # Idempotence: we erase ONLY this thread's own rows. Tasks that
        # came from a Claude call card (source IS NULL) are not touched — they
        # are not ours, and 152 such rows are already in the archive.
        # ADR-056: only undecided proposals are erased; a decided row survives the re-run.
        c.execute(
            "DELETE FROM action_items WHERE source = ? AND status = 'proposed' AND "
            "transcription_id IN (SELECT id FROM transcriptions WHERE tg_thread_id = ?)",
            (SOURCE, thread_id))

        kept, dropped = task_proposals.filter_candidates(
            res.get("tasks", []), min_confidence=task_proposals.min_confidence())
        skipped_decided = 0
        keys_by_tx: dict[int, set[str]] = {}
        for t in kept:
            task = str(t.get("task") or "").strip()
            if not task:
                continue
            n = t.get("msg")
            tx_id = id_by_n.get(n) if isinstance(n, int) else None
            if tx_id is None:
                tx_id = fallback_id
            if tx_id is None:
                continue
            if tx_id not in keys_by_tx:
                keys_by_tx[tx_id] = task_proposals.existing_keys(c, tx_id)
            key = task_proposals.text_key(task)
            if key in keys_by_tx[tx_id]:
                skipped_decided += 1
                continue
            keys_by_tx[tx_id].add(key)
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
                "due, due_date, due_precision, status, source, text_key, for_whom, "
                "confidence, importance) "
                "VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?, ?, ?, ?, ?)",
                (tx_id, task, owner, raw_due, due_date, due_prec, SOURCE,
                 key, t["for_whom"], t["confidence"], t["importance"]))
            saved += 1

        c.execute("UPDATE tg_threads SET tasks_at = CURRENT_TIMESTAMP, tasks_model = ? "
                  "WHERE id = ?", (res.get("model") or "", thread_id))
        conn.commit()

    logger.info("[tg_tasks] thread %s: tasks=%d skipped_decided=%d dropped_third_party=%d "
                "dropped_low_confidence=%d (in=%d out=%d)", thread_id, saved, skipped_decided,
                dropped["third_party"], dropped["low_confidence"],
                res.get("input_tokens", 0), res.get("output_tokens", 0))
    return {"thread_id": thread_id, "status": "ok", "tasks": saved,
            "input_tokens": res.get("input_tokens", 0),
            "output_tokens": res.get("output_tokens", 0),
            "skipped_decided": skipped_decided,
            "dropped_third_party": dropped["third_party"],
            "dropped_low_confidence": dropped["low_confidence"]}


def extract_all(db_path: str, *, dry_run: bool = True, chat_id: Optional[int] = None,
                force: bool = False, limit: Optional[int] = None,
                model: Optional[str] = None, effort: str = TASKS_EFFORT,
                progress_cb: Optional[Any] = None) -> dict:
    ids = extract_candidates(db_path, chat_id=chat_id, force=force, limit=limit)
    if dry_run:
        return {"dry_run": True, "candidates": len(ids)}

    from app.services import text_polishing
    if not text_polishing.is_available():
        logger.warning("[tg_tasks] extraction skipped: no ANTHROPIC_API_KEY")
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

    # We link the owner to the graph with the same pass as for calls
    # (Track 1): otherwise a task from a chat would not reach the per-person slice.
    stat["owners_linked"] = commitments.link_owners(db_path).get("linked", 0)
    return stat


# ============================================================
# State
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
                                description="Commitments from conversations (Wave 5.1).")
    p.add_argument("--db", default=default_db)
    sub = p.add_subparsers(dest="command", required=True)

    t = sub.add_parser("triage", help="Local verdict: is there an agreement in the thread")
    t.add_argument("--dry-run", action="store_true")
    t.add_argument("--chat", type=int, default=None)
    t.add_argument("--force", action="store_true", help="Re-evaluate already triaged")
    t.add_argument("--limit", type=int, default=None)
    t.add_argument("--model", default=None)

    e = sub.add_parser("extract", help="Claude extraction of tasks over the selected threads")
    e.add_argument("--dry-run", action="store_true")
    e.add_argument("--chat", type=int, default=None)
    e.add_argument("--force", action="store_true", help="Recount already counted")
    e.add_argument("--limit", type=int, default=None)
    e.add_argument("--model", default=None)
    e.add_argument("--effort", default=TASKS_EFFORT, choices=("low", "medium", "high"))

    sub.add_parser("stats", help="Triage and extraction state")

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
