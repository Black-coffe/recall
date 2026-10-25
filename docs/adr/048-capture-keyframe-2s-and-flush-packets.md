# ADR-048: Screen capture emits a keyframe every 2 s and flushes every packet

- Status: proposed
- Date: 2026-10-25
- Spec: docs/specs/live-video-frames

## Context
The readable live edge (ADR-047) can only advance once a fragment completes. With `+frag_keyframe` a fragment
completes at each keyframe, and the muxer only completes it once it has written it out. The plan records (Goal, A9,
C4, verbatim):

> Screen capture gets a keyframe every 2 s so that edge lags real time by about 2 s instead of ~8 s.

> **A9. `-g 2*fps` only** (no `-keyint_min`, no `-forced-idr`): NVENC honours `-g`. Verified at stage 05 on a real
> recording (ffprobe keyframe spacing <= 2 s), not in offline tests.

> **C4. Capture** (story 01): `build_capture_command(...)` keeps its signature and argv order; `-g <2*fps>` is
> inserted among the encoder options (after `-c:v`, before `-movflags`), for every codec branch (h264_nvenc and the
> hevc fallback).

Repair round 1 (`live-video-frames-05-repair-round-1.md`, Findings ASK 5) measured that a 2 s GOP alone does not deliver the
edge:

> writing a 1080p static slide (a call with a still screen) for 40 s, polling readable_until every 0.1 s saw: file
> size 28 B until wall 22.6 s, first readable edge 20.0 at wall 22.6 s, next at 40.1 s — the muxer output buffer
> (256 KiB–1 MiB per flush) holds fragments back; keyframes are at 2.0 s ... but a 2 s GOP alone does not deliver the
> edge. ... Same runs with -flush_packets 1 added: static edge 2.0@2.5 s, 4.0@4.5, 6.0@6.6 …; moving 2.0@2.03,
> 4.0@4.0, 6.0@6.01 → lag ≤ ~2.5 s.

Implementation note: "capture argv gets `-flush_packets 1` after `-movflags` (ASK 5: muxer buffer held static-screen
fragments ~20 s)."

## Options
1. NVENC default GOP. Rejected: the edge lags about 8 s.
2. `-g 2*fps` alone. Rejected after measurement: on a still screen the muxer buffer held fragments for about 20 s.
3. `-g 2*fps` plus `-flush_packets 1`. Chosen: measured lag of about 2.5 s or less on both static and moving content.
4. Adding `-keyint_min` / `-forced-idr` was not chosen. A9 records that NVENC honours `-g` without them.

## Decision
`build_capture_command` emits `-g <2*fps>` among the encoder options for every codec branch, and `-flush_packets 1`
after `-movflags`. The signature and argv order are otherwise unchanged. `test_video_capture.py` asserts both.

## Consequences
The live edge lags real time by about 2-2.5 s for recordings started after 4.30.0 (and a Recall restart). A recording
that was already running keeps the old lag. Other costs (file size, encoder load from shorter GOPs and per-packet
flushes) are not recorded.

## Invariants created
- Capture argv keeps `-g 2*fps` and `-flush_packets 1` on every codec branch. Removing either silently brings back an
  edge lag of about 8 s (GOP) or about 20 s (still screen, muxer buffer).
- A new capture codec branch gets both flags and a test.
- Keyframe spacing is verified on a real recording (ffprobe), not only in offline tests.

## Revisit when
Not recorded.
