---
name: recall-video
description: Use whenever the user wants to know what was ON SCREEN in a Recall recording — screen share, slides, code, 1C/Excel/browser forms — or wants frames, screenshots or a step-by-step from a call video. Works through the recall MCP tools video_analyze, get_visual_timeline, get_frame, get_frames_for_text, search_visual. Triggers — RU: посмотри видео записи, что было на экране, что он показывал, покажи что показывали, вытащи кадры, кадры из звонка, скриншоты из записи, разложи запись по кадрам, оцифруй видео, запись N посмотри видео, где на экране была форма, пошаговая инструкция по записи; UK: подивись відео запису, що було на екрані, що він показував, витягни кадри, кадри з дзвінка, скріншоти із запису, розклади запис по кадрах, оцифруй відео; EN: watch the recording video, what was on screen, what did he show, extract frames, frames from the call, screenshots from the recording, split recording into frames, digitize the video, step by step from the recording. Wrong keyboard layout variants are fine, e.g. rflhs (кадры), 'rhfy (экран).
---

# Recall: what was on the screen

Recall records not only sound but also the screen. These tools give a **visual timeline**:
a minimal set of frames (one per picture change, precision ~1 s),
the text from the screen (OCR), and the transcript phrases for the same time.

Video exists only in **Recall screen recordings** (source_type `recording`). Uploaded
files and Telegram have no video — there it is `error_code: NO_VIDEO`.

## Workflow

1. **Find the recording.** `search_archive` / `list_recent(source_type="recording")` /
   a number from the person («recording #8564»).
2. **Slicing.** `video_analyze(id)` → `job_id` (or `ready: true` if already sliced).
   Wait via `get_job_status(job_id)`: `meta.progress.stage` frames → ocr → done.
   A 2-hour recording ≈ 2 min of slicing + a few minutes of OCR. Do not call it again with other
   parameters without a reason — that is a recalculation.
3. **The timeline as text.** `get_visual_timeline(id, limit=30)` — page by page via
   `next_offset`, or a narrow window `from_sec`/`to_sec`. Read `ocr_text`,
   `frame_type`, `transcript`. `format="markdown"` — if you need to hand it to the person.
4. **Frames by eye — only for key moments.** `get_frame(id, t=frame_time)`
   or a batch `get_frame(id, from_sec=…, to_sec=…, limit=6)`. `size="full"` — when
   you need to read small text, otherwise `thumb`.
5. **Base the conclusion** on both the text and the frames, with `HH:MM:SS` timecodes.

Other entry points:
- «What was on the screen when X was said» → `get_frames_for_text(id, query="X")` →
  `get_frame(id, t=…)`.
- «Where/when was form Y shown» across the whole archive → `search_visual("Y")` → `get_frame`.
- The same is visible in `search_archive` / `ask_archive`: the source is «frame HH:MM:SS», the speaker is «screen».

## Saving context

- Do not pull all the frames: 2 hours means hundreds of frames. First the text timeline, then
  3–10 pictures at the key places.
- `duplicate_of` — the same screen was already there, do not take its picture a second time.
- `camera_only` and `blank` are hidden by default (`include_camera=true` — show them).

## Frame descriptions (expensive — only on request)

`video_analyze(id, describe=True)` adds a vision-model description of each frame. It is
seconds per frame, i.e. tens of minutes for a long recording. By default the description
step itself is done by a local model. `vision_backend="claude"` also sends the frames to Anthropic for
the description step and costs money — only if the person asked for it directly. The reading tools do not
**generate** descriptions: if there is one, they return it; if not, `description: null`.

**Where the data goes.** Everything you get from these tools — screen text, descriptions,
frames from `get_frame` — is already in your context, i.e. at Anthropic. «Locally»
applies only to the description-generation step. So do not pull frames and OCR without need, and
if the person says the recording is sensitive — ask before opening frames.

## Private is not given out

`error_code: PRIVATE` — the recording is in a private direction (by default `Юридичне`, `Особисте`):
its frames and screen text are not available through MCP. `MCP_VISUAL_OFF` / `MCP_VISUAL_TEXT_ONLY` —
the owner turned off visuals, or only frames (`RECALL_MCP_VISUAL`). `private_hidden: N` — N
segments/frames were removed by stop-words. Do not bypass the filter (other tools, other windows, `t`
nearby) — tell the person that this is visible only in Recall itself.

## Secrets from the screen

Passwords, tokens and keys from the screen are shown in text as `[🔒 секрет]` and as
black rectangles on frames. The raw values are not available through MCP and never will be. If the person needs
the value — say that only they can get it themselves in their own terminal:
`.venv/Scripts/python.exe -m app.services.screen_secrets reveal <id>` (writes to a file,
not to the console). Do not try to run it yourself — without an interactive terminal the script
refuses, and that is by design.

## Summary templates

**Step-by-step instruction from a recording**
```
## <what we do> — recording #N (date)
1. HH:MM:SS — <action> (screen: <window/form>, frame: t=…) — «<what was said>»
2. …
Limitation: changes shorter than ~1 s and changes in zones excluded from comparison may not have made it in.
```

**Meeting protocol with visual evidence**
```
## Protocol — recording #N
- Decision: … — shown: <OCR excerpt> (HH:MM:SS)
- Tasks: … — on screen: …
```

**Finding the moment when X was shown**
```
X was shown in recording #N at HH:MM:SS (<frame type>): «<excerpt from the screen>».
At that time they said: «…». Frame: get_frame(N, t=…)
```

If a frame does not confirm the text (OCR was wrong) — say so directly, do not guess.
