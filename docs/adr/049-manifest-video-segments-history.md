# ADR-049: Each manifest video track keeps a `segments` history across pause/resume

- Status: proposed
- Date: 2026-10-25
- Spec: docs/specs/live-video-frames

## Context
On resume the capture supervisor starts a new file `video_mon{idx}_seg{n}.mp4`. Recon R1 (verbatim):

> `session_store.add_video_track` upserts by `track_id` ..., and `VideoCaptureSupervisor.resume` overwrites
> `track.path` with the new `video_mon{idx}_seg{n}.mp4` ...; `_spawn_track` recomputes `start_offset_sec = now -
> audio_start_wallclock` ... So the manifest holds only the CURRENT segment per track; earlier `_seg` files exist on
> disk but nothing records their timeline position.

Coverage carry-forward flagged that video from before a pause would be refused (Ask 1 partial). Delta at approval
(verbatim):

> **C5 (new contract).** Manifest video track gains `segments: [{"n": int, "path": str, "start_offset_sec": float}]`,
> appended on every spawn (start = n 0, each resume n+1); `path`/`start_offset_sec` keep meaning "current segment".
> Story 02 maps timeline `t` to the segment whose `[start_offset_sec, start_offset_sec + readable_until(file)]` holds
> it; gaps (pause) -> `NOT_RECORDED` 404 with the readable ranges. Legacy track without `segments` = one segment.

> A4 superseded by C5: `segments_unmapped` remains only for sessions recorded before this ships.

## Options
1. Serve the current segment only. Earlier segments are reported `unmapped` and refused (A4 as resolved by R1).
   Rejected: it leaves frames from before a pause unreachable.
2. Record every segment's path and timeline offset in the manifest (C5). Chosen by the owner at approval
   ("recommended option").

## Decision
Every spawn appends `{n, path, start_offset_sec}` to the track's `segments`. The top-level `path` and
`start_offset_sec` still mean the current segment. A timeline `t` maps to the segment whose readable span holds it.
A `t` inside a pause answers 404 `NOT_RECORDED` with the readable ranges. A track without `segments` is read as one
segment.

## Consequences
The capture supervisor now writes manifest state (A8 amended: the live service itself still writes nothing). Sessions
recorded before 4.30.0 still report `segments_unmapped`. Paging in changes mode has to jump across a pause gap (repair
round 1, finding 1). It is not recorded whether the wall-clock `start_offset_sec` after a pause matches the
live-transcript axis. Stage 05 was due to check it.

## Invariants created
- A time inside a pause, or inside a segment that cannot be placed on the timeline, is refused with the readable
  ranges. It is never mapped by guessing (the `AMBIGUOUS_SESSION` principle of ADR-026 applied to time).
- `path` / `start_offset_sec` on a track mean the current segment. Readers that need history use `segments`.
- A manifest track without `segments` stays readable as a single segment.

## Revisit when
Not recorded.
