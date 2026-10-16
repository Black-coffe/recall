"""Conversation threads inside a Telegram chat (Wave 4.5).

**Problem.** A chat of messages is 955 unconnected records, and «Ок»
physically carries no context. A live failure: for «коли зустріч з губернатором» search
returned 7 slots of 8, and all seven were questions without any answer. The answer was
the next message of the thread, shares no words with the query, so neither the vector nor
FTS found it. The mirror problem with labels: the category comes from the whole chat, so
any message from «ACMECORP&Робота» lands in the Acmecorp slice,
even if it is about a vacation.

**The unit of meaning is a thread inside a chat**, not the chat and not a single message.

Why exactly this cascade (each step stands on a measurement, not a guess):

- **`tg_reply_to` is a hard override, but almost empty.** This is what Telegram ITSELF
  knows about the thread, a mistake here is impossible. The column is forward-only
  (appeared in Wave 4, 03.08), so in the live archive it covers some records— 0.6%. It keeps growing, but history cannot be stitched with it.

- **The e5 cosine does NOT decide.** Measured on the archive: reply pairs (a real
  thread) give a mean cosine of 0.828, random pairs in the same chat 0.827.
  Separation +0.02 sigma, i.e. zero. Centering (subtracting the corpus mean) raises it
  to +0.61 sigma — still about a third of errors. The cause is visible in the pairs themselves:
  «Дзвінка не буде?» <- «ні, нічого Адам не писав» is a real thread with cosine
  0.837, like a random pair. **A short reply has nothing to embed as a topic:**
  «ні» is about the speech register, not the subject. So the centroid here is only a
  RANKER of candidates for the LLM, and there is no absolute threshold in the code.

- **The decision is made by the local LLM, but not for every message.** Messages
  are clustered into bursts: with a 3 h pause the archive collapses to 569 bursts
  (median 4 messages), of which 484 contain more than one. That is 484 calls
  instead of 3900 — eight times cheaper at the same resolution.
  Gluing by time window as the FINAL answer was rejected by the editorial board, and
  rightly so: in a chat with parallel topics it stitches unrelated things. Here it is
  only a candidate generator, and topics INSIDE a burst are separated by the model.

All local and free (Ollama, `LOCAL_LLM_MODEL`). No Ollama: the module
degrades: a burst stays a single thread, `tg_thread_src='burst'`, and this is visible
in the data, not hidden.

CLI:
    python -m app.services.tg_threads backfill --dry-run
    python -m app.services.tg_threads backfill --chat -1001234567890
    python -m app.services.tg_threads stats
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

import numpy as np

from app.db.connection import get_db_connection

logger = logging.getLogger(__name__)


# The pause after which a conversation counts as a new burst. 180 min comes from measuring
# the distribution: at 30 min a third of the bursts degenerate into singletons (nothing to
# group), at 1440 min a burst grows to messages and several working days
# get into it. At 180 min the median is 4, singletons 15%.
BURST_GAP_MIN = int(os.environ.get("TG_THREAD_BURST_GAP_MIN", "180"))

# How many messages and characters we give the model at a time. Long bursts (up to 130
# messages, up to 338k characters) are cut into sub-batches, carrying the already open
# threads over: otherwise the prompt does not fit in the context and the model silently truncates the tail.
MAX_BATCH_MSGS = int(os.environ.get("TG_THREAD_BATCH_MSGS", "25"))
MAX_BATCH_CHARS = int(os.environ.get("TG_THREAD_BATCH_CHARS", "6000"))

# How many open threads of a chat to show the model as continuation candidates.
OPEN_THREAD_CANDIDATES = int(os.environ.get("TG_THREAD_CANDIDATES", "5"))

# The model for splitting bursts. Deliberately separate from LOCAL_LLM_MODEL: the copilot
# works in real time during a call and is tuned to its own model, while here the
# task is structural (group 4-10 short replies): in the measurement the 7b gave the same
# split as the 32b in a quarter of the time. None = take LOCAL_LLM_MODEL.
THREAD_MODEL = os.environ.get("TG_THREAD_MODEL") or None

# A thread with no new messages for this many days is considered closed and is no longer
# offered as a candidate. It deletes nothing — it only removes it from the active ones.
#
# 3, not 21. This is NOT a matter of taste: in a run with a 21-day window the maximum
# gap inside a thread hit exactly 21 (max=21.0 at a median of 2.8), i.e. the
# boundary was set by the window, not by the content. a share of the threads had an internal gap over
# 3 days, 34% over a week, and the thread «Попередження про дзвінок» stretched from
# 18.06 to 30.07, gluing together a calendar, «давай зараз з Настею» and jokes.
# Reason: the model is shown a list of open threads with names, and the 7B readily
# picks something from the list — it has no «nothing fits» pressure. So the
# continuation is restrained by the WINDOW, not by the model's judgement. 3 days keep the real
# cases (a conversation that continued the next morning; Friday -> Monday) and
# remove gluing across weeks.
THREAD_IDLE_DAYS = int(os.environ.get("TG_THREAD_IDLE_DAYS", "3"))

# A thread also closes by VOLUME, not only by silence. Without this it grows as long as
# the chat is written to more often than THREAD_IDLE_DAYS: on the first pass a few threads collected
# messages (a large share of the archive), the largest messages over two months, and
# inside it an office address, a presentation, scenarios and a separate conversation about Adam.
# That is no longer a topic but a chat inside a chat — exactly what the wave cures.
# 30 comes from the distribution: threads many messages hold the bulk and look like
# topics, beyond that accumulation begins.
#
# The ceiling is SOFT, deliberately: it forbids a thread to accept a NEW conversation, but
# never tears one conversation in half. A group the model judged to be one topic
# enters whole, so the actual size may exceed the ceiling by the group size
# (observed 49 at a ceiling of 30). Tearing a coherent conversation for a round
# number would be worse than the accumulation itself: the point of a thread is that it is whole.
THREAD_MAX_MSGS = int(os.environ.get("TG_THREAD_MAX_MSGS", "30"))

# Truncation of message text in the prompt. Long forwards (articles, documents)
# must not crowd out neighbours in a burst — the beginning is enough to assign a topic.
_MSG_PREVIEW_CHARS = 400

_SCHEMA = {
    "type": "object",
    "properties": {
        "threads": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "msgs": {"type": "array", "items": {"type": "integer"}},
                    "label": {"type": "string"},
                    "continues": {"type": "integer"},
                },
                "required": ["msgs", "label"],
            },
        }
    },
    "required": ["threads"],
}

_SYSTEM = (
    "Ти розбираєш робочу переписку на теми. Тема — це те, ПРО ЩО йдеться "
    "(проєкт, питання, домовленість), а не хто пише і коли. "
    "Питання і відповідь на нього — ОДНА тема. Коротка репліка («ок», «+», "
    "«ні», «дякую») належить темі, до якої вона реагує, а не власній. "
    "Кожне повідомлення потрапляє рівно в одну тему. Відповідай лише JSON."
)


# ============================================================
# Bursts
# ============================================================

def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def segment_bursts(messages: list, gap_min: int = BURST_GAP_MIN) -> list[list]:
    """Cut time-ordered chat messages into bursts.

    A burst is a time-dense batch. It is NOT a topic (there can be several inside a burst
    — which is why the model goes next), only a candidate that sharply narrows
    the amount of work: messages -> 569 bursts."""
    bursts: list[list] = []
    current: list = []
    prev: Optional[datetime] = None
    for msg in messages:
        ts = _parse_date(msg["tg_date"])
        if prev is not None and ts is not None and (ts - prev) > timedelta(minutes=gap_min):
            bursts.append(current)
            current = []
        current.append(msg)
        if ts is not None:
            prev = ts
    if current:
        bursts.append(current)
    return bursts


def _batches(burst: list) -> list[list]:
    """Cut a burst into sub-batches that fit the model's context."""
    out: list[list] = []
    cur: list = []
    chars = 0
    for msg in burst:
        size = min(len(msg["transcript_text"] or ""), _MSG_PREVIEW_CHARS)
        if cur and (len(cur) >= MAX_BATCH_MSGS or chars + size > MAX_BATCH_CHARS):
            out.append(cur)
            cur, chars = [], 0
        cur.append(msg)
        chars += size
    if cur:
        out.append(cur)
    return out


