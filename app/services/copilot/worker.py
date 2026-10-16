"""Co-pilot — CopilotWorker (Phase 19, Step 2).

One daemon thread per active session (modelled on :class:`LiveTranscribeWorker`).
Each tick:
  1. checks the recording pause (recording_service.get_state.is_paused) — while paused
     it idles (operator requirement), does not analyze;
  2. takes new live segments (live_transcribe_worker.get_preview), accumulates a
     text window;
  3. when the window is sufficient — feeds :class:`TopicTracker`;
  4. on a topic shift/return — persists to copilot_topics/_events and publishes
     ``copilot_topic`` / ``copilot_status`` to the SSE channel ``recording:{sid}``
     (record.js is already subscribed).

$0: only local embeddings, no API calls. Everything degrades: no embeddings
or no live worker → topic tracking simply does not start, recording works as before.

Testability: all the logic of one pass is in :meth:`_tick`, which can be called
directly with stubs (broker/live/recording/embed_fn) — offline, without a microphone.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import numpy as np

from app.services import embeddings, local_llm
from app.services.categories import CategoryError
from app.services.copilot.dispatcher import Dispatcher
from app.services.copilot.escalate import Escalator
from app.services.copilot.topics import TopicTracker


logger = logging.getLogger(__name__)


@dataclass
class _WS:
    """Worker state for one session."""
    copilot_session_id: int
    recording_session_id: str
    config: dict
    tracker: TopicTracker
    seen: set = field(default_factory=set)          # keys of segments already consumed
    shown_chunks: dict = field(default_factory=dict)  # cid -> True, insertion-ordered (bounded dedup window)
    pending: list = field(default_factory=list)     # text accumulated until the window
    pending_chars: int = 0
    carryover: str = ""                             # tail of the previous window (context)
    last_offset: float = 0.0                        # max end timecode (offset in the recording)
    topic_db_ids: dict = field(default_factory=dict)  # topic_index -> copilot_topics.id
    paused: bool = False
    # --- Track 3: operator attention budget ---
    cards_shown: int = 0                             # how many cards have been shown
    last_card_ts: float = 0.0                        # time.monotonic of the last card
    cards_suppressed: int = 0                        # how many were muted (go to the post-brief)
    budget_notice_sent: bool = False
    scope_ambiguous_notice_sent: bool = False         # namesake scope: warn the operator once per session
    # --- Step 3: dispatcher LLM (triage + RAG + insights) ---
    transcript: str = ""                             # rolling buffer of lines for the dispatcher
    summary: str = ""                                # session rolling-summary (in-memory)
    topic_chunks: dict = field(default_factory=dict)  # topic_index -> cached RAG chunks
    return_to: Optional[int] = None                  # topic just returned to (reuse cache)
    last_dispatch: float = 0.0                       # time.monotonic of the last dispatch
    dispatch_count: int = 0
    profile: Optional[str] = None                    # direction name (lazy)
    llm_warned: bool = False
    # --- Step 5: escalation to Claude (the big brother) ---
    budget_exhausted: bool = False                   # budget hard stop → local-only
    last_sweep: float = 0.0                          # time.monotonic of the last safety-sweep
    insights_by_event: dict = field(default_factory=dict)  # event_id -> {ins, chunks, topic_index} (for manual escalation)
    esc_lock: threading.Lock = field(default_factory=threading.Lock)  # serializes session escalations (daemon tick vs manual escalate_now)
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: Any = None


class CopilotWorker:
    """Runs the topic trackers of active sessions. Singleton in app.state.copilot_worker."""

    def __init__(self, *, broker, live_transcribe_worker, recording_service,
                 copilot_service, db_path: Optional[str] = None,
                 embed_fn: Optional[Callable[[str], np.ndarray]] = None,
                 dispatcher: Optional[Dispatcher] = None, llm: Any = local_llm,
                 escalator: Optional[Escalator] = None,
                 tick_sec: Optional[float] = None, min_window_chars: Optional[int] = None,
                 carryover_chars: int = 120, transcript_chars: int = 4000,
                 default_cadence_sec: float = 50.0, summary_every: int = 3,
                 shown_cap: int = 500):
        self._broker = broker
        self._live = live_transcribe_worker
        self._rec = recording_service
        self._svc = copilot_service
        self._db_path = db_path
        self._embed_fn = embed_fn or embeddings.embed_query
        self._dispatcher = dispatcher  # None → built lazily in start() from db_path
        self._escalator = escalator    # None → lazy Escalator() (Claude via text_polishing)
        self._llm = llm
        self._tick_sec = tick_sec or float(os.environ.get("COPILOT_TOPIC_TICK_SEC", "6"))
        self._min_window_chars = min_window_chars or int(
            os.environ.get("COPILOT_TOPIC_WINDOW_CHARS", "200"))
        self._carryover_chars = carryover_chars
        self._transcript_chars = transcript_chars
        self._default_cadence = default_cadence_sec
        self._summary_every = summary_every
        self._shown_cap = shown_cap
        self._sessions: dict[str, _WS] = {}
        self._lock = threading.RLock()

    def _get_dispatcher(self) -> Optional[Dispatcher]:
        """Lazy dispatcher construction (needs db_path for RAG). None if there is no
        db_path (then the Step-3 layer is off, topics work as at Step 2)."""
        if self._dispatcher is not None:
            return self._dispatcher
        if not self._db_path:
            return None
        self._dispatcher = Dispatcher(db_path=self._db_path, llm=self._llm)
        return self._dispatcher

    def _get_escalator(self) -> Escalator:
        """Lazy escalator construction (Claude via text_polishing._get_client)."""
        if self._escalator is None:
            self._escalator = Escalator()
        return self._escalator

    # ----------------------------------------------------------- lifecycle

    def start(self, *, copilot_session_id: int, recording_session_id: str,
              config: Optional[dict] = None) -> None:
        """Start topic tracking for a session. Idempotent. Softly skips if
        local embeddings are unavailable (the default embed_fn)."""
        if self._embed_fn is embeddings.embed_query and not embeddings.is_available():
            logger.info("[copilot] embeddings unavailable — topic tracking disabled for %s",
                        recording_session_id)
            return
        with self._lock:
            if recording_session_id in self._sessions:
                return
            ws = _WS(
                copilot_session_id=copilot_session_id,
                recording_session_id=recording_session_id,
                config=config or {},
                tracker=TopicTracker(self._embed_fn),
            )
            t = threading.Thread(target=self._loop, args=(ws,),
                                 name=f"copilot-{recording_session_id[:8]}", daemon=True)
            ws.thread = t
            self._sessions[recording_session_id] = ws
            t.start()
        logger.info("[copilot] worker started for %s (copilot_session=%s, tick=%.0fs)",
                    recording_session_id, copilot_session_id, self._tick_sec)

    def stop(self, recording_session_id: str, wait: bool = False) -> None:
        with self._lock:
            ws = self._sessions.pop(recording_session_id, None)
        if ws is None:
            return
        ws.stop_event.set()
        if wait and ws.thread is not None:
            ws.thread.join(timeout=5.0)

    def is_active(self, recording_session_id: str) -> bool:
        with self._lock:
            return recording_session_id in self._sessions

    # ----------------------------------------------------------- loop / tick

    def _loop(self, ws: _WS) -> None:
        try:
            while not ws.stop_event.is_set():
                ws.stop_event.wait(self._tick_sec)
                if ws.stop_event.is_set():
                    break
                try:
                    self._tick(ws)
                except Exception as e:  # one tick failure does not kill the worker
                    logger.warning("[copilot] tick error %s: %s",
                                   ws.recording_session_id, e, exc_info=True)
        finally:
            logger.info("[copilot] worker stopped for %s (topics=%d)",
                        ws.recording_session_id, len(ws.tracker.topics))

    def _tick(self, ws: _WS) -> Optional[dict]:
        """One pass. Returns the dispatch result (for tests) or None."""
        # 1. Recording pause → idle (operator requirement).
        if self._is_paused(ws.recording_session_id):
            if not ws.paused:
                ws.paused = True
                self._publish(ws, "copilot_status", {"state": "paused"})
            return None
        if ws.paused:
            ws.paused = False
            self._publish(ws, "copilot_status", {"state": "analyzing"})

        # 2. New segments → the dispatcher rolling buffer + offset in the recording.
        new_text, max_end = self._collect_new(ws)
        if new_text:
            ws.transcript = (ws.transcript + " " + new_text).strip()[-self._transcript_chars:]
            if max_end is not None:
                ws.last_offset = max_end

        # 3. Dispatcher LLM — on a cadence. Topics are now decided by the LLM ITSELF in triage
        #    (continue/shift/return), not by a cosine threshold on the window.
        return self._maybe_dispatch(ws)

    # ----------------------------------------------------------- helpers

    def _is_paused(self, recording_session_id: str) -> bool:
        if self._rec is None:
            return False
        try:
            snap = self._rec.get_state(recording_session_id)
            return bool(snap.get("is_paused"))
        except Exception:
            return False

    def _collect_new(self, ws: _WS) -> tuple[str, Optional[float]]:
        try:
            segs = self._live.get_preview(ws.recording_session_id) or []
        except Exception:
            return "", None
        parts: list[str] = []
        max_end: Optional[float] = None
        for s in segs:
            key = (round(float(s.get("start", 0) or 0), 2), s.get("stream") or "")
            if key in ws.seen:
                continue
            ws.seen.add(key)
            txt = (s.get("text") or "").strip()
            if txt:
                parts.append(txt)
            e = s.get("end")
            if e is not None:
                max_end = e if max_end is None else max(max_end, e)
        return " ".join(parts), max_end

    def _handle_topic_event(self, ws: _WS, ev: dict) -> None:
        idx = ev["topic_index"]
        topic = ws.tracker.topics[idx]
        try:
            topic_id = self._svc.upsert_topic(
                ws.copilot_session_id, idx, label=topic["label"],
                centroid_bytes=None,  # topics are LLM-driven — we do not store centroids
                first_ts=topic["first_ts"], last_ts=topic["last_ts"],
            )
            ws.topic_db_ids[idx] = topic_id
            kind = "topic_return" if ev["action"] == "return" else "topic_shift"
            self._svc.log_event(
                ws.copilot_session_id, kind, ts_offset_sec=ev.get("ts"),
                topic_id=topic_id, source="local",
                payload={k: ev.get(k) for k in
                         ("label", "returned_from", "similarity", "topic_index")},
            )
        except Exception as e:
            logger.warning("[copilot] persist topic event failed: %s", e)

        # Return to a topic → the next dispatch reuses the cached RAG context
        # of this topic rather than searching anew (savings + consistency, Step 3).
        if ev["action"] == "return":
            ws.return_to = idx

        self._publish(ws, "copilot_topic", {
            "action": ev["action"], "topic_index": idx, "label": ev["label"],
            "returned_from": ev.get("returned_from"), "ts_offset_sec": ev.get("ts"),
        })

    def _persist_topic_label(self, ws: _WS, topic_index: int, label: str) -> None:
        """Update the topic name from the LLM triage in the DB + relabel in the widget. The topic already exists
        in copilot_topics (created by _handle_topic_event on the shift), so upsert_topic
        only rewrites the label (COALESCE). The live strip is redrawn via
        copilot_topic action=relabel (applyCopilotTopic can already update an existing chip)."""
        if self._svc is not None and topic_index in ws.topic_db_ids:
            try:
                self._svc.upsert_topic(ws.copilot_session_id, topic_index, label=label)
            except Exception as e:
                logger.debug("[copilot] persist topic label failed: %s", e)
        self._publish(ws, "copilot_topic", {
            "action": "relabel", "topic_index": topic_index, "label": label,
            "ts_offset_sec": ws.last_offset,
        })

    # ----------------------------------------------------- dispatcher (Step 3)

    def _maybe_dispatch(self, ws: _WS) -> Optional[dict]:
        """Cadence-gated dispatcher pass: triage → (RAG) → insights.

        Returns a result dict (for tests) or None if it is not time / there is no
        text / the LLM is unavailable. Everything degrades: LLM failure → log + None, topics live on.
        """
        disp = self._get_dispatcher()
        if disp is None:
            return None
        if not ws.transcript:
            return None
        cadence = float(ws.config.get("cadence_sec") or self._default_cadence)
        now = time.monotonic()
        if ws.last_dispatch and (now - ws.last_dispatch) < cadence:
            return None

        # Is the LLM available? (short cache in local_llm; degrade softly)
        if self._llm is local_llm and not local_llm.is_available():
            if not ws.llm_warned:
                ws.llm_warned = True
                logger.info("[copilot] local LLM unavailable — dispatcher disabled "
                            "for %s (%s)", ws.recording_session_id,
                            local_llm.unavailability_reason())
                self._publish(ws, "copilot_status",
                              {"state": "degraded", "reason": "local_llm_unavailable"})
            return None
        ws.llm_warned = False
        ws.last_dispatch = now

        profile = self._get_profile(ws)
        triage = disp.triage(window=ws.transcript, summary=ws.summary, profile=profile,
                             topics=ws.tracker.topic_list(),
                             current_index=ws.tracker.current)
        if triage is None:
            return None

        # The topic is the decision of the LLM ITSELF (continue/shift/return), not a cosine threshold.
        # The tracker applies it deterministically and returns an event.
        ev = ws.tracker.apply(status=triage.get("topic_status"),
                              label=triage.get("topic_label"),
                              return_to=triage.get("return_to_index"),
                              ts=ws.last_offset)
        if ev["action"] != "continue":
            self._handle_topic_event(ws, ev)        # persist shift/return + SSE
        elif ev.get("relabeled"):
            self._persist_topic_label(ws, ev["topic_index"], ev["label"])
        cur = ws.tracker.current

        chunks = self._operator_comments(ws) + self._gather_chunks(ws, disp, cur, triage)
        insights, used_chunks = self._build_insights(ws, disp, chunks, triage, profile)

        for ins in insights:
            self._process_insight(ws, cur, ins, used_chunks, profile)

        # Safety-sweep: an independent Sonnet pass over the window once per T (insurance).
        sweep_chunks = chunks or ws.topic_chunks.get(cur) or []
        self._maybe_safety_sweep(ws, sweep_chunks, profile, cur)

        # rolling-summary once per N dispatches (keeps the context small)
        ws.dispatch_count += 1
        if self._summary_every > 0 and ws.dispatch_count % self._summary_every == 0:
            new_sum = disp.summarize(prev_summary=ws.summary, window=ws.transcript)
            if new_sum:
                ws.summary = new_sum

        return {"triage": triage, "insights": insights}

    def _process_insight(self, ws: _WS, topic_index: int, ins: dict,
                         used_chunks: dict, profile: str) -> None:
        """One insight: persist always, show — by the gate and the Claude verdict.

        Track 3 flips the cascade. It was: the local card was shown immediately, and
        Claude verified what was already shown (and only 5.9% of cards ever reached
        verification). Now: verdict first, we show only `real`. That is the "quieter
        and more precise" — the volume drops by an order of magnitude, and what remains is confirmed.

        Soft degradation: if the API is unavailable (no key, budget exhausted,
        "local only" mode) — verified_only does not apply, otherwise the copilot would go
        fully silent; then we show the local one, but only the one anchored by evidence.
        """
        verified_only = bool(ws.config.get("verified_only")) and self._api_active(ws)
        gate = self._card_gate(ws)

        if not verified_only:
            allowed = gate is None and (ins.get("evidence_chunk_ids") or not self._api_active(ws))
            event_id, _ = self._emit_insight(ws, topic_index, ins, used_chunks,
                                             publish=bool(allowed))
            if not allowed and gate:
                self._note_suppressed(ws, gate)
            if event_id is not None and self._should_escalate(ws, ins):
                self._escalate_insight(ws, event_id, ins, used_chunks, profile)
            return

        # verified_only: persist silently, show only after a `real` verdict.
        event_id, _ = self._emit_insight(ws, topic_index, ins, used_chunks, publish=False)
        if event_id is None:
            return
        if gate is not None:
            self._note_suppressed(ws, gate)
            return
        if not self._should_escalate(ws, ins):
            self._note_suppressed(ws, "below_bar")
            return
        verdict = self._escalate_insight(ws, event_id, ins, used_chunks, profile,
                                         publish_update=False)
        if verdict is None:
            self._note_suppressed(ws, "verify_unavailable")
            return
        if verdict.get("verdict") != "real":
            self._note_suppressed(ws, "refuted")
            return
        # Show the VERIFIED text (Claude also polishes it), not the local draft.
        self._publish(ws, "copilot_insight", {
            "id": event_id, "kind": ins["kind"],
            "text": verdict.get("text") or ins["text"],
            "evidence": [{"chunk_id": cid,
                          "transcription_id": used_chunks[cid].get("transcription_id"),
                          "source_name": used_chunks[cid].get("source_name"),
                          "meeting_date": used_chunks[cid].get("meeting_date"),
                          "start_time": used_chunks[cid].get("start_time")}
                         for cid in ins.get("evidence_chunk_ids", []) if cid in used_chunks],
            "confidence": verdict.get("confidence"), "source": "api",
            "verdict": "real", "topic_index": topic_index,
            "ts_offset_sec": ws.last_offset,
        })
        self._count_card(ws)

    #: How many operator comments we pull into the analysis window. The cap is deliberately
    #: small: the 7B context is tight anyway, and fresh lines are more useful than
    #: ones written half an hour ago — we take the LATEST.
    _OPERATOR_COMMENTS = 4

    #: A separate id lane for operator comments. Negative ids are already taken by the
    #: comment layer in `retrieval` (there it is `-comment_chunks.id`), while here the key is
    #: `comments.id`: different tables, the spaces overlap, and in one window
    #: two different rows could appear under `-5`. Then the insight's `evidence_chunk_ids`
    #: would point at who knows what.
    _OPERATOR_CHUNK_BASE = -1_000_000_000

    def _operator_comments(self, ws: _WS) -> list[dict]:
        """Comments the operator typed DURING this call (Wave 4).

        This is the strongest signal in the window: a person has just deliberately formulated what
        is actually happening — more precisely than anything the transcript or the
        archive gave. So they go into the context FIRST and regardless of whether
        triage asked for a search: a comment is not an answer to a query, it is a pointer.

        They are given in the form of a regular chunk, so that `analyze()` can refer to
        them via `evidence_chunk_ids` without a separate branch — they are distinguished by
        `source_type='comment'`, for which `_fmt_chunks` sets a different label.
        The `shown_chunks` dedup works as for the rest: a shown comment is not
        repeated by a card a second time.

        Degrades quietly: no table (old DB), no DB — the copilot works as
        before. It is an add-on, not a condition of operation.
        """
        if not self._db_path or not ws.recording_session_id:
            return []
        try:
            from app.services import comments as comments_svc
            rows = comments_svc.list_for(self._db_path, "recording_session",
                                         ws.recording_session_id)
        except Exception as e:
            logger.debug("[copilot] operator comments not read: %s", e)
            return []
        if not rows:
            return []
        # `list_for` returns pinned ones FIRST, so a "last N" slice would cut off
        # exactly those the operator deliberately raised. We take pinned plus
        # the freshest of the rest — both are deliberately important, but for different reasons.
        cap = self._OPERATOR_COMMENTS
        picked = [c for c in rows if c.get("pinned")][:cap]
        room = cap - len(picked)
        if room > 0:
            picked += [c for c in rows if not c.get("pinned")][-room:]
        out = []
        for c in picked:
            at = c.get("anchor_time")
            out.append({
                "chunk_id": self._OPERATOR_CHUNK_BASE - int(c["id"]),
                "source_type": "comment",
                # Distinguishes a live line of this call from an archive comment
                # that arrived from RAG — see dispatcher._fmt_chunks.
                "live_operator": True,
                "comment_id": c["id"],
                "comment_kind": c.get("kind"),
                "text": c["body"],
                "anchor_label": (f"{int(at) // 60:02d}:{int(at) % 60:02d}"
                                 if at is not None else ""),
                "source_name": "коментар оператора",
                "meeting_date": None,
            })
        return out

    def _scope_entity_ids(self, ws: _WS, db_path: Optional[str]) -> Optional[list]:
        """Entities (project/participants) of the current session — the fine search scope (Track 2).

        We resolve ONCE per session and cache in the worker state: during a call every
        extra DB query eats the same time budget as useful work. An empty
        list = no scope, the search goes as before (by category).

        A namesake (`CategoryError('ambiguous')` from `resolve_node`, v47) is a separate case
        from "not found": the scope really exists, just in several directions, so
        quietly narrowing to one of the namesakes would substitute the answer. The search goes
        WITHOUT narrowing (as with not-found), but we warn the operator once per
        session — otherwise "Маркетинг" is silently widened to the whole archive for the whole session
        and nobody finds out.
        """
        cached = getattr(ws, "_scope_ids", None)
        if cached is not None:
            return cached or None
        names = (ws.config or {}).get("scope_projects") or []
        ids: list = []
        if names:
            candidates: list = []
            try:
                from app.services import scope as scope_svc
                ids = scope_svc.scope_filter_ids(db_path, names) if db_path else []
            except CategoryError as exc:
                if exc.code == "ambiguous":
                    candidates = list(exc.candidates or [])
                    logger.warning("[copilot] scope %s is ambiguous: %s", names, candidates)
                else:
                    logger.warning("[copilot] could not parse scope %s: %s", names, exc)
                ids = []
            except Exception as exc:
                logger.warning("[copilot] could not parse scope %s: %s", names, exc)
                ids = []
            if candidates:
                if not ws.scope_ambiguous_notice_sent:
                    ws.scope_ambiguous_notice_sent = True
                    self._notify_scope_ambiguous(ws, names, candidates)
            elif not ids:
                logger.info("[copilot] scope %s not found in the graph — searching without narrowing", names)
        try:
            ws._scope_ids = ids
        except Exception:
            logger.debug("[copilot] session state does not cache the scope (dataclass with slots?)")
        return ids or None

    def _notify_scope_ambiguous(self, ws: _WS, names: list, candidates: list) -> None:
        """Namesake scope: warn the operator via the existing insight channel (Round 6
        major 1), not a separate ``copilot_status``. Persisted via ``log_event`` +
        publishing ``copilot_insight`` with ``shown=true`` — so the warning is both
        rendered as a card in the widget and lives in ``copilot_events`` (session
        history, `get_state()`/MCP `get_live_copilot`, timeline). Deliberately outside
        `_card_gate` and the `cards_shown` counter — the attention budget concerns
        dispatcher insights, not system warnings."""
        scope_label = ", ".join(str(n) for n in names)
        text = (f"Скоуп «{scope_label}» є в кількох напрямках — шукаю без "
                "звуження, уточніть скоуп повним шляхом:")
        event_id = None
        try:
            event_id = self._svc.log_event(
                ws.copilot_session_id, "insight_local",
                ts_offset_sec=ws.last_offset,
                topic_id=ws.topic_db_ids.get(ws.tracker.current), source="local",
                payload={"kind": "scope_notice", "text": text,
                         "candidates": candidates, "evidence": [], "shown": True},
            )
        except Exception as e:
            logger.debug("[copilot] persist scope notice failed: %s", e)
        self._publish(ws, "copilot_insight", {
            "id": event_id, "kind": "scope_notice", "text": text,
            "candidates": candidates, "evidence": [], "confidence": None,
            "source": "local", "topic_index": ws.tracker.current,
            "ts_offset_sec": ws.last_offset,
        })

    def _gather_chunks(self, ws: _WS, disp: Dispatcher, cur: int,
                       triage: dict) -> list[dict]:
        """RAG chunks for analysis: reuse the cache when returning to a topic, otherwise search."""
        # 1. Return to a topic → cached context (no new search).
        if ws.return_to is not None:
            cached = ws.topic_chunks.get(ws.return_to)
            if cached is None and self._svc is not None:
                try:
                    cached = self._svc.get_topic_retrieval(ws.copilot_session_id, ws.return_to)
                except Exception:
                    cached = None
            ws.return_to = None
            if cached:
                logger.info("[copilot] reused cached retrieval for topic %d (%d chunks)",
                            cur, len(cached))
                return cached
        # 2. Triage asked for a search → search the direction's archive and cache it in the topic.
        if not triage.get("needs_retrieval") or not triage.get("retrieval_query"):
            return []
        try:
            res = disp.retrieve(triage["retrieval_query"],
                                top_k=int(ws.config.get("top_k") or 7),
                                category_id=ws.config.get("category_id"),
                                scope_tids=self._scope_entity_ids(ws, self._db_path))
            chunks = res.get("chunks") or []
        except Exception as e:
            logger.warning("[copilot] RAG search failure: %s", e)
            return []
        if chunks:
            ws.topic_chunks[cur] = chunks
            if self._svc is not None:
                try:
                    self._svc.update_topic_retrieval(ws.copilot_session_id, cur, chunks)
                    self._svc.log_event(ws.copilot_session_id, "retrieval",
                                        ts_offset_sec=ws.last_offset, source="local",
                                        payload={"query": triage["retrieval_query"],
                                                 "n": len(chunks), "topic_index": cur})
                except Exception as e:
                    logger.debug("[copilot] persist retrieval failed: %s", e)
        return chunks

    # Kinds we allow WITHOUT proofs from the archive (the other unanchored ones are 7B noise).
    _UNANCHORED_KINDS = ("contradiction", "fact")

    def _build_insights(self, ws: _WS, disp: Dispatcher, chunks: list[dict],
                        triage: dict, profile: str) -> tuple[list[dict], dict]:
        """Build insights. There is a fresh archive → analysis-reconciliation (insights with evidence);
        none → primary triage observations (no evidence) — filtered as noise. Dedup:
        we do not send chunks already shown to the operator to the LLM."""
        used = {c["chunk_id"]: c for c in chunks if c.get("chunk_id") is not None}
        fresh = [c for c in chunks if c.get("chunk_id") not in ws.shown_chunks]
        if fresh:
            res = disp.analyze(window=ws.transcript, chunks=fresh, profile=profile)
            insights = (res or {}).get("insights") or []
        else:
            insights = triage.get("observations") or []
        return self._filter_unanchored(ws, insights), used

    def _filter_unanchored(self, ws: _WS, insights: list[dict]) -> list[dict]:
        """Mute noise: insights WITHOUT proofs from the archive (evidence_chunk_ids) are kept
        only if they are contradiction/fact with conf >= min_unanchored_conf (the mode threshold).
        question/clarification without proof are always dropped. Insights WITH proofs
        (analysis-reconciliation) pass as is. Runtime test 2026-06-02: without this 82% of cards
        were meaningless observations without evidence."""
        thr = float(ws.config.get("min_unanchored_conf") or 0.80)
        out: list[dict] = []
        for ins in insights:
            if ins.get("evidence_chunk_ids"):  # anchored — keep
                out.append(ins)
                continue
            if ins.get("kind") not in self._UNANCHORED_KINDS:
                continue
            try:
                conf = float(ins.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0.0
            if conf >= thr:
                out.append(ins)
        return out

    def _card_gate(self, ws: _WS) -> Optional[str]:
        """Whether a card can be shown RIGHT NOW. Returns the refusal reason or None.

        Two limiters (Track 3): the card budget per session and the pause between them.
        The pause matters separately from the budget: otherwise all 5 cards fly out in the first
        three minutes of the call and then silence — while the useful often sounds near the end.
        """
        limit = int(ws.config.get("max_cards") or 0)
        if limit and ws.cards_shown >= limit:
            return "budget"
        gap = float(ws.config.get("min_card_gap_sec") or 0)
        if gap and ws.last_card_ts and (time.monotonic() - ws.last_card_ts) < gap:
            return "too_soon"
        return None

    def _count_card(self, ws: _WS) -> None:
        ws.cards_shown += 1
        ws.last_card_ts = time.monotonic()

    def _note_suppressed(self, ws: _WS, reason: str) -> None:
        """A muted card does not vanish — it stays in the session history (an event
        in the DB) and goes into the post-call review. We tell the operator once that
        the budget is spent, so that silence is not read as "the copilot died"."""
        ws.cards_suppressed += 1
        if reason == "budget" and not ws.budget_notice_sent:
            ws.budget_notice_sent = True
            self._publish(ws, "copilot_status",
                          {"state": "quiet", "reason": "card_budget",
                           "cards_shown": ws.cards_shown,
                           "suppressed": ws.cards_suppressed})

    def _emit_insight(self, ws: _WS, topic_index: int, ins: dict,
                      chunk_meta: dict, *, source: str = "local",
                      kind_event: str = "insight_local",
                      publish: bool = True) -> tuple[Optional[int], list]:
        """Persist an insight (+ publish to the widget if publish=True).

        ``source`` 'local' (dispatcher) or 'api' (safety-sweep — verified right away).
        ``publish=False`` — the card is NOT shown to the operator (did not pass the gate or
        is waiting for the Claude verdict), but the event stays in the DB: the session history and
        the post-call review must see everything the copilot noticed."""
        evidence, full = [], {}
        for cid in ins.get("evidence_chunk_ids", []):
            c = chunk_meta.get(cid)
            if not c:
                continue
            full[cid] = c
            evidence.append({
                "chunk_id": cid,
                "transcription_id": c.get("transcription_id"),
                "source_name": c.get("source_name"),
                "meeting_date": c.get("meeting_date"),
                "start_time": c.get("start_time"),
            })
            self._mark_shown(ws, cid)
        event_id = None
        try:
            event_id = self._svc.log_event(
                ws.copilot_session_id, kind_event,
                ts_offset_sec=ws.last_offset,
                topic_id=ws.topic_db_ids.get(topic_index), source=source,
                confidence=ins.get("confidence"),
                payload={"kind": ins["kind"], "text": ins["text"],
                         "evidence": evidence, "topic_index": topic_index,
                         "escalate": self._should_escalate(ws, ins),
                         # Track 3: whether the operator saw this during the call. Muted
                         # does not vanish — it stays for the review AFTER the call,
                         # and the timeline must be able to tell them apart.
                         "shown": bool(publish)},
            )
        except Exception as e:
            logger.debug("[copilot] persist insight failed: %s", e)
        # remember for manual escalation (the "dig deeper" button, Step 4→5)
        if event_id is not None and full:
            ws.insights_by_event[event_id] = {
                "ins": ins, "chunks": full, "topic_index": topic_index}
            if len(ws.insights_by_event) > 80:
                ws.insights_by_event.pop(next(iter(ws.insights_by_event)))
        if publish:
            self._publish(ws, "copilot_insight", {
                "id": event_id, "kind": ins["kind"], "text": ins["text"],
                "evidence": evidence, "confidence": ins.get("confidence"),
                "source": source, "topic_index": topic_index,
                "ts_offset_sec": ws.last_offset,
            })
            self._count_card(ws)
        return event_id, evidence

    # ------------------------------------------------ escalation to Claude (Step 5)

    def _api_active(self, ws: _WS) -> bool:
        """Whether we can call Claude: api enabled, budget not exhausted, model present."""
        return bool(ws.config.get("api_enabled") and not ws.budget_exhausted
                    and ws.config.get("model_api"))

    def _should_escalate(self, ws: _WS, ins: dict) -> bool:
        """Escalate if there is evidence and the local model is confident enough (the mode threshold)."""
        if not ins.get("evidence_chunk_ids"):
            return False
        try:
            conf = float(ins.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        return conf >= float(ws.config.get("escalate_threshold") or 0.65)

    def _escalate_insight(self, ws: _WS, event_id: int, ins: dict,
                          chunk_meta: dict, profile: str,
                          publish_update: bool = True) -> Optional[dict]:
        """Verify a local insight through Claude; update the card + accounting/budget."""
        if not self._api_active(ws):
            return None
        full = [chunk_meta[c] for c in ins.get("evidence_chunk_ids", []) if c in chunk_meta]
        if not full:
            return None
        esc = self._get_escalator()
        votes = int(ws.config.get("verify_votes") or 1)
        # Serialize session escalations (daemon tick vs manual escalate_now from an HTTP thread)
        # and re-gate the budget under the lock — so that two escalations do not overspend
        # together (Step 9 fix #2). Accounting — PER vote, with an early stop on the
        # budget inside a multi-vote (fix #1).
        with ws.esc_lock:
            if not self._api_active(ws):
                return None
            try:
                verdict = esc.verify(
                    insight=ins, evidence_chunks=full, profile=profile,
                    model=ws.config.get("model_api"), votes=votes,
                    on_vote=lambda r: self._account_usage(ws, r),
                    can_continue=lambda: self._api_active(ws))
            except Exception as e:
                logger.warning("[copilot] verify failure: %s", e)
                return None
            if verdict is None:
                return None
            # "Opus on gnarly" (Step 6): an unsure verdict + there is an arbiter model + budget
            # still left → re-arbitration by the stronger model (only at high importance).
            gnarly = ws.config.get("model_api_gnarly")
            if verdict["verdict"] == "uncertain" and gnarly and self._api_active(ws):
                try:
                    arb = esc.verify(insight=ins, evidence_chunks=full, profile=profile,
                                     model=gnarly, votes=1,
                                     on_vote=lambda r: self._account_usage(ws, r))
                except Exception:
                    arb = None
                if arb is not None:
                    verdict = arb
        try:
            self._svc.log_event(
                ws.copilot_session_id, "insight_verified",
                ts_offset_sec=ws.last_offset, source="api",
                confidence=verdict["confidence"], tokens_in=verdict["tokens_in"],
                tokens_out=verdict["tokens_out"],
                payload={"ref_event_id": event_id, "verdict": verdict["verdict"],
                         "text": verdict["text"], "model": verdict["model"]})
        except Exception as e:
            logger.debug("[copilot] persist verified failed: %s", e)
        # publish_update=False — the card was NOT shown yet (verified_only): the update
        # of a non-existent card is simply ignored by the frontend, and the decision to show is made by
        # _process_insight by the verdict.
        if publish_update:
            self._publish(ws, "copilot_insight_update", {
                "id": event_id, "source": "api", "verdict": verdict["verdict"],
                "confidence": verdict["confidence"], "text": verdict["text"]})
        return verdict

    def _maybe_safety_sweep(self, ws: _WS, chunks: list, profile: str,
                            topic_index: int) -> Optional[dict]:
        """Forced Sonnet pass over the window once per safety_sweep_sec (insurance)."""
        sweep_sec = float(ws.config.get("safety_sweep_sec") or 0)
        if sweep_sec <= 0 or not self._api_active(ws) or not chunks or not ws.transcript:
            return None
        now = time.monotonic()
        if ws.last_sweep and (now - ws.last_sweep) < sweep_sec:
            return None
        ws.last_sweep = now
        try:
            res = self._get_escalator().sweep(
                window=ws.transcript, chunks=chunks, profile=profile,
                model=ws.config.get("model_api"))
        except Exception as e:
            logger.warning("[copilot] safety-sweep failure: %s", e)
            return None
        if res is None:
            return None
        self._account_usage(ws, res)
        meta = {c["chunk_id"]: c for c in chunks if c.get("chunk_id") is not None}
        for ins in res.get("insights", []):
            # dedup: sweep does not repeat insights already shown to the operator
            if all(cid in ws.shown_chunks for cid in ins.get("evidence_chunk_ids", [])):
                continue
            # Track 3: sweep also yields cards, and the attention budget for them is the same.
            # Otherwise the "insurance" would quietly bypass the limit and bring back noise.
            gate = self._card_gate(ws)
            if gate is not None:
                self._note_suppressed(ws, gate)
            self._emit_insight(ws, topic_index, ins, meta, source="api",
                               kind_event="insight_verified", publish=(gate is None))
        return res

    def escalate_now(self, recording_session_id: str, event_id: int) -> bool:
        """Manual escalation of a specific card (the "dig deeper" button, Step 4).
        Called from the blueprint thread. Returns True if the escalation was performed."""
        with self._lock:
            ws = self._sessions.get(recording_session_id)
        if ws is None or not self._api_active(ws):
            return False
        rec = ws.insights_by_event.get(int(event_id))
        if not rec:
            return False
        verdict = self._escalate_insight(ws, int(event_id), rec["ins"], rec["chunks"],
                                         self._get_profile(ws))
        return verdict is not None

    def update_config(self, recording_session_id: str, new_config: dict) -> bool:
        """Replace the session's behavioural config on the fly (mode/importance change during
        the call, Step 6). Resets the exhaustion flag — re-evaluated on the next usage."""
        with self._lock:
            ws = self._sessions.get(recording_session_id)
            if ws is None:
                return False
            ws.config = dict(new_config or {})
            ws.budget_exhausted = False
        logger.info("[copilot] session %s config updated on the fly (mode=%s, importance=%s)",
                    recording_session_id, ws.config.get("mode"), ws.config.get("importance"))
        return True

    def _account_usage(self, ws: _WS, usage: dict) -> None:
        """Add the accounting of an API call to the session, update the counter in the widget, check
        the budget (hard stop → degradation to local-only)."""
        if self._svc is None:
            return
        try:
            tot = self._svc.add_usage(
                ws.copilot_session_id, tokens_in=usage.get("tokens_in", 0),
                tokens_out=usage.get("tokens_out", 0),
                cache_read=usage.get("cache_read", 0), cost=usage.get("cost", 0.0))
        except Exception as e:
            logger.debug("[copilot] add_usage failed: %s", e)
            return
        if tot.get("exhausted") and not ws.budget_exhausted:
            ws.budget_exhausted = True
            logger.info("[copilot] session %s budget exhausted ($%.4f) → local-only",
                        ws.copilot_session_id, tot.get("cost_estimate"))
        # Cost-governor (Step 6): soft warn before the hard stop.
        budget = tot.get("budget_usd")
        warn_ratio = float(ws.config.get("budget_warn_ratio") or 0.8)
        warned = bool(budget and budget > 0 and not ws.budget_exhausted
                      and tot.get("cost_estimate", 0) >= warn_ratio * budget)
        state = ("budget_exhausted" if ws.budget_exhausted
                 else "warn" if warned
                 else "hybrid" if self._api_active(ws) else "local")
        self._publish(ws, "copilot_usage", {
            "tokens_in": tot.get("tokens_in"), "tokens_out": tot.get("tokens_out"),
            "cache_read": tot.get("cache_read"), "cost_estimate": tot.get("cost_estimate"),
            "budget": tot.get("budget_usd"), "state": state})

    def _mark_shown(self, ws: _WS, cid) -> None:
        """Mark a chunk as shown (for dedup) — a bounded FIFO window, so the set
        does not grow unboundedly on long calls (Step 9 fix #3)."""
        if cid in ws.shown_chunks:
            return
        ws.shown_chunks[cid] = True
        if len(ws.shown_chunks) > self._shown_cap:
            ws.shown_chunks.pop(next(iter(ws.shown_chunks)))

    def _get_profile(self, ws: _WS) -> str:
        if ws.profile is not None:
            return ws.profile
        name = None
        if self._svc is not None:
            try:
                name = self._svc.get_category_name(ws.config.get("category_id"))
            except Exception:
                name = None
        ws.profile = f"Напрямок: {name}" if name else ""
        return ws.profile

    def _publish(self, ws: _WS, event: str, data: dict) -> None:
        if self._broker is None:
            return
        try:
            self._broker.publish(f"recording:{ws.recording_session_id}", event, data)
        except Exception as e:
            logger.debug("[copilot] publish error: %s", e)
