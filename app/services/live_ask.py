"""Local question-answer — «live conversation + archive» (Story 02, mcp-live-call).

A new path that did not exist before: an agent (an MCP client) asks a clarifying question, and it is
handled by the LOCAL model (Ollama, $0), not by Claude. The sources are the live transcript
of the current call (RAM-only, `live_transcribe.LiveTranscribeWorker`) and/or the archive
(`retrieval.search`). Unlike `rag.answer_question` (Claude, paid,
citations with [n] links) and `copilot/dispatcher.py` (JSON schemas for topic
triage) — this is a simple text request/response pair, with no state and no write
anywhere: the agent's question must NOT appear in the operator's widget or in the DB
(the owner chose «only me», plan `docs/specs/mcp-live-call/plan.md` non-goals).

Pure orchestration: the LLM (`local_llm`) and the search (`retrieval.search`) are injected
(as in `copilot/dispatcher.py`) → the module is tested offline, without Ollama/torch.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

from app.core import settings as _settings
from app.core.i18n import tr
from app.services import local_llm, retrieval


logger = logging.getLogger(__name__)

# A live call transcript can grow for a long conversation — we cut it by the tail
# (the freshest is the most relevant to a question about «what was just said»), the same approach
# as `Dispatcher._clip` in the copilot.
_MAX_TRANSCRIPT_CHARS = 6000

# `generate()` holds the shared `local_llm._GEN_LOCK` (one Ollama, one GPU) —
# the same lock as the copilot ticks (`CopilotWorker._tick`, the default
# cadence `COPILOT_TOPIC_TICK_SEC=6`s, `app/services/copilot/worker.py:127`).
# Measured on this machine: Ollama serves `qwen2.5:32b-instruct` (the configured
# `qwen2.5:14b-instruct-q5_K_M` is not installed) ≈9.6 tokens/s — 512 tokens is
# ~53s of holding `_GEN_LOCK`, ten times longer than a ~6s tick. Therefore during an active
# recording `ask_local` does NOT take the lock at all (Story 08, `recording_active=True`
# below) — no token limit can give a useful answer faster than one
# tick. Outside a recording `_MAX_ANSWER_TOKENS` stays tied to the accepted
# tick budget (`Dispatcher.__init__` default `max_tokens=1024`,
# `app/services/copilot/dispatcher.py:183`) — half of it, as before.
_MAX_ANSWER_TOKENS = 512

# Protection against an uncontrolled archive search (and alignment with `_RERANK_ENABLED`
# in `rag.py` below) — top_k from the HTTP layer is also clamped (`copilot.py`), here it is
# the second line of defence for direct `ask_local` calls.
_MAX_TOP_K = 20

# `rag.answer_question` (`ask_archive`) enables rerank through the same env var
# (`app/services/rag.py::_RERANK_ENABLED`) — we read it here directly (the same
# approach as in rag.py: "so the module stays standalone-testable"), rather than
# importing rag.py, so as not to pull its dependencies (the Claude API) into the simple
# local path. Previously `ask_local` always ran with rerank=False — the divergence
# from ask_archive is removed by this flag.
_RERANK_ENABLED = _settings.env_bool("RECALL_RERANK_ENABLED")

_SYSTEM_PROMPT = (
    "Ти — локальний асистент оператора під час дзвінка. Тобі дають ПИТАННЯ і, "
    "за наявності, ЖИВИЙ ТРАНСКРИПТ поточної розмови та/або ФРАГМЕНТИ АРХІВУ "
    "минулих записів. Відповідай стисло і по суті, спираючись ВИКЛЮЧНО на надані "
    "дані. Якщо даних не вистачає для відповіді — прямо скажи про це, не вигадуй."
)

_SCOPES = ("call", "archive", "both")


def _clip(text: str) -> str:
    text = (text or "").strip()
    return text[-_MAX_TRANSCRIPT_CHARS:] if len(text) > _MAX_TRANSCRIPT_CHARS else text


def _to_source(chunk: dict) -> dict:
    """A `retrieval.search` chunk → C2 provenance (`chunk_id/transcription_id/title/date/snippet`)."""
    text = (chunk.get("text") or "").strip()
    snippet = text if len(text) <= 400 else text[:400].rstrip() + "…"
    return {
        "chunk_id": chunk.get("chunk_id"),
        "transcription_id": chunk.get("transcription_id"),
        "title": chunk.get("source_name"),
        "date": chunk.get("meeting_date"),
        "snippet": snippet,
    }


def _empty_reason(scope: str) -> str:
    if scope == "call":
        return tr("svc_recording.live_ask.empty_call")
    if scope == "archive":
        return tr("svc_recording.live_ask.empty_archive")
    return tr("svc_recording.live_ask.empty_both")


def _build_prompt(question: str, transcript_text: str, sources: list[dict]) -> str:
    parts = [f"ПИТАННЯ: {question}"]
    if transcript_text:
        parts.append(f"ЖИВИЙ ТРАНСКРИПТ ПОТОЧНОЇ РОЗМОВИ:\n{transcript_text}")
    if sources:
        ctx = "\n\n".join(
            f"[{i}] {s.get('title') or '?'} ({s.get('date') or '?'})\n{s.get('snippet') or ''}"
            for i, s in enumerate(sources, 1)
        )
        parts.append(f"ФРАГМЕНТИ АРХІВУ:\n{ctx}")
    return "\n\n".join(parts)


def _raw_response(scope: str, used_transcript: str, sources: list[dict]) -> dict:
    """Story 08: during an active recording the local model is not called
    at all — the agent gets raw data (the live transcript + the found archive
    chunks) and draws the conclusion itself. ``mode="raw"`` is an explicit, self-explanatory
    marker (acceptance), so that the agent never confuses raw data with the model's
    answer. ``transcript_text`` here is full (cut by `_clip`), not just a
    character counter — otherwise «raw data» would give the agent nothing to read."""
    available = bool(used_transcript or sources)
    return {
        "answer": "", "model": "", "scope": scope,
        "used_transcript_chars": len(used_transcript), "sources": sources,
        "transcript_text": used_transcript or None,
        "available": available,
        "reason": None if available else _empty_reason(scope),
        "mode": "raw",
    }


def ask_local(
    db_path: str,
    question: str,
    *,
    transcript_text: Optional[str] = None,
    scope: str = "both",
    top_k: int = 6,
    llm: Any = local_llm,
    search_fn: Optional[Callable[..., dict]] = None,
    recording_active: bool = False,
) -> dict:
    """A question → (live transcript and/or archive) → the local model → an answer.

    ``scope``: ``call`` (live transcript only) | ``archive`` (archive only) |
    ``both`` (default, both). ``call`` does NOT touch `search_fn`, ``archive``
    does NOT read ``transcript_text`` — the C2 contract/acceptance of this story.

    ``recording_active`` (Story 08): while a recording is in progress, `llm` gets NO
    call at all (neither `availability()` nor `generate()`) — the shared generation
    lock (`local_llm._GEN_LOCK`, the same one held by the copilot ticks)
    is not taken even for a moment. The caller (the HTTP layer `copilot.py`) determines this by
    the presence of an active recording session and does NOT accept a request parameter — the agent has
    no way to force generation during a call (non-goals/acceptance).
    The archive search (``search_fn``) runs in both modes as always —
    only the generation is cut.

    Never raises an exception outward: Ollama being unavailable or an empty context is
    ``available=False`` with a human-readable ``reason``, not a 500.

    Returns ``{"answer", "model", "scope", "used_transcript_chars", "sources",
    "available", "reason", "mode"}`` — the C2 shape without the HTTP wrapper (``success``/
    ``session_id`` is added by the blueprint). ``mode`` is ``"raw"`` (a recording is in progress, data without
    the model's answer) or ``"generated"`` (the normal path).
    """
    search_fn = search_fn or retrieval.search
    scope = scope if scope in _SCOPES else "both"
    question = (question or "").strip()
    try:
        top_k = int(top_k)
    except (TypeError, ValueError, OverflowError):  # e.g. float('inf') — the HTTP layer
        top_k = 6                                    # already filters this out to 400, here it is a fallback
    top_k = min(max(top_k, 1), _MAX_TOP_K)

    used_transcript = _clip(transcript_text) if scope in ("call", "both") else ""

    sources: list[dict] = []
    if scope in ("archive", "both"):
        try:
            res = search_fn(db_path, question, top_k=top_k, rerank=_RERANK_ENABLED)
            chunks = res.get("chunks") or []
        except Exception as e:  # network/DB/embeddings — we degrade, we do not fall
            logger.warning("[live_ask] archive search failed: %s", e)
            chunks = []
        sources = [_to_source(c) for c in chunks]

    if recording_active:
        return _raw_response(scope, used_transcript, sources)

    if not used_transcript and not sources:
        return {
            "answer": "", "model": "", "scope": scope,
            "used_transcript_chars": 0, "sources": [],
            "available": False, "reason": _empty_reason(scope), "mode": "generated",
        }

    ok, reason = llm.availability()
    if not ok:
        return {
            "answer": "", "model": "", "scope": scope,
            "used_transcript_chars": len(used_transcript), "sources": sources,
            "available": False, "reason": reason, "mode": "generated",
        }

    prompt = _build_prompt(question, used_transcript, sources)
    try:
        resp = llm.generate(prompt, system=_SYSTEM_PROMPT, max_tokens=_MAX_ANSWER_TOKENS)
    except Exception as e:  # LocalLLMError / network — the same degradation as dispatcher._run
        logger.warning("[live_ask] generation failed: %s", e)
        return {
            "answer": "", "model": "", "scope": scope,
            "used_transcript_chars": len(used_transcript), "sources": sources,
            "available": False, "reason": tr("svc_recording.live_ask.llm_failed", error=e),
            "mode": "generated",
        }

    answer = (resp.get("response") or "").strip()
    model = resp.get("model") or getattr(llm, "LOCAL_LLM_MODEL", "")
    return {
        "answer": answer, "model": model, "scope": scope,
        "used_transcript_chars": len(used_transcript), "sources": sources,
        "available": True, "reason": None, "mode": "generated",
    }