# ============================================================
# Candidate threads
# ============================================================

def _vec(blob: Optional[bytes]) -> Optional[np.ndarray]:
    if not blob:
        return None
    return np.frombuffer(blob, dtype=np.float32).astype(np.float64)


def _burst_centroid(conn, burst: list) -> Optional[np.ndarray]:
    """Mean vector of a burst from already computed chunks (no GPU needed)."""
    ids = [m["id"] for m in burst]
    if not ids:
        return None
    rows = conn.execute(
        f"SELECT embedding FROM chunks WHERE transcription_id IN "
        f"({','.join('?' * len(ids))}) AND chunk_index = 0 AND embedding IS NOT NULL",
        ids,
    ).fetchall()
    vecs = [_vec(r["embedding"]) for r in rows]
    vecs = [v for v in vecs if v is not None]
    if not vecs:
        return None
    mean = np.mean(vecs, axis=0)
    norm = np.linalg.norm(mean)
    return mean / norm if norm else mean


def _candidate_threads(conn, chat_id: int, before: Optional[str],
                       centroid: Optional[np.ndarray],
                       limit: int = OPEN_THREAD_CANDIDATES) -> list[dict]:
    """Open threads of the chat most similar to the burst — so the model can say
    «this continues yesterday's conversation» instead of spawning duplicate topics.

    The cosine RANKS here and does not decide: no threshold, just a display order.
    An absolute threshold on this data is unjustified (see the module docstring)."""
    rows = conn.execute(
        # msg_count < THREAD_MAX_MSGS: an overfull thread is no longer continued,
        # otherwise it accumulates the chat over months and stops being a topic.
        "SELECT id, label, centroid, last_date, msg_count FROM tg_threads "
        "WHERE chat_id = ? AND status = 'open' AND msg_count < ? "
        "AND (? IS NULL OR last_date <= ?) "
        "ORDER BY last_date DESC LIMIT 60",
        (chat_id, THREAD_MAX_MSGS, before, before),
    ).fetchall()
    cands = []
    for r in rows:
        # A thread silent longer than THREAD_IDLE_DAYS before this burst is not a candidate.
        if before and r["last_date"]:
            gap = _parse_date(before), _parse_date(r["last_date"])
            if gap[0] and gap[1] and (gap[0] - gap[1]) > timedelta(days=THREAD_IDLE_DAYS):
                continue
        score = 0.0
        tv = _vec(r["centroid"])
        if tv is not None and centroid is not None:
            norm = np.linalg.norm(tv)
            if norm:
                score = float(centroid @ (tv / norm))
        cands.append({"id": r["id"], "label": r["label"], "score": score,
                      "last_date": r["last_date"], "msg_count": r["msg_count"]})
    cands.sort(key=lambda c: c["score"], reverse=True)
    return cands[:limit]


