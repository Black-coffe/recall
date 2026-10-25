---
domain: video
tags: [video, ocr, secrets, mcp]
related: [rag-context-integrity.md, recording-sources-and-sessions.md]
last-verified: 2026-10-25
---

# Video visual timeline and secrets from the screen

Video exists only in Recall screen recordings (`audio_downloads.has_video=1` -> `recording_video_tracks`); `video_analysis.resolve_recording_video` is the single source of truth, no video -> `NO_VIDEO`.

- **Frame images are not stored.** `video_keyframes.image_path` = NULL for new rows (left over from Phase 23; a recalculation deletes the old jpgs). A frame is cut from the video on demand (`video_frames.grab_frame`, `video_timeline.render_frame`), so `/frame` and `/frames?t=` work without analysis; `/keyframe/<id>` also cuts on the fly, but needs the row.
- **Time.** `video_keyframes.ts_offset_sec/end_sec` is timeline time (video + the track's `start_offset_sec`); `video_time_sec` is the representative's time within the video itself. A frame for `t` = `t - start_offset_sec`.
- **Cache = parameters.** `transcriptions.video_analysis_params` (JSON: `algo`=`video_frames.ALGO_VERSION`, method, step, min_res, threshold, settle, ignore_regions, max_frames, ocr, describe, vision_backend). A match without `force` -> the ready result, no decoding. Other parameters or version -> a full recalculation (DELETE keyframes + chunks with `speaker='екран'`).
- **Secrets are masked BEFORE any write or description.** `screen_secrets.scan_all` over the OCR words; `ocr_text`, `vision_text`, `ocr_boxes_json` and RAG chunks (`speaker='екран'`, section `кадр ГГ:ХХ:СС`) keep `[🔒 секрет]`. The raw values live only in the `screen_secrets` table (v49), which no HTTP endpoint or MCP tool reads. The only path to the raw: `python -m app.services.screen_secrets reveal <id>` from a TTY + confirmation, into a file.
- **An image goes outward only with the boxes painted over** (`screen_secrets.redact`). A frame that is not the segment's representative is OCRed on the fly (`_live_secret_boxes`); if OCR is unavailable the frame is returned fully black. A vision-model description (`describe=True`, off by default) also comes from the already painted-over frame.
- **Outward through MCP there is also a privacy filter** (`app/services/privacy.py`, 4.11.0; the Recall UI sees everything). `RECALL_MCP_VISUAL`: `images` (default) | `text` (`get_frame` refuses) | `off` (all 5 video tools refuse, chunks with `speaker='екран'` vanish from `search_archive`/`grep_archive`/`ask_archive`); an unknown value = `off`. A recording in a direction from `RECALL_PRIVATE_CATEGORIES` (default `Юридичне,Особисте`; a name at any level of the tree + descendants, namesakes are all hidden) -> `PRIVATE`. A word from `private.txt` (a `casefold` substring, no morphology) hides the segment/frame/hit/chunk, with the `private_hidden` counter. Fail closed: a check failure or an unreadable file -> hide. A frame without OCR (`frame_text=None`) is not returned through MCP. The same stop-list is used by the K4 bot.
- **Live frames follow the same rules (4.30.0).** `live_frames` serves a recording still running: frames are cut from the growing fMP4 per request, never stored, and every one passes `_live_secret_boxes` + `redact` (image) and the masked OCR text. The readable edge is the last complete `moof`+`mdat` (never ffprobe duration); past it -> `PAST_LIVE_EDGE`, inside a pause -> `NOT_RECORDED`. The MCP tools `get_live_video|get_live_frames` obey `RECALL_MCP_VISUAL` (`off` refuses, `text` strips images) and the stop-list, and drop a frame without OCR text; the recording has no direction yet, so `RECALL_PRIVATE_CATEGORIES` cannot apply.
- **Frame chunks:** a repeat by content (`duplicate_of`, dHash <= 4 bits, non-adjacent only) is not duplicated in RAG. The `camera_only`/`blank` types are hidden from the timeline by default (`include_camera`).
- **OCR environment:** `document_parser._configure_tesseract` (`OCR_TESSERACT_CMD`, `OCR_TESSDATA_DIR`); video OCR sets `OMP_THREAD_LIMIT=1` and `OCR_TESSDATA_FAST_DIR` for the duration of the call (`video_analysis._VideoOcrEnv`).

See `docs/video-timeline.md`, [rag-context-integrity](rag-context-integrity.md) (the `екран` chunks in search), [recording-sources-and-sessions](recording-sources-and-sessions.md).

Decisions: ADR-030 (secrets), ADR-033 (the privacy filter at the MCP output, one stop-list), ADR-034 (a frame is checked against its own text).
