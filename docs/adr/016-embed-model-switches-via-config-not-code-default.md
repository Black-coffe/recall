# ADR-016: A new embedding model/version pair ships as `.env` config, not a code-level default change

- Status: proposed
- Date: 2026-09-17
- Spec: docs/specs/production-rag-wave-b

## Context

From `docs/specs/production-rag-wave-b/plan.md`, `## Assumptions`:

> Дефолти коду не змінюються цією хвилею. `EMBED_MODEL` лишається `intfloat/multilingual-e5-large`,
> `EMBED_VERSION` (стає env через реєстр) — `2`. Пара `Qwen/Qwen3-Embedding-0.6B` + `3` вмикається
> двома рядками `.env` (задокументовано в `.env.example`) — спочатку для процесу re-embed на знімку й
> гейта, потім, після рішення власника, у бойовому `.env`. Перекидання дефолтів у коді — Tier 0 після
> рішення. Причина: злитий код інакше змінив би поведінку живого пошуку до будь-якого порівняння.

And `## Tradeoffs`:

> Модель через env, не через код (C3) проти «дефолт = Qwen3, версія 3 у коді» (Ask 3 буквально):
> обрано env, бо злиття гілки тоді не міняє живий пошук і відкат — два рядки; ціна — один Tier 0
> після рішення власника, щоб дефолти наздогнали `.env`.

## Options

1. Code-level default: ship `EMBED_MODEL=Qwen/Qwen3-Embedding-0.6B`, `EMBED_VERSION=3` as the new
   defaults in `app/core/settings.py` (Ask 3, taken literally). Rejected: merging the branch would
   change live search behaviour before any `evals.compare` run had a chance to judge it, and rollback
   would require a code revert instead of an `.env` edit.
2. Config-first: code defaults stay at the current pair (e5-large / version 2); the candidate pair is
   enabled by two `.env` lines for the snapshot/gate process only, and only promoted to production
   `.env` after the owner has seen a comparison. Chosen.

## Decision

The embedding model and version a running process actually uses come from `.env`
(`EMBED_MODEL`/`EMBED_VERSION`, read through the settings registry), never from a code-level default
change, until the owner has approved the switch from an `evals.compare` table. Carrying the decided
pair into the code defaults is a Tier 0 follow-up task, not part of the model-introduction story.

## Consequences

Easier: merging the branch that adds support for a new model never itself changes what live search
returns, and reverting a bad choice is two `.env` lines, not a code revert. Harder: an explicit
follow-up task is required to move the eventually-decided pair into the code-level default, and until
that task runs, the code's "default" and production's "actual" pair can diverge — `.env.example`
is the record of what each `.env` line does.

## Invariants created

A new embedding model/version pair is introduced through `.env`-driven config (registry-backed
`EMBED_MODEL`/`EMBED_VERSION`), validated via `evals.compare` on two `reembed`-produced snapshots,
before it ever becomes the code-level default. This applies to the *next* model swap too, not only
the e5 → Qwen3 one this spec staged.

## Revisit when

The owner runs the post-ship comparison procedure (snapshot A/B, `reembed`, `evals.gate` on both,
`evals.compare`) and decides a pair should become the default — at that point the Tier 0 "carry the
decided pair into code" follow-up closes the open action for this pair, though the invariant itself
(config first, code default second) stays in force for whichever model comes after Qwen3.
