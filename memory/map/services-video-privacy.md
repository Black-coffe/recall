# services — video timeline, screen secrets, privacy

last-verified: 2026-10-25 (4.31.0 filter_chats; live_frames.py, thumbs_pass start/window); 2026-10-18

Part of `app/services/*`; hub — [services.md](services.md). Wiki: `docs/wiki/video-timeline-and-screen-secrets.md`;
measurement and limits — `docs/video-timeline.md`. Video exists only in screen recordings.

## Visual timeline (`video-visual-timeline`, 4.10.0)
- **video_frames.py** (no DB) — `select_keyframes(path, *, method='thumbs'|'bisect', step, min_res, threshold,
  ignore_regions, max_frames, settle, cache_dir)` → `{segments, capped, width, height}`. thumbs: one ffmpeg pass
  1/min_res fps 160x90 gray, comparison with the segment's ANCHOR (`diff_score`; `PIX_DELTA=12`, `DEFAULT_THRESHOLD=0.02`,
  `DEFAULT_SETTLE=2.0`), mask = ignore_regions minus `flicker_mask`; cache `thumbs.npz` in `<session>/frames_cache`
  (valid only for the same duration+fps). `DEFAULT_MAX_FRAMES=1500` (`cap_segments` merges the shortest).
  `grab_frame(path, t, max_width)` → PIL in memory only. `mark_duplicates` (dHash ≤4 bits), `classify` →
  `blank|transition|camera_only|slide|screen_share`. `ALGO_VERSION=1`. `pick_sharpest` is not called by the pipeline.
- **video_analysis.py** — `resolve_recording_video()`, `analyze_video(db, tid, force, job, vision_backend, *, method, step,
  min_res, threshold, settle, ignore_regions, max_frames, ocr=True, describe=False)` (`:249`): segments → OCR by a thread pool
  (`ocr_words` with boxes; `_VideoOcrEnv`: `OMP_THREAD_LIMIT=1` + `OCR_TESSDATA_FAST_DIR`) → `screen_secrets` → description (only
  `describe`) → vectors BEFORE the transaction → `video_keyframes` + chunks `speaker='екран'` + `video_analysis_params`.
  The same `_params()` without `force` → `skipped:'already analyzed'`. Progress `job.meta['progress']`. No video →
  `error_code:'NO_VIDEO'`. Does not throw.
- **screen_secrets.py** — `scan_all(text, words)`, `mask_text/mask_words` (`MASK='[🔒 секрет]'`), `redact(img, boxes, w, h)`,
  `secret_boxes(ocr_boxes_json)`, `store()` (table `screen_secrets`, v49, `migrations.py:2066`). CLI `reveal <tid>` —
  TTY + confirmation only, to a file.
- **video_timeline.py** — `build()` (`HIDDEN_TYPES=('camera_only','blank')`, ≤200), `to_markdown`, `render_frame()`
  (timeline time → a frame with secrets painted over; a non-representative one is OCR'd on the fly; without OCR → black; meta carries
  `frame_text` — the text of THIS very frame, `None` if there was no OCR, `:185-200`), `frames()` (≤12, base64),
  `frames_for_text()`, `search_visual()` (a casefold substring in Python over OCR+description). `TimelineError.code`:
  `NOT_FOUND|NO_VIDEO|BAD_REQUEST|NOT_ANALYZED`. `_live_secret_boxes(video, t, ffmpeg)` (`:208`) → `(boxes, w, h, text)`,
  without OCR `text=None`.

## live_frames.py (`live-video-frames`, 4.30.0)
Frames of a recording still running; computed per request from the growing fMP4, nothing stored (no manifest/DB write, no thread).
`store`, `service`, `ffmpeg` injected. Times are recording-timeline seconds (`video_t = t - segment.start_offset_sec`).
- `readable_until(path)` — stdlib `struct` walk of top-level boxes, sums `trun` durations of each `moof` whose `mdat` is fully on disk;
  never ffprobe `format=duration` (over-reports a partial file). `None` = no complete fragment.
- Tracks come from `manifest.streams.video[]`; `_track_segments` uses `segments` (one per start/resume), a legacy track = one segment;
  other `video_mon<N>[_segN].mp4` on disk are only counted (`segments_unmapped`). Pauses are gaps between segments.
- API: `status(sid)` -> tracks + `readable_until` + per-segment ranges; `frame_at(sid, t, track, size)`; `frames_range(sid, start, end, mode
  step|changes, step, min_res, limit<=12 (MAX_FRAMES_PER_CALL), cursor, track, size)` -> `next_t` (verbatim cursor, None = done), `end_clamped`,
  `scanned_until`, `truncated`. `changes` scans <= `LIVE_SCAN_MAX_THUMBS`=300 thumbs per call via `thumbs_pass(start=, window=)`.
  `step`/`min_res` must be >= 1/fps. Seek snaps to the frame index, a quarter frame early (`_seek_time`).
- Masking: every frame goes through `video_timeline._live_secret_boxes` + `screen_secrets.redact`; `text` is the masked OCR text (`None` w/o OCR).
- Not live (`finalized` / not active) -> `{available: False, reason}`, not an error. `LiveFramesError.code`: `NOT_FOUND|NO_VIDEO|INVALID_ARGUMENT|
  PAST_LIVE_EDGE` (409, carries `readable_until`) `|NOT_RECORDED` (404, `readable_ranges`, t inside a pause).
- `video_frames.thumbs_pass` gained `start`/`window` (bound the ffmpeg pass). HTTP/MCP — `blueprints-media.md`, `root-processes.md`.

## privacy.py (`mcp-visual-privacy`, 4.11.0)
No torch/flask — imported from the stdio-MCP; K4 uses it too. What does not leave the archive.
- `visual_mode()` (`RECALL_MCP_VISUAL` `images|text|off`, unknown → `off`).
- `private_tids(db, tids)` — records in directions from `RECALL_PRIVATE_CATEGORIES` (default `Юридичне,Особисте`; a name at any
  level + descendants, namesakes are all hidden; DB failure → `PrivacyError`).
- `stop_words()` (`RECALL_PRIVATE_WORDS_FILE` or `private.txt` in the root; no file → `[]`, unreadable → `PrivacyError`;
  template `private.example.txt`), `has_stop_word` (`casefold` substring, no morphology).
- `screen_chunk_allowed(chunk, private, words, mode)` (only `speaker='екран'`; `off`/private/stop word → no),
  `filter_screen_chunks(db, chunks)` → `(kept, hidden)`; `PrivacyError` → all screen chunks removed (fail closed).
- `category_change_exposes(db, old_cid, new_cid)` (4.19.0) — True when a re-direction takes a label out of a private direction into
  an open one or none (DB failure -> `PrivacyError`); consumers: `blueprints/mail.py` PATCH for `X-Recall-Actor: mcp`, `telegram_chats._set_category` (every caller).
- `filter_chats(rows, *, db_path, key_id='chat_id', key_title, key_username, key_category='category_id')` (4.31.0, `:130`) — chats/dialogs a non-UI
  caller may see: a row is dropped for a stop word in title/username, its own private `category_id` (`key_category=None` skips this, used for dialogs), or an id that is a
  monitored chat in a private direction. Unreadable stop words / failed check -> `PrivacyError` (caller refuses the whole call). Consumers: `telegram.py` `/chats`, `/dialogs`.
- Consumers: `rag._mcp_screen_filter` (`rag.py:320`; only `channel='mcp'`, calls `:368,:526` before Claude),
  `mcp_server.py` (see root.md), `pipes_privacy.py`. The Recall UI is not affected by the filter.
