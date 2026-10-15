# Visual timeline of a video recording

What was on the screen of a Recall recording: the minimal set of frames that covers every change
of the picture with ~1 s precision, the text from the screen (OCR), an optional frame description, and the transcript
phrases for the same interval. Available in Recall (endpoints), through MCP
(`video_analyze`, `get_visual_timeline`, `get_frame`, `get_frames_for_text`,
`search_visual`) and the global skill `recall-video`.

Code: `app/services/video_frames.py` (frame selection), `video_analysis.py` (pipeline),
`screen_secrets.py` (secrets), `video_timeline.py` (timeline, on-the-fly frames).

## Where the video comes from

Video exists only in **Recall screen recordings** (`audio_downloads.has_video = 1`,
`recording_video_tracks`). An uploaded video file is deleted after the audio is extracted,
Telegram video is not downloaded — such records have `error_code: NO_VIDEO`.

## Selection algorithm

Both methods are implemented, the default was chosen by measurements (below).

**`thumbs` (default).** One decoding pass: ffmpeg outputs 1 frame/s as 160×90 grayscale
thumbnails (a rawvideo pipe). Each thumbnail is compared with the **anchor** of the current
segment (its first thumbnail), not just with the previous one: this catches both a sharp
change, and a slow drift (scrolling), and a short visit A→B→A.

**`bisect`.** Reference frames every `step` s (seek `-ss` before `-i`); an interval with different
ends is split in half down to `min_res`. **The method's limit:** if within a step the picture
changed and came back (A→B→A), the interval's ends coincide and the change is lost.

**Comparison.** The share of mask pixels that changed by more than 12 levels out of 255.
Mask = everything minus `ignore_regions` (frame fractions `[x, y, w, h]`) minus the auto-mask:
a pixel that changes in more than 40 % of neighboring pairs (a clock in the tray, a cursor) is
excluded from the comparison.

**`settle`.** A series of changes that come more often than once per `settle` s (typing,
scrolling) is one segment. The segment boundary = the first change of the series (precision `min_res`),
the representative frame is after the last change, i.e. the already settled screen.

**The segment's representative** — the frame 0.7 s after settling (no later than the middle of the
segment). **Frame type** — a heuristic without a model: `blank` (low contrast and no
text), `transition` (a segment shorter than 2·`min_res`), `camera_only` (text < 15
characters), `slide` (≤ 12 lines in a large font), otherwise `screen_share`. Known
errors: a window with little text is classified as `camera_only`; a slide with a
small font — as `screen_share`. **Repeats:** the frame's dHash; a match with a NON-adjacent
segment → `duplicate_of`, in RAG such a frame is not duplicated, and no description is
generated for it.

## Parameters and defaults

| Parameter | Default | What it does |
|---|---|---|
| `method` | `thumbs` | `thumbs` or `bisect` |
| `threshold` | 0.02 | the share of changed pixels from which a frame is "different" |
| `settle` | 2 s | a series of changes more often than this — one segment |
| `min_res` | 1 s | boundary precision; `thumbs` takes 1/`min_res` frames/s |
| `step` | 10 s | step of reference frames (`bisect`) |
| `ignore_regions` | — | `[[x, y, w, h]]` in frame fractions, e.g. webcam tiles |
| `max_frames` | 1500 | above the ceiling the shortest segments are merged, `capped: true` |
| `ocr` | yes | local Tesseract |
| `describe` | no | frame description by a vision model (slow) |
| `vision_backend` | `local` | `local` (Ollama) or `claude` (frames go to Anthropic) |

The result is stored with the algorithm version and parameters
(`transcriptions.video_analysis_params`): the same parameters without `force` → the finished result without
decoding; different ones → recomputation. The thumbnail cache is `frames_cache/thumbs.npz` in the
recording session folder (changing the threshold does not decode the video a second time).

## Measurements (13.10.2026, RTX 3090 / 28 CPU threads, video 2560×1440 30 fps)

