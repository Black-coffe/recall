# ADR-029: An action button's token lives as long as the call it acts on

- Status: proposed
- Date: 2026-10-08
- Spec: docs/specs/k2-call-autoprompt

## Context
K2 puts buttons outside the Recall tab: a Windows notification and an in-tab banner offer
«Записати» / «Не потрібно» while a call runs, and «Зупинити» once it goes quiet. Each button is a
URL on `127.0.0.1` carrying a token (`/api/recording/autoprompt/<token>/<action>`), so the token
is the only thing that separates a deliberate click from any other local request. The plan first
gave it a fixed 10 minutes. Plan delta of story 01, verbatim:

> Было (Assumptions, критерий истории 01): токен живёт 10 мин. Стало: токен живёт, пока жив его
> звонок («Записати?») или наблюдение за записью («Зупинити?»), жёсткий потолок 3 ч; одноразовость
> и 128-битная случайность без изменений. Почему: «Записати?» висит на экране весь звонок
> (уведомление-напоминание), и кнопка, молча мёртвая на 11-й минуте разговора, — ровно тот пропуск,
> против которого K2. Отвергнуто: 10 мин (мёртвая кнопка посреди звонка).

## Options
1. Fixed 10 minutes - a short window, but the reminder toast stays on screen for the whole call,
   so the button dies silently mid-call: the missed recording K2 exists to prevent.
2. Lives as long as the thing it acts on (the call for «Записати?», the watched recording for
   «Зупинити?»), hard ceiling 3 h - the button works whenever it is visible; the ceiling bounds a
   prompt whose call end was never seen.

## Decision
Option 2. A button that is visible must work. The ceiling (`PROMPT_MAX_AGE_SEC = 3 * 3600` in
`app/services/recording/call_watch.py`) only bounds a prompt whose call end or watch end was never
observed (`_expire`); the delta does not record why 3 h and not another number.

## Consequences
A token can stay valid for hours instead of minutes. That is accepted because it is single-use,
128-bit random (`secrets.token_urlsafe(16)`), dies with its call or watch, and the server binds
to `127.0.0.1` by default. Expired and consumed prompts are logged (`prompt_expired`, `click`) to
`logs/call_watch.jsonl`, so a dead button shows up in the K2 measurement rather than silently.
Not recorded in the delta: how long a token lives when the app is reached over a network bind
(`RECALL_BIND_ALL`); today the autoprompt only runs on the Windows desktop profile.

## Invariants created
- An action token from a notification or banner is single-use: the first valid click consumes it,
  every later or wrong-action click gets nothing (`CallWatcher.consume`).
- It is at least 128 bits from `secrets`, never derived from a PID, exe name or time.
- It lives as long as the call or watched recording it acts on, and never past 3 h.
- When its call or watch ends, the token is removed together with its toast.

## Revisit when
The K2 measurement (`docs/specs/k2-call-autoprompt/followup-measure.md`) shows prompts living
past their call. Also when action URLs are reachable from anywhere but `127.0.0.1`, or a second
kind of out-of-tab action button appears (K4/K5 Telegram buttons are a candidate). Then this rule
becomes the general one for action tokens or is replaced by an authenticated one.
