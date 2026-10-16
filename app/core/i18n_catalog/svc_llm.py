"""Server text catalog: the svc_llm area (local model client, copilot, polishing)."""
MESSAGES: dict = {
    # --- local_llm (Ollama) ---
    "svc_llm.local.unavailable": {
        "uk": "Ollama недоступний на {url} ({reason}). "
              "Запустіть `ollama serve` (зазвичай стартує сам після інсталяції).",
        "en": "Ollama is unavailable at {url} ({reason}). "
              "Run `ollama serve` (it usually starts by itself after installation).",
    },
    "svc_llm.local.error": {"uk": "Ollama помилка: {error}", "en": "Ollama error: {error}"},
    "svc_llm.local.no_models": {
        "uk": "Ollama працює, але моделей нема. Виконайте: ollama pull {model}",
        "en": "Ollama is running, but there are no models. Run: ollama pull {model}",
    },
    "svc_llm.local.ok": {"uk": "OK ({model})", "en": "OK ({model})"},
    "svc_llm.local.ok_substitute": {
        "uk": "OK ({effective} — заміна для {model})",
        "en": "OK ({effective} — substitute for {model})",
    },
    "svc_llm.local.model_missing": {
        "uk": "Модель '{model}' не знайдена. Доступні: {available}. Виконайте: ollama pull {model}",
        "en": "Model '{model}' not found. Available: {available}. Run: ollama pull {model}",
    },
    "svc_llm.local.model_not_in_ollama": {
        "uk": "Модель '{model}' не знайдена в Ollama. Доступні: {available}. Виконайте: ollama pull {model}",
        "en": "Model '{model}' not found in Ollama. Available: {available}. Run: ollama pull {model}",
    },
    "svc_llm.local.unreachable": {
        "uk": "Ollama недоступний: {reason}",
        "en": "Ollama is unavailable: {reason}",
    },
    "svc_llm.local.bad_json": {
        "uk": "Модель не повернула валідний JSON після {attempts} спроб: {error}",
        "en": "The model did not return valid JSON after {attempts} attempts: {error}",
    },
    # --- text_polishing (Claude) ---
    "svc_llm.polish.no_key": {
        "uk": "ANTHROPIC_API_KEY не встановлено. Створіть .env у корені проекту "
              "з рядком ANTHROPIC_API_KEY=sk-ant-... та перезапустіть сервер.",
        "en": "ANTHROPIC_API_KEY is not set. Create a .env in the project root "
              "with the line ANTHROPIC_API_KEY=sk-ant-... and restart the server.",
    },
    "svc_llm.polish.bad_json": {
        "uk": "Claude повернув некоректний JSON. Спробуйте ще раз.",
        "en": "Claude returned invalid JSON. Please try again.",
    },
    # --- copilot session export (.md / .json) ---
    "svc_llm.cp_export.kind.contradiction": {"uk": "ПРОТИРІЧЧЯ", "en": "CONTRADICTION"},
    "svc_llm.cp_export.kind.question": {"uk": "ПИТАННЯ", "en": "QUESTION"},
    "svc_llm.cp_export.kind.clarification": {"uk": "УТОЧНЕННЯ", "en": "CLARIFICATION"},
    "svc_llm.cp_export.kind.fact": {"uk": "ФАКТ", "en": "FACT"},
    "svc_llm.cp_export.mode.light": {"uk": "лайт", "en": "light"},
    "svc_llm.cp_export.mode.medium": {"uk": "середній", "en": "medium"},
    "svc_llm.cp_export.mode.hard": {"uk": "жорсткий", "en": "hard"},
    "svc_llm.cp_export.imp.low": {"uk": "низька", "en": "low"},
    "svc_llm.cp_export.imp.medium": {"uk": "середня", "en": "medium"},
    "svc_llm.cp_export.imp.high": {"uk": "висока", "en": "high"},
    "svc_llm.cp_export.you": {"uk": "Ви", "en": "You"},
    "svc_llm.cp_export.other": {"uk": "Інший", "en": "Other"},
    "svc_llm.cp_export.speaker_n": {"uk": "Спікер {n}", "en": "Speaker {n}"},
    "svc_llm.cp_export.meta": {
        "uk": "*Режим: {mode} · важливість: {imp} · підказок: {notes} "
              "(перевірено {verified}) · вартість: ${cost}*",
        "en": "*Mode: {mode} · importance: {imp} · hints: {notes} "
              "(verified {verified}) · cost: ${cost}*",
    },
    "svc_llm.cp_export.comment": {
        "uk": "✍ **{label}** (оператор{pin}): {body}",
        "en": "✍ **{label}** (operator{pin}): {body}",
    },
    "svc_llm.cp_export.pinned": {"uk": " · закріплено", "en": " · pinned"},
    "svc_llm.cp_export.session_n": {"uk": "Сесія #{id}", "en": "Session #{id}"},
    "svc_llm.cp_export.title": {"uk": "Ко-пілот сесії — {name}", "en": "Session co-pilot — {name}"},
    "svc_llm.cp_export.topics": {"uk": "Теми", "en": "Topics"},
    "svc_llm.cp_export.topic_n": {"uk": "Тема {n}", "en": "Topic {n}"},
    "svc_llm.cp_export.dialog": {
        "uk": "Діалог, нотатки ко-пілота і коментарі оператора",
        "en": "Dialogue, co-pilot notes and operator comments",
    },
    "svc_llm.cp_export.comments": {"uk": "Коментарі оператора", "en": "Operator comments"},
    "svc_llm.cp_export.summary": {"uk": "Зведення підказок", "en": "Hint summary"},
    "svc_llm.cp_export.badge.verified": {"uk": "перевірено", "en": "verified"},
    "svc_llm.cp_export.badge.local": {"uk": "локально", "en": "local"},
    "svc_llm.cp_export.badge.refuted": {"uk": "спростовано", "en": "refuted"},
    "svc_llm.cp_export.sources": {"uk": " — джерела: {sources}", "en": " — sources: {sources}"},
}
