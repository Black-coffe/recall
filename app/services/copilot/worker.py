"""Co-pilot — CopilotWorker (Phase 19, Крок 2).

Один daemon-thread на активну сесію (за зразком :class:`LiveTranscribeWorker`).
Кожен тік:
  1. перевіряє паузу запису (recording_service.get_state.is_paused) — на паузі
     ідлить (вимога оператора), не аналізує;
  2. забирає нові live-сегменти (live_transcribe_worker.get_preview), накопичує
     вікно тексту;
  3. коли вікно достатнє — годує :class:`TopicTracker`;
  4. на зсув/повернення теми — персистить у copilot_topics/_events і публікує
     ``copilot_topic`` / ``copilot_status`` у SSE-канал ``recording:{sid}``
     (record.js уже підписаний).

$0: лише локальні ембеддинги, жодних API-викликів. Усе деградує: нема ембеддингів
або live-воркера → топік-трекінг просто не стартує, запис працює як раніше.

Тестованість: вся логіка одного проходу — у :meth:`_tick`, який можна викликати
напряму з підставними (broker/live/recording/embed_fn) — офлайн, без мікрофона.
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
from app.services.copilot.dispatcher import Dispatcher
from app.services.copilot.escalate import Escalator
from app.services.copilot.topics import TopicTracker


logger = logging.getLogger(__name__)


def _scope_entity_ids(ws, db_path: Optional[str]) -> Optional[list]:
    """Сутності (проєкт/учасники) поточної сесії — тонкий скоуп пошуку (Трек 2).

    Резолвимо ОДИН раз на сесію і кешуємо у стані воркера: під час дзвінка кожен
    зайвий запит до БД їсть той самий бюджет часу, що й корисна робота. Порожній
    список = скоупу нема, пошук іде як раніше (по категорії).
    """
    cached = getattr(ws, "_scope_ids", None)
    if cached is not None:
        return cached or None
    names = (ws.config or {}).get("scope_projects") or []
    ids: list = []
    if names:
        try:
            from app.services import scope as scope_svc
            ids = scope_svc.scope_filter_ids(db_path, names) if db_path else []
        except Exception as exc:
            logger.warning("[copilot] не вдалось розібрати скоуп %s: %s", names, exc)
            ids = []
        if names and not ids:
            logger.info("[copilot] скоуп %s не знайдено у графі — шукаю без звуження", names)
    try:
        ws._scope_ids = ids
    except Exception:
        logger.debug("[copilot] стан сесії не кешує скоуп (dataclass із slots?)")
    return ids or None


@dataclass
class _WS:
    """Стан воркера для однієї сесії."""
    copilot_session_id: int
    recording_session_id: str
    config: dict
    tracker: TopicTracker
    seen: set = field(default_factory=set)          # ключі вже спожитих сегментів
    shown_chunks: dict = field(default_factory=dict)  # cid -> True, insertion-ordered (обмежене вікно дедупу)
    pending: list = field(default_factory=list)     # накопичений текст до вікна
    pending_chars: int = 0
    carryover: str = ""                             # хвіст попереднього вікна (контекст)
    last_offset: float = 0.0                        # макс. end-таймкод (зсув у записі)
    topic_db_ids: dict = field(default_factory=dict)  # topic_index -> copilot_topics.id
    paused: bool = False
    # --- Трек 3: бюджет уваги оператора ---
    cards_shown: int = 0                             # скільки карток уже показано
    last_card_ts: float = 0.0                        # time.monotonic останньої картки
    cards_suppressed: int = 0                        # скільки притишено (йдуть у пост-бриф)
    budget_notice_sent: bool = False
    # --- Крок 3: диспетчер-LLM (триаж + RAG + інсайти) ---
    transcript: str = ""                             # ковзний буфер реплік для диспетчера
    summary: str = ""                                # rolling-summary сесії (in-memory)
    topic_chunks: dict = field(default_factory=dict)  # topic_index -> закешовані RAG-чанки
    return_to: Optional[int] = None                  # тема, до якої щойно повернулись (reuse cache)
    last_dispatch: float = 0.0                       # time.monotonic останнього диспатчу
    dispatch_count: int = 0
    profile: Optional[str] = None                    # назва напрямку (лінива)
    llm_warned: bool = False
    # --- Крок 5: ескалація в Claude (старший брат) ---
    budget_exhausted: bool = False                   # хард-стоп бюджету → локальний-only
    last_sweep: float = 0.0                          # time.monotonic останнього safety-sweep
    insights_by_event: dict = field(default_factory=dict)  # event_id -> {ins, chunks, topic_index} (для ручної ескалації)
    esc_lock: threading.Lock = field(default_factory=threading.Lock)  # серіалізує ескалації сесії (daemon tick vs ручна escalate_now)
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: Any = None


class CopilotWorker:
    """Крутить топік-трекери активних сесій. Singleton у app.state.copilot_worker."""

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
        self._dispatcher = dispatcher  # None → будуємо лінива в start() з db_path
        self._escalator = escalator    # None → лінива Escalator() (Claude через text_polishing)
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
        """Лінива побудова диспетчера (потрібен db_path для RAG). None якщо нема
        db_path (тоді Крок-3 шар вимкнено, теми працюють як на Кроці 2)."""
        if self._dispatcher is not None:
            return self._dispatcher
        if not self._db_path:
            return None
        self._dispatcher = Dispatcher(db_path=self._db_path, llm=self._llm)
        return self._dispatcher

    def _get_escalator(self) -> Escalator:
        """Лінива побудова ескалатора (Claude через text_polishing._get_client)."""
        if self._escalator is None:
            self._escalator = Escalator()
        return self._escalator

    # ----------------------------------------------------------- lifecycle

    def start(self, *, copilot_session_id: int, recording_session_id: str,
              config: Optional[dict] = None) -> None:
        """Запустити топік-трекінг для сесії. Idempotent. М'яко пропускає, якщо
        локальні ембеддинги недоступні (дефолтний embed_fn)."""
        if self._embed_fn is embeddings.embed_query and not embeddings.is_available():
            logger.info("[copilot] ембеддинги недоступні — топік-трекінг вимкнено для %s",
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
        logger.info("[copilot] worker запущено для %s (copilot_session=%s, tick=%.0fs)",
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
                except Exception as e:  # один збій тіку не валить воркер
                    logger.warning("[copilot] tick error %s: %s",
                                   ws.recording_session_id, e, exc_info=True)
        finally:
            logger.info("[copilot] worker зупинено для %s (тем=%d)",
                        ws.recording_session_id, len(ws.tracker.topics))

    def _tick(self, ws: _WS) -> Optional[dict]:
        """Один прохід. Повертає результат диспатчу (для тестів) або None."""
        # 1. Пауза запису → ідлимо (вимога оператора).
        if self._is_paused(ws.recording_session_id):
            if not ws.paused:
                ws.paused = True
                self._publish(ws, "copilot_status", {"state": "paused"})
            return None
        if ws.paused:
            ws.paused = False
            self._publish(ws, "copilot_status", {"state": "analyzing"})

        # 2. Нові сегменти → ковзний буфер диспетчера + зсув у записі.
        new_text, max_end = self._collect_new(ws)
        if new_text:
            ws.transcript = (ws.transcript + " " + new_text).strip()[-self._transcript_chars:]
            if max_end is not None:
                ws.last_offset = max_end

        # 3. Диспетчер-LLM — за каденсом. Теми тепер визначає САМА LLM у триажі
        #    (continue/shift/return), не косинусний поріг по вікну.
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
                centroid_bytes=None,  # теми LLM-керовані — центроїди не зберігаємо
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

        # Повернення до теми → наступний диспатч переюзає закешований RAG-контекст
        # цієї теми, а не шукає заново (економія + консистентність, Крок 3).
        if ev["action"] == "return":
            ws.return_to = idx

        self._publish(ws, "copilot_topic", {
            "action": ev["action"], "topic_index": idx, "label": ev["label"],
            "returned_from": ev.get("returned_from"), "ts_offset_sec": ev.get("ts"),
        })

    def _persist_topic_label(self, ws: _WS, topic_index: int, label: str) -> None:
        """Оновити назву теми від LLM-триажу в БД + relabel у віджеті. Тема вже існує
        в copilot_topics (її створив _handle_topic_event на зсуві), тож upsert_topic
        лише перепише label (COALESCE). Live-смужка перемальовується через
        copilot_topic action=relabel (applyCopilotTopic уже вміє оновити наявний чип)."""
        if self._svc is not None and topic_index in ws.topic_db_ids:
            try:
                self._svc.upsert_topic(ws.copilot_session_id, topic_index, label=label)
            except Exception as e:
                logger.debug("[copilot] persist topic label failed: %s", e)
        self._publish(ws, "copilot_topic", {
            "action": "relabel", "topic_index": topic_index, "label": label,
            "ts_offset_sec": ws.last_offset,
        })

    # ----------------------------------------------------- диспетчер (Крок 3)

    def _maybe_dispatch(self, ws: _WS) -> Optional[dict]:
        """Каденс-гейтований прохід диспетчера: триаж → (RAG) → інсайти.

        Повертає словник з результатом (для тестів) або None, якщо не час / нема
        тексту / LLM недоступний. Усе деградує: збій LLM → лог + None, теми живуть.
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

        # LLM доступний? (короткий кеш у local_llm; деградуємо м'яко)
        if self._llm is local_llm and not local_llm.is_available():
            if not ws.llm_warned:
                ws.llm_warned = True
                logger.info("[copilot] локальний LLM недоступний — диспетчер вимкнено "
                            "для %s (%s)", ws.recording_session_id,
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

        # Тема — рішення САМОЇ LLM (continue/shift/return), а не косинусний поріг.
        # Трекер застосовує його детерміновано й повертає подію.
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

        # Safety-sweep: незалежний Sonnet-прохід по вікну раз на T (страховка).
        sweep_chunks = chunks or ws.topic_chunks.get(cur) or []
        self._maybe_safety_sweep(ws, sweep_chunks, profile, cur)

        # rolling-summary раз на N диспатчів (тримає контекст малим)
        ws.dispatch_count += 1
        if self._summary_every > 0 and ws.dispatch_count % self._summary_every == 0:
            new_sum = disp.summarize(prev_summary=ws.summary, window=ws.transcript)
            if new_sum:
                ws.summary = new_sum

        return {"triage": triage, "insights": insights}

    def _process_insight(self, ws: _WS, topic_index: int, ins: dict,
                         used_chunks: dict, profile: str) -> None:
        """Один інсайт: персист завжди, показ — за гейтом і вердиктом Claude.

        Трек 3 перевертає каскад. Було: локальна картка показувалась одразу, а
        Claude перевіряв уже показане (і лише 5.9% карток узагалі доходили до
        перевірки). Стало: спершу вердикт, показуємо лише `real`. Це і є «тихіше
        і точніше» — обсяг падає на порядок, а те, що лишилось, підтверджене.

        Мʼяка деградація: якщо API недоступний (немає ключа, вичерпано бюджет,
        режим «лише локально») — verified_only не діє, інакше копілот замовк би
        зовсім; тоді показуємо локальне, але тільки анкороване доказами.
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

        # verified_only: персистимо мовчки, показуємо лише після вердикту `real`.
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
        # Показуємо ВЕРИФІКОВАНИЙ текст (Claude його ще й полірує), не чернетку локалки.
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

    #: Скільки коментарів оператора тягнемо у вікно аналізу. Стеля мала
    #: навмисно: контекст 7B і так тісний, а свіжі репліки корисніші за
    #: написані пів години тому — беремо ОСТАННІ.
    _OPERATOR_COMMENTS = 4

    #: Окрема смуга id для коментарів оператора. Відʼємні id вже зайняті шаром
    #: коментарів у `retrieval` (там це `-comment_chunks.id`), а тут ключ —
    #: `comments.id`: різні таблиці, простори перетинаються, і в одному вікні
    #: могли б трапитись два різні рядки під `-5`. Тоді `evidence_chunk_ids`
    #: інсайту вказував би невідомо на що.
    _OPERATOR_CHUNK_BASE = -1_000_000_000

    def _operator_comments(self, ws: _WS) -> list[dict]:
        """Коментарі, які оператор вписав ПІД ЧАС цього дзвінка (Волна 4).

        Це найсильніший сигнал у вікні: людина щойно свідомо сформулювала, що
        насправді відбувається, — точніше за будь-що, що дала розшифровка чи
        архів. Тому вони йдуть у контекст ПЕРШИМИ і незалежно від того, чи
        триаж просив пошук: коментар не є відповіддю на запит, він є вказівкою.

        Подаються у формі звичайного чанка, щоб `analyze()` міг послатись на
        них через `evidence_chunk_ids` без окремої гілки — відрізняє їх
        `source_type='comment'`, за яким `_fmt_chunks` ставить іншу мітку.
        Дедуп `shown_chunks` діє як для решти: показаний коментар не
        повторюється карткою вдруге.

        Деградує тихо: немає таблиці (стара БД), немає БД — копілот працює як
        раніше. Це надбудова, а не умова роботи.
        """
        if not self._db_path or not ws.recording_session_id:
            return []
        try:
            from app.services import comments as comments_svc
            rows = comments_svc.list_for(self._db_path, "recording_session",
                                         ws.recording_session_id)
        except Exception as e:
            logger.debug("[copilot] коментарі оператора не прочитано: %s", e)
            return []
        if not rows:
            return []
        # `list_for` віддає закріплені ПЕРШИМИ, тож зріз «останні N» відрізав би
        # рівно ті, які оператор свідомо підняв. Беремо закріплені плюс
        # найсвіжіші з решти — і те, і те свідомо важливе, але з різних причин.
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
                # Відрізняє живу репліку цього дзвінка від архівного коментаря,
                # що приїхав із RAG — див. dispatcher._fmt_chunks.
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

    def _gather_chunks(self, ws: _WS, disp: Dispatcher, cur: int,
                       triage: dict) -> list[dict]:
        """RAG-чанки для аналізу: reuse кешу при поверненні теми, інакше пошук."""
        # 1. Повернення до теми → закешований контекст (без нового пошуку).
        if ws.return_to is not None:
            cached = ws.topic_chunks.get(ws.return_to)
            if cached is None and self._svc is not None:
                try:
                    cached = self._svc.get_topic_retrieval(ws.copilot_session_id, ws.return_to)
                except Exception:
                    cached = None
            ws.return_to = None
            if cached:
                logger.info("[copilot] reused cached retrieval for topic %d (%d чанків)",
                            cur, len(cached))
                return cached
        # 2. Триаж попросив пошук → шукаємо в архіві напрямку й кешуємо в тему.
        if not triage.get("needs_retrieval") or not triage.get("retrieval_query"):
            return []
        try:
            res = disp.retrieve(triage["retrieval_query"],
                                top_k=int(ws.config.get("top_k") or 7),
                                category_id=ws.config.get("category_id"),
                                scope_tids=_scope_entity_ids(ws, self._db_path))
            chunks = res.get("chunks") or []
        except Exception as e:
            logger.warning("[copilot] RAG пошук збій: %s", e)
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

    # Види, які допускаємо БЕЗ пруфів з архіву (решта unanchored — шум від 7B).
    _UNANCHORED_KINDS = ("contradiction", "fact")

    def _build_insights(self, ws: _WS, disp: Dispatcher, chunks: list[dict],
                        triage: dict, profile: str) -> tuple[list[dict], dict]:
        """Сформувати інсайти. Є свіжий архів → аналіз-сверка (інсайти з евіденс);
        нема → первинні спостереження триажу (без евіденс) — фільтруємо як шум. Дедуп:
        у LLM не шлемо чанки, вже показані оператору."""
        used = {c["chunk_id"]: c for c in chunks if c.get("chunk_id") is not None}
        fresh = [c for c in chunks if c.get("chunk_id") not in ws.shown_chunks]
        if fresh:
            res = disp.analyze(window=ws.transcript, chunks=fresh, profile=profile)
            insights = (res or {}).get("insights") or []
        else:
            insights = triage.get("observations") or []
        return self._filter_unanchored(ws, insights), used

    def _filter_unanchored(self, ws: _WS, insights: list[dict]) -> list[dict]:
        """Притишити шум: інсайти БЕЗ пруфів з архіву (evidence_chunk_ids) лишаємо
        тільки якщо це contradiction/fact з conf >= min_unanchored_conf (поріг режиму).
        question/clarification без пруфа — відкидаємо завжди. Інсайти З пруфами
        (аналіз-сверка) проходять як є. Рантайм-тест 2026-06-02: без цього 82% карток
        були беззмістовними спостереженнями без доказів."""
        thr = float(ws.config.get("min_unanchored_conf") or 0.80)
        out: list[dict] = []
        for ins in insights:
            if ins.get("evidence_chunk_ids"):  # анкорений — лишаємо
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
        """Чи можна ЗАРАЗ показати картку. Повертає причину відмови або None.

        Два обмежувачі (Трек 3): бюджет карток на сесію і пауза між ними.
        Пауза важлива окремо від бюджету: інакше всі 5 карток вилітають у перші
        три хвилини дзвінка й далі тиша — а корисне часто звучить під кінець.
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
        """Притишена картка не зникає — вона лишається в історії сесії (подія
        в БД) і потрапляє в пост-колл розбір. Оператору один раз кажемо, що
        бюджет вичерпано, щоб тиша не читалась як «копілот помер»."""
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
        """Персист інсайту (+ публікація у віджет, якщо publish=True).

        ``source`` 'local' (диспетчер) або 'api' (safety-sweep — одразу перевірено).
        ``publish=False`` — картку НЕ показуємо оператору (не пройшла гейт або
        чекає вердикту Claude), але подія лишається в БД: історія сесії і
        пост-колл розбір мають бачити все, що копілот помітив."""
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
                         # Трек 3: чи бачив це оператор під час дзвінка. Притишене
                         # не зникає — воно лишається для розбору ПІСЛЯ дзвінка,
                         # і таймлайн має вміти їх розрізняти.
                         "shown": bool(publish)},
            )
        except Exception as e:
            logger.debug("[copilot] persist insight failed: %s", e)
        # запам'ятати для ручної ескалації (кнопка «копнути глибше», Крок 4→5)
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

    # ------------------------------------------------ ескалація в Claude (Крок 5)

    def _api_active(self, ws: _WS) -> bool:
        """Чи можна дзвонити в Claude: api увімкнено, бюджет не вичерпано, модель є."""
        return bool(ws.config.get("api_enabled") and not ws.budget_exhausted
                    and ws.config.get("model_api"))

    def _should_escalate(self, ws: _WS, ins: dict) -> bool:
        """Ескалювати, якщо є докази й локалка достатньо впевнена (поріг режиму)."""
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
        """Верифікувати локальний інсайт через Claude; оновити картку + облік/бюджет."""
        if not self._api_active(ws):
            return None
        full = [chunk_meta[c] for c in ins.get("evidence_chunk_ids", []) if c in chunk_meta]
        if not full:
            return None
        esc = self._get_escalator()
        votes = int(ws.config.get("verify_votes") or 1)
        # Серіалізуємо ескалації сесії (daemon tick vs ручна escalate_now з HTTP-потоку)
        # і повторно гейтуємо бюджет під локом — щоб дві ескалації не перевитратили
        # разом (Крок 9 fix #2). Облік — ПО кожному голосу, з раннім стопом за
        # бюджетом усередині мульти-голосу (fix #1).
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
                logger.warning("[copilot] verify збій: %s", e)
                return None
            if verdict is None:
                return None
            # «Opus на гнарлі» (Крок 6): невпевнений вердикт + є модель-арбітр + бюджет
            # ще лишився → переарбітраж сильнішою моделлю (тільки на high-важливості).
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
        # publish_update=False — картку ще НЕ показували (verified_only): апдейт
        # неіснуючої картки фронт просто проігнорує, а рішення про показ ухвалює
        # _process_insight за вердиктом.
        if publish_update:
            self._publish(ws, "copilot_insight_update", {
                "id": event_id, "source": "api", "verdict": verdict["verdict"],
                "confidence": verdict["confidence"], "text": verdict["text"]})
        return verdict

    def _maybe_safety_sweep(self, ws: _WS, chunks: list, profile: str,
                            topic_index: int) -> Optional[dict]:
        """Форсований Sonnet-прохід по вікну раз на safety_sweep_sec (страховка)."""
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
            logger.warning("[copilot] safety-sweep збій: %s", e)
            return None
        if res is None:
            return None
        self._account_usage(ws, res)
        meta = {c["chunk_id"]: c for c in chunks if c.get("chunk_id") is not None}
        for ins in res.get("insights", []):
            # дедуп: sweep не повторює вже показані оператору інсайти
            if all(cid in ws.shown_chunks for cid in ins.get("evidence_chunk_ids", [])):
                continue
            # Трек 3: sweep — теж картки, і бюджет уваги для них той самий.
            # Інакше «страховка» тихо обходила б обмеження і повертала шум.
            gate = self._card_gate(ws)
            if gate is not None:
                self._note_suppressed(ws, gate)
            self._emit_insight(ws, topic_index, ins, meta, source="api",
                               kind_event="insight_verified", publish=(gate is None))
        return res

    def escalate_now(self, recording_session_id: str, event_id: int) -> bool:
        """Ручна ескалація конкретної картки (кнопка «копнути глибше», Крок 4).
        Викликається з blueprint-потоку. Повертає True, якщо ескалацію виконано."""
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
        """Підмінити поведінковий конфіг сесії на льоту (зміна режиму/важливості під
        час дзвінка, Крок 6). Скидає прапор вичерпання — переоцінимо на наст. usage."""
        with self._lock:
            ws = self._sessions.get(recording_session_id)
            if ws is None:
                return False
            ws.config = dict(new_config or {})
            ws.budget_exhausted = False
        logger.info("[copilot] конфіг сесії %s оновлено на льоту (mode=%s, importance=%s)",
                    recording_session_id, ws.config.get("mode"), ws.config.get("importance"))
        return True

    def _account_usage(self, ws: _WS, usage: dict) -> None:
        """Додати облік API-виклику до сесії, оновити лічильник у віджеті, перевірити
        бюджет (хард-стоп → деградація в локальний-only)."""
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
            logger.info("[copilot] бюджет сесії %s вичерпано ($%.4f) → локальний-only",
                        ws.copilot_session_id, tot.get("cost_estimate"))
        # Cost-governor (Крок 6): м'який warn перед хард-стопом.
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
        """Позначити чанк як показаний (для дедупу) — обмежене FIFO-вікно, щоб сет
        не ріс необмежено на довгих дзвінках (Крок 9 fix #3)."""
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