# ============================================================
# Splitting a burst with the model
# ============================================================

def _preview(text: Optional[str]) -> str:
    t = " ".join((text or "").split())
    return t[:_MSG_PREVIEW_CHARS] if t else "[без тексту]"


def _build_prompt(chat_title: str, batch: list, candidates: list[dict]) -> str:
    lines = [f"Чат: «{chat_title or 'без назви'}»", ""]
    if candidates:
        lines.append("Уже відкриті теми цього чату (можна продовжити одну з них):")
        for c in candidates:
            lines.append(f"  T{c['id']}: {c['label'] or 'без назви'}")
        lines.append("")
    lines.append("Повідомлення:")
    for i, m in enumerate(batch, start=1):
        who = m["tg_sender"] or "невідомо"
        when = (m["tg_date"] or "")[:16].replace("T", " ")
        lines.append(f"{i}. [{when} {who}] {_preview(m['transcript_text'])}")
    lines += [
        "",
        "Згрупуй ці повідомлення в теми. Кожен номер рівно в одній темі.",
        "Якщо тема продовжує вже відкриту — вкажи її номер у полі continues (без «T»).",
        'JSON: {"threads":[{"msgs":[1,2],"label":"коротка назва теми","continues":123}]}',
    ]
    return "\n".join(lines)


