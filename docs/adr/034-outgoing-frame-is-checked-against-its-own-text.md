# ADR-034: An outgoing frame is checked against its own text, not its segment representative's

- Status: proposed
- Date: 2026-10-14
- Spec: docs/specs/mcp-visual-privacy

## Context
The stop-word filter (ADR-033) checks the text of what leaves. For an arbitrary `t`, `get_frame`
only had the OCR of the segment's representative frame, not of the frame actually shown. Plan
delta, verbatim:

> 2026-10-14, story 01 during the build: `get_frame` for an arbitrary `t` returns the OCR of the segment's representative, not of the frame that is in the picture — a stop-word on the frame itself cannot be checked. Decision: `video_timeline.render_frame` returns `frame_text` (the live OCR of this frame, secrets masked), MCP checks it and does not let it out; +`app/services/video_timeline.py` and `tests/test_video_timeline.py` (the old test hit the live DB) in `## Files` of story 01. Rejected: trusting the representative's text (not fail closed) and refusing `get_frame` for any non-representative (breaks "show the frame at 12:34").

## Options
1. Trust the representative's text. Rejected because it does not fail closed.
2. Refuse `get_frame` for any frame that is not a representative. Rejected because it breaks "show
   the frame at 12:34".
3. `render_frame` returns `frame_text`, the live OCR of this frame with secrets masked, and MCP
   checks it before sending. This is the one chosen.

## Decision
Option 3. The deciding factor is fail closed without losing arbitrary-time frames.

## Consequences
Recorded: `app/services/video_timeline.py` changed, and `tests/test_video_timeline.py` was reworked
because the old test hit the live database. Per the plan's Assumptions, a frame that cannot be
checked is not served. Not recorded: the latency cost of live OCR per request.

## Invariants created
- A frame image leaving through MCP is cleared by the text of that exact frame (`frame_text`). The
  text of another frame in the segment never stands in for it.
- If there is no text to check (OCR unavailable), the frame is not served.
- This extends ADR-030's live OCR, which there only paints secret boxes, to the stop-word check.

## Revisit when
Not recorded.
