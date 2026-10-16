"""Live profile of a Telegram chat (Wave 4.5.2).

The owner's request: «each new message should not be unique — it should
add to the context of its group, constantly widening it».

**What changed after threads (v32).** The original design had the profile guess
topics from the raw message stream. Now topics are open threads, i.e. an already
computed fact. So the profile answers exactly what is NOT derived from
threads and is a property of the chat itself:

- **who is here and what they do**: participants with hard numbers;
- **what hangs unanswered**: questions nobody answered;
- **what was agreed**: decisions scattered across threads.

**Hard numbers are computed by SQL, not the model.** How much a person wrote, when they appeared
and when they last wrote is a fact; an invented number in a living person's profile is worse
than its absence. The model receives them ready-made and writes only a role description and a digest.

**Updates are threshold-based, never per message**: otherwise these are exactly the
3142 calls that Wave 0 put a guard against (local now, but just as
pointless). Threshold: `TG_CONTEXT_MIN_NEW` new messages OR
`TG_CONTEXT_MAX_AGE_H` hours since the last update.

No Ollama: the profile is still built, but only from the hard part
(participants, open threads); the text fields stay empty, and that is visible.

CLI:
    python -m app.services.tg_chat_context refresh --dry-run
    python -m app.services.tg_chat_context refresh --chat -1001234567890 --force
    python -m app.services.tg_chat_context show --chat -1001234567890
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from app.db.connection import get_db_connection

logger = logging.getLogger(__name__)


# Update threshold. 30 messages is roughly 6 bursts by the median,
# i.e. the profile is not rewritten on every «ok» reply, but does not lag
# by weeks either. 24 hours so that a silent chat also gets updated sometime.
MIN_NEW_MESSAGES = int(os.environ.get("TG_CONTEXT_MIN_NEW", "30"))
MAX_AGE_HOURS = int(os.environ.get("TG_CONTEXT_MAX_AGE_H", "24"))

# How many recent threads to show the model. The profile is about the current state of affairs,
# not a chronicle: last year's threads only blur the digest.
RECENT_THREADS = int(os.environ.get("TG_CONTEXT_THREADS", "12"))

# How many thread messages to give the model for analysis. A thread already has a name —
# the sample is needed only to see how the conversation ended (an unanswered
# question, an agreement). 12 threads x 8 messages x 300 characters gave a
# prompt on which the 32B did not finish even in 300 seconds, so the largest chat —
# the only one the dossier is needed for — consistently degraded.
THREAD_SAMPLE = int(os.environ.get("TG_CONTEXT_THREAD_SAMPLE", "4"))
_MSG_PREVIEW = 180

CONTEXT_MODEL = os.environ.get("TG_CONTEXT_MODEL") or None

# The dossier is a batch task (22 chats, once a day), not real time, so the default
# 60 seconds of local_llm is too little here: a prompt of 12 threads x 8 messages on the 32B
# consistently ran out of time, and every call fell into degradation.
CONTEXT_TIMEOUT = float(os.environ.get("TG_CONTEXT_TIMEOUT", "300"))

_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "roles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "role": {"type": "string"}},
                "required": ["name", "role"],
            },
        },
        "open_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"question": {"type": "string"},
                               "asked_by": {"type": "string"},
                               "thread_id": {"type": "integer"}},
                "required": ["question"],
            },
        },
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"decision": {"type": "string"},
                               "thread_id": {"type": "integer"}},
                "required": ["decision"],
            },
        },
    },
    "required": ["summary"],
}

_SYSTEM = (
    "Ти ведеш робоче досьє чату. Пиши стисло і лише те, що прямо випливає з "
    "повідомлень: нічого не додумуй про людей і не приписуй їм намірів. "
    "Відкрите питання — те, на яке в наведених повідомленнях НІХТО не відповів. "
    "Рішення — те, про що явно домовились. "
    # The language is set explicitly: on the first run the 7B drifted to Russian in
    # Ukrainian-language chats, and the dossier came out in a different language than the archive itself.
    "ВСЕ пиши УКРАЇНСЬКОЮ, навіть якщо в повідомленнях є інші мови. "
    # The role «takes part in discussions» carries no information — it describes the very
    # fact of presence in the chat, which is visible from the message count anyway.
    #
    # We deliberately give no example wording here: the first version had
    # «напр. веде юридичну частину», and the 7B started putting exactly this phrase on different
    # people in different chats. Attributing someone else's area of responsibility to a living person is
    # worse than leaving the field empty, so we describe the criterion and give no sample
    # to copy.
    "Роль має називати конкретну зону відповідальності цієї людини, видну з "
    "її ВЛАСНИХ повідомлень, а не факт участі в чаті. Бери формулювання з "
    "того, що вона реально робить у наведених повідомленнях. Якщо зона не "
    "видна — став null, порожня роль краща за здогадку. "
    "Відповідай лише JSON."
)


# ============================================================
# Hard part: computed by SQL
# ============================================================

def participants(db_path: str, chat_id: int) -> list[dict]:
    """Who writes in this chat — with numbers that can be trusted."""
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT tg_sender AS name, COUNT(*) AS messages, "
            "MIN(tg_date) AS first_seen, MAX(tg_date) AS last_seen "
            "FROM transcriptions WHERE source_type = 'telegram' AND tg_chat_id = ? "
            "AND deleted_at IS NULL AND tg_sender IS NOT NULL "
            "GROUP BY tg_sender ORDER BY messages DESC", (chat_id,)).fetchall()
    return [dict(r) for r in rows]


def open_threads(db_path: str, chat_id: int, limit: int = RECENT_THREADS) -> list[dict]:
    """The chat's current topics are open threads, not a model guess."""
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT id AS thread_id, label, msg_count, first_date, last_date "
            "FROM tg_threads WHERE chat_id = ? AND status = 'open' AND msg_count > 0 "
            "ORDER BY last_date DESC LIMIT ?", (chat_id, limit)).fetchall()
    return [dict(r) for r in rows]


