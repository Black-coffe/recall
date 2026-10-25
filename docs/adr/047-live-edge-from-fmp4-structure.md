# ADR-047: The readable live edge of a growing recording comes from its fMP4 structure, never ffprobe duration

- Status: proposed
- Date: 2026-10-25
- Spec: docs/specs/live-video-frames

## Context
Live frames are cut from a screen-capture file that is still being written. The service has to know up to which
second the file can be decoded. The plan records (A3, verbatim):

> **A3. Readable edge.** `readable_until` = end of the last complete `moof`+`mdat` fragment of the growing file (box
> walk, stdlib `struct`), never ffprobe `format=duration` (measured to over-report a partial file). A request past it
> -> 409 `PAST_LIVE_EDGE` with `readable_until`.

Contract C1:

> `readable_until(path: str) -> float | None` — video-file seconds covered by complete fragments (last complete
> `moof` whose `mdat` fits in the file; `tfdt` + `trun` durations / `mdhd` timescale). `None` when the file has no
> complete fragment yet. Never reads ffprobe duration.

## Options
1. ffprobe `format=duration` on the growing file. Rejected: measured to over-report a partial file, which would
   promise frames that cannot be decoded yet.
2. Walk the fragmented-MP4 boxes and take the end of the last complete `moof`+`mdat`. Chosen.

## Decision
`live_frames.readable_until` walks the boxes with stdlib `struct` and returns the end of the last complete fragment
(`None` before the first one). A request past it is refused with 409 `PAST_LIVE_EDGE` carrying `readable_until`
(and `track_id`). It never returns a stale or nearest frame. A range `end` past the edge is clamped and reported, and
a range `start` past the edge is refused.

## Consequences
Not recorded beyond the above. The edge depends on how often the muxer completes fragments. See ADR-048.

## Invariants created
- Code that reads a file still being recorded never trusts ffprobe duration for its length. It uses the fragment
  walk (`live_frames.readable_until`).
- A request past the readable edge gets a typed refusal that carries the edge. It never gets a guessed or stale frame.
- Capture output stays fragmented MP4 (`-movflags +frag_keyframe+empty_moov+default_base_moof`). Changing the
  container breaks the edge.

## Revisit when
Not recorded.
