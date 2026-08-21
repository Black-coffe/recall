# Hive memory index

<!-- Pointer index: <= 60 lines, always loaded. Pointers are hints - verify against code before acting.
     Maintained by drone-docs (pointers) and librarian (hygiene). Humans welcome too. -->

## Codebase map
<!-- one line per mapped module, added by /vulyk-bootstrap and /vulyk-map -->
- [index](map/index.md) — навігація по карті: який файл за що відповідає (мігровано з docs/map 21.08.2026)
- [overview](map/overview.md) — вся система з висоти: потоки даних, процеси, точки входу
- [root](map/root.md) — кореневі модулі: app.py boot, whisper_manager_new, config, mcp_server, telegram_listener
- [blueprints](map/blueprints.md) — 14 Flask blueprints: маршрути API за доменами
- [services](map/services.md) — app/services/*: database, transcription, RAG, commitments, scope
- [copilot](map/copilot.md) — живий ко-пілот дзвінка: топік-машина, диспетчер, каскад local→Claude
- [recording](map/recording.md) — серверний recorder: mic+loopback, live-діаризація, SSE
- [frontend](map/frontend.md) — SPA: router/views/cmdk, recall.css, service worker

## Unmapped territory
- tests/ (структуру описує pytest.ini + CLAUDE.md; окремої карти нема)
- scripts/mirror/ (санітизоване дзеркало — README у теці, у публічний репо не їде)

## Wiki domains
<!-- load-bearing domain notes in docs/wiki/ -->
- (none yet)

## Verification
- build: none — інтерпретований Python + vanilla JS
- test: `.venv/Scripts/python.exe -m pytest -q -m "not slow"` (⚠️ мігрує бойову БД — не під час запису)
- lint: none

## Learnings
- Consolidated: memory/learnings/CONSOLIDATED.md (run /vulyk-gc to refresh)
- Уроки власника/фідбек живуть окремо в auto-memory Claude Code (поза репо) — це feedback-шар, не карта коду