def _thread_samples(db_path: str, thread_ids: list[int]) -> dict[int, list[dict]]:
    if not thread_ids:
        return {}
    out: dict[int, list[dict]] = {}
    with get_db_connection(db_path) as conn:
        for tid in thread_ids:
            rows = conn.execute(
                "SELECT tg_sender, tg_date, transcript_text FROM transcriptions "
                "WHERE tg_thread_id = ? AND deleted_at IS NULL ORDER BY tg_date LIMIT ?",
                (tid, THREAD_SAMPLE)).fetchall()
            out[tid] = [dict(r) for r in rows]
    return out


def _watermark(db_path: str, chat_id: int) -> tuple[int, Optional[str]]:
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, MAX(tg_date) AS last FROM transcriptions "
            "WHERE source_type = 'telegram' AND tg_chat_id = ? AND deleted_at IS NULL",
            (chat_id,)).fetchone()
    return row["n"] or 0, row["last"]


# ============================================================
# Model
# ============================================================

def _preview(text: Optional[str]) -> str:
    t = " ".join((text or "").split())
    return t[:_MSG_PREVIEW] if t else "[без тексту]"


def _build_prompt(title: str, people: list[dict], threads: list[dict],
                  samples: dict[int, list[dict]]) -> str:
    lines = [f"Чат: «{title or 'без назви'}»", "", "Учасники (числа вже пораховані):"]
    for p in people[:15]:
        lines.append(f"  {p['name']} — {p['messages']} повідомлень, "
                     f"{(p['first_seen'] or '')[:10]} … {(p['last_seen'] or '')[:10]}")
    lines += ["", "Поточні теми (нитки розмов):"]
    for t in threads:
        lines.append(f"\nT{t['thread_id']}: {t['label'] or 'без назви'} "
                     f"({t['msg_count']} повідомлень, останнє {(t['last_date'] or '')[:10]})")
        for m in samples.get(t["thread_id"], []):
            lines.append(f"    [{m['tg_sender'] or 'невідомо'}] {_preview(m['transcript_text'])}")
    lines += [
        "",
        "Склади досьє чату:",
        "  summary — про що цей чат, 2-3 речення;",
        "  roles — для кожного учасника чим він тут займається (лише з видимого);",
        "  open_questions — питання, на які в наведеному НІХТО не відповів "
        "(вкажи thread_id);",
        "  decisions — про що явно домовились (вкажи thread_id).",
        "Якщо чогось у повідомленнях немає — віддай порожній список, не вигадуй.",
    ]
    return "\n".join(lines)


