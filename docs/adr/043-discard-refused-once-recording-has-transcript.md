# ADR-043: Discard is refused once a recording has a transcript

- Status: proposed
- Date: 2026-10-19
- Spec: docs/specs/recording-autosave

## Context
Spec recording-autosave lets the server save a stopped recording by itself: it transcribes it, titles it and files it, while the tab that was recording may still be showing the save screen. The story 01 build delta records:

> 2026-10-19 · story 01 build · `app/state.py` replaces `app/services/recording/reconcile.py` in story 01 `## Files`: the scheduler is a state singleton for the blueprint, and the boot re-arm runs in app.py after reconcile. Also added: `/discard` refuses a recording that already has a transcript (side-agent warning that the frozen tab's `«Скасувати»` deletes final.mp3).

`POST /api/recording/<sid>/discard` deletes all of the session's files. A stale tab whose cancel button is pressed after autosave has run would delete final.mp3 from under a transcript that already exists. The docstring of `discard_recording` (app/blueprints/recording.py) puts it this way: "deleting final.mp3 then would leave the transcript and the library row on a dead file".

## Options
None recorded. The delta states the chosen shape only.

## Decision
`/discard` returns 409 with `error_code` `RECORDING_KEPT` once anything about the recording has been kept:
- the autosave scheduler reports `running` or `done` for the session, or
- an `audio_downloads` row for the session (`recording_session_id = sid` or `youtube_id = 'recording_<sid>'`) joins a non-deleted `transcriptions` row on `file_path`.

If the transcript lookup fails, discard is refused rather than allowed. If no database is configured, discard is allowed, because there is nothing a discard could orphan. Implemented in `_recording_kept` and `discard_recording`, app/blueprints/recording.py.

The deciding factor is the delta's warning: a frozen tab's cancel must not destroy audio that a transcript and a library row already point to.

## Consequences
Not recorded.

## Invariants created
- No code path deletes a recording's session files (final.mp3) while a non-deleted transcript, or a running or finished autosave, refers to it. Any new delete or discard route goes through the same check as `_recording_kept`.
- When the guard cannot tell whether the recording was kept, it refuses the destructive action.

## Revisit when
Not recorded.