**Decoding.** 10 min of video into 1 fps thumbnails: CPU 9.3 s, CUDA (`-hwaccel cuda`)
39 s — on small frames copying from the GPU costs more than decoding. One seek frame
— 0.17 s. The keyframe in Recall recordings comes once per ~8.3 s, so a "keyframes
only" mode does not give 1 s precision.

**`thumbs` versus `bisect`** on a 2-hour recording: `thumbs` 112 s → 893 segments;
`bisect` 876 s → 617 segments (7.8 times longer and misses A→B→A). The default is `thumbs`.

**Frames per recording** (`threshold` 0.02, `settle` 2) versus "1 frame/s":

| Duration | 1 frame/s | Timeline |
|---|---|---|
| 48 s | 48 | 9 |
| 11 min | 640 | 52 |
| 1.3 h | 4 802 | 275 |
| 2.1 h | 7 442 | 893 |
| 2.4 h | 8 614 | 1 204 |
| 3.2 h | 11 658 | 796 |

The recordings are the whole desktop: a live terminal that scrolls every few seconds, and
webcam tiles. Head movement in the tiles is the main source of extra changes: the auto-mask does not
catch it (the tiles move around), a contour-based metric was tried and rejected (almost no
gain). Against it — `ignore_regions` for the tile zone.

**A manual check of 20 random changes** on a 2-hour recording (frame before / frame after):
20 out of 20 are real changes (scrolling, window switching, dialogs, the desktop),
no false ones. Missed changes are not measured by such a sample.

**OCR.** Tesseract 5.4, `ukr+rus+eng`, frame 2560 px: the standard models 6.9 s/frame,
`tessdata_fast` 4.9 s at the same quality on on-screen Cyrillic (at 1920 px the quality
drops noticeably). For video OCR each tesseract process is single-threaded, 16 in parallel:
**0.59 s/frame** → ~9 min per 900 frames. In total a 2-hour recording ≈ 2 min of slicing + OCR.

## Local vision model (for `describe`)

Research of 13.10.2026 (every key claim — two sources: the model card on HF,
the Ollama page, an independent benchmark): the strongest open-source model that fits on an
RTX 3090 24 GB is **`qwen3.8:27b-q4_K_M`** (Qwen3.8-27B, Apache-2.0, August 2026, 18 GB,
OmniDocBench 1.5 = 91.1). The faster one is **`qwen3.5:9b-q4_K_M`** (6.6 GB, OCRBench 89.2 — only the
model card). The current `qwen2.5vl:7b` — OCRBench 86.4. Both new ones are available in Ollama 0.40.2.
Qwen3.x "reasons" by default — for a frame description `think: false` is needed.

The default `VIDEO_VISION_MODEL_LOCAL` is still `qwen2.5vl:7b`: a measurement of seconds per frame and of quality on
on-screen Cyrillic is postponed, because the GPU was busy with another process during development. No
benchmark measures on-screen Cyrillic — the decision is only by our own measurement on 20–30 frames.

## What may be missed

- a change shorter than `min_res` (1 s) and a change inside a series of changes more frequent than `settle` —
  the timeline keeps the settled final screen of the series;
- a change that touches less than `threshold` of the area of the 160×90 thumbnail — one line
  of small text may fall short;
- changes in `ignore_regions` and in the flicker auto-mask;
- in `bisect` — A→B→A shorter than `step`;
- above `max_frames` the shortest segments are merged.

## Binding to the transcript

Time in the timeline = time in the video + the video track's `start_offset_sec`. Transcript phrases that
intersect with a segment are in the `transcript` field. The `offset_sec` parameter shifts the
transcript if the phrases "lag behind"; in Recall recordings audio and video start
together (track offset ~0.1–0.2 s), so the default is 0.

## Secrets from the screen

Passwords, tokens and keys from the screen must not get into the chat through Recall tools.

- A detector without a model over OCR words with boxes: `пароль`/`password`/`pin`/`token`/`secret`/
  `api key`/`ключ` + a value on the same line; the formats `sk-…`, `ghp_…`, `xox?-…`,
  `AKIA…`, `AIza…`, JWT, a Telegram bot token, PRIVATE KEY, `user:pass@` in a URL; long
  (≥ 24) strings with uppercase, lowercase and digits, except paths and ordinary URLs. The error
  goes toward hiding.
