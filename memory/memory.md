# Hive memory index

<!-- Pointer index: <= 60 lines, always loaded. Pointers are hints - verify against code before acting.
     Maintained by drone-docs (pointers) and librarian (hygiene). Humans welcome too. -->

## Codebase map
<!-- one line per mapped module, added by /vulyk-bootstrap and /vulyk-map -->
- [index](map/index.md) — навігація по карті: який файл за що відповідає (мігровано з docs/map 21.08.2026)
- [overview](map/overview.md) — вся система з висоти: потоки даних, процеси, точки входу
- [root](map/root.md) — кореневі модулі: app.py boot, whisper_manager_new, config, mcp_server, telegram_listener
- [blueprints](map/blueprints.md) — 14 Flask blueprints: маршрути API за доменами
- [services](map/services.md) — app/services/*: database, transcription, RAG, commitments, scope, taxonomy (categories.py — dir→project tree + tags)
- [copilot](map/copilot.md) — живий ко-пілот дзвінка: топік-машина, диспетчер, каскад local→Claude
- [recording](map/recording.md) — серверний recorder: до N паралельних сесій, mic+loopback, ізоляція звуку по застосунку (process loopback), SSE
- [frontend](map/frontend.md) — SPA: router/views/cmdk, recall.css, service worker

## Unmapped territory
- tests/ (структуру описує pytest.ini + CLAUDE.md; окремої карти нема)
- scripts/mirror/ (санітизоване дзеркало — README у теці, у публічний репо не їде)
- evals/ (RAG recall@k + `graph_links.py` — read-only замір графа на знімку БД; `compare.py` — Хвиля B, зводить два `--json-out` прогони гейта поруч; описано в [services](map/services.md))

## Wiki domains
<!-- load-bearing domain notes in docs/wiki/ -->
- [entity-graph-provenance](../docs/wiki/entity-graph-provenance.md) — one mention/one owner, `thread_match` vs `thread_morph` provenance never interchangeable, morph matcher opt-in only
- [rag-context-integrity](../docs/wiki/rag-context-integrity.md) — duplicate_of excludes from search only, thread stitching char ceilings (hit ≥ neighbour), chronological order_citables, ask_log logs every successful answer from both channels; Wave B: context prefix in vector+BM25 never in citations, vector branch never mixes embedding epochs, re-embed only on a snapshot, query-rewrite flag lives in retrieval.search; 4.6.0: prefix carries direction→project path + tags for every record type, PREFIX_FORMAT/embedding_epochs refuses to write a new prefix format into an old epoch, schedule_records_reembed batches multi-record writes
- [taxonomy-tree-and-tags](../docs/wiki/taxonomy-tree-and-tags.md) — canonical node identity is its full path (siblings-only name uniqueness, v47), a bare ambiguous name never resolves silently (CategoryError('ambiguous') propagates, never swallowed into empty), --dry-run on a write pass is the same code with commit=False + rollback, rename/reparent invalidates the whole subtree's embedded prefix
- [recording-sources-and-sessions](../docs/wiki/recording-sources-and-sessions.md) — parallel sessions (registry, per-session lock, SESSION_LIMIT), AudioSource boundary, `all` byte-identical, fixed .pcm format (EXCLUDE-own-PID), silence padding + source_events, session-PID tree-root targeting, one copilot per process, AMBIGUOUS_SESSION never guesses

## Verification
- build: none — інтерпретований Python + vanilla JS
- test: `.venv/Scripts/python.exe -m pytest -q -m "not slow"` (⚠️ мігрує бойову БД — не під час запису)
- lint: none

## Learnings
- Consolidated: memory/learnings/CONSOLIDATED.md (run /vulyk-gc to refresh)
- Уроки власника/фідбек живуть окремо в auto-memory Claude Code (поза репо) — це feedback-шар, не карта коду
