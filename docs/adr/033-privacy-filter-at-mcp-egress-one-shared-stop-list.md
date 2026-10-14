# ADR-033: The privacy filter sits at the MCP egress, fails closed, and uses one stop-list shared by every outgoing channel

- Status: proposed
- Date: 2026-10-14
- Spec: docs/specs/mcp-visual-privacy

## Context
Masking (ADR-030) covers passwords and tokens, but not the rest of what is on screen. Frames, screen
text and frame descriptions requested through MCP still reach Claude. The owner's remark, verbatim
from the brief:

> Зауваження (13.10.2026): «"Описание локально" защищает только шаг описания кадров. Сами кадры и текст с экрана, запрошенные через MCP, всё равно уходят в Claude.»

The owner's constraint: «выдавать их через MCP и Claude нельзя, чтобы они в чат не попадали».
This decision comes from the grill (brief `## Answers` 1-2, Ask 5) and the plan's `## Goal`, not
from a Plan delta. Verbatim:

> 1. Загальний пошук — Да, все выходы MCP (Рекомендую): текст с экрана из приватных записей, со стоп-словами или при off не попадёт ни в одну тулзу — иначе утечка просто переедет из get_visual_timeline в search_archive.
> 2. Стоп-лист — Один на бота и MCP (Рекомендую): один файл private.txt и одна настройка направлений; имя, добавленное раз, скрыто везде. Сейчас делаю общее ядро, K4 потом его переиспользует.

> 5. UI Recall и локальные скрипты фильтр не трогает.

> Візуальне (кадри, текст з екрана, описи кадрів) виходить із Recall у Claude лише через MCP, тож фільтр
> стоїть на виході MCP, а не в app.py: UI й локальні скрипти бачать усе як і раніше. [...] Будь-яка
> помилка перевірки = «приховати».

## Options
1. Filter only the five video tools. Rejected in Answer 1 because the leak "would just move from
   get_visual_timeline to search_archive".
2. Filter every MCP output: the five video tools plus the screen chunks in search_archive,
   grep_archive and ask_archive (mcp channel). This is the one chosen.

The brief records no alternative to "one list for the bot and MCP", and the plan records none to
"at the MCP egress, not in app.py". For those two parts, the brief and plan state the chosen
shape only.

## Decision
Option 2, with the filter at the MCP egress and one shared core. Recorded reasons:
- The filter sits at the egress because visual data reaches Claude only through MCP, and the UI
  and local scripts must keep seeing everything (Ask 5).
- Every MCP output is covered because otherwise the leak just moves to another tool (Answer 1).
- There is one list because "a name added once is hidden everywhere" (Answer 2).

## Consequences
Recorded:
- The UI and local scripts are unfiltered. ask_archive from the UI (channel ui) is not filtered.
- A stop word hides the whole segment, frame or chunk; it is not masked word by word. Responses
  carry the hidden count.
- Transcript phrases are not filtered, except when the whole record is private.
- There is no morphology: the owner writes stems in `private.txt`.

Not recorded: what this costs in answer quality when hiding is broad.

## Invariants created
- Any channel that sends visual data (frames, screen text, frame descriptions, `speaker='екран'`
  chunks) out of Recall to a model or chat goes through `app/services/privacy.py`. This covers MCP
  today and the K4 bot. No channel keeps its own stop-list or its own private-category setting.
- The single sources are `private.txt` (path set by `RECALL_PRIVATE_WORDS_FILE`; gitignored; only
  `private.example.txt` is in the repo) and `RECALL_PRIVATE_CATEGORIES` (default
  `Юридичне,Особисте`, matched at any tree level, with all descendants via `parent_id`).
- The filter lives at the egress, not in `app.py` routes. The UI and local scripts stay unfiltered.
- It fails closed:
  - a failed category check (`PrivacyError`) or an unreadable `private.txt` hides the visual data;
  - an unknown `RECALL_MCP_VISUAL` value means `off`;
  - a missing `private.txt` means no stop words.
- The filter uses no model.
- ask_archive over MCP filters screen chunks before the model is called.

## Revisit when
Not recorded.