def split_batch(chat_title: str, batch: list, candidates: list[dict],
                model: Optional[str] = None) -> Optional[list[dict]]:
    """Hand a batch to the model and return [{"msgs":[idx…], "label":…,
    "continues": thread_id|None}] in 1-based batch indices.

    None — the model is unavailable or answered unparseably; the caller then leaves the
    batch as one thread. There is no silent «random thread» here."""
    from app.services import local_llm

    prompt = _build_prompt(chat_title, batch, candidates)
    valid_ids = {c["id"] for c in candidates}
    try:
        resp = local_llm.generate_json(prompt, schema=_SCHEMA, system=_SYSTEM,
                                       max_tokens=800, model=model or THREAD_MODEL)
    except Exception as exc:
        logger.warning("[tg_threads] local model did not respond (%s) — "
                       "the burst stays a single thread", exc)
        return None

    data = resp.get("data") or {}
    raw = data.get("threads")
    if not isinstance(raw, list) or not raw:
        logger.warning("[tg_threads] response without threads — burst as a single thread")
        return None

    out: list[dict] = []
    seen: set[int] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        idxs = [i for i in (item.get("msgs") or [])
                if isinstance(i, int) and 1 <= i <= len(batch) and i not in seen]
        if not idxs:
            continue
        seen.update(idxs)
        cont = item.get("continues")
        out.append({
            "msgs": sorted(idxs),
            "label": (item.get("label") or "").strip()[:120] or None,
            # The model readily invents ids — we take only those we showed ourselves.
            "continues": cont if isinstance(cont, int) and cont in valid_ids else None,
        })
    # Messages the model lost must not vanish from the archive. We mark
    # them separately: this is not «the model decided the topic is lonely» but «the model stayed silent»,
    # and merging these two cases into one provenance means losing the ability
    # to recompute exactly the misses later.
    #
    # Where to put them. A separate thread per message is guaranteed to be a thread without
    # context (on the first pass that gave messages, 10.a small share of the archive).
    # A positional neighbour is a guess, but in a conversation adjacent replies of a burst
    # are almost always about the same thing, so it is almost always better than loneliness.
    # The guess is visible in provenance (`orphan`) and can be recomputed separately.
    missed = [i for i in range(1, len(batch) + 1) if i not in seen]
    if missed:
        logger.info("[tg_threads] model did not assign %d messages — placing with neighbours",
                    len(missed))
        for i in missed:
            host = min(out, key=lambda g: min(abs(j - i) for j in g["msgs"]),
                       default=None) if out else None
            if host is None:
                out.append({"msgs": [i], "label": None, "continues": None, "orphan": True})
            else:
                host["msgs"] = sorted(host["msgs"] + [i])
                host.setdefault("orphan_msgs", []).append(i)
    return out or None


# ============================================================
# Writing
# ============================================================

def _fallback_label(messages: list, max_chars: int = 60) -> Optional[str]:
    """A thread title without the model — the start of the first substantive message.

    Not for beauty: the names of open threads are exactly what the model
    sees when deciding «does this burst continue something from before». A thread without
    a name is useless in the candidate list, and a one-message burst is not shown to the model at all
    (nothing to group with), so otherwise it would stay mute."""
    for m in messages:
        text = " ".join((m["transcript_text"] or "").split())
        if text and not text.startswith("["):
            return text[:max_chars]
    return None


def _create_thread(conn, chat_id: int, label: Optional[str]) -> int:
    cur = conn.execute(
        "INSERT INTO tg_threads (chat_id, label, status) VALUES (?, ?, 'open')",
        (chat_id, label))
    return cur.lastrowid


def _attach(conn, thread_id: int, messages: list, src: str) -> None:
    """Attach a message to a thread and recompute its aggregates."""
    conn.executemany(
        "UPDATE transcriptions SET tg_thread_id = ?, tg_thread_src = ? WHERE id = ?",
        [(thread_id, src, m["id"]) for m in messages])
    _refresh_thread(conn, thread_id)


