---
domain: ui-language
tags: [i18n, uk, en, telegram]
related: [scheduled-pipes-delivery.md]
last-verified: 2026-10-15
---

# UI language (uk/en)

- One setting, `RECALL_UI_LANG` = `auto|uk|en` (default `auto`, `app/core/settings.py:77`), stored in `.env` via
  `POST /api/settings/ui-lang` and effective without restart (`_set_env_var` also sets `os.environ`;
  `app/core/i18n.py:32` reads it per call). An explicit `uk`/`en` beats `auto`.
- Browser resolves `auto` itself (`navigator.languages[0]` starting with `uk` -> uk, else en; `static/js/recall/i18n.js:13`)
  and sends the resolved language as `X-Recall-Lang` on every API call (`api.js`; direct `fetch` must add `R.i18n.headers()`).
- Server `request_lang()`: header -> explicit setting -> `uk`. The server has no browser language, so header-less
  requests (downloads, tests) with `auto` are Ukrainian; the MCP proxy sends `X-Recall-Lang: en`.
- Telegram (brief text, buttons, callback replies) never sees a request: it uses `server_lang()` (`en` only when the
  setting is explicitly `en`). Language is read at render/press time, not frozen at import (`brief_actions.FOOTER` is a lazy attribute).
- Not translated by design: archive content (summaries, tasks, Claude's brief headline, direction/entity names,
  transcripts), LLM prompts, transcription language values, exports and CLI output. Logs and internal exception texts
  are English (`.claude/rules/code-language.md`); user-facing service texts (YouTube, local model, errors, bot) live in
  the `svc_*` catalog modules and follow the request language. The MCP process runs with `RECALL_UI_LANG=en` and sends
  `X-Recall-Lang: en` to the app, so the whole MCP surface is English.
- Every UI string has a key in both languages with identical `{vars}` and plural forms: server catalog
  `app/core/i18n_catalog/<area>.py`, client `static/js/recall/i18n/<area>.js`. Missing key returns the key (never throws).
  Guards: `tests/test_i18n.py`, `tests/test_i18n_js.py`, `tests/test_i18n_coverage.py` (no Cyrillic literals outside
  dictionaries; allowlist in the test).
- `tests/conftest.py` forces `RECALL_UI_LANG=auto` so the owner's `.env` cannot leak into tests.
- Known gap: `data-i18n-content` on the `<meta description>` in `templates/shell.html:7` is not applied by
  `i18n.apply()`; the description stays Ukrainian.
