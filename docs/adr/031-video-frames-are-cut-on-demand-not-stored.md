# ADR-031: Video frames are cut from the video on demand, never stored

- Status: proposed
- Date: 2026-10-13
- Spec: docs/specs/video-visual-timeline

## Context
The visual timeline keeps up to 1500 keyframes per recording. Phase 23 stored scene-detect
keyframes as `keyframes/*.jpg`. The owner's second addendum, recorded in the plan's Assumptions
(an Assumptions decision, not a Plan delta), changed that. Verbatim:

> **Frames are not stored (addendum 2).** The original is the video itself. The DB holds only timecodes, OCR,
> word boxes, secret boxes and descriptions. The frame image is cut from the video on request (`-ss` before `-i`, one
> frame, ~0.2–0.5 s), the secret boxes are painted over on the fly, the frame is compressed (JPEG, `thumb` ≤ 960 px /
> `full` ≤ 1920 px wide) and served. A temporary cache in `%TEMP%` with a size limit and auto-cleanup.
> During analysis, frames for OCR are extracted to a temporary folder and deleted after OCR. The old
> `keyframes/*.jpg` scheme (1 row, #2911) disappears on re-analysis.

## Options
1. Store frame images on disk (the Phase 23 `keyframes/*.jpg` scheme). This is the scheme being
   replaced. The plan gives no tradeoff for it beyond "The original is the video itself".
2. Store only metadata and cut each frame from the video on request. This is the one chosen.

## Decision
Option 2. The recorded reason is that the video is the original: a stored frame is a derived
copy of it.

## Consequences
Recorded in the plan:
- Each frame request costs about 0.2–0.5 s of ffmpeg seek. A bounded, self-cleaning `%TEMP%` cache
  absorbs repeats.
- Secret boxes are painted over at serve time rather than once at write time. This keeps the
  database free of both redacted and unredacted image copies (see ADR-030).
- Frames extracted for OCR during analysis are temporary.

Not recorded: what happens to a timeline if its video file is moved or deleted.

## Invariants created
- The database and the session folder hold no frame images, only timecodes, OCR, word boxes,
  secret boxes and descriptions.
- A frame image is produced by seeking the source video (`-ss` before `-i`), painted over, and
  compressed to `thumb` ≤ 960 px or `full` ≤ 1920 px width.
- Any frame cache lives in `%TEMP%`, is bounded in size and cleans itself up.
- Frames extracted for analysis are deleted once OCR finishes.

## Revisit when
Not recorded.