def _refresh_thread(conn, thread_id: int) -> None:
    """Recompute centroid / msg_count / date bounds from the thread's actual composition.

    We count AFRESH, not incrementally: messages can be reassigned
    (the decision is revisable by design), and an accumulated mean would then silently
    drift from the real composition."""
    rows = conn.execute(
        "SELECT t.tg_date, ch.embedding FROM transcriptions t "
        "LEFT JOIN chunks ch ON ch.transcription_id = t.id AND ch.chunk_index = 0 "
        "WHERE t.tg_thread_id = ? AND t.deleted_at IS NULL",
        (thread_id,),
    ).fetchall()
    if not rows:
        conn.execute("UPDATE tg_threads SET msg_count = 0 WHERE id = ?", (thread_id,))
        return
    dates = sorted(d for d in (r["tg_date"] for r in rows) if d)
    vecs = [v for v in (_vec(r["embedding"]) for r in rows) if v is not None]
    centroid_blob = None
    if vecs:
        mean = np.mean(vecs, axis=0)
        norm = np.linalg.norm(mean)
        if norm:
            mean = mean / norm
        centroid_blob = mean.astype(np.float32).tobytes()
    conn.execute(
        "UPDATE tg_threads SET msg_count = ?, first_date = ?, last_date = ?, "
        "centroid = COALESCE(?, centroid) WHERE id = ?",
        (len(rows), dates[0] if dates else None, dates[-1] if dates else None,
         centroid_blob, thread_id))
    # Closing by volume. A thread that reached the ceiling is no longer continued —
    # the next conversation goes into a separate thread (with its own name) and is not stuck
    # onto this one. Silence closes a thread separately, in close_idle_threads.
    if len(rows) >= THREAD_MAX_MSGS:
        conn.execute("UPDATE tg_threads SET status = 'closed', "
                     "closed_at = COALESCE(closed_at, CURRENT_TIMESTAMP) "
                     "WHERE id = ? AND status = 'open'", (thread_id,))


def close_idle_threads(db_path: str, *, now: Optional[datetime] = None,
                       idle_days: int = THREAD_IDLE_DAYS) -> int:
    """Close threads silent longer than idle_days. Deletes nothing —
    a closed thread simply stops being a continuation candidate."""
    cutoff = ((now or datetime.now()) - timedelta(days=idle_days)).isoformat()
    with get_db_connection(db_path) as conn:
        cur = conn.execute(
            "UPDATE tg_threads SET status = 'closed', closed_at = CURRENT_TIMESTAMP "
            "WHERE status = 'open' AND (last_date IS NULL OR last_date < ?)",
            (cutoff,))
        conn.commit()
        return cur.rowcount


# ============================================================
# Cascade
# ============================================================

def _reply_parent_thread(conn, msg) -> Optional[int]:
    """Step 0: the parent's thread by tg_reply_to. Exact and free."""
    if not msg["tg_reply_to"]:
        return None
    row = conn.execute(
        "SELECT tg_thread_id FROM transcriptions "
        "WHERE tg_chat_id = ? AND tg_message_id = ? AND tg_thread_id IS NOT NULL",
        (msg["tg_chat_id"], msg["tg_reply_to"])).fetchone()
    return row["tg_thread_id"] if row else None