def _ask_model(title: str, people: list[dict], threads: list[dict],
               samples: dict[int, list[dict]], model: Optional[str]) -> Optional[dict]:
    from app.services import local_llm

    try:
        resp = local_llm.generate_json(
            _build_prompt(title, people, threads, samples), schema=_SCHEMA,
            system=_SYSTEM, max_tokens=1200, model=model or CONTEXT_MODEL,
            timeout=CONTEXT_TIMEOUT)
    except Exception as exc:
        logger.warning("[tg_context] model did not respond (%s) — keeping the hard part", exc)
        return None
    data = resp.get("data")
    return data if isinstance(data, dict) else None


def _effective_model(model: Optional[str]) -> str:
    """Name of the model that actually generated the text.

    Not `model or CONTEXT_MODEL`: both are empty in the most common case (nothing
    was overridden), and then the column wrote NULL — i.e. «what generated it» was
    not stored exactly when everything worked. And NULL here must mean exactly
    one thing: there is no text at all."""
    from app.services import local_llm
    return model or CONTEXT_MODEL or local_llm.LOCAL_LLM_MODEL


#: What the model writes when it means «nothing». Asking it to put null and
#: checking only non-emptiness is not enough: on a live run 18 participants
#: got the role string "null", and in the dossier it looked like a real role.
_PLACEHOLDERS = {"null", "none", "n/a", "na", "-", "—", "невідомо", "unknown",
                 "немає", "нема", "не вказано", "не видно"}


def _clean_role(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text or text.casefold() in _PLACEHOLDERS:
        return None
    return text[:200]


def _valid_thread_ids(threads: list[dict]) -> set[int]:
    return {t["thread_id"] for t in threads}


def _clean_items(items: Any, key: str, valid: set[int]) -> list[dict]:
    """Drop invented thread_ids and empty strings.

    The model readily cites threads that were not shown; such a citation
    leads nowhere and is worse than its absence."""
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        text = (it.get(key) or "").strip()
        if not text:
            continue
        entry = {key: text}
        tid = it.get("thread_id")
        if isinstance(tid, int) and tid in valid:
            entry["thread_id"] = tid
        if it.get("asked_by"):
            entry["asked_by"] = str(it["asked_by"]).strip()[:80]
        out.append(entry)
    return out


# ============================================================
# Update
# ============================================================

def needs_refresh(db_path: str, chat_id: int, *, now: Optional[datetime] = None) -> bool:
    """Whether it is time to rebuild the profile. A threshold, not every message."""
    total, _ = _watermark(db_path, chat_id)
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT messages_seen, updated_at FROM tg_chat_context WHERE chat_id = ?",
            (chat_id,)).fetchone()
    if row is None:
        return total > 0
    if total - (row["messages_seen"] or 0) >= MIN_NEW_MESSAGES:
        return True
    updated = row["updated_at"]
    if not updated:
        return True
    try:
        stamp = datetime.fromisoformat(str(updated).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return True
    return ((now or datetime.now()) - stamp) >= timedelta(hours=MAX_AGE_HOURS)


def refresh_chat(db_path: str, chat_id: int, *, model: Optional[str] = None,
                 use_llm: bool = True) -> dict:
    """Rebuild the profile of one chat."""
    people = participants(db_path, chat_id)
    threads = open_threads(db_path, chat_id)
    total, last_date = _watermark(db_path, chat_id)
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT tg_chat_title FROM transcriptions WHERE tg_chat_id = ? "
            "AND tg_chat_title IS NOT NULL LIMIT 1", (chat_id,)).fetchone()
    title = row["tg_chat_title"] if row else None

    data = None
    if use_llm and threads:
        samples = _thread_samples(db_path, [t["thread_id"] for t in threads])
        data = _ask_model(title, people, threads, samples, model)

    valid = _valid_thread_ids(threads)
    summary = None
    roles: dict[str, str] = {}
    questions: list[dict] = []
    decisions: list[dict] = []
    previous = get_context(db_path, chat_id)
    if data is None and previous:
        # The model did not respond — that is NOT a reason to erase an already assembled dossier.
        # Caught on a live run: one 32B timeout turned a normal
        # profile into an empty one, because the upsert wrote None over it. The hard part
        # (participants, threads) and the watermark are updated — they do not depend on the
        # model; the text part keeps the previous one, marked stale.
        summary = previous.get("summary")
        roles = {p["name"]: p["role"] for p in (previous.get("participants") or [])
                 if p.get("role")}
        questions = previous.get("open_questions") or []
        decisions = previous.get("decisions") or []
    if data:
        summary = (data.get("summary") or "").strip() or None
        for r in data.get("roles") or []:
            if not isinstance(r, dict) or not r.get("name"):
                continue
            role = _clean_role(r.get("role"))
            if role:
                roles[str(r["name"]).strip()] = role
        questions = _clean_items(data.get("open_questions"), "question", valid)
        decisions = _clean_items(data.get("decisions"), "decision", valid)

    # The role is attached to the participant's HARD row: the numbers stay from SQL,
    # only the description comes from the model.
    enriched = [{**p, "role": roles.get(p["name"])} for p in people]

    with get_db_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO tg_chat_context (chat_id, summary, participants_json, "
            "topics_json, open_questions_json, decisions_json, model, messages_seen, "
            "last_message_date, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(chat_id) DO UPDATE SET summary=excluded.summary, "
            "participants_json=excluded.participants_json, topics_json=excluded.topics_json, "
            "open_questions_json=excluded.open_questions_json, "
            "decisions_json=excluded.decisions_json, model=excluded.model, "
            "messages_seen=excluded.messages_seen, "
            "last_message_date=excluded.last_message_date, updated_at=CURRENT_TIMESTAMP",
            (chat_id, summary,
             json.dumps(enriched, ensure_ascii=False),
             json.dumps(threads, ensure_ascii=False),
             json.dumps(questions, ensure_ascii=False),
             json.dumps(decisions, ensure_ascii=False),
             # model=None means «the text part could not be assembled». When
             # the previous one is kept, its model is kept too — otherwise the record would say
             # there is no text, when there is (just not fresh).
             _effective_model(model) if data else (previous or {}).get("model"),
             total, last_date))
        conn.commit()

    return {"chat_id": chat_id, "title": title, "participants": len(enriched),
            "threads": len(threads), "open_questions": len(questions),
            "decisions": len(decisions), "summary": bool(summary),
            "degraded": data is None}


