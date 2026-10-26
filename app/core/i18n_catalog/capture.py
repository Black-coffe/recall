"""Catalog of server texts: the capture area (recording, youtube, copilot blueprints)."""
MESSAGES: dict = {
    # --- youtube ---
    "capture.yt.err.no_url": {"uk": "URL не вказано", "en": "URL not provided"},
    "capture.yt.err.bad_url": {"uk": "Невірний YouTube URL", "en": "Invalid YouTube URL"},
    "capture.yt.err.json_object": {"uk": "Очікується JSON-обʼєкт", "en": "A JSON object is expected"},
    "capture.yt.err.info": {
        "uk": "Помилка отримання інформації про відео",
        "en": "Failed to get video information",
    },
    "capture.yt.err.no_ffmpeg": {"uk": "FFmpeg не встановлено.", "en": "FFmpeg is not installed."},
    "capture.yt.err.start": {
        "uk": "Помилка запуску завантаження",
        "en": "Failed to start the download",
    },
    "capture.yt.unknown_video": {"uk": "Невідоме відео", "en": "Unknown video"},
    "capture.yt.unknown_author": {"uk": "Невідомий автор", "en": "Unknown author"},
    # --- copilot ---
    "capture.cp.err.disabled": {
        "uk": "Ко-пілот вимкнено (COPILOT_ENABLED=False)",
        "en": "Co-pilot is disabled (COPILOT_ENABLED=False)",
    },
    "capture.cp.err.busy": {
        "uk": "Ко-пілот уже працює на іншому записі",
        "en": "Co-pilot is already running on another recording",
    },
    "capture.cp.err.empty_question": {"uk": "Порожнє питання", "en": "Empty question"},
    "capture.cp.err.top_k_number": {"uk": "top_k має бути числом", "en": "top_k must be a number"},
    "capture.cp.err.top_k_finite": {
        "uk": "top_k має бути скінченним числом",
        "en": "top_k must be a finite number",
    },
    "capture.cp.err.top_k_positive": {"uk": "top_k має бути додатним", "en": "top_k must be positive"},
    "capture.cp.err.ambiguous": {
        "uk": "Активних записів кілька — вкажіть session_id",
        "en": "Several recordings are active — specify session_id",
    },
    "capture.cp.err.move_ids": {
        "uk": "Потрібні from_session_id і to_session_id",
        "en": "from_session_id and to_session_id are required",
    },
    "capture.cp.err.move_inactive": {
        "uk": "Запис не знайдено або він не активний",
        "en": "Recording not found or not active",
    },
    "capture.cp.err.move_not_running": {
        "uk": "На цьому записі ко-пілот не працює",
        "en": "Co-pilot is not running on this recording",
    },
    "capture.cp.err.move_failed": {
        "uk": "Не вдалося запустити ко-пілот на новому записі",
        "en": "Could not start the co-pilot on the new recording",
    },
    "capture.cp.err.session_not_found": {"uk": "Сесію не знайдено", "en": "Session not found"},
    "capture.cp.err.no_hints": {
        "uk": "Нема підказок для збереження",
        "en": "No hints to save",
    },
    "capture.cp.err.no_fields": {"uk": "Нема валідних полів", "en": "No valid fields"},
    "capture.cp.err.unknown_action": {"uk": "Невідома дія", "en": "Unknown action"},
    # --- recording ---
    "capture.rec.err.disabled": {
        "uk": "Recording feature вимкнено (RECORDING_ENABLED=False або PyAudioWPatch не встановлено)",
        "en": "Recording feature is disabled (RECORDING_ENABLED=False or PyAudioWPatch is not installed)",
    },
    "capture.rec.err.need_source": {
        "uk": "Потрібен хоча б один з mic_device_index/system_device_index",
        "en": "At least one of mic_device_index/system_device_index is required",
    },
    "capture.rec.err.prompt_gone": {"uk": "Підказки вже немає", "en": "The prompt is gone"},
    "capture.rec.err.session_not_found": {"uk": "Сесія не знайдена", "en": "Session not found"},
    "capture.rec.err.kept": {
        "uk": "Запис уже збережено й транскрибовано — скасувати його не можна. Видалити можна в Бібліотеці.",
        "en": "The recording is already saved and transcribed, so it cannot be cancelled. Delete it in the Library.",
    },
    "capture.rec.err.crashed": {
        "uk": "Сесія крашнулась — фінальний файл недоступний",
        "en": "The session crashed — the final file is unavailable",
    },
    "capture.rec.err.finalize_slow": {
        "uk": "Finalize triває довше очікуваного. Спробуйте /save пізніше.",
        "en": "Finalize is taking longer than expected. Try /save later.",
    },
    "capture.rec.err.no_final": {
        "uk": "Final MP3 не створений (можливо порожній запис)",
        "en": "Final MP3 was not created (the recording may be empty)",
    },
    "capture.rec.err.invalid_reason": {
        "uk": "reason має бути одним із {reasons}",
        "en": "reason must be one of {reasons}",
    },
    "capture.rec.err.sse": {"uk": "SSE broker недоступний", "en": "SSE broker is unavailable"},
    "capture.rec.stop.message": {
        "uk": 'Finalize пушено у JobQueue. Слідкуйте за status="finalized" через /state або SSE.',
        "en": 'Finalize was pushed to JobQueue. Watch for status="finalized" via /state or SSE.',
    },
    "capture.rec.live.disabled": {
        "uk": "live-транскрипція вимкнена в цьому інстансі",
        "en": "live transcription is disabled in this instance",
    },
    "capture.rec.live.finished": {
        "uk": "сесія завершена — повний текст доступний через транскрипт запису",
        "en": "the session has ended — the full text is available through the recording transcript",
    },
    "capture.live_video.err.not_found": {"uk": "Сесія запису не знайдена", "en": "Recording session not found"},
    "capture.live_video.err.no_video": {
        "uk": "Ця сесія не записує відео екрана",
        "en": "This session records no screen video",
    },
    "capture.live_video.err.invalid_argument": {
        "uk": "Невірний параметр запиту кадрів",
        "en": "Invalid frame request parameter",
    },
    "capture.live_video.err.past_edge": {
        "uk": "Цей момент ще не записано на диск: дивіться readable_until",
        "en": "This moment is not on disk yet: see readable_until",
    },
    "capture.live_video.err.not_recorded": {
        "uk": "У цей момент відео не записувалося (пауза): дивіться readable_ranges",
        "en": "No video was recorded at this moment (a pause): see readable_ranges",
    },
    "capture.live_video.finished": {
        "uk": "сесія завершена — кадри доступні через get_frame / get_visual_timeline за транскрипцією запису",
        "en": "the session has ended — frames are available through get_frame / get_visual_timeline on the recording's transcription",
    },
    "capture.live_video.not_active": {
        "uk": "сесія не записується зараз — живі кадри недоступні",
        "en": "the session is not recording now — live frames are not available",
    },
    "capture.rec.live.no_preview": {
        "uk": "live-прев'ю для цієї сесії недоступне",
        "en": "live preview is not available for this session",
    },
    "capture.rec.page.link_expired": {
        "uk": "Посилання вже не дійсне",
        "en": "This link is no longer valid",
    },
    "capture.rec.page.link_expired_text": {
        "uk": "Цю підказку вже використано, або дзвінок закінчився.",
        "en": "This prompt has already been used, or the call has ended.",
    },
    "capture.rec.page.already_stopped": {"uk": "Запис уже зупинено", "en": "Recording already stopped"},
    "capture.rec.page.nothing_to_do": {"uk": "Нічого робити не треба.", "en": "Nothing to do."},
    "capture.rec.page.stop_failed": {
        "uk": "Не вдалося зупинити запис",
        "en": "Could not stop the recording",
    },
    "capture.rec.page.see_log": {
        "uk": "Подробиці — у журналі Recall.",
        "en": "See the Recall log for details.",
    },
    "capture.rec.page.not_started": {"uk": "Запис не почато", "en": "Recording not started"},
    "capture.rec.page.limit": {
        "uk": "Уже йде максимум записів ({limit}). Нічого не зупинено.",
        "en": "The maximum number of recordings ({limit}) is already running. Nothing was stopped.",
    },
    "capture.rec.page.open": {"uk": "Відкрити запис у Recall", "en": "Open recording in Recall"},
    "capture.rec.page.agent_refused": {
        "uk": "Ця кнопка лише для власника",
        "en": "This button is for the owner only",
    },
    "capture.rec.page.agent_refused_text": {
        "uk": "Агент не може відповідати на підказку дзвінка. Запис запускайте й зупиняйте інструментами агента.",
        "en": "An agent cannot answer a call prompt. Start and stop recordings with the agent tools.",
    },
    # --- mcp-recording-live: agent recordings ---
    "capture.err.owner_session": {
        "uk": "Це ваш запис: агент може лише зупинити його, не ставити на паузу й не міняти джерело.",
        "en": "This is the owner's recording: an agent may only stop it, not pause it or change its source.",
    },
    "capture.err.notify_unavailable": {
        "uk": "Не вдалося показати сповіщення про старт запису — запис агента не почато.",
        "en": "The recording-start notification could not be shown, so the agent's recording was not started.",
    },
    "capture.err.cancel_window_closed": {
        "uk": "Скасувати можна лише власний живий запис агента, молодший за 5 хвилин. Зупиніть його натомість.",
        "en": "Only the agent's own live recording younger than 5 minutes can be cancelled. Stop it instead.",
    },
    "capture.rec.notice.title": {
        "uk": "Recall: агент починає запис",
        "en": "Recall: an agent starts a recording",
    },
    "capture.rec.notice.body": {
        "uk": "Мікрофон: {mic}. Звук: {apps}. Мова: {language}. Назва: {name}. Називає себе {label}",
        "en": "Microphone: {mic}. Sound: {apps}. Language: {language}. Name: {name}. Claims to be {label}",
    },
    "capture.rec.notice.all_system": {"uk": "увесь системний звук", "en": "all system audio"},
    "capture.rec.notice.no_system": {"uk": "лише мікрофон", "en": "microphone only"},
    "capture.rec.notice.with_screen": {"uk": "з екраном", "en": "with screen"},
    "capture.rec.notice.no_name": {"uk": "без назви", "en": "no name"},
}