def assign_burst(conn, chat_id: int, chat_title: str, burst: list, *,
                 model: Optional[str] = None, use_llm: bool = True) -> dict:
    """Distribute one burst across threads. Returns a summary for the report."""
    created = 0
    continued = 0
    by_reply = 0

    for batch in _batches(burst):
        centroid = _burst_centroid(conn, batch)
        before = min((m["tg_date"] for m in batch if m["tg_date"]), default=None)
        candidates = _candidate_threads(conn, chat_id, before, centroid)

        groups = split_batch(chat_title, batch, candidates, model=model) if use_llm else None
        src = "llm"
        if groups is None:
            # Degradation without inventions: the whole batch is one thread, and this is visible in the data
            # via tg_thread_src, so exactly these can be recomputed later.
            groups = [{"msgs": list(range(1, len(batch) + 1)), "label": None,
                       "continues": None}]
            # A one-message batch did not need the model: it is lonely
            # trivially, not because of a failure. Mixing this with real degradation
            # makes the flag useless exactly when it is needed.
            src = "single" if len(batch) == 1 else "burst"

        for g in groups:
            msgs = [batch[i - 1] for i in g["msgs"]]
            # Step 0 has priority over the model: Telegram knows the thread exactly.
            thread_id = None
            for m in msgs:
                parent = _reply_parent_thread(conn, m)
                if parent:
                    thread_id = parent
                    by_reply += 1
                    break
            msg_src = "reply" if thread_id else src
            if thread_id is None and g["continues"]:
                thread_id = g["continues"]
                continued += 1
            if thread_id is None:
                thread_id = _create_thread(conn, chat_id,
                                           g["label"] or _fallback_label(msgs))
                created += 1
            elif g["label"]:
                # The thread lives on — update the name only if it had none.
                conn.execute("UPDATE tg_threads SET label = COALESCE(label, ?) WHERE id = ?",
                             (g["label"], thread_id))
            if msg_src == "llm":
                if g.get("orphan"):
                    msg_src = "orphan"
                elif len(msgs) == 1:
                    msg_src = "single"
            # Provenance is per message: a group may hold both the model's decisions and
            # misses appended by adjacency. One src for the whole group would make
            # the guess indistinguishable from a decision — which is exactly what the
            # field exists for.
            orphan_idx = set(g.get("orphan_msgs") or [])
            if orphan_idx and msg_src != "reply":
                decided = [batch[i - 1] for i in g["msgs"] if i not in orphan_idx]
                guessed = [batch[i - 1] for i in g["msgs"] if i in orphan_idx]
                if decided:
                    _attach(conn, thread_id, decided,
                            "single" if len(decided) == 1 and msg_src == "llm" else msg_src)
                _attach(conn, thread_id, guessed, "orphan")
            else:
                _attach(conn, thread_id, msgs, msg_src)

    return {"created": created, "continued": continued, "by_reply": by_reply}


def backfill(db_path: str, *, chat_id: Optional[int] = None, dry_run: bool = True,
             limit_bursts: Optional[int] = None, model: Optional[str] = None,
             use_llm: bool = True, only_unassigned: bool = True) -> dict:
    """Pass over the archive: distribute TG messages into threads.

    Idempotent: by default it touches only messages without a thread."""
    started = datetime.now()
    with get_db_connection(db_path) as conn:
        where = ["source_type = 'telegram'", "deleted_at IS NULL", "tg_date IS NOT NULL"]
        params: list[Any] = []
        if only_unassigned:
            where.append("tg_thread_id IS NULL")
        if chat_id is not None:
            where.append("tg_chat_id = ?")
            params.append(chat_id)
        rows = conn.execute(
            f"SELECT id, tg_chat_id, tg_chat_title, tg_message_id, tg_reply_to, "
            f"tg_date, tg_sender, transcript_text FROM transcriptions "
            f"WHERE {' AND '.join(where)} ORDER BY tg_chat_id, tg_date",
            params).fetchall()

        by_chat: dict[int, list] = {}
        for r in rows:
            by_chat.setdefault(r["tg_chat_id"], []).append(r)

        plan = []
        total_bursts = 0
        for cid, msgs in by_chat.items():
            bursts = segment_bursts(msgs)
            total_bursts += len(bursts)
            plan.append({"chat_id": cid, "title": msgs[0]["tg_chat_title"],
                         "messages": len(msgs), "bursts": len(bursts)})

        if dry_run:
            return {"dry_run": True, "messages": len(rows), "chats": len(by_chat),
                    "bursts": total_bursts,
                    "llm_calls_estimate": sum(
                        len(_batches(b)) for msgs in by_chat.values()
                        for b in segment_bursts(msgs) if len(b) > 1),
                    "plan": sorted(plan, key=lambda p: -p["messages"])}

        totals = {"created": 0, "continued": 0, "by_reply": 0}
        done = 0
        for cid, msgs in by_chat.items():
            title = msgs[0]["tg_chat_title"]
            for burst in segment_bursts(msgs):
                if limit_bursts is not None and done >= limit_bursts:
                    break
                # A one-message burst does not need the model: nothing to group.
                res = assign_burst(conn, cid, title, burst, model=model,
                                   use_llm=use_llm and len(burst) > 1)
                for k in totals:
                    totals[k] += res[k]
                done += 1
                conn.commit()
                if done % 25 == 0:
                    logger.info("[tg_threads] bursts processed: %d/%d", done, total_bursts)
            if limit_bursts is not None and done >= limit_bursts:
                break
        conn.commit()

    return {"dry_run": False, "messages": len(rows), "bursts_processed": done,
            "threads_created": totals["created"], "threads_continued": totals["continued"],
            "by_reply": totals["by_reply"],
            "seconds": round((datetime.now() - started).total_seconds(), 1)}


