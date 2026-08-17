"""Co-pilot — CopilotService (Phase 19, Крок 1).

Тонкий persist-шар над ``copilot_sessions``. Singleton у ``app.state.copilot_service``
(поряд з recording_service / live_transcribe_worker). На Кроці 1 — лише CRUD
сесії (start/get_state/end); фоновий аналіз (топіки, диспетчер, ескалація) —
окремий CopilotWorker у Кроках 2+.

НЕ залежить від flask app context — приймає db_path у конструкторі (як
enrichment/embeddings приймають db_path явно), щоб лишатись тестованим.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Optional

from app.db.connection import get_db_connection
from app.repositories import transcriptions as tx_repo
from app.services.copilot import config as copilot_config


logger = logging.getLogger(__name__)


class CopilotService:
    """Контролер копілот-сесій. Один інстанс на процес."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.RLock()

    def start(self, settings: Optional[dict] = None,
              recording_session_id: Optional[str] = None) -> dict:
        """Створити копілот-сесію зі знімком резолвлених налаштувань.

        Returns {"copilot_session_id", "config": <resolved settings>}.
        """
        cfg = copilot_config.resolve_settings(settings)
        with self._lock, get_db_connection(self._db_path) as conn:
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
            "[copilot] сесія %s створена (rec=%s, mode=%s, importance=%s, api=%s, "
            "budget=$%.2f, category=%s)",
            sid, recording_session_id, cfg["mode"], cfg["importance"],
            cfg["api_enabled"], cfg["budget_usd"], cfg["category_id"],
        )
        return {"copilot_session_id": sid, "config": cfg}

    def get_transcript_brief(self, transcription_id: Optional[int]) -> Optional[dict]:
        """Сегменти+мета транскрипту для експорту сесії (Крок 8). None якщо нема."""
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
        """Прив'язати копілот-сесію до створеного транскрипту (Крок 7) — щоб таймлайн
        ко-пілота відкривався зі сторінки транскрипту. Idempotent."""
        with self._lock, get_db_connection(self._db_path) as conn:
            conn.execute("UPDATE copilot_sessions SET transcription_id = ? WHERE id = ?",
                         (transcription_id, copilot_session_id))
            conn.commit()

    def get_by_transcription(self, transcription_id: int) -> Optional[dict]:
        """Копілот-сесія, прилінкована до транскрипту (остання). None якщо нема."""
        with get_db_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT * FROM copilot_sessions WHERE transcription_id = ? "
                "ORDER BY id DESC LIMIT 1", (transcription_id,)).fetchone()
        return dict(row) if row else None

    def get_timeline(self, copilot_session_id: int) -> Optional[dict]:
        """Повна історична доріжка сесії для таймлайну (Крок 7): сесія + теми +
        події (відсортовані за таймкодом) + агрегати. None якщо сесію не знайдено."""
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
        """Агрегати дій оператора (👍/👎/pin/dismiss/escalate) — сигнал для
        калібрування порогів (Крок 9). Безпечно: лише читання, без сесій теж 0."""
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
        """Агрегувати дії оператора (👍/👎/pin/dismiss/escalate) ЗА confidence-
        бакетами ІНСАЙТУ, над яким діяли (T6.8, замикає обірваний цикл
        feedback→калібрування з REMEDIATION_PLAN). Кожна operator_action-подія
        лінкує на первинний insight_local/insight_verified через
        ``payload.ref_event_id`` (див. :meth:`log_action`) — беремо звідти
        ``confidence`` і рахуємо, скільки 👍/👎 припадає на кожен діапазон
        впевненості.

        НЕ змінює жодних дефолтів (escalate_threshold/min_unanchored_conf у
        copilot/config.py) — лише дає ЧИСЛА для періодичного ручного перегляду.
        Наприклад, високий thumbs_down_ratio у бакеті 0.6-0.7 — сигнал, що
        escalate_threshold варто підняти; людина вирішує й бампає константу.

        Returns {
            "bucket_width": float,
            "buckets": [{"range": "0.6-0.7", "lo": 0.6, "hi": 0.7,
                         "thumbs_up": int, "thumbs_down": int, "pin": int,
                         "dismiss": int, "escalate": int, "total": int,
                         "thumbs_down_ratio": float|None}, ...],  # лише непорожні, за зростанням lo
            "unlinked": int,  # дії без resolvable ref_event_id/confidence (не рахуються в buckets)
        }
        Безпечно: лише читання; порожня БД → buckets=[], unlinked=0.
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
        """Список копілот-сесій (новіші перші) для огляду «що підказував ко-пілот»."""
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
        """Перерезолвити налаштування сесії на льоту (Крок 6). Зміна режиму зберігає
        api/бюджет; зміна важливості застосовує її дефолти (тир API/бюджет/модель).
        Повертає новий резолвлений config або None, якщо сесію не знайдено."""
        snap = self.get_state(copilot_session_id)
        if not snap:
            return None
        cur = snap.get("config") or {}
        raw = {
            "mode": mode or cur.get("mode"),
            "importance": importance or cur.get("importance"),
            "category_id": cur.get("category_id"),
        }
        if importance is None:  # лише режим → зберегти явний вибір api/бюджету
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
        logger.info("[copilot] сесія %s переналаштована (mode=%s, importance=%s, api=%s)",
                    copilot_session_id, cfg["mode"], cfg["importance"], cfg["api_enabled"])
        return cfg

    def get_state(self, copilot_session_id: int) -> Optional[dict]:
        """Snapshot сесії (з розпарсеним config). None якщо не знайдено."""
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
        """Активна копілот-сесія для даного запису (остання). None якщо нема."""
        with get_db_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT * FROM copilot_sessions WHERE recording_session_id = ? "
                "ORDER BY id DESC LIMIT 1", (recording_session_id,)
            ).fetchone()
        return dict(row) if row else None

    # ---------------------------------------------- топіки / події (Крок 2)

    def upsert_topic(self, copilot_session_id: int, topic_index: int,
                     label: Optional[str] = None, centroid_bytes: Optional[bytes] = None,
                     first_ts: Optional[float] = None, last_ts: Optional[float] = None) -> int:
        """Створити або оновити топік-блок сесії (унікальний по
        (session, topic_index)). Повертає copilot_topics.id."""
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
        """Назва напрямку (для профілю диспетчера). None якщо нема/не знайдено."""
        if not category_id:
            return None
        with get_db_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT name FROM categories WHERE id = ?", (category_id,)
            ).fetchone()
        return row["name"] if row else None

    def update_topic_retrieval(self, copilot_session_id: int, topic_index: int,
                               chunks: list) -> None:
        """Закешувати RAG-чанки теми (для повернення до теми — Крок 3). Топік-рядок
        має вже існувати (його створює upsert_topic на shift/return)."""
        with self._lock, get_db_connection(self._db_path) as conn:
            conn.execute(
                "UPDATE copilot_topics SET retrieval_cache_json = ? "
                "WHERE copilot_session_id = ? AND topic_index = ?",
                (json.dumps(chunks, ensure_ascii=False), copilot_session_id, topic_index),
            )
            conn.commit()

    def get_topic_retrieval(self, copilot_session_id: int,
                            topic_index: int) -> Optional[list]:
        """Закешовані RAG-чанки теми або None."""
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
        """Записати подію в історичну доріжку copilot_events. Повертає id."""
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
        """Атомарно додати облік одного API-виклику до сесії. Повертає нові підсумки
        + чи вичерпано бюджет (хард-стоп → деградація в локальний-only, Крок 5).
        budget_usd <= 0 трактуємо як «без ліміту» (коли api_enabled)."""
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
        """Записати дію оператора над карткою (dismiss/pin/thumbs_up/thumbs_down/
        escalate) — для тюнінгу й історії (Крок 4). ref_event_id лінкує на інсайт,
        над яким діяли. escalate підбере Крок 5."""
        p = dict(payload or {})
        if ref_event_id is not None:
            p["ref_event_id"] = ref_event_id
        return self.log_event(copilot_session_id, "operator_action",
                              ts_offset_sec=ts_offset_sec, source="operator",
                              operator_action=action, payload=p or None)

    def end(self, copilot_session_id: int,
            transcription_id: Optional[int] = None) -> bool:
        """Завершити сесію (status='ended'). Опційно прилінкувати transcription_id
        (коли транскрипт створено після finalize). Idempotent."""
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
            logger.info("[copilot] сесія %s завершена", copilot_session_id)
        return ended