- What is found becomes `[🔒 секрет]` in the frame text, the description, the RAG chunks and everything that
  the endpoints and MCP return. On images the boxes are painted black — both in `get_frame`,
  and at the input of the description (local or Claude). A frame of a recording that has not been analyzed yet
  is OCR'd on the fly before it is served.
- The raw values are in the `screen_secrets` table; no endpoint and no
  MCP tool reads it. They can be obtained only in your own terminal:
  `.venv/Scripts/python.exe -m app.services.screen_secrets reveal <id> [--out file]` —
  the script requires an interactive terminal and a typed confirmation, writes the values to a file
  (default `%USERPROFILE%\RecallSecrets\`), and to the console — only the path and the count.
- **The protection boundary.** Localhost in Recall is trusted: the browser, MCP and the agent's `curl`
  are indistinguishable, so there is no "show password" button in the UI. Code run on behalf of the
  same Windows user can read the DB directly. The video as a whole
  (`/api/transcription/<id>/video`, the player in the UI) is not painted over.

## Where data goes through MCP

Whoever calls MCP is Claude. Everything returned by `get_visual_timeline`, `get_frame`,
`get_frames_for_text`, `search_visual` (and frame chunks in `search_archive`/`ask_archive`)
gets into the model's context, i.e. to Anthropic: the text from the screen, descriptions, the frames themselves.
"Local" in the `vision_backend` parameter concerns only the description-generation step. Masking
covers passwords and tokens, but not the rest of the screen content — so at the MCP egress there is a privacy
filter (spec `mcp-visual-privacy`, `app/services/privacy.py`, without a model):

- **Stop-list directions** — `RECALL_PRIVATE_CATEGORIES` (default `Юридичне,Особисте`). The name
  is matched at any level of the tree together with all descendants. The video tools answer for such a record
  with `error_code: PRIVATE`; its screen chunks disappear from `search_archive`,
  `grep_archive` (and from the neighbors' context) and `ask_archive` — before Claude is called.
- **Stop-words** — `private.txt` in the root (in `.gitignore`; example — `private.example.txt`),
  the path is changed by `RECALL_PRIVATE_WORDS_FILE`. One per line, case does not matter, **there is no morphology —
  write the stem** (`Петренк`). A segment, frame, hit or screen chunk with such a word is removed
  entirely; how much was removed is the `private_hidden` field. `get_frame` checks the text of exactly this
  frame (`frame_text`, live OCR); a frame that cannot be checked by anything (OCR unavailable) is not served.
- **The switch** `RECALL_MCP_VISUAL`: `images` (default — frames and text), `text` (`get_frame`
  refuses with `MCP_VISUAL_TEXT_ONLY`), `off` (all five tools — `MCP_VISUAL_OFF`, no screen chunks in
  search). An unknown value = `off`.
- **Fail closed**: it was not possible to check the direction or read `private.txt` — visual material is not
  served. Ordinary transcript lines are not touched by the filter — it is only about visual material.

The Recall UI and local scripts are not affected by the filter: everything is visible in the interface itself. The same
stop-list is used by the K4 bot.

## Images are not stored

The DB holds only timecodes, OCR, word boxes, descriptions. A frame is cut from the video on request
(0.2–0.5 s), compressed (JPEG, `thumb` ≤ 960 px, `full` ≤ 1920 px) and served.
During analysis frames live only in memory (for a description — in a temporary file for the duration of
the call). Frames of the old pipeline (Phase 23, `keyframes/*.jpg`) are deleted on
re-analysis.

## Cost

OCR — $0 (local). A description by a local model — $0, seconds per frame on the GPU (competes with
Whisper and the copilot). A description through Claude (`vision_backend: "claude"`) is paid: on the
default `VIDEO_VISION_MODEL_CLAUDE` (Haiku 4.5, $1/$5 per Mtok in `app/services/pricing.py`)
a frame ≈ 1.8 thousand input tokens + ~200 output ≈ 0.3 cent; 300 frames ≈ $1 (an estimate by
the rate, not a bill). So only explicitly.
