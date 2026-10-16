"""Unanswered questions (Wave 5.3).

«Arrived in Telegram and left unanswered» is the last wave of the roadmap. The plan
said: «a question to me, after which there is no message from me in the thread (needs
`tg_reply_to`)». Measurement removed two of the plan's supports and left the third.

**`tg_reply_to` does not work**: some records(0.6%), a forward-only column since
Wave 4. Where it exists we trust it; we cannot build on it.

**The local model is no judge here.** Checked on 14 live questions: the 7B called
«@johndoe тобі вдасться долучитись?» a question NOT to the owner (it does not know
that this handle is the owner), in other cases it invented an addressee («адресовано
Julia Bondarenko», who is not in the thread) and copied wording straight from the prompt.
1 hit out of 14, and the miss was exactly on the case the wave was built for.
So the addressee is determined by STRUCTURE, not the model: the owner's @handle or a private chat.
A group question without an addressee (233 of 294 candidates) is deliberately NOT shown: neither
structure nor the 7B can say whether the owner is expected to answer, and
a guess here costs trust in the whole list.

**«No reply from me in the thread» is also a wrong test.** Of 10 explicitly addressed questions,
in 4 the owner answered after 0.0-0.4 hours, but the answer landed in a NEIGHBOURING thread
(threads are labelled by the model). So we look for presence within the CHAT in the window
`reply_window_h`: wrote in the chat after the question means saw it. This is a deliberately soft
criterion: calling forgotten something the person answered is worse than missing it.

The cost of the pass is zero: neither Claude nor Ollama, only SQL. So nothing is
persisted and nothing goes stale: the list is computed on request.

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

# Reply window: wrote in this chat within this many hours after the question —
# we assume they saw it. 24 h, because answering an evening question in the morning is normal.
REPLY_WINDOW_H = float(os.environ.get("TG_QUESTIONS_WINDOW_H", "24"))
# The window is wider than in the task digest (30 days there), because an unanswered question sometimes does NOT
# resolve itself: in the live archive all six hang for 2-4 months. The list is
# short by construction (the addressee must be proven), so its width does not flood it.
DEFAULT_DAYS = int(os.environ.get("TG_QUESTIONS_DAYS", "90"))

_SELF_CACHE = Path(__file__).resolve().parents[2] / "data" / "tg_self.local.json"

# A link's query string is not a question: `?igsh=`, `?from=search` gave 77 false
# candidates out of 574, and the model dutifully judged Instagram links.
_URL_RE = re.compile(r"https?://\S+|www\.\S+|t\.me/\S+")
_HANDLE_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_]{3,31})")


# ============================================================
# Who "I" am
# ============================================================

def _listener_status(timeout: float = 5.0) -> Optional[dict]:
    """Ask the listener who it is logged in as (it alone holds the session)."""
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
        logger.debug("[tg_questions] listener unavailable: %s", e)
        return None


def _derive_from_private_chats(db_path: str) -> Optional[dict]:
    """Fallback: in a 1:1 chat the owner is the sender who is NOT the chat title.

    Works only if at least one private chat is monitored. The name has no handle or id, so
    @mentions are not caught this way; `source` says so honestly.
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
    """Who owns the archive: {'id','name','username','source'} or None.

    Order: env (set explicitly by a person) → listener (authority, it holds the session) →
    cache of the last query → deduction from private chats. The cache is needed because passes and MCP
    work even when the listener is off.

    The guess «the owner is whoever is present in the most chats» is NOT
    used: on live data it points at two other people (16 chats
    versus the owner's 12).
    """
    name = os.environ.get("TELEGRAM_SELF_NAME", "").strip()
    if name:
        user = os.environ.get("TELEGRAM_SELF_USERNAME", "").strip().lstrip("@") or None
        return {"id": None, "name": name, "username": user, "source": "env"}

    # A cache without a handle is incomplete, not ready: that is what an old listener leaves,
    # one that cannot yet report username. Silently accepting it would mean
    # disabling @mention detection forever and telling no one.
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
            except OSError as e:      # the cache is a convenience, not a requirement
                logger.debug("[tg_questions] cache not written: %s", e)
            return me

    if _SELF_CACHE.exists():
        try:
            cached = json.loads(_SELF_CACHE.read_text(encoding="utf-8"))
            if cached.get("name"):
                cached["source"] = "cache"
                return cached
        except (OSError, ValueError) as e:
            logger.debug("[tg_questions] cache not read: %s", e)

    return _derive_from_private_chats(db_path)


# ============================================================
# Detector
# ============================================================

def is_question(text: Optional[str]) -> bool:
    """A question mark in the text, not in a link's query string."""
    return "?" in _URL_RE.sub(" ", text or "")


def _handles(text: Optional[str]) -> set:
    return {h.lower() for h in _HANDLE_RE.findall(_URL_RE.sub(" ", text or ""))}


def addressed_to_me(row: Any, me: dict) -> Optional[str]:
    """Why this is a question TO THE OWNER: 'private' | 'mention' | None.

    None means «unknown», not «no»: a group question without an address may be
    to the owner too; there is just nothing to prove it, so it does not go on the list.
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
    """Questions to the owner after which they did not appear in the chat.

    Returns {'me', 'questions', 'skipped_group'}: `skipped_group` is how many
    questions stayed off the list because of an unknown addressee (this is not zero and must
    be visible, otherwise the list reads as «nothing more»).
    """
    me = resolve_self(db_path)
    if not me:
        return {"me": None, "questions": [], "skipped_group": 0,
                "reason": "unknown who owns the archive: the listener is off and "
                          "there is neither TELEGRAM_SELF_NAME nor private chats"}

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

            # A direct reply is the strongest evidence when it exists (0.6%).
            replied = conn.execute(
                "SELECT 1 FROM transcriptions WHERE tg_sender = ? AND deleted_at IS NULL "
                "AND tg_reply_to = ? LIMIT 1", (me["name"], r["tg_message_id"])).fetchone()
            if replied:
                continue

            # Presence in the CHAT, not the thread: threads are labelled by the model, and
            # the reply regularly lands in a neighbouring one (measured: 4 cases of 10
            # had a reply within 0.0-0.4 h in the same chat).
            nxt = conn.execute(
                "SELECT MIN(tg_date) AS d FROM transcriptions WHERE tg_sender = ? "
                "AND deleted_at IS NULL AND tg_chat_id = ? AND tg_date > ?",
                (me["name"], r["tg_chat_id"], r["tg_date"])).fetchone()["d"]
            gap_h = None
            if nxt:
                try:
                    gap_h = (datetime.fromisoformat(nxt)
                             - datetime.fromisoformat(r["tg_date"])).total_seconds() / 3600
                except ValueError:                 # date in a foreign format — do not stay silent
                    logger.debug("[tg_questions] date not parsed: %r", nxt)
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
                                description="Unanswered questions (Wave 5.3).")
    p.add_argument("--db", default=default_db)
    sub = p.add_subparsers(dest="command", required=True)

    q = sub.add_parser("list", help="Questions to me that I have not answered")
    q.add_argument("--days", type=int, default=DEFAULT_DAYS)
    q.add_argument("--window", type=float, default=REPLY_WINDOW_H,
                   help="Hours of presence in the chat to count as an answer")
    q.add_argument("--chat", type=int, default=None)
    q.add_argument("--limit", type=int, default=20)

    w = sub.add_parser("whoami", help="Whom the archive considers the owner and how that is known")
    w.add_argument("--refresh", action="store_true", help="Ask the listener again")

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