def refresh_all(db_path: str, *, dry_run: bool = True, force: bool = False,
                chat_id: Optional[int] = None, model: Optional[str] = None,
                use_llm: bool = True) -> dict:
    with get_db_connection(db_path) as conn:
        if chat_id is not None:
            chats = [chat_id]
        else:
            chats = [r[0] for r in conn.execute(
                "SELECT DISTINCT tg_chat_id FROM transcriptions WHERE "
                "source_type = 'telegram' AND tg_chat_id IS NOT NULL "
                "AND deleted_at IS NULL").fetchall()]

    due = [c for c in chats if force or needs_refresh(db_path, c)]
    if dry_run:
        return {"dry_run": True, "chats": len(chats), "due": len(due), "chat_ids": due}

    results = [refresh_chat(db_path, c, model=model, use_llm=use_llm) for c in due]
    return {"dry_run": False, "refreshed": len(results),
            "degraded": sum(1 for r in results if r["degraded"]), "chats": results}


# ============================================================
# Reading
# ============================================================

def get_context(db_path: str, chat_id: int) -> Optional[dict]:
    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM tg_chat_context WHERE chat_id = ?",
                           (chat_id,)).fetchone()
    if not row:
        return None
    out = dict(row)
    for field in ("participants_json", "topics_json", "open_questions_json",
                  "decisions_json"):
        raw = out.pop(field, None)
        key = field[:-5]
        try:
            out[key] = json.loads(raw) if raw else []
        except (ValueError, TypeError):
            out[key] = []
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

    p = argparse.ArgumentParser(prog="tg_chat_context",
                                description="Live profile of a Telegram chat (Wave 4.5.2).")
    p.add_argument("--db", default=default_db)
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("refresh", help="Rebuild chat profiles by threshold")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--chat", type=int, default=None)
    r.add_argument("--force", action="store_true", help="Ignore the threshold")
    r.add_argument("--model", default=None)
    r.add_argument("--no-llm", action="store_true", help="Hard part only")

    s = sub.add_parser("show", help="Show a chat profile")
    s.add_argument("--chat", type=int, required=True)

    args = p.parse_args(argv)
    if args.command == "refresh":
        return _print(refresh_all(args.db, dry_run=args.dry_run, force=args.force,
                                  chat_id=args.chat, model=args.model,
                                  use_llm=not args.no_llm))
    return _print(get_context(args.db, args.chat) or {"error": "no profile yet"})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
