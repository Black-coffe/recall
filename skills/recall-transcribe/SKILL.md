---
name: recall-transcribe
description: Use when the user wants Recall to transcribe something — a local audio or video file, a YouTube link, a finished Recall recording or an Audio library item — through the recall MCP tools transcribe and get_job_status. Triggers — RU: транскрибируй, расшифруй запись, сделай транскрипт, загони в Recall, транскрипт ютуба; UK: транскрибуй, розшифруй запис, зроби транскрипт, закинь у Recall, транскрипт ютубу; EN: transcribe this, transcribe the recording, make a transcript, add this file to Recall, transcribe this YouTube video.
argument-hint: "<path | YouTube URL | recording session_id | library id>"
allowed-tools: mcp__recall__get_job_status mcp__recall__list_whisper_models mcp__recall__list_audio_library mcp__recall__get_active_recording mcp__recall__wait_for
---

# Recall: transcribe

`transcribe` is a WRITE tool. It needs Recall running and the `full` MCP profile.
Source to transcribe: $ARGUMENTS

## Pick the source
- A local path → `source="path"`, `path="<absolute path>"`. Only files inside the folders the
  user allowed (`RECALL_IMPORT_ROOTS`) are accepted; `PATH_NOT_ALLOWED` means ask the user to move
  the file there or extend that setting. Use the path exactly as the user gave it.
- A YouTube link → `source="youtube"`, `url=…` (the audio is downloaded first).
- A finished Recall recording → `source="recording"`, `recording_session_id=…`.
- An Audio library row → `source="library"`, `audio_download_id=…` (`list_audio_library`).

Optional: `language` (default uk), `model` (default large-v3; `list_whisper_models`),
`diarize=true` for speakers, `title`, `category_id`.

## Run and follow
1. `transcribe(source=…, …, wait_seconds=25)` answers `{job_id, created}`. `created: false` means
   the same source is already being transcribed: follow that job instead.
2. Poll `get_job_status(job_id, wait_seconds=25)` while `state` is `queued` or `running`. Report
   `phase` changes briefly. Stop after about 40 polls (roughly 15 minutes) and tell the user the job
   is still running, with its `job_id`.
3. `completed` → `result.transcription_id` is the new record; offer `get_transcript` or a summary.
   `failed` / `crashed` → report `error_code`; `crashed` (Recall restarted) is safe to retry once.

## Refusals
- `RECORDING_ACTIVE`: a call is being recorded; transcription waits, try later.
- `NOT_FINALIZED`: the recording is still being saved; `wait_for(channel="recording:<session_id>")`,
  then repeat.
- `UNSUPPORTED_EXTENSION`, `FILE_TOO_LARGE`, `MODEL_NOT_INSTALLED`, `NOT_FOUND`: report as is.

PAID when an Anthropic key is set: the new record is enriched by Claude automatically.
