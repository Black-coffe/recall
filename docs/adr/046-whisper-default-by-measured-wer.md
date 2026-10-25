# ADR-046: The default Whisper model is chosen by a measured WER, split archive vs live

- Status: proposed
- Date: 2026-10-21
- Spec: docs/specs/whisper-best-default

## Context
The default Whisper model was a literal repeated across the SPA views, the settings registry, the transcribe routes,
the warm-up and the live worker, and it had drifted: `large-v3-turbo` in most selects, `large-v3` on the record
screen and autosave, `base` as the API/MCP fallback, `small` for live call text. The owner asked for the best-quality
model as the default everywhere. `measurement.md` scored all 8 installed multilingual models on FLEURS dev through the
production manager: uk WER large-v3 8.2 %, large-v3-turbo 8.7 % (12.6x vs 30.6x realtime), small 20.8 % (29.5x),
base 44.4 %. The large-v3 / turbo gap is within noise on this sample (64 vs 68 errors of 780 words).

## Options
1. large-v3 for every path, live included - rejected: 2.4x slower and heavier next to the copilot and diarization
   during a call, where text latency matters more than the last half-point of WER.
2. large-v3 for archive paths, large-v3-turbo for live - chosen (owner, brief answers 1-2).
3. Keep turbo everywhere - rejected: the owner asked for the best measured quality on archive paths.

## Decision
Every path that picks a model when the caller did not uses `large-v3` (SPA selects, `/api/transcribe` and
`/api/transcribe/async`, MCP `transcribe`, Telegram, recording autosave, warm-up, the catalog's `recommended` flag).
Live call transcription uses `large-v3-turbo`. A model chosen by the user or set in `.env` always wins.
`tests/test_whisper_defaults.py` pins all literals to one value.

## Consequences
Telegram media transcribes ~2.4x slower (still ~12x realtime). Idle VRAM after boot rose from ~4.0 GB to ~5.9 GB.
A model-less API/MCP call on a machine without large-v3 downloaded answers MODEL_NOT_INSTALLED instead of running base.

## Invariants created
- A new path that transcribes without an explicit model defaults to the same value as `whisper_preload.DEFAULT_PRELOAD_MODEL`
  and is added to `tests/test_whisper_defaults.py`.
- Changing the default needs a measurement against human references (not intuition, not the model's own size),
  with the speed part taken on a free GPU: under VRAM contention the same models ran 0.2-0.9x realtime with no error.

## Revisit when
A new Whisper model is installed; conversational/call audio with references becomes available; ru is measured for
large-v3 vs turbo; VRAM during a real call with copilot shows contention.
