# ADR-028: With hotwords, the sequential path passes `condition_on_previous_text=False` to stay within 448 prompt tokens

- Status: accepted (2026-10-08, власник: «ВСЕ принял все даю тебе» — прийнято разом із трьома місцями поза дельтами плану; shipped in 4.8.0 at ccd3de6)
- Date: 2026-10-08
- Spec: docs/specs/k1-name-vocabulary

## Context

In faster-whisper 1.2.1, `hotwords` is added to every window's prompt *next to* the previous
context, not in place of it. The plan's Goal (verbatim):

> **`hotwords`** добавляется в промпт **каждого** окна (`get_prompt`, :1542) РЯДОМ с предыдущим
> контекстом `previous_tokens[-223:]`, а не вместо него. Предел самих hotwords —
> `max_length // 2 - 1` = 223 токена, хвост отрезается. При заданном `prefix` hotwords не действуют.
> Вместе с контекстом промпт может превысить 448 токенов: 1 + 222 + 223 + 3 = 449. Поэтому в
> последовательном режиме с hotwords контекст не передаётся, как и в batched (поправка ревью
> раунда 1).

Source delta (plan.md `## Plan deltas`, "ремонт раунда 1", verbatim):

> 2026-10-08, ремонт раунда 1. В последовательном режиме при hotwords передаётся
> `condition_on_previous_text=False`, иначе промпт переполнял 448 токенов и транскрипция падала.
> Известная цена: сегменты крупнее, на клипе 240 с их 22 вместо 80, текст полный. Затрагивает файлы и
> записи до 10 минут и голосовые TG. Точность имён и разметка спикеров в этой конфигурации не мерились.
> Если на коротких записях пострадают спикеры, следующий шаг — меньший бюджет словаря для
> последовательного пути.

`RECALL_VOCAB_MODE=hotwords` is now the default, so any change to the vocabulary budget, the decode
options or the sequential/batched split meets this limit again.

## Options

None recorded - the delta states the chosen shape only. The delta names a smaller vocabulary budget
for the sequential path only as a later step, not as an alternative that was weighed.

## Decision

On the sequential path, when `hotwords` is set, `transcribe_with_progress` passes
`condition_on_previous_text=False`, as the batched path already does. Deciding factor: with
previous-text conditioning the prompt went over 448 tokens and transcription crashed.

## Consequences

Accepted cost (from the delta): segments are coarser (22 instead of 80 on a 240 s clip; the text is
complete). This affects files and recordings up to 10 minutes and Telegram voice messages. Name
accuracy and speaker labelling in this configuration were not measured.

## Invariants created

1. The prompt of any decode window never exceeds 448 tokens: previous-text context + hotwords
   (at most 223 tokens, trimmed at a name boundary) + special tokens.
2. On the sequential path, `hotwords` and `condition_on_previous_text=True` are never set together.
   Re-enabling conditioning requires shrinking the hotwords budget so that the sum fits under 448.

## Revisit when

Speaker labelling on short recordings (sequential path) gets worse. The delta's next step is then a
smaller vocabulary budget for the sequential path. Also when faster-whisper's `get_prompt` or the
`max_length` changes on an upgrade.
