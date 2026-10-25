# ADR-050: Live video frames are on demand only: no background timeline, no writes, decoding in app.py

- Status: proposed
- Date: 2026-10-25
- Spec: docs/specs/live-video-frames

## Context
Agents can read frames of a recording that is still running. A cache built in the background would make range requests
instant, but it would add load to a live call. The plan records (A8, amended at approval, and the rejected
alternative, verbatim):

> **A8. No writes, no background work.** The live service writes nothing (no `thumbs.npz`, no manifest or DB change),
> starts no thread, and touches neither `VideoCaptureSupervisor`, the SSE channel nor `static/js/recall/views/record.js`.

> A8 amended: the capture supervisor changes (C5) but the live service still writes nothing.

> **Rejected alternative: a background live timeline.** A thread that runs the thumbs pass continuously during the
> call and caches changes so a range request is instant. Rejected by the owner ("Только по запросу"), and it would be
> a second consumer changing the live session's load profile (the A10 invariant of mcp-live-call). Also rejected:
> the MCP process reading the session directory and running ffmpeg/Tesseract itself (`mcp-stdio-no-heavy-models`;
> the privacy filter and secret masking live in app.py).

## Options
1. Background live timeline (a thread that keeps scanning and caching). Rejected by the owner ("Только по запросу",
   "on request only"). It would also change the live session's load profile.
2. The MCP process decodes the session files itself. Rejected: the MCP process does not run heavy work, and the privacy
   filter and secret masking live in app.py.
3. Each call spawns bounded ffmpeg work in app.py and writes nothing. Chosen.

## Decision
`app/services/live_frames.py` does all its work per request, within bounds: at most 12 frames per call
(`MAX_FRAMES_PER_CALL`) and at most `LIVE_SCAN_MAX_THUMBS` thumbnails per changes scan, with a `next_t` cursor for the
rest. It writes nothing to the session, manifest or DB and starts no thread. The MCP tools `get_live_video` /
`get_live_frames` proxy the app.py routes.

## Consequences
Range requests in changes mode pay the scan cost on every call. Decoding up to 12 frames on the CPU next to a running
NVENC encode is accepted. The owner was to measure GPU contention at stage 05 (A8). The reasons for the numeric caps are
not recorded.

## Invariants created
- Live-recording read features add no background thread, cache file or DB/manifest write during a session.
- The MCP process never reads session media or runs ffmpeg/Tesseract. It proxies app.py, where masking and the privacy
  filter apply (ADR-030, ADR-033, ADR-034).
- Every live read call is bounded and paged with a cursor.

## Revisit when
Not recorded.
