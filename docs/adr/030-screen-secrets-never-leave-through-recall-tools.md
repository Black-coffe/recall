# ADR-030: Screen secrets never leave through Recall's tools; raw values only via a TTY script to a file

- Status: proposed
- Date: 2026-10-13
- Spec: docs/specs/video-visual-timeline

## Context
The visual timeline OCRs every frame of a recording, so passwords, tokens and keys that were on
screen become text that search, RAG, MCP and Claude could otherwise see. The owner added a
requirement to the brief: mask them before they reach any of those places. The plan's Assumptions
(bullet "Secrets from the screen (owner's addendum)") also record why the UI cannot reveal them. This
is an Assumptions decision, not a Plan delta. Verbatim:

> - What is found is replaced with `[🔒 секрет]` in `ocr_text`, in `vision_text`, in the `екран` chunks, in the timeline, in
>   Markdown and across all of MCP. The raw values go into a separate table `screen_secrets(keyframe_id, kind,
>   value, bbox)`, which no HTTP endpoint and no MCP tool reads.
> - A frame with a secret leaves only with the boxes painted over (on the fly from the video): `get_frame`, the frame
>   endpoint, `describe` through Claude. An unpainted frame is seen only by a person in the video player in the UI.
> - Issuing the raw values: `python -m app.services.screen_secrets reveal <tid> --out <file>`. The script works only
>   from an interactive terminal (`isatty`) after a typed confirmation, so the agent's Bash (without a TTY)
>   gets a refusal. The values are written to a file (default — the user's profile folder outside the repo). Only the path
>   and the count go to stdout.
> - A "superadmin" over HTTP cannot be told apart from the agent: localhost is trusted (`app/core/auth.py`), MCP and the agent's
>   `curl` also come from localhost. That is why there is no "show password" button in the UI, the UI sees only
>   `🔒 N секретів, отримати скриптом` ("N secrets, get with the script"). The protection boundary: secrets do not get into the chat through the Recall
>   tools. Code run on behalf of the same Windows user can read the DB directly, and this is
>   documented.

The detector is told to err on the side of hiding: "the error goes toward hiding".

## Options
1. A "show password" button in the UI, rejected by the plan's reasoning: an HTTP superadmin
   can't be told apart from the agent, because localhost is trusted and MCP and the agent's
   `curl` also come from localhost.
2. No reveal over HTTP or MCP at all. Raw values come only from a script that needs an
   interactive terminal and a typed confirmation, and writes them to a file. This is the one
   chosen.

Only option 1 is named as rejected; no other alternatives were recorded.

## Decision
Option 2. The deciding factor is that, with localhost trusted, the only thing that separates a
human from an agent is an interactive TTY, which the agent's Bash does not have.

## Consequences
What the plan records:
- The protection boundary is narrow: secrets do not reach the chat through Recall's tools. Code
  running as the same Windows user can still read the database directly. This is documented,
  not solved.
- The full video (`/api/transcription/<tid>/video`) stays unredacted, because it is the human's
  player in the UI. That limitation is documented too.
- A Claude `describe` call only ever gets the painted-over copy.

What it costs a human to retrieve a secret (a terminal step instead of a click) is not recorded
beyond the script's existence.

## Invariants created
- Every text derived from screen OCR or vision (`ocr_text`, `vision_text`, `екран` chunks, the
  timeline, Markdown, every MCP response) carries `[🔒 секрет]` in place of a detected secret.
- No HTTP endpoint and no MCP tool reads `screen_secrets`.
- Any frame image that leaves the process (frame endpoint, `get_frame`, Claude `describe`) has
  its secret boxes painted over. Only the UI video player shows unredacted pixels.
- The UI shows only a count of secrets and points to the script. There is no reveal control.
- `reveal` refuses to run without `isatty` and a typed confirmation. It writes values to a file
  (by default outside the repo) and prints only the path and the count to stdout.
- When detection is uncertain, it masks.

## Revisit when
Not recorded. Likely triggers would be HTTP auth that can tell a human from an agent, or a
non-localhost bind. Neither is stated in the plan.
