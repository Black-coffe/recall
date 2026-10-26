---
name: recall-record-stop
description: Stop a Recall recording, or cancel a recording this agent started less than 5 minutes ago, through the recall MCP tools recording_stop and recording_cancel_own. Typed by the user only; every call raises a Windows dialog the user confirms.
argument-hint: "[cancel] [session_id] [name]"
disable-model-invocation: true
allowed-tools: mcp__recall__get_active_recording mcp__recall__wait_for
---

# Recall: stop or cancel a recording

The user typed this command: they want a recording to end. Arguments: $ARGUMENTS

Both tools are DESTRUCTIVE class. Each call raises a native Windows dialog on the user's machine;
the dialog is the confirmation, nothing here skips it. If the user is away, the call waits and
fails closed: say so, do not retry in a loop.

## Which recording
`get_active_recording()`. One live recording → that one. Several → `session_id` is null: list
them (`sessions`, with `started_by`) and ask which, unless the arguments name it.

## Stop (keeps the audio)
`recording_stop(session_id=…, name="<optional name>")` answers `status: "stopping"` at once;
`already: true` means it was stopped before. Saving follows by itself: wait with
`wait_for(channel="recording:<session_id>")` until the status is `finalized`, then report it.
To transcribe it next, use **recall-transcribe** with `source="recording"`.

## Cancel (deletes the audio)
Only when the arguments say `cancel`. `recording_cancel_own(session_id=…)` deletes a recording
this agent started less than 5 minutes ago; there is no restore. Refusals:
- `OWNER_SESSION`: the user's own recording, never cancelled by an agent;
- `CANCEL_WINDOW_CLOSED`: older than 5 minutes or not live; offer `recording_stop` instead;
- `RECORDING_KEPT`: already saved.
