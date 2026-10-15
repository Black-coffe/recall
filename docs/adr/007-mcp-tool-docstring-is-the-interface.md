# ADR-007: An MCP tool docstring is the interface for the calling model, not a comment for a human

- Status: proposed
- Date: 2026-09-04
- Spec: docs/specs/grep-explainability

## Context

From `## Plan deltas` (D2) of the `grep-explainability` spec:

> **The `grep_archive` docstring is an interface, not a comment (new finding 4).** It is the only text by
> which the model chooses the tool and interprets `truncated`. It must describe the reasons for truncation, `match_total`
> and the restriction about chunk boundaries.

And (D3), when the same class of oversight turned up a second time, in two different places:

> **Docstrings that are interfaces must be complete.** The field inventory in the `archive_grep` header
> named three of the four added fields; the `grep_archive` docstring on `max_scan`/`deadline` said that
> raising `limit` is pointless, and did not name the action available to the caller (narrow
> `days`/`source_type`/`transcription_id`).

## Options

None recorded - the plan fixes the chosen shape of the decision, not the alternatives that were weighed.

## Decision

The docstring of an MCP tool (and the module header describing the shape of its response) is an interface specification for the
calling model, not an explanatory comment for a human reading the code. It must fully
list the response fields, all reasons for a partial/truncated result (and what exactly the caller can
do in each case — not only what not to do), and the known structural limitations of the method.
An incomplete docstring here is the same defect class as an incomplete code contract: the model either picks the wrong
tool or misinterprets `truncated`/similar fields.

## Consequences

Easier: the model consuming the tool makes correct decisions about tool choice and interpretation of partial
results without extra clarification rounds. Harder: an MCP tool docstring requires the same
completeness review as the response contract itself — a short "convenient" description that does not keep up with the shape of the
response is considered a defect, not a stylistic question.

## Invariants created

The docstring of any MCP tool of this project (and the module header describing the shape of its response) describes the
full set of response fields, all reasons for a partial/truncated result, and the action available
to the caller in each such case. Changing the response shape of a tool without a corresponding change to the docstring
is considered an unfinished edit.

## Revisit when

A mechanical way appears to check an MCP tool docstring against the actual response shape (e.g. a test
comparing the fields listed in the docstring with the schema) — then the discipline can be partially moved from
review to automatic checking.