# ============================================================
# Live ingest
# ============================================================

def assign_incoming(db_path: str, transcription_id: int) -> Optional[int]:
    """Assign a just-accepted message to a thread — WITHOUT calling the model.

    The decision here is deliberately provisional (`tg_thread_src='pending'`). The reason is
    the owner's requirement: the decision must be REVISABLE — a message that looks
    like a new topic may turn out to be a continuation after the next three, and
    an irrevocable choice per reply kills that possibility. Besides, a model call per
    message is exactly the per-item price that the burst
    was invented to avoid.

    So only exact and free steps work in real time, and
    ``resettle_pending`` redistributes the burst with the model once it has finished talking."""
    with get_db_connection(db_path) as conn:
        msg = conn.execute(
            "SELECT id, tg_chat_id, tg_message_id, tg_reply_to, tg_date, transcript_text "
            "FROM transcriptions WHERE id = ? AND source_type = 'telegram'",
            (transcription_id,)).fetchone()
        if not msg or msg["tg_chat_id"] is None:
            return None

        thread_id = _reply_parent_thread(conn, msg)
        src = "reply"
        if thread_id is None:
            # The same burst as the previous message of the chat?
            cutoff = None
            ts = _parse_date(msg["tg_date"])
            if ts is not None:
                cutoff = (ts - timedelta(minutes=BURST_GAP_MIN)).isoformat()
            row = conn.execute(
                "SELECT id FROM tg_threads WHERE chat_id = ? AND status = 'open' "
                "AND (? IS NULL OR last_date >= ?) ORDER BY last_date DESC LIMIT 1",
                (msg["tg_chat_id"], cutoff, cutoff)).fetchone()
            thread_id = (row["id"] if row
                         else _create_thread(conn, msg["tg_chat_id"],
                                             _fallback_label([msg])))
            src = "pending"

        _attach(conn, thread_id, [msg], src)
        conn.commit()
        return thread_id


def resettle_pending(db_path: str, *, model: Optional[str] = None,
                     now: Optional[datetime] = None) -> dict:
    """Redistribute with the model the bursts that have finished talking.

    Takes messages with ``tg_thread_src='pending'`` whose burst has ended
    (silence longer than BURST_GAP_MIN), detaches them and runs them through the usual cascade.
    Idempotent: what the model has already distributed is not touched."""
    cutoff = ((now or datetime.now()) - timedelta(minutes=BURST_GAP_MIN)).isoformat()
    processed = 0
    totals = {"created": 0, "continued": 0, "by_reply": 0}
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT id, tg_chat_id, tg_chat_title, tg_message_id, tg_reply_to, "
            "tg_date, tg_sender, transcript_text FROM transcriptions "
            "WHERE source_type = 'telegram' AND deleted_at IS NULL "
            "AND tg_thread_src = 'pending' AND tg_date < ? "
            "ORDER BY tg_chat_id, tg_date", (cutoff,)).fetchall()
        if not rows:
            return {"resettled": 0, **totals}

        touched = {r["id"] for r in rows}
        stale_threads = {r["tg_thread_id"] for r in conn.execute(
            f"SELECT DISTINCT tg_thread_id FROM transcriptions WHERE id IN "
            f"({','.join('?' * len(touched))}) AND tg_thread_id IS NOT NULL",
            list(touched)).fetchall()}
        conn.executemany("UPDATE transcriptions SET tg_thread_id = NULL, "
                         "tg_thread_src = NULL WHERE id = ?", [(i,) for i in touched])

        by_chat: dict[int, list] = {}
        for r in rows:
            by_chat.setdefault(r["tg_chat_id"], []).append(r)
        for cid, msgs in by_chat.items():
            for burst in segment_bursts(msgs):
                res = assign_burst(conn, cid, msgs[0]["tg_chat_title"], burst,
                                   model=model, use_llm=len(burst) > 1)
                for k in totals:
                    totals[k] += res[k]
                processed += len(burst)

        # Threads that became empty after detaching need no cleanup:
        # _refresh_thread sets msg_count=0, and they do not get into the output.
        for tid in stale_threads:
            if tid:
                _refresh_thread(conn, tid)
        conn.commit()
    return {"resettled": processed, **totals}


