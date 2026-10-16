"""Local embeddings + chunking (Phase 13B).

Semantic layer of the RAG archive. A transcript is cut into chunks (windows
along speaker turns with overlap), each encoded by a local multilingual model
(default: multilingual-e5-large on an RTX 3090, 1024-dim; configurable via
EMBED_MODEL/EMBED_VERSION, the query/passage prefix style follows the model
family, see _style_for()) into a float32 vector -> stored as a BLOB in the
chunks table.
(Why NOT bge-m3: see the comment next to EMBED_MODEL below.)

Why local rather than cloud: free over thousands of chunks, private (nothing
leaves the machine), excellent for UA/RU, the GPU is already there. The model
is a lazy singleton, pulled from HF on first use (~2.2GB in
~/.cache/huggingface).

Everything is optional: if sentence-transformers/torch are unavailable,
is_available() is False, embeddings are simply switched off, FTS5 search keeps
working.

Wave B (story 05): every chunk also carries a CONTEXT PREFIX
(build_context_prefix): type/title/date/speaker/direction|thread|page + a
summary line of the unit of meaning. directions-projects-tags-03 (new epoch,
the number is set by the `EMBED_VERSION` line in `.env`, see the comment next
to the constant): the direction became a PATH "Direction -> Project" and is
printed for ALL record types, and a "tags: ..." line appeared under line 1.
The prefix goes into the vector and into chunks_fts (column
chunks.context_prefix, migration v43), but NOT into chunks.text: quotes,
export and threads still see only what was said.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
from datetime import datetime
from typing import Optional

import numpy as np

from app.db.connection import get_db_connection
# Single taxonomy resolver (contract C2): we take ONLY `norm_name` from here;
# the module is stdlib-only, no torch/flask, so the import is safe for the stdio MCP too.
from app.services import categories as _categories


logger = logging.getLogger(__name__)

# Model (override via env). multilingual-e5-large: 1024-dim, multilingual,
# excellent for UA/RU. Why NOT bge-m3: bge-m3 only has pytorch_model.bin (no
# safetensors), and transformers>=4.56 blocks torch.load on torch<2.6 (CVE-2025-32434),
# while our torch is pinned to 2.5.1 (faster-whisper/ctranslate2 stability). e5-large
# has model.safetensors -> loads without problems. Cost: 512-token limit + e5
# needs query:/passage: prefixes (handled below via _style_for()).
#
# production-rag-wave-b, story 04: switch to Qwen/Qwen3-Embedding-0.6B -
# the pair `EMBED_MODEL=Qwen/Qwen3-Embedding-0.6B` + a fresh epoch number (both
# as lines in `.env`), rollback with the same two. Code defaults did NOT change
# (Non-goals) - this is an optional switch, not a new default.
EMBED_MODEL = os.environ.get("EMBED_MODEL", "intfloat/multilingual-e5-large")
EMBED_DIM = 1024  # default until the model is lazily loaded; _apply_model_dim() replaces it with the real value
# T6.8: EMBED_VERSION is now STRUCTURALLY wired into the idempotency check in
# chunk_and_embed_transcription(): the version is stored next to embedding_model
# in transcriptions.embedding_version (migration v28) and compared when deciding
# "is a re-embed needed". Before, the constant was declarative (read nowhere),
# so a change of the chunking LOGIC without a model name change was NOT detected.
#
# v2 (T6.5, 14.08.2026): audio segments are cut on natural boundaries (pause /
# speaker change), not only on buffer overflow; the cutting changed, so
# existing vectors are stale. The bump means: the indexer work list
# (enrichment.list_unenriched_ids) starts returning the WHOLE archive for re-embed
# (locally, without Claude; the card is skipped by enrichment_version).
#
# EMBED_VERSION is configured via env (registry app/core/settings.py), and
# ONLY through it: ADR-016 ("config first, code default second") keeps the code
# default at 2 regardless of which epoch the live DB currently carries. The live
# DB went through the following epochs exactly that way: a line in `.env`, the code default did not move.
#
# directions-projects-tags-03 changes the very text that goes into the embedder and into
# `chunks_fts.context_prefix` again: the chunk context prefix carries the direction
# PATH ("Direction -> Project") for ALL record types and a "tags: ..." line, so existing
# vectors are stale. The new epoch is switched on by a line in `.env` (step 1 of the
# migration runbook), and the number is taken STRICTLY above the pair the live DB holds:
#   SELECT embedding_model, embedding_version, COUNT(*)
#     FROM transcriptions GROUP BY 1, 2;
# A number that already exists in the DB would not separate the formats: the ADR-017 pin ("a vector never
# mixes epochs") would compare old and new prefixes as one epoch, and
# `reembed.run --dry-run` would show 0. This is exactly what PREFIX_FORMAT below guards -
# check the pair BEFORE restart: `python -m app.services.reembed epoch-check`.
EMBED_VERSION = int(os.environ.get("EMBED_VERSION", "2"))

# Format of the chunk context prefix built by THIS code (a code fact, not a
# setting: there is deliberately no env for it):
#   1 - the prefix before the directions-projects-tags spec (without the "Direction ->
#       Project" path for all types and without the "tags: ..." line);
#   2 - current.
# Rule: chunks of the new format may only be written into an epoch NOBODY
# built with an older format. Which format built each pair is remembered by the
# `embedding_epochs` table (migration v46; everything that existed before it is
# format 1); the list of archive pairs comes from the same SQL as above:
#   SELECT embedding_model, embedding_version, COUNT(*)
#     FROM transcriptions GROUP BY 1, 2;
# A number matching an older format = REFUSAL to write (epoch_status below), not
# a silent mix of two formats in one retrieval.search selection.
PREFIX_FORMAT = 2

# Sequence length ceiling. Needed because NOT every model ships one
# itself: e5 has sentence_bert_config.json with max_seq_length=512, while
# Qwen3-Embedding-0.6B has no such file at all, and sentence-transformers
# then takes min(config.max_position_embeddings, tokenizer.model_max_length)
# (Transformer.py:98), i.e. 32768. The consequence was measured on 17.09.2026 on a snapshot re-embed:
# monster chunks (in the live archive max 73k characters ~ 18k tokens -
# documents and TG, the _MAX_CHARS=1000 limit only applies to the audio path) went through
# all 28 attention layers at full length, the torch caching allocator held a 17.3 GB VRAM
# peak and never released it, video memory overflowed, WDDM started
# evicting it to RAM - speed dropped 30 times (45 -> 1.5 records/min).
#
# 1024, not 512 "as in e5": on our Cyrillic the Qwen3 tokenizer gives 2.02
# characters/token versus 3.57 in e5 (measured on 300 live chunks), so a normal
# chunk is p50=285, p95=471, max=551 tokens. 512 would already truncate normal
# chunks. At 1024 a normal chunk fits whole, and monsters are cut at ~2070
# characters versus ~1830 in e5 - so both models read almost the same number of
# CHARACTERS, and the eval difference stays a difference of models, not of cut depth.
# Applied only as a CEILING: a model with its own smaller limit (e5 - 512)
# is not touched.
EMBED_MAX_SEQ = int(os.environ.get("EMBED_MAX_SEQ", "1024"))


def _style_for(model_name: str) -> str:
    """Query/passage prefix style by model FAMILY (derived from the name of
    EMBED_MODEL; there is deliberately no separate env variable for the style, the family and
    the model name are one and the same decision).

    - ``e5`` (``intfloat/multilingual-e5-*``): query:/passage: prefixes.
    - ``qwen3`` (``Qwen/Qwen3-Embedding-*``): query instruction per the model
      card on HF (``Instruct: {task}\\nQuery: {q}``), passage WITHOUT a prefix.
    - other (``plain``): no prefixes, a plain encode.
    """
    name = (model_name or "").lower()
    if "qwen3" in name:
        return "qwen3"
    if "e5" in name:
        return "e5"
    return "plain"


_STYLE = _style_for(EMBED_MODEL)

# Task for the Qwen3 query instruction (model card on HF) - generic
# retrieval, the same text for any question over the archive.
_QWEN3_QUERY_TASK = "Given a search query, retrieve relevant passages that answer the query"

# Chunking. _MAX_CHARS is kept below the 512-token e5 limit. Measurement on the live
# archive (3000 audio-path chunks, multilingual-e5-large tokenizer): p50=304,
# p99=407 tokens, >512 only 1 chunk of 3000 - i.e. on our Cyrillic it comes to
# ~3.5 characters/token, not 2, and there is still headroom before truncation. We keep the limit at 1000:
# it is not about tokens here but about result granularity.
_MAX_CHARS = 1000
_TEXT_OVERLAP = 150        # overlap for plain-text chunking

# --- T6.5: natural chunk boundaries for audio segments ---
# Before, there was a single boundary: buffer overflow at _MAX_CHARS. Because of this a thought
# was torn mid-phrase, and the chunk speaker ("dominant by characters") could be
# the one in the minority in that chunk: a measurement over 12.8k audio-path chunks gave
# a share of the chunks with weak attribution (dominant < 70% of characters) and only 54%
# of chunks with ONE speaker. For "who promised what" questions that is a lying signature.
# Now the primary signal is "the thought ended" (a pause or a speaker change),
# and _MAX_CHARS stays as the safety net on top.
_MIN_CHARS = 350           # below this we do not cut even on a natural boundary:
                           # the median segment is 56 characters, cutting at every
                           # utterance would shatter a dialogue into fragments
_PAUSE_BOUNDARY_SEC = 1.5  # a pause between segments that we read as the end of a thought.
                           # Pause distribution measurement: p50=0.0, p90=0.88, p95=1.86;
                           # >=1.5s is 5.8% of joins (~19 per transcript), i.e.
                           # the signal is rare and does not fragment chunk boundaries
_SEG_OVERLAP_CHARS = 150   # overlap ONLY when the limit cut the chunk (the thought
                           # was torn after all, the neighbour needs context). On a
                           # natural boundary overlap is harmful: it pulls into the
                           # new chunk a thought that has already ended

_model = None
_model_lock = threading.Lock()
_unavailable_reason: Optional[str] = None

# --- T6.6: vector search scale monitoring (observation only, NOT ANN) ---
# retrieval._vector_search loads ALL embedded chunks (BLOBs) into memory on
# every query (brute-force numpy, no ANN/cache). While chunks are few it is <100ms
# and little memory, but it grows linearly with the archive (1024-dim float32 = 4KB/
# chunk). To learn about the problem IN ADVANCE (not from a latency/RAM complaint),
# we count embedded chunks with a cheap COUNT(*) (WITHOUT loading BLOBs)
# and log a WARNING when the threshold is exceeded, so that the migration to sqlite-vec (ANN)
# is planned ahead of time.
_VECTOR_WARN_THRESHOLD = int(os.environ.get("RECALL_VECTOR_WARN_THRESHOLD", "50000"))
_scale_warned = False  # process-level: do not spam the log on every call until the threshold "lets go"


def check_vector_scale(db_path: str) -> dict:
    """Count embedded chunks (COUNT(*), WITHOUT loading BLOBs into memory)
    and, if needed, log a WARNING about approaching the limit of brute-force numpy
    vector search (retrieval._vector_search loads ALL vectors on every
    query). Cheap - can be called at application start and after every
    embedding (chunk_and_embed_transcription), without building a numpy matrix.

    Returns {"chunk_count": int, "threshold": int, "over_threshold": bool}.
    """
    global _scale_warned
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE embedding IS NOT NULL"
        ).fetchone()
    n = int(row["n"]) if row is not None else 0
    over = n > _VECTOR_WARN_THRESHOLD
    if over and not _scale_warned:
        logger.warning(
            "[embeddings] Embedded chunks in the archive: %d - threshold %d exceeded "
            "(RECALL_VECTOR_WARN_THRESHOLD). retrieval._vector_search loads "
            "ALL vectors into memory on every query (brute-force numpy, no ANN) - "
            "plan the migration to sqlite-vec ahead of time, before this degrades "
            "latency/RAM.", n, _VECTOR_WARN_THRESHOLD,
        )
        _scale_warned = True
    elif not over:
        # allow a repeated warning if the count grows past the threshold again
        # (e.g. after a manual cleanup and renewed archive growth)
        _scale_warned = False
    return {"chunk_count": n, "threshold": _VECTOR_WARN_THRESHOLD, "over_threshold": over}


# ============================================================
# Model
# ============================================================

def is_available() -> bool:
    """Whether embeddings can be computed (sentence-transformers + torch import)."""
    global _unavailable_reason
    try:
        import sentence_transformers  # noqa: F401
        import torch  # noqa: F401
        return True
    except Exception as e:  # pragma: no cover
        _unavailable_reason = str(e)
        return False


def unavailability_reason() -> str:
    return _unavailable_reason or "sentence-transformers/torch unavailable"


def _apply_model_dim(model) -> int:
    """Synchronises EMBED_DIM with the actual dimension of the loaded model.

    Until this call EMBED_DIM stays at the default 1024, so
    retrieval._vector_search sees the actual dimension right after the lazy
    model load, without a process restart. Extracted into a separate function
    to cover it with a test without a real load (torch/sentence-transformers
    are not brought up in unit tests, memory mcp-stdio-no-heavy-models)."""
    global EMBED_DIM
    EMBED_DIM = model.get_sentence_embedding_dimension()
    return EMBED_DIM


def _apply_max_seq(model) -> int:
    """Lowers the model's max_seq_length to EMBED_MAX_SEQ if that is smaller.

    Downwards only: a model with its own smaller limit (e5 - 512) stays as is,
    otherwise we would RAISE that limit for it. A separate function for the same reason
    as _apply_model_dim: the test does not bring up torch."""
    current = getattr(model, "max_seq_length", None)
    if current is None or EMBED_MAX_SEQ <= 0:
        return current
    if current > EMBED_MAX_SEQ:
        logger.info("[embeddings] max_seq_length %d → %d (EMBED_MAX_SEQ ceiling)",
                    current, EMBED_MAX_SEQ)
        model.max_seq_length = EMBED_MAX_SEQ
    return model.max_seq_length


def _get_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        import torch
        from sentence_transformers import SentenceTransformer
        device = os.environ.get("EMBED_DEVICE") or ("cuda" if torch.cuda.is_available() else "cpu")
        logger.info("[embeddings] Loading %s on %s …", EMBED_MODEL, device)
        t0 = datetime.now()
        # use_safetensors=True guarantees we will NOT fall into torch.load(.bin), which
        # transformers blocks on torch<2.6 (see the comment next to EMBED_MODEL).
        _model = SentenceTransformer(
            EMBED_MODEL, device=device, model_kwargs={"use_safetensors": True},
        )
        _apply_model_dim(_model)
        _apply_max_seq(_model)
        logger.info("[embeddings] Model ready in %.1fs (dim=%d, max_seq=%s)",
                    (datetime.now() - t0).total_seconds(), EMBED_DIM,
                    getattr(_model, "max_seq_length", "?"))
        return _model


def _encode(texts: list[str], batch_size: int = 32) -> np.ndarray:
    model = _get_model()
    vecs = model.encode(
        texts, batch_size=batch_size, normalize_embeddings=True,
        show_progress_bar=False, convert_to_numpy=True,
    )
    return np.asarray(vecs, dtype=np.float32)


def embed_texts(texts: list[str], batch_size: int = 32) -> np.ndarray:
    """Encode passage chunks -> np.float32 [N, dim], L2-normalised.

    The prefix depends on the model family (_style_for): e5 - 'passage: ', qwen3
    and plain - no prefix (the Qwen3-Embedding card prefixes only the query)."""
    if not texts:
        return np.zeros((0, EMBED_DIM), dtype=np.float32)
    if _STYLE == "e5":
        texts = [f"passage: {t}" for t in texts]
    return _encode(texts, batch_size)


def embed_query(text: str) -> np.ndarray:
    """Vector of a single query (1D float32, normalised).

    The prefix depends on the model family (_style_for): e5 - 'query: ', qwen3 -
    an instruction per the model card on HF, plain - no prefix."""
    t = text or ""
    if _STYLE == "e5":
        t = f"query: {t}"
    elif _STYLE == "qwen3":
        t = f"Instruct: {_QWEN3_QUERY_TASK}\nQuery: {t}"
    v = _encode([t])
    return v[0] if len(v) else np.zeros(EMBED_DIM, dtype=np.float32)


def blob_to_vec(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


# ============================================================
# Chunking
# ============================================================

def _resolve_speaker(raw: Optional[str], speaker_map: dict[str, str]) -> Optional[str]:
    if not raw:
        return None
    if raw in speaker_map and speaker_map[raw]:
        return speaker_map[raw]
    if raw == "self":
        return "Ви"
    if raw == "SPEAKER_UNKNOWN":
        return None
    m = re.match(r"^SPEAKER_(\d+)$", raw)
    if m:
        return f"Спікер {int(m.group(1)) + 1}"
    return raw


def _is_turn_change(prev_speaker: Optional[str], speaker: Optional[str]) -> bool:
    """Whether the speaker changed between neighbouring segments.

    SPEAKER_UNKNOWN is a diarization hole, not a separate participant: entering it and
    leaving it is not a change of turn (on live data: 465 segments in 5
    transcripts, and each would have produced a needless cut). Segments with no label at all
    (an undiarized transcript: 104k of 120k segments) give no signal."""
    if not prev_speaker or not speaker:
        return False
    if "SPEAKER_UNKNOWN" in (prev_speaker, speaker):
        return False
    return prev_speaker != speaker


def _chunk_from_segments(segments: list[dict], speaker_map: dict[str, str]) -> list[dict]:
    """Windows over segments with natural boundaries (T6.5). Keeps the timecodes
    (start/end) and the dominant speaker of the chunk.

    Order of boundary signals:
      1) a speaker change or a pause >= _PAUSE_BOUNDARY_SEC: "the thought ended",
         cut WITHOUT overlap (but not earlier than _MIN_CHARS, otherwise a dialogue
         would shatter into 50-character utterances);
      2) _MAX_CHARS is the safety net: the thought was torn artificially, so the new chunk
         starts with the tail of the previous one (_SEG_OVERLAP_CHARS).
    """
    chunks: list[dict] = []
    cur: list[dict] = []   # {"text","speaker","start","end","len"}
    cur_len = 0            # all characters in the buffer (including the carried tail)
    fresh_len = 0          # characters added AFTER the last flush
    carried = 0            # how many segments in cur are the tail of the previous chunk
    idx = 0

    def _emit():
        nonlocal idx
        sp_chars: dict[str, int] = {}
        for s in cur:
            if s["speaker"]:
                sp_chars[s["speaker"]] = sp_chars.get(s["speaker"], 0) + s["len"]
        dominant = max(sp_chars, key=sp_chars.get) if sp_chars else None
        starts = [s["start"] for s in cur if s["start"] is not None]
        ends = [s["end"] for s in cur if s["end"] is not None]
        chunks.append({
            "chunk_index": idx,
            "start_time": starts[0] if starts else None,
            "end_time": ends[-1] if ends else None,
            "speaker": _resolve_speaker(dominant, speaker_map),
            "text": " ".join(s["text"] for s in cur).strip(),
        })
        idx += 1

    def _flush(overlap: bool):
        """Close the chunk. overlap=True keeps a tail of ~_SEG_OVERLAP_CHARS
        characters as the start of the next one (only for a break at the limit)."""
        nonlocal cur, cur_len, fresh_len, carried
        if not cur:
            return
        _emit()
        fresh_len = 0
        if not overlap or _SEG_OVERLAP_CHARS <= 0:
            cur, cur_len, carried = [], 0, 0
            return
        # Into the tail we take only what FITS the overlap budget, the first
        # segment included. The rule "at least one segment always" (inherited from the
        # old chunker) dragged whole 900-character monologues into the next chunk:
        # the text was duplicated in the index and skewed the speaker vote. If the
        # last segment is itself bigger than the budget, no overlap is needed:
        # the chunk ended on a whole segment anyway, nobody cut a thought.
        tail: list[dict] = []
        tail_len = 0
        for s in reversed(cur):
            if tail_len + s["len"] > _SEG_OVERLAP_CHARS:
                break
            tail.insert(0, s)
            tail_len += s["len"]
        cur, cur_len, carried = tail, tail_len, len(tail)

    prev_end: Optional[float] = None
    prev_speaker: Optional[str] = None
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start, end = seg.get("start"), seg.get("end")
        speaker = seg.get("speaker")

        # The threshold is counted on the chunk's OWN text (fresh_len), not together with the carried
        # tail: otherwise a chunk that has just received 150 characters of overlap is already
        # "almost ripe", and the natural boundary would fire on its own content of
        # 200 characters. len(cur) > carried: the buffer must hold at least one segment of its own,
        # otherwise the boundary would emit a chunk made of the tail alone (a duplicate).
        if fresh_len >= _MIN_CHARS and len(cur) > carried:
            long_pause = (
                isinstance(prev_end, (int, float)) and isinstance(start, (int, float))
                and (start - prev_end) >= _PAUSE_BOUNDARY_SEC
            )
            if long_pause or _is_turn_change(prev_speaker, speaker):
                _flush(overlap=False)

        cur.append({"text": text, "speaker": speaker, "start": start,
                    "end": end, "len": len(text)})
        cur_len += len(text)
        fresh_len += len(text)
        if isinstance(end, (int, float)):
            prev_end = end
        prev_speaker = speaker

        if cur_len >= _MAX_CHARS:
            _flush(overlap=True)
    # len(cur) > carried: if the limit fired on the LAST segment, only the
    # tail is left in the buffer; emitting it would add a chunk fully
    # contained in the previous one (a needless vector and a needless chunks_fts row).
    if len(cur) > carried:
        _flush(overlap=False)
    return chunks


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?…])\s+|\n{2,}", text)
    return [p.strip() for p in parts if p and p.strip()]


def _overlap_tail(text: str, limit: int) -> str:
    """Text tail (~limit characters) for overlap, cut at a sentence boundary,
    or at worst at a word boundary.

    T6.5: before, the tail was taken as a raw slice `text[-limit:]`, i.e. the knife fell
    mid-word; on the live archive 1607 of 1873 non-first chunks of the text
    path started with a stub. Such a start spoils both the vector and the quote in chat."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    tail = text[-limit:]
    m = re.search(r"(?<=[.!?…])\s+(\S)", tail)
    if m:
        return tail[m.start(1):]
    space = tail.find(" ")
    return tail[space + 1:] if space != -1 else tail


