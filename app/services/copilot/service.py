"""Co-pilot — CopilotService (Phase 19, Step 1).

A thin persist layer over ``copilot_sessions``. Singleton in ``app.state.copilot_service``
(next to recording_service / live_transcribe_worker). At Step 1 — only session
CRUD (start/get_state/end); background analysis (topics, dispatcher, escalation) is the
separate CopilotWorker in Steps 2+.

Does NOT depend on the flask app context — takes db_path in the constructor (as
enrichment/embeddings take db_path explicitly), to stay testable.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Callable, Optional

from app.db.connection import get_db_connection
from app.repositories import transcriptions as tx_repo
from app.services.copilot import config as copilot_config


logger = logging.getLogger(__name__)


class CopilotBusyError(Exception):
    """The copilot is already driving another live recording (C6: one owner per process)."""

    def __init__(self, owner_session_id: str):
        super().__init__(f"copilot busy on recording {owner_session_id}")
        self.owner_session_id = owner_session_id


class CopilotService:
    """Co-pilot session controller. One instance per process."""

    def __init__(self, db_path: str,
                 is_recording_active: Optional[Callable[[str], bool]] = None):
        self._db_path = db_path
        self._lock = threading.RLock()
        # None (tests, boot without recording) → treat every active row as live.
        self.is_recording_active = is_recording_active

    def _is_live(self, recording_session_id: str) -> bool:
        if self.is_recording_active is None:
            return True
        return bool(self.is_recording_active(recording_session_id))

    def start(self, settings: Optional[dict] = None,
              recording_session_id: Optional[str] = None) -> dict:
        """Create a copilot session with a snapshot of the resolved settings.

        Returns {"copilot_session_id", "config": <resolved settings>}.

        Raises CopilotBusyError if there is an active copilot session of another recording that
        is still live. Active rows whose recording is already gone are closed right here.
        """
        cfg = copilot_config.resolve_settings(settings)
        with self._lock, get_db_connection(self._db_path) as conn:
            others = conn.execute(
                "SELECT id, recording_session_id FROM copilot_sessions "
                "WHERE status = 'active' AND recording_session_id IS NOT NULL "
                "AND recording_session_id IS NOT ? ORDER BY id",
                (recording_session_id,),
            ).fetchall()
            for row in others:
                owner = row["recording_session_id"]
                if self._is_live(owner):
                    raise CopilotBusyError(owner)
            for row in others:
                conn.execute(
                    "UPDATE copilot_sessions SET status = 'ended', "
                    "ended_at = CURRENT_TIMESTAMP WHERE id = ? AND status != 'ended'",
                    (row["id"],),
                )
                logger.info("[copilot] session %s closed: recording %s is no longer live",
                            row["id"], row["recording_session_id"])
            cur = conn.execute(
                "INSERT INTO copilot_sessions "
                "(recording_session_id, category_id, mode, importance, api_enabled, "
                " budget_usd, status, config_json, model_local, model_api) "
                "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                (
                    recording_session_id, cfg["category_id"], cfg["mode"],
                    cfg["importance"], 1 if cfg["api_enabled"] else 0,
                    cfg["budget_usd"], json.dumps(cfg, ensure_ascii=False),
                    cfg["model_local"], cfg["model_api"],
                ),
            )
            conn.commit()
            sid = cur.lastrowid
        logger.info(
            "[copilot] session %s created (rec=%s, mode=%s, importance=%s, api=%s, "
            "budget=$%.2f, category=%s)",
            sid, recording_session_id, cfg["mode"], cfg["importance"],
            cfg["api_enabled"], cfg["budget_usd"], cfg["category_id"],
        )
        return {"copilot_session_id": sid, "config": cfg}

    def get_transcript_brief(self, transcription_id: Optional[int]) -> Optional[dict]:
        """Transcript segments+meta for the session export (Step 8). None if absent."""
        if not transcription_id:
            return None
        with get_db_connection(self._db_path) as conn:
            row = tx_repo.get_by_id(
                conn, transcription_id,
                columns=("source_name", "created_at", "language", "category_id", "segments"),
            )
        if not row:
            return None
        d = dict(row)
        try:
            d["segments"] = json.loads(d.get("segments") or "[]")
        except (ValueError, TypeError):
            d["segments"] = []
        return d

    def link_transcription(self, copilot_session_id: int, transcription_id: int) -> None:
        """Bind the copilot session to the created transcript (Step 7) — so that the co-pilot
        timeline opens from the transcript page. Idempotent."""
        with self._lock, get_db_connection(self._db_path) as conn:
            conn.execute("UPDATE copilot_sessions SET transcription_id = ? WHERE id = ?",
                         (transcription_id, copilot_session_id))
            conn.commit()

    def get_by_transcription(self, transcription_id: int) -> Optional[dict]:
        """The copilot session linked to a transcript (the latest). None if absent."""
        with get_db_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT * FROM copilot_sessions WHERE transcription_id = ? "
                "ORDER BY id DESC LIMIT 1", (transcription_id,)).fetchone()
        return dict(row) if row else None

    def get_timeline(self, copilot_session_id: int) -> Optional[dict]:
        """Full historical track of a session for the timeline (Step 7): session + topics +
        events (sorted by timecode) + aggregates. None if the session is not found."""
        session = self.get_state(copilot_session_id)
        if session is None:
            return None
        with get_db_connection(self._db_path) as conn:
            trows = conn.execute(
                "SELECT topic_index, label, first_ts, last_ts, summary FROM copilot_topics "
                "WHERE copilot_session_id = ? ORDER BY topic_index", (copilot_session_id,)
            ).fetchall()
            erows = conn.execute(
                "SELECT id, ts_offset_sec, kind, topic_id, source, confidence, "
                "payload_json, tokens_in, tokens_out, operator_action FROM copilot_events "
                "WHERE copilot_session_id = ? "
                "ORDER BY (ts_offset_sec IS NULL), ts_offset_sec, id", (copilot_session_id,)
            ).fetchall()
        events, agg = [], {}
        for r in erows:
            e = dict(r)
            try:
                e["payload"] = json.loads(e.pop("payload_json") or "{}")
            except (ValueError, TypeError):
                e["payload"] = {}
            events.append(e)
            agg[e["kind"]] = agg.get(e["kind"], 0) + 1
        return {
            "session": session,
            "topics": [dict(r) for r in trows],
            "events": events,
            "aggregates": agg,
        }

    def get_feedback_stats(self) -> dict:
        """Aggregates of operator actions (👍/👎/pin/dismiss/escalate) — a signal for
        calibrating thresholds (Step 9). Safe: read-only, 0 even without sessions."""
        out = {"thumbs_up": 0, "thumbs_down": 0, "pin": 0, "dismiss": 0,
               "escalate": 0, "sessions": 0}
        try:
            with get_db_connection(self._db_path) as conn:
                for r in conn.execute(
                    "SELECT operator_action, COUNT(*) AS n FROM copilot_events "
                    "WHERE kind = 'operator_action' GROUP BY operator_action"):
                    if r["operator_action"] in out:
                        out[r["operator_action"]] = r["n"]
                out["sessions"] = conn.execute(
                    "SELECT COUNT(*) AS n FROM copilot_sessions").fetchone()["n"]
        except Exception:
            pass
        return out

    def get_feedback_by_confidence_bucket(self, bucket_width: float = 0.1) -> dict:
        """Aggregate operator actions (👍/👎/pin/dismiss/escalate) BY the confidence
        buckets of the INSIGHT acted on (T6.8, closes the broken
        feedback→calibration loop from REMEDIATION_PLAN). Each operator_action event
        links to the primary insight_local/insight_verified via
        ``payload.ref_event_id`` (see :meth:`log_action`) — we take ``confidence``
        from there and count how many 👍/👎 fall on each confidence
        range.

        Changes NO defaults (escalate_threshold/min_unanchored_conf in
        copilot/config.py) — only gives NUMBERS for a periodic manual review.
        For example, a high thumbs_down_ratio in the 0.6-0.7 bucket is a signal that
        escalate_threshold should be raised; a human decides and bumps the constant.

        Returns {
            "bucket_width": float,
            "buckets": [{"range": "0.6-0.7", "lo": 0.6, "hi": 0.7,
                         "thumbs_up": int, "thumbs_down": int, "pin": int,
                         "dismiss": int, "escalate": int, "total": int,
                         "thumbs_down_ratio": float|None}, ...],  # non-empty only, ascending lo
            "unlinked": int,  # actions without a resolvable ref_event_id/confidence (not counted in buckets)
        }
        Safe: read-only; empty DB → buckets=[], unlinked=0.
        """
        width = bucket_width if bucket_width and bucket_width > 0 else 0.1
        n_buckets = max(1, round(1.0 / width))
        buckets: dict[int, dict] = {}
        unlinked = 0
        try:
            with get_db_connection(self._db_path) as conn:
                insight_conf = {
                    r["id"]: r["confidence"] for r in conn.execute(
                        "SELECT id, confidence FROM copilot_events "
                        "WHERE kind IN ('insight_local', 'insight_verified') "
                        "AND confidence IS NOT NULL")
                }
                rows = conn.execute(
                    "SELECT operator_action, payload_json FROM copilot_events "
                    "WHERE kind = 'operator_action' AND operator_action IS NOT NULL"
                ).fetchall()
        except Exception:
            return {"bucket_width": width, "buckets": [], "unlinked": 0}

        for r in rows:
            action = r["operator_action"]
            try:
                payload = json.loads(r["payload_json"] or "{}")
            except (ValueError, TypeError):
                payload = {}
            ref_id = payload.get("ref_event_id")
            conf = insight_conf.get(ref_id) if ref_id is not None else None
            if conf is None:
                unlinked += 1
                continue
            try:
                conf = max(0.0, min(1.0, float(conf)))
            except (TypeError, ValueError):
                unlinked += 1
                continue
            idx = min(int(conf / width), n_buckets - 1)
            b = buckets.setdefault(idx, {
                "thumbs_up": 0, "thumbs_down": 0, "pin": 0,
                "dismiss": 0, "escalate": 0, "total": 0,
            })
            if action in b:
                b[action] += 1
            b["total"] += 1

        out_buckets = []
        for idx in sorted(buckets):
            b = buckets[idx]
            lo, hi = round(idx * width, 4), round((idx + 1) * width, 4)
            ud_total = b["thumbs_up"] + b["thumbs_down"]
            out_buckets.append({
                "range": f"{lo}-{hi}", "lo": lo, "hi": hi,
                **b,
                "thumbs_down_ratio": round(b["thumbs_down"] / ud_total, 3) if ud_total else None,
            })
        return {"bucket_width": width, "buckets": out_buckets, "unlinked": unlinked}

    def list_sessions(self, *, limit: int = 50, offset: int = 0) -> list:
        """List of copilot sessions (newest first) for the "what the co-pilot suggested" overview."""
        with get_db_connection(self._db_path) as conn:
            rows = conn.execute(
                "SELECT s.id, s.recording_session_id, s.transcription_id, s.category_id, "
                "s.mode, s.importance, s.api_enabled, s.status, s.started_at, s.ended_at, "
                "s.tokens_in, s.tokens_out, s.cost_estimate, "
                "(SELECT COUNT(*) FROM copilot_events e WHERE e.copilot_session_id = s.id "
                " AND e.kind IN ('insight_local','insight_verified')) AS insights, "
                "(SELECT COUNT(*) FROM copilot_topics tp WHERE tp.copilot_session_id = s.id) AS topics, "
                "t.source_name AS transcript_name "
                "FROM copilot_sessions s LEFT JOIN transcriptions t "
                "ON t.id = s.transcription_id AND t.deleted_at IS NULL "
                "ORDER BY s.id DESC LIMIT ? OFFSET ?", (max(1, min(limit, 200)), max(0, offset))
            ).fetchall()
        return [dict(r) for r in rows]

    def update_settings(self, copilot_session_id: int, *, mode: Optional[str] = None,
                        importance: Optional[str] = None) -> Optional[dict]:
        """Re-resolve session settings on the fly (Step 6). A mode change keeps
        api/budget; an importance change applies its defaults (API tier/budget/model).
        Returns the new resolved config or None if the session is not found."""
        snap = self.get_state(copilot_session_id)
        if not snap:
            return None
        cur = snap.get("config") or {}
        raw = {
            "mode": mode or cur.get("mode"),
            "importance": importance or cur.get("importance"),
            "category_id": cur.get("category_id"),
        }
        if importance is None:  # mode only → keep the explicit api/budget choice
            raw["api_enabled"] = cur.get("api_enabled")
            raw["budget_usd"] = cur.get("budget_usd")
        cfg = copilot_config.resolve_settings(raw)
        with self._lock, get_db_connection(self._db_path) as conn:
            conn.execute(
                "UPDATE copilot_sessions SET mode = ?, importance = ?, api_enabled = ?, "
                "budget_usd = ?, model_api = ?, config_json = ? WHERE id = ?",
                (cfg["mode"], cfg["importance"], 1 if cfg["api_enabled"] else 0,
                 cfg["budget_usd"], cfg["model_api"],
                 json.dumps(cfg, ensure_ascii=False), copilot_session_id),
            )
            conn.commit()
        logger.info("[copilot] session %s reconfigured (mode=%s, importance=%s, api=%s)",
                    copilot_session_id, cfg["mode"], cfg["importance"], cfg["api_enabled"])
        return cfg

    def get_state(self, copilot_session_id: int) -> Optional[dict]:
        """Session snapshot (with the parsed config). None if not found."""
        with get_db_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT * FROM copilot_sessions WHERE id = ?", (copilot_session_id,)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["config"] = json.loads(d.get("config_json") or "{}")
        except (ValueError, TypeError):
            d["config"] = {}
        return d

    def get_by_recording(self, recording_session_id: str) -> Optional[dict]:
        """The active copilot session for a given recording (the latest). None if absent."""
        with get_db_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT * FROM copilot_sessions WHERE recording_session_id = ? "
                "ORDER BY id DESC LIMIT 1", (recording_session_id,)
            ).fetchone()
        return dict(row) if row else None

    # ---------------------------------------------- topics / events (Step 2)

    def upsert_topic(self, copilot_session_id: int, topic_index: int,
                     label: Optional[str] = None, centroid_bytes: Optional[bytes] = None,
                     first_ts: Optional[float] = None, last_ts: Optional[float] = None) -> int:
        """Create or update a session topic block (unique by
        (session, topic_index)). Returns copilot_topics.id."""
        with self._lock, get_db_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT id FROM copilot_topics WHERE copilot_session_id = ? AND topic_index = ?",
                (copilot_session_id, topic_index),
            ).fetchone()
            if row:
                tid = row["id"]
                conn.execute(
                    "UPDATE copilot_topics SET label = COALESCE(?, label), "
                    "centroid = COALESCE(?, centroid), last_ts = COALESCE(?, last_ts) "
                    "WHERE id = ?",
                    (label, centroid_bytes, last_ts, tid),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO copilot_topics "
                    "(copilot_session_id, topic_index, label, centroid, first_ts, last_ts) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (copilot_session_id, topic_index, label, centroid_bytes, first_ts, last_ts),
                )
                tid = cur.lastrowid
            conn.commit()
        return tid

    def get_category_name(self, category_id: Optional[int]) -> Optional[str]:
        """Direction name (for the dispatcher profile). None if absent/not found."""
        if not category_id:
            return None
        with get_db_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT name FROM categories WHERE id = ?", (category_id,)
            ).fetchone()
        return row["name"] if row else None

    def update_topic_retrieval(self, copilot_session_id: int, topic_index: int,
                               chunks: list) -> None:
        """Cache the topic's RAG chunks (for returning to a topic — Step 3). The topic row
        must already exist (it is created by upsert_topic on shift/return)."""
        with self._lock, get_db_connection(self._db_path) as conn:
            conn.execute(
                "UPDATE copilot_topics SET retrieval_cache_json = ? "
                "WHERE copilot_session_id = ? AND topic_index = ?",
                (json.dumps(chunks, ensure_ascii=False), copilot_session_id, topic_index),
            )
            conn.commit()

    def get_topic_retrieval(self, copilot_session_id: int,
                            topic_index: int) -> Optional[list]:
        """Cached RAG chunks of the topic or None."""
        with get_db_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT retrieval_cache_json FROM copilot_topics "
                "WHERE copilot_session_id = ? AND topic_index = ?",
                (copilot_session_id, topic_index),
            ).fetchone()
        if not row or not row["retrieval_cache_json"]:
            return None
        try:
            return json.loads(row["retrieval_cache_json"])
        except (ValueError, TypeError):
            return None

    def log_event(self, copilot_session_id: int, kind: str, *,
                  ts_offset_sec: Optional[float] = None, topic_id: Optional[int] = None,
                  source: str = "local", confidence: Optional[float] = None,
                  payload: Optional[dict] = None, tokens_in: Optional[int] = None,
                  tokens_out: Optional[int] = None,
                  operator_action: Optional[str] = None) -> int:
        """Write an event into the historical track copilot_events. Returns id."""
        with get_db_connection(self._db_path) as conn:
            cur = conn.execute(
                "INSERT INTO copilot_events "
                "(copilot_session_id, ts_offset_sec, kind, topic_id, source, confidence, "
                " payload_json, tokens_in, tokens_out, operator_action) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (copilot_session_id, ts_offset_sec, kind, topic_id, source, confidence,
                 json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                 tokens_in, tokens_out, operator_action),
            )
            conn.commit()
            return cur.lastrowid

    def add_usage(self, copilot_session_id: int, *, tokens_in: int = 0,
                  tokens_out: int = 0, cache_read: int = 0, cost: float = 0.0) -> dict:
        """Atomically add the accounting of one API call to the session. Returns the new totals
        + whether the budget is exhausted (hard stop → degradation to local-only, Step 5).
        budget_usd <= 0 is treated as "no limit" (when api_enabled)."""
        with self._lock, get_db_connection(self._db_path) as conn:
            conn.execute(
                "UPDATE copilot_sessions SET tokens_in = tokens_in + ?, "
                "tokens_out = tokens_out + ?, cache_read = cache_read + ?, "
                "cost_estimate = cost_estimate + ? WHERE id = ?",
                (tokens_in, tokens_out, cache_read, cost, copilot_session_id),
            )
            row = conn.execute(
                "SELECT tokens_in, tokens_out, cache_read, cost_estimate, budget_usd, "
                "api_enabled FROM copilot_sessions WHERE id = ?", (copilot_session_id,)
            ).fetchone()
            conn.commit()
        if not row:
            return {"tokens_in": 0, "tokens_out": 0, "cache_read": 0,
                    "cost_estimate": 0.0, "budget_usd": None, "exhausted": False}
        budget = row["budget_usd"]
        cap_active = budget is not None and budget > 0
        return {
            "tokens_in": row["tokens_in"], "tokens_out": row["tokens_out"],
            "cache_read": row["cache_read"],
            "cost_estimate": round(row["cost_estimate"], 6),
            "budget_usd": budget,
            "exhausted": bool(cap_active and row["cost_estimate"] >= budget),
        }

    def log_action(self, copilot_session_id: int, action: str, *,
                   ref_event_id: Optional[int] = None, ts_offset_sec: Optional[float] = None,
                   payload: Optional[dict] = None) -> int:
        """Record an operator action on a card (dismiss/pin/thumbs_up/thumbs_down/
        escalate) — for tuning and history (Step 4). ref_event_id links to the insight
        acted on. escalate is picked up by Step 5."""
        p = dict(payload or {})
        if ref_event_id is not None:
            p["ref_event_id"] = ref_event_id
        return self.log_event(copilot_session_id, "operator_action",
                              ts_offset_sec=ts_offset_sec, source="operator",
                              operator_action=action, payload=p or None)

    def end(self, copilot_session_id: int,
            transcription_id: Optional[int] = None) -> bool:
        """Finish the session (status='ended'). Optionally link transcription_id
        (when the transcript is created after finalize). Idempotent."""
        with self._lock, get_db_connection(self._db_path) as conn:
            if transcription_id is not None:
                conn.execute(
                    "UPDATE copilot_sessions SET transcription_id = ? WHERE id = ?",
                    (transcription_id, copilot_session_id),
                )
            cur = conn.execute(
                "UPDATE copilot_sessions SET status = 'ended', "
                "ended_at = CURRENT_TIMESTAMP WHERE id = ? AND status != 'ended'",
                (copilot_session_id,),
            )
            conn.commit()
        ended = cur.rowcount > 0
        if ended:
            logger.info("[copilot] session %s ended", copilot_session_id)
        return ended