# ============================================================
# Reading (for retrieval / RAG)
# ============================================================

def thread_messages(db_path: str, thread_id: int, *, limit: int = 40) -> list[dict]:
    """Thread messages in chronological order — what stitches
    a question to its answer in the results."""
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT id, tg_message_id, tg_date, tg_sender, transcript_text "
            "FROM transcriptions WHERE tg_thread_id = ? AND deleted_at IS NULL "
            "ORDER BY tg_date LIMIT ?", (thread_id, limit)).fetchall()
    return [dict(r) for r in rows]


def stats(db_path: str) -> dict:
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS threads, "
            "SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_threads, "
            "AVG(msg_count) AS avg_msgs, MAX(msg_count) AS max_msgs FROM tg_threads"
        ).fetchone()
        assigned = conn.execute(
            "SELECT COUNT(*) FROM transcriptions WHERE source_type='telegram' "
            "AND deleted_at IS NULL AND tg_thread_id IS NOT NULL").fetchone()[0]
        total = conn.execute(
            "SELECT COUNT(*) FROM transcriptions WHERE source_type='telegram' "
            "AND deleted_at IS NULL").fetchone()[0]
        by_src = dict(conn.execute(
            "SELECT COALESCE(tg_thread_src,'—'), COUNT(*) FROM transcriptions "
            "WHERE source_type='telegram' AND deleted_at IS NULL "
            "GROUP BY 1").fetchall())
    return {"threads": row["threads"] or 0, "open": row["open_threads"] or 0,
            "avg_messages": round(row["avg_msgs"] or 0, 1),
            "max_messages": row["max_msgs"] or 0,
            "messages_assigned": assigned, "messages_total": total,
            "by_source": by_src}


# ============================================================
# CLI
# ============================================================

def _print(res: dict) -> int:
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

    p = argparse.ArgumentParser(prog="tg_threads",
                                description="Conversation threads in Telegram chats (Wave 4.5).")
    p.add_argument("--db", default=default_db)
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("backfill", help="Distribute messages into threads")
    b.add_argument("--dry-run", action="store_true", help="Count the volume, write nothing")
    b.add_argument("--chat", type=int, default=None, help="Only one chat (tg_chat_id)")
    b.add_argument("--limit-bursts", type=int, default=None, help="Process at most N bursts")
    b.add_argument("--model", default=None, help="Override LOCAL_LLM_MODEL")
    b.add_argument("--no-llm", action="store_true",
                   help="No model: burst = thread (for degradation measurements)")
    b.add_argument("--redo", action="store_true",
                   help="Redistribute already assigned messages too")

    r = sub.add_parser("resettle", help="Redistribute with the model the bursts that have finished talking")
    r.add_argument("--model", default=None)

    c = sub.add_parser("close-idle", help="Close silent threads")
    c.add_argument("--days", type=int, default=THREAD_IDLE_DAYS)

    sub.add_parser("stats", help="How many threads and how they were assigned")

    args = p.parse_args(argv)
    if args.command == "backfill":
        return _print(backfill(args.db, chat_id=args.chat, dry_run=args.dry_run,
                               limit_bursts=args.limit_bursts, model=args.model,
                               use_llm=not args.no_llm,
                               only_unassigned=not args.redo))
    if args.command == "resettle":
        return _print(resettle_pending(args.db, model=args.model))
    if args.command == "close-idle":
        return _print({"closed": close_idle_threads(args.db, idle_days=args.days)})
    return _print(stats(args.db))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
