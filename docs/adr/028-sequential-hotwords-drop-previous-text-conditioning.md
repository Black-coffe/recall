# ADR-028: With hotwords, the sequential path passes `condition_on_previous_text=False` to stay within 448 prompt tokens

- Status: accepted (2026-10-08, owner: "I accept everything, I give it all to you" — accepted together with the three places outside the plan deltas; shipped in 4.8.0 at ccd3de6)
- Date: 2026-10-08
- Spec: docs/specs/k1-name-vocabulary

## Context

In faster-whisper 1.2.1, `hotwords` is added to every window's prompt *next to* the previous
context, not in place of it. The plan's Goal (verbatim):

> **`hotwords`** is added to the prompt of **every** window (`get_prompt`, :1542) NEXT TO the previous
> context `previous_tokens[-223:]`, not instead of it. The limit of the hotwords themselves is
> `max_length // 2 - 1` = 223 tokens, the tail is cut off. When `prefix` is set, hotwords have no effect.
> Together with the context the prompt can exceed 448 tokens: 1 + 222 + 223 + 3 = 449. Therefore in
> sequential mode with hotwords the context is not passed, as in batched (a round 1 review
> correction).

Source delta (plan.md `## Plan deltas`, "round 1 repair", verbatim):

> 2026-10-08, round 1 repair. In sequential mode with hotwords `condition_on_previous_text=False` is
> passed, otherwise the prompt overflowed 448 tokens and transcription crashed.
> Known cost: segments are larger, on a 240 s clip there are 22 of them instead of 80, the text is complete. Affects files and
> recordings up to 10 minutes and TG voice messages. Name accuracy and speaker labelling in this configuration were not measured.
> If speakers suffer on short recordings, the next step is a smaller vocabulary budget for the
> sequential path.

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
