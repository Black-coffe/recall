# ADR-034: An outgoing frame is checked against its own text, not its segment representative's

- Status: proposed
- Date: 2026-10-14
- Spec: docs/specs/mcp-visual-privacy

## Context
The stop-word filter (ADR-033) checks the text of what leaves. For an arbitrary `t`, `get_frame`
only had the OCR of the segment's representative frame, not of the frame actually shown. Plan
delta, verbatim:

> 2026-10-14, історія 01 під час побудови: `get_frame` для довільного `t` віддає OCR представника відрізка, а не того кадру, що на картинці, — стоп-слово на самому кадрі не перевірити. Рішення: `video_timeline.render_frame` повертає `frame_text` (живий OCR цього кадру, секрети замасковані), MCP перевіряє його і не пропускає назовні; +`app/services/video_timeline.py` і `tests/test_video_timeline.py` (старий тест бив по живій БД) у `## Files` історії 01. Відкинуто: вірити тексту представника (не fail closed) і відмовляти в `get_frame` для будь-якого не-представника (ламає «покажи кадр на 12:34»).

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