def _window_text(text: str) -> list[str]:
    """Sentence-aware sliding window -> list of text pieces <= _MAX_CHARS
    with an overlap of ~_TEXT_OVERLAP. Shared core of plain-text and block chunking."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= _MAX_CHARS:
        return [text]
    sentences = _split_sentences(text)
    pieces: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for s in sentences:
        cur.append(s)
        cur_len += len(s) + 1
        if cur_len >= _MAX_CHARS:
            joined = " ".join(cur).strip()
            pieces.append(joined)
            tail = _overlap_tail(joined, _TEXT_OVERLAP)
            cur = [tail] if tail else []
            cur_len = len(tail)
    if cur:
        joined = " ".join(cur).strip()
        if joined:
            pieces.append(joined)
    return pieces


def _chunk_from_text(text: str) -> list[dict]:
    """Sentence-aware sliding window for plain text (no timecodes/provenance)."""
    return [{"chunk_index": i, "start_time": None, "end_time": None,
             "speaker": None, "text": p}
            for i, p in enumerate(_window_text(text))]


def _chunk_from_blocks(blocks: list[dict]) -> list[dict]:
    """Chunking of documents with provenance (Phase 16B). Windows do NOT cross the
    boundaries of a block (page/slide); each chunk keeps the page+section of its block.
    A large block is cut sentence-aware into sub-chunks (the same page/section)."""
    chunks: list[dict] = []
    idx = 0
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btext = (block.get("text") or "").strip()
        if not btext:
            continue
        page = block.get("page")
        section = block.get("section")
        for piece in _window_text(btext):
            chunks.append({
                "chunk_index": idx,
                "start_time": None, "end_time": None, "speaker": None,
                "page": page, "section": section,
                "text": piece,
            })
            idx += 1
    return chunks


def build_chunks(segments_json: Optional[str], transcript_text: str,
                 speaker_map: dict[str, str],
                 structure_json: Optional[str] = None) -> list[dict]:
    """Choose a chunking strategy:
      1) a document with structure (structure_json) -> block chunker with provenance;
      2) audio with segments (timecodes) -> segment chunker;
      3) otherwise -> flat text chunker.
    """
    import json
    # 1) Document provenance (Phase 16B): PDF pages / PPTX slides.
    if structure_json:
        try:
            blocks = json.loads(structure_json)
        except (ValueError, TypeError):
            blocks = None
        if blocks and any(isinstance(b, dict) and (b.get("text") or "").strip() for b in blocks):
            return _chunk_from_blocks(blocks)
    # 2) Audio segments with timecodes.
    segments = None
    if segments_json:
        try:
            segments = json.loads(segments_json)
        except (ValueError, TypeError):
            segments = None
    if segments and any(isinstance(s, dict) and (s.get("text") or "").strip() for s in segments):
        return _chunk_from_segments(segments, speaker_map)
    # 3) Flat text.
    return _chunk_from_text(transcript_text)


# ============================================================
# Chunk context prefix (Wave B, story 05)
# ============================================================
#
# A chunk was self-sufficient only for someone who already knows where it comes from: the vector and
# BM25 got the bare text of the utterance. Chat title, date, author, direction and thread topic
# sat in neighbouring columns and took no part in search at all.
#
# The prefix is DB FIELDS, not a model's retelling of the text: line 1 is provenance,
# line 2 is a single summary line of the unit of meaning (`summaries.unit_summary_line`,
# contract C1). It goes into the embedder and into `chunks_fts`, but NOT into `chunks.text`:
# quotes, export and threads keep showing only what the person actually said.

#: The order of fields in line 1 is fixed (contract C4 of the plan): the prefix must be
#: deterministic: the same record gives the same line, otherwise every re-embed
#: would change vectors "by itself".
_PREFIX_SEP = " · "

_KIND_LABELS = {"telegram": "переписка", "document": "документ"}


def _prefix_date(value: Optional[str]) -> Optional[str]:
    """`YYYY-MM-DD` from the record date. meeting_date and created_at both start
    with an ISO date, so we take the first 10 characters and check the shape;
    garbage ("unknown", empty) is not let into the prefix."""
    s = (str(value) if value is not None else "").strip()
    if not s:
        return None
    head = s[:10]
    return head if re.match(r"^\d{4}-\d{2}-\d{2}$", head) else None


#: The record description (`transcriptions.description`) can be up to 4000 characters:
#: only the head goes into the prefix: the prefix is repeated in EVERY chunk of the record, and
#: a long description would push the text itself out of the embedder window.
_PREFIX_DESCRIPTION_MAX = 300


def _prefix_description(value) -> Optional[str]:
    """Description on one line: line breaks -> spaces, cut to 300 characters.
    Empty/not a string -> None (the "description:" line simply will not be in the prefix)."""
    if not isinstance(value, str):
        return None
    flat = " ".join(value.split())
    if not flat:
        return None
    return flat[:_PREFIX_DESCRIPTION_MAX]


#: Separator for gluing tags in SQL (`GROUP_CONCAT(..., char(31))`): U+001F
#: (unit separator). Not a comma: a comma in a tag name is allowed and would break parsing.
_TAG_SEP = "\x1f"

#: How many tags of a record go into the prefix and the ceiling for the whole line.
#: Tags are repeated in EVERY chunk of the record: a long tail would push the text itself out of
#: the embedder window, and there is almost no value after the first few names.
_PREFIX_TAGS_MAX = 5
_PREFIX_TAGS_LINE_MAX = 60


def _prefix_tags_line(names) -> Optional[str]:
    """The line "tags: a, b, c" or None. <=5 names, the whole line <=60 characters.

    We cut BY WHOLE TAG: a truncated name ("invest") is not a tag but a junk
    token that would also match other people's queries. `names` arrives already
    ordered by `name_norm` (`_load_prefix_meta`).
    """
    if not names:
        return None
    line, taken = "теги:", 0
    for raw in list(names)[:_PREFIX_TAGS_MAX]:
        name = " ".join(str(raw).split())
        if not name:
            continue
        candidate = line + (", " if taken else " ") + name
        if len(candidate) > _PREFIX_TAGS_LINE_MAX:
            break
        line, taken = candidate, taken + 1
    return line if taken else None


def build_context_prefix(meta: dict, chunk: dict) -> str:
    """Context header of a chunk (contracts C4 + C5): 1-4 lines.

    Line 1: ``[type] title|chat · YYYY-MM-DD · speaker|author · Direction → Project
    · tail``, where the tail depends on the type: ``thread: label`` (chat),
    ``page N`` or a section (document), empty (call/file). Empty fields
    are simply skipped; a "None" line never happens.

    The direction path (directions-projects-tags-03) stands in line 1 for ALL
    record types, not only calls: it is the prefixes that gave +10.3 pp on the
    live archive, and TG/documents must not stay without the hierarchy. Without a direction
    the path segment simply disappears: a record without a direction and tags gives a prefix
    byte-equal to epoch 2.

    Next: the line "tags: a, b" (if there are tags), the line "description: ..." (if the owner
    wrote one) and the `unit_summary_line` of the unit of meaning, if there is one.

    `meta` is the RECORD fields (`_load_prefix_meta`), `chunk` is the fields of the chunk itself
    (`speaker`, `page`, `section`). The function is pure and deterministic.
    """
    source_type = (meta.get("source_type") or "").strip()
    kind = _KIND_LABELS.get(source_type, "дзвінок")

    parts: list[str] = [f"[{kind}]"]

    def _add(value) -> None:
        s = str(value).strip() if value is not None else ""
        if s:
            parts.append(s)

    _add(meta.get("title"))
    _add(_prefix_date(meta.get("date")))
    _add(chunk.get("speaker"))

    # The direction path is for all types and BEFORE the type tail. A call/file
    # has no tail, so the position of the path stayed the same as in epoch 2.
    _add(meta.get("category_path") or meta.get("category"))

    if source_type == "telegram":
        label = (meta.get("thread_label") or "").strip() if meta.get("thread_label") else ""
        if label:
            _add(f"нитка: {label}")
    elif source_type == "document":
        page = chunk.get("page")
        if page is not None and str(page).strip():
            _add(f"стор. {page}")
        else:
            _add(chunk.get("section"))

    # The first element is the type label in brackets, it is glued to the title with a space,
    # the rest go through the separator: "[дзвінок] Назва · 2026-09-17 · Андрій · Фонд".
    if len(parts) == 1:
        line1 = parts[0]
    else:
        line1 = parts[0] + " " + _PREFIX_SEP.join(parts[1:])

    lines = [line1]
    tags_line = _prefix_tags_line(meta.get("tags"))
    if tags_line:
        lines.append(tags_line)
    description = _prefix_description(meta.get("description"))
    if description:
        lines.append(f"опис: {description}")

    summary_line = (meta.get("summary_line") or "").strip() if meta.get("summary_line") else ""
    if summary_line:
        lines.append(summary_line)
    return "\n".join(lines)


def _load_prefix_meta(conn, transcription_id: int) -> dict:
    """Record fields for the prefix in one join: the direction with its parent,
    tags and the thread label lie in neighbouring tables, not in `transcriptions`.

    One SELECT on purpose: `_load_prefix_meta` is called for EVERY record of a
    mass re-embed (thousands), and a second trip to the DB for tags would cost
    an extra round per record.

    The summary is taken via `summaries.unit_summary_line` (SQL only: the module
    deliberately has no torch/anthropic at import level, memory
    `mcp-stdio-no-heavy-models`)."""
    row = conn.execute(
        "SELECT t.source_type, t.source_name, t.title, t.description, "
        "       t.original_filename, t.tg_chat_title, "
        "       t.meeting_date, t.created_at, t.tg_date, "
        "       cat.name AS category_name, parent.name AS parent_name, "
        "       th.label AS thread_label, "
        # The separator is U+001F (unit separator), not a comma: a comma is allowed in a tag
        # name, and gluing with commas would break the parsing back.
        "       (SELECT GROUP_CONCAT(tg.name, char(31)) FROM record_tags rt "
        "          JOIN tags tg ON tg.id = rt.tag_id "
        "         WHERE rt.transcription_id = t.id) AS tag_names "
        "FROM transcriptions t "
        "LEFT JOIN categories cat ON cat.id = t.category_id "
        "LEFT JOIN categories parent ON parent.id = cat.parent_id "
        "LEFT JOIN tg_threads th ON th.id = t.tg_thread_id "
        "WHERE t.id = ?",
        (transcription_id,),
    ).fetchone()
    if not row:
        return {}

    source_type = row["source_type"]
    if source_type == "telegram":
        fallback = row["tg_chat_title"] or row["source_name"]
    elif source_type == "document":
        fallback = row["source_name"] or row["original_filename"]
    else:
        fallback = row["source_name"]

    # The own title (`transcriptions.title`, contract C2) beats provenance; if it
    # is absent, the same logic as before story 03 stays (for TG it is the chat).
    # If both fields are empty the prefix keeps an empty slot (as it was), not
    # the artificial "Запис #id" that display_name gives for the UI.
    from app.services.record_meta import display_name as _display_name
    title = (_display_name({"title": row["title"], "source_name": fallback})
             if (row["title"] or fallback) else None)

    try:
        from app.services.summaries import unit_summary_line
        summary_line = unit_summary_line(conn, transcription_id)
    except Exception:  # noqa: BLE001 - a prefix without a summary beats a broken embedding
        logger.debug("[embeddings] unit_summary_line failed for tx=%s",
                     transcription_id, exc_info=True)
        summary_line = None

    # Direction path: "Direction → Project" for a project node, just the name for a
    # direction. The tree has exactly two levels (ADR-023 §E), so one self-JOIN
    # is enough: no recursion needed.
    category_name = row["category_name"]
    parent_name = row["parent_name"]
    if category_name and parent_name:
        category_path = f"{parent_name} → {category_name}"
    else:
        category_path = category_name

    # Tag order is by `name_norm`, computed in PYTHON: SQLite LOWER()
    # folds only ASCII, so sorting Cyrillic on the DB side would be random
    # (memory note `plan-dictated-sql-ignored-own-memory`).
    raw_tags = row["tag_names"] or ""
    tags = sorted((t for t in raw_tags.split(_TAG_SEP) if t.strip()),
                  key=_categories.norm_name)

    return {
        "source_type": source_type,
        "title": title,
        "description": row["description"],
        "date": row["meeting_date"] or row["tg_date"] or row["created_at"],
        "category": category_name,
        "category_path": category_path,
        "tags": tags,
        "thread_label": row["thread_label"],
        "summary_line": summary_line,
    }


# ============================================================
# Persistence
# ============================================================

#: Stubs the ingest puts when there is nothing to recognise: "[фото без тексту]",
#: "[відео без розпізнаного тексту]", "[голосове — очікує розпізнавання]" etc.
#: This is not text but a service marker: a vector from it is harmful (152 identical
#: points on live data), and the word "фото" in it also matches queries about photos.
_TG_CONTENTLESS = re.compile(r"^\s*\[[^\]]{0,60}\]\s*$")


def _is_contentless_tg(text: str) -> bool:
    return bool(_TG_CONTENTLESS.match(text or ""))


# ============================================================
# Prefix format guard (epoch <-> format), story 17
# ============================================================

#: The format-mismatch ERROR is printed ONCE per process: otherwise every new
#: transcript (and on TG that is dozens per hour) would add the same paragraph to the log.
_epoch_mismatch_logged = False


def _db_prefix_format(conn, model: str, version: int) -> Optional[int]:
    """Which format built the epoch `(model, version)`, according to DB data.

    `None` means nobody has built the epoch yet. A DB below v46 (no table) is read
    by the same rule the migration backfill encodes: a pair already present in
    `transcriptions` was built with format 1. Without this step the check BEFORE
    restart (step 1 of the runbook, exactly the moment when v46 has not been applied yet)
    would always say "new epoch" and would not catch a forgotten `.env`.
    """
    try:
        row = conn.execute(
            "SELECT prefix_format FROM embedding_epochs WHERE model = ? AND version = ?",
            (model, version),
        ).fetchone()
    except sqlite3.OperationalError:
        logger.debug("[embeddings] embedding_epochs is missing (DB below v46); "
                     "determining the epoch format from transcriptions", exc_info=True)
        legacy = conn.execute(
            "SELECT 1 FROM transcriptions WHERE embedding_model = ? "
            "AND embedding_version = ? LIMIT 1", (model, version),
        ).fetchone()
        return 1 if legacy is not None else None
    return int(row[0]) if row is not None else None


def epoch_status(conn) -> dict:
    """Whether chunks of the current `PREFIX_FORMAT` may be written into the current epoch.

    Returns `{"ok", "reason", "pair", "db_format"}`, where `reason` is:

    - ``"ok"``              - the epoch was already built with this very format;
    - ``"format_mismatch"`` - the epoch was built with a DIFFERENT format: writing is not allowed,
      because `stale_ids` will not see such a record (the pair matches), and the two formats
      would mix in one `retrieval.search` selection (ADR-017);
    - ``"new_epoch"``       - the epoch does not exist yet, the format is free (a fresh install
      or a raised `EMBED_VERSION`).
    """
    pair = (EMBED_MODEL, EMBED_VERSION)
    db_format = _db_prefix_format(conn, EMBED_MODEL, EMBED_VERSION)
    if db_format is None:
        reason = "new_epoch"
    elif db_format == PREFIX_FORMAT:
        reason = "ok"
    else:
        reason = "format_mismatch"
    return {"ok": reason != "format_mismatch", "reason": reason,
            "pair": pair, "db_format": db_format}


def register_epoch(conn) -> None:
    """Remember that the epoch `(EMBED_MODEL, EMBED_VERSION)` is built by `PREFIX_FORMAT`.

    Called in the same transaction as the first chunks of the epoch, so that there is
    no state "chunks of the new format exist, but no memory of the format".
    """
    try:
        conn.execute(
            "INSERT OR IGNORE INTO embedding_epochs (model, version, prefix_format) "
            "VALUES (?, ?, ?)", (EMBED_MODEL, EMBED_VERSION, PREFIX_FORMAT))
    except sqlite3.OperationalError:
        # DB below v46: the migration is applied at app.py start; we do not forbid
        # writing chunks because of this, but we cannot stay silent about it.
        logger.warning("[embeddings] epoch %s/v%s not registered: no table "
                       "embedding_epochs (DB below v46)", EMBED_MODEL, EMBED_VERSION)


def epoch_mismatch_message(status: dict) -> str:
    """One refusal text for all three places (chunk writer, `reembed.run`,
    CLI `epoch-check`), so the log and the console give the same hint."""
    model, version = status["pair"]
    return (f"epoch {model} / v{version} was built with prefix format "
            f"{status['db_format']}, but the code writes format {PREFIX_FORMAT}. "
            f"Raise EMBED_VERSION in .env STRICTLY above any pair the "
            f"DB holds (check: python -m app.services.reembed epoch-check), "
            f"and restart; then run the mass pass python -m app.services.reembed run.")


def _mark_embedded_without_chunks(db_path: str, transcription_id: int) -> None:
    """The record yields no chunk at all: remove the old ones and mark it processed.

    The DELETE is mandatory: on the normal path chunks are overwritten (DELETE+INSERT
    below), but here we exit early; without it RE-INDEXING left the old
    junk vectors alive, and the stub filter would change nothing for the existing
    archive (caught on live data: 188 "[фото без тексту]" survived the pass).
    The mark is needed, otherwise the indexer would consider the record work forever."""
    with get_db_connection(db_path) as conn:
        conn.execute("DELETE FROM chunks WHERE transcription_id = ?", (transcription_id,))
        conn.execute(
            "UPDATE transcriptions SET embedded_at = CURRENT_TIMESTAMP, "
            "embedding_model = ?, embedding_version = ?, chunk_count = 0 WHERE id = ?",
            (EMBED_MODEL, EMBED_VERSION, transcription_id))
        conn.commit()


def chunk_and_embed_transcription(db_path: str, transcription_id: int,
                                  force: bool = False) -> dict:
    """Cut the transcript into chunks + encode + save. Idempotent via
    embedded_at. Overwrites chunks on re-run (force or a model change)."""
    if not is_available():
        return {"status": "unavailable", "reason": unavailability_reason()}

    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        row = c.execute(
            "SELECT id, transcript_text, polished_text, segments, structure_json, "
            "embedded_at, embedding_model, embedding_version, source_type, tg_sender "
            "FROM transcriptions WHERE id = ?",
            (transcription_id,),
        ).fetchone()
        if not row:
            return {"status": "not_found", "transcription_id": transcription_id}

        # T6.8: idempotency compares the MODEL and the VERSION.
        # T6.5 (14.08.2026): NULL no longer means "compatible". Before, it did so
        # on purpose, so that the v28 bump would not recompute the archive retroactively. Now
        # the cutting really changed, and NULL means "embedded before the
        # version was written at all" (records on the live archive), i.e.
        # certainly the old cutting. Leaving them "compatible" would be a silent hole:
        # part of the archive forever with a different chunking. The condition here MUST
        # mirror enrichment.list_unenriched_ids, otherwise the indexer would hand out
        # records this function silently skips, and the work would never end.
        row_version = row["embedding_version"] if "embedding_version" in row.keys() else None
        version_matches = row_version == EMBED_VERSION
        already = (bool(row["embedded_at"])
                  and row["embedding_model"] == EMBED_MODEL
                  and version_matches)
        if already and not force:
            return {"status": "skipped", "transcription_id": transcription_id,
                    "reason": "already_embedded"}

        # Story 17: chunks of the new prefix format do not go into an epoch that an
        # older format built. We do not save the record here: it stays
        # without chunks and `reembed.run` picks it up after EMBED_VERSION is raised
        # (otherwise the two formats would mix in one retrieval.search selection, and
        # `stale_ids` would not see it: the pair matches).
        epoch = epoch_status(conn)
        if not epoch["ok"]:
            global _epoch_mismatch_logged
            if not _epoch_mismatch_logged:
                _epoch_mismatch_logged = True
                logger.error("[embeddings] chunks NOT written: %s",
                             epoch_mismatch_message(epoch))
            return {"status": "epoch_mismatch", "transcription_id": transcription_id,
                    "reason": epoch["reason"], "db_format": epoch["db_format"]}

        body = row["polished_text"] or row["transcript_text"] or ""
        if not body.strip():
            return {"status": "empty", "transcription_id": transcription_id}

        sp_rows = c.execute(
            "SELECT m.raw_label, s.name FROM transcription_speaker_map m "
            "LEFT JOIN speakers s ON s.id = m.speaker_id WHERE m.transcription_id = ?",
            (transcription_id,),
        ).fetchall()
        speaker_map = {r["raw_label"]: r["name"] for r in sp_rows if r["name"]}
        segments_json = row["segments"]
        structure_json = row["structure_json"] if "structure_json" in row.keys() else None
        is_telegram = row["source_type"] == "telegram"
        tg_sender = row["tg_sender"] if is_telegram else None
        # Wave B: fields for the context prefix, through the same connection while
        # it is open (title/chat, date, direction, thread label, summary line).
        prefix_meta = _load_prefix_meta(conn, transcription_id)

    chunks = build_chunks(segments_json, body, speaker_map, structure_json)

    if is_telegram:
        # The message author. TG has no segments (nothing to diarize), so
        # build_chunks left speaker=None: empty in ALL chunks on live data.
        # Because of this RAG could not say WHO promised something, although the name
        # sat in the neighbouring column.
        if tg_sender:
            for ch in chunks:
                if not ch.get("speaker"):
                    ch["speaker"] = tg_sender
        # Stubs without content must not become vectors: 152 identical
        # "[фото без тексту]" are 152 identical points that compete for a place
        # in the results and mean nothing.
        chunks = [ch for ch in chunks if not _is_contentless_tg(ch["text"])]

    if not chunks:
        if is_telegram:
            _mark_embedded_without_chunks(db_path, transcription_id)
            return {"status": "skipped", "transcription_id": transcription_id,
                    "reason": "tg_contentless"}
        return {"status": "empty", "transcription_id": transcription_id}

    # Wave B: the context prefix is computed AFTER cutting and the TG post-process:
    # the chunk speaker takes part in it (for TG it has just been set) and the
    # page/section of the document.
    for ch in chunks:
        ch["context_prefix"] = build_context_prefix(prefix_meta, ch)

    # We encode outside the DB connection (a GPU operation). Prefix+text goes into the vector;
    # the text itself stays in `chunks.text` untouched (quotes, export, threads).
    vecs = embed_texts([
        (ch["context_prefix"] + "\n" + ch["text"]) if ch["context_prefix"] else ch["text"]
        for ch in chunks
    ])

    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        if epoch["reason"] == "new_epoch":
            # The same transaction as the first chunks of the epoch: otherwise a state
            # "chunks of the new format exist, but no memory of the format" would remain.
            register_epoch(conn)
        c.execute("DELETE FROM chunks WHERE transcription_id = ?", (transcription_id,))
        for ch, vec in zip(chunks, vecs):
            c.execute(
                "INSERT INTO chunks (transcription_id, chunk_index, start_time, "
                "end_time, speaker, text, embedding, token_estimate, page, section, "
                "context_prefix) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (transcription_id, ch["chunk_index"], ch["start_time"], ch["end_time"],
                 ch["speaker"], ch["text"], vec.astype(np.float32).tobytes(),
                 len(ch["text"]) // 4, ch.get("page"), ch.get("section"),
                 ch.get("context_prefix") or None),
            )
        c.execute(
            "UPDATE transcriptions SET embedded_at = CURRENT_TIMESTAMP, "
            "embedding_model = ?, embedding_version = ?, chunk_count = ? WHERE id = ?",
            (EMBED_MODEL, EMBED_VERSION, len(chunks), transcription_id),
        )
        conn.commit()

    logger.info("[embeddings] tx=%s: %d chunks encoded (%s)",
                transcription_id, len(chunks), EMBED_MODEL)

    # T6.6: a periodic scale check (on every embedding, i.e. on every archive growth).
    # Cheap: only COUNT(*), without loading BLOBs.
    try:
        check_vector_scale(db_path)
    except Exception:  # pragma: no cover - monitoring must not break the main flow
        logger.debug("[embeddings] check_vector_scale failed", exc_info=True)

    return {"status": "embedded", "transcription_id": transcription_id,
            "chunks": len(chunks), "model": EMBED_MODEL}
