"""Server text catalog: the svc_recording area (recorder service errors, call prompts)."""
MESSAGES: dict = {
    # --- recording service errors ---
    "svc_recording.err.no_stream": {
        "uk": "Потрібен хоча б один потік (mic_device_index або system_device_index)",
        "en": "At least one stream is required (mic_device_index or system_device_index)",
    },
    "svc_recording.err.limit": {
        "uk": "Ліміт одночасних записів ({limit}) вичерпано: {active}",
        "en": "The limit of simultaneous recordings ({limit}) is reached: {active}",
    },
    "svc_recording.err.open_stream": {
        "uk": "Не вдалось відкрити потік: {error}",
        "en": "Failed to open the stream: {error}",
    },
    "svc_recording.err.start_stream": {
        "uk": "Не вдалось запустити потік: {error}",
        "en": "Failed to start the stream: {error}",
    },
    "svc_recording.err.all_needs_device": {
        "uk": "Системний звук 'all' потребує system_device_index",
        "en": "System audio 'all' requires system_device_index",
    },
    "svc_recording.err.no_system_stream": {
        "uk": "Сесія {session_id} стартувала без системного звуку",
        "en": "Session {session_id} started without system audio",
    },
    "svc_recording.err.open_new_source": {
        "uk": "Не вдалось відкрити нове джерело: {error}",
        "en": "Failed to open the new source: {error}",
    },
    "svc_recording.err.no_active_session": {
        "uk": "Немає активної сесії {session_id}",
        "en": "No active session {session_id}",
    },
    # --- call prompts (system notifications) ---
    "svc_recording.call.prompt.title": {
        "uk": "Recall · дзвінок у {label}",
        "en": "Recall · call in {label}",
    },
    "svc_recording.call.prompt.body": {
        "uk": "Записати? Мікрофон і звук лише з {label}.",
        "en": "Record? Microphone and sound from {label} only.",
    },
    "svc_recording.call.prompt.record": {"uk": "Записати", "en": "Record"},
    "svc_recording.call.prompt.sources": {"uk": "Інші джерела", "en": "Other sources"},
    "svc_recording.call.ended.title": {
        "uk": "Recall · дзвінок завершився",
        "en": "Recall · the call has ended",
    },
    "svc_recording.call.ended.body": {
        "uk": "Зупинити запис? Дзвінок у {label} закінчився. Якщо {minutes} хв буде тиша — зупиню сам.",
        "en": "Stop recording? The call in {label} has ended. If there are {minutes} min of silence, I will stop it myself.",
    },
    "svc_recording.call.ended.stop": {"uk": "Зупинити", "en": "Stop"},
    "svc_recording.call.ended.keep": {"uk": "Продовжити", "en": "Continue"},
    "svc_recording.call.autostopped.title": {
        "uk": "Recall · запис зупинено",
        "en": "Recall · recording stopped",
    },
    "svc_recording.call.autostopped.body": {
        "uk": "{minutes} хв тиші після дзвінка. Запис збережено повністю.",
        "en": "{minutes} min of silence after the call. The recording was saved in full.",
    },
    # --- live_ask: reasons returned to the MCP tool ask_live ---
    "svc_recording.live_ask.empty_call": {
        "uk": "живий транскрипт дзвінка недоступний (немає активної сесії "
              "запису або live-транскрипція ще не накопичила текст)",
        "en": "the live call transcript is unavailable (no active recording "
              "session, or live transcription has not accumulated any text yet)",
    },
    "svc_recording.live_ask.empty_archive": {
        "uk": "в архіві не знайдено релевантних фрагментів за цим питанням",
        "en": "no relevant archive fragments were found for this question",
    },
    "svc_recording.live_ask.empty_both": {
        "uk": "немає ні живого транскрипту, ні релевантних фрагментів архіву",
        "en": "there is neither a live transcript nor relevant archive fragments",
    },
    "svc_recording.live_ask.llm_failed": {
        "uk": "локальна модель не відповіла: {error}",
        "en": "the local model did not answer: {error}",
    },
}
