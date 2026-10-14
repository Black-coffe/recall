# ADR-030: Screen secrets never leave through Recall's tools; raw values only via a TTY script to a file

- Status: proposed
- Date: 2026-10-13
- Spec: docs/specs/video-visual-timeline

## Context
The visual timeline OCRs every frame of a recording, so passwords, tokens and keys that were on
screen become text that search, RAG, MCP and Claude could otherwise see. The owner added a
requirement to the brief: mask them before they reach any of those places. The plan's Assumptions
(bullet «Секрети з екрана (доповнення власника)») also record why the UI cannot reveal them. This
is an Assumptions decision, not a Plan delta. Verbatim:

> - Знайдене замінюється на `[🔒 секрет]` у `ocr_text`, у `vision_text`, у чанках `екран`, у стрічці, у
>   Markdown і в усьому MCP. Сирі значення лягають в окрему таблицю `screen_secrets(keyframe_id, kind,
>   value, bbox)`, яку не читає жоден HTTP-ендпоінт і жодна MCP-тулза.
> - Кадр зі секретом назовні йде лише із зафарбованими рамками (на льоту з відео): `get_frame`, ендпоінт
>   кадру, `describe` через Claude. Незафарбований кадр бачить лише людина у плеєрі відео в UI.
> - Видача сирих значень: `python -m app.services.screen_secrets reveal <tid> --out <файл>`. Скрипт працює лише
>   з інтерактивного терміналу (`isatty`) після введеного підтвердження, тож Bash агента (без TTY)
>   отримує відмову. Значення пишуться у файл (дефолт — тека профілю користувача поза репо). На stdout
>   іде тільки шлях і кількість.
> - «Суперадмін» на HTTP не можна відрізнити від агента: localhost довірений (`app/core/auth.py`), MCP і
>   `curl` агента теж ідуть із localhost. Тому кнопки «показати пароль» в UI немає, UI бачить лише
>   «🔒 N секретів, отримати скриптом». Межа захисту: секрети не потрапляють у чат через інструменти
>   Recall. Код, запущений від імені того самого користувача Windows, може прочитати БД напряму, і це
>   задокументовано.

The detector is told to err on the side of hiding: «Помилка йде в бік приховування».

## Options
1. A «показати пароль» button in the UI, rejected by the plan's reasoning: an HTTP superadmin
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
