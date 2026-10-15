# ADR-027: A translated or transliterated name is not an ASR error; the cross-script phonetic key `vocabulary.same_name` decides

- Status: accepted (2026-10-08, owner: "I accept everything, I give it all to you" — accepted together with the three places outside the plan deltas; shipped in 4.8.0 at ccd3de6)
- Date: 2026-10-08
- Spec: docs/specs/k1-name-vocabulary

## Context

K1 builds Whisper's name vocabulary from the entity graph and ranks "hard" names by
"occurrences × number of alias-errors". The plan's Assumptions first defined an alias-error as an
alias that occurs in transcripts and does not match the canonical name by stem: the first 4 letters
after `casefold()`. Under that rule every Cyrillic spelling of a Latin name, and every uk/ru
translation of a first name, counted as a Whisper mistake. The owner corrected this.

Source delta (plan.md `## Plan deltas`, 2026-10-08, story 01, verbatim):

> 2026-10-08, story 01, owner's correction ("it counts as a Whisper error any name written in Cyrillic
> instead of Latin… a system of synonyms and a system of translations is needed"). The definition of "alias-error" from
> Assumptions is replaced: an alias is the same name if the stem matches OR the phonetic key across uk/ru/en
> (`vocabulary.same_name`: Cyrillic transliteration, English digraphs → sound, vowels dropped, Latin j
> gives `дж` and `й`) matches. `Гугл`, `Телеграм`, `Олексій`/`Алексей`, `Julia Bondarenko`/`Юлія` are not errors.
> The cost: the key does not distinguish vowels, so `Орбос` and `Роботес` from the Goal are no longer counted as errors;
> `Акмеец`, `Тиграм` are still errors. Rejected: a stored dictionary of translations for the whole graph (needs a
> migration and Claude for every entity — a separate step if the owner wants it). Defect card
> `docs/defects/translation-counted-as-asr-error.md` (block).

Any later story that counts ASR errors, scores name accuracy, merges aliases or ranks entities faces
the same question again: is `Гугл` a wrong "Google", or the same name?

## Options

1. **Stem match OR cross-script phonetic key, computed at runtime (`vocabulary.same_name`).** No
   migration and no per-entity model call. Cost: the key ignores vowels, so some real mis-hearings
   (`Орбос`, `Роботес`) are no longer counted as errors.
2. **Stored translation dictionary for the whole graph.** Rejected in the delta: it needs a
   migration and a Claude call per entity. Left as a separate step if the owner wants it.

## Decision

Option 1. Deciding factor: the owner's correction that a name written in another script or language
is the same name, and the rejected option's cost (a migration plus a Claude call per entity).

## Consequences

Easier: Cyrillic/Latin spellings and uk/ru/en forms of a name are treated as one name without any
stored data, and the defect card `docs/defects/translation-counted-as-asr-error.md` guards the rule.

Harder / debt accepted: the key does not tell vowels apart, so errors that differ only in vowels
(`Орбос`, `Роботес`) go undetected and drop out of the error-based ranking. Measurement is affected
too: `measure/report.md` scores names both strictly and "with translations", and the two give
different numbers.

## Invariants created

1. Whether two strings are the same name is decided by `vocabulary.same_name` (stem match or
   cross-script phonetic key across uk/ru/en). No other code may count an alias as an ASR error on
   its own rule.
2. A transliteration or translation of a name (Cyrillic for Latin, uk for ru, a localized first name)
   is never counted as an ASR error.
3. Case folding for this comparison is done in Python (`casefold()`), never with SQLite `LOWER`,
   which does not fold Cyrillic.

## Revisit when

The delta records no trigger. The one it implies is the owner asking for the stored translation
dictionary (the rejected option), or vowel-only errors such as `Орбос` turning out to matter.
