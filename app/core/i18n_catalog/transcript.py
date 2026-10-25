"""Catalog of server texts: the transcript area (transcription.py, bookmarks.py)."""
MESSAGES: dict = {
    "transcript.bm.name_len": {
        "uk": "name 1-100 символів",
        "en": "name must be 1-100 characters",
    },
    "transcript.bm.note_string": {
        "uk": "note мусить бути string",
        "en": "note must be a string",
    },
    "transcript.bm.query_object": {
        "uk": "query мусить бути object",
        "en": "query must be an object",
    },
    "transcript.bm.search_exists": {
        "uk": "Search з таким ім'ям уже існує",
        "en": "A saved search with this name already exists",
    },
    "transcript.bm.seg_index_int": {
        "uk": "segment_index мусить бути int >= 0",
        "en": "segment_index must be an integer >= 0",
    },
    "transcript.bm.seg_index_range": {
        "uk": "segment_index виходить за межі",
        "en": "segment_index is out of range",
    },
    "transcript.err.already_in_lang": {
        "uk": "Транскрипт вже на мові '{lang}'",
        "en": "The transcript is already in '{lang}'",
    },
    "transcript.err.already_running": {
        "uk": "Транскрипція цього запису вже виконується",
        "en": "Transcription of this record is already running",
    },
    "transcript.err.api_key_env": {
        "uk": "ANTHROPIC_API_KEY не встановлено. Створіть .env у корені проекту з ключем.",
        "en": "ANTHROPIC_API_KEY is not set. Create a .env file in the project root with the key.",
    },
    "transcript.err.api_key_unset": {
        "uk": "ANTHROPIC_API_KEY не встановлено.",
        "en": "ANTHROPIC_API_KEY is not set.",
    },
    "transcript.err.audio_dl_not_number": {
        "uk": "audio_download_id мусить бути числом",
        "en": "audio_download_id must be a number",
    },
    "transcript.err.audio_dl_required": {
        "uk": "audio_download_id обов'язковий",
        "en": "audio_download_id is required",
    },
    "transcript.err.audio_extract_failed": {
        "uk": "Не вдалося витягти аудіо з відео",
        "en": "Could not extract audio from the video",
    },
    "transcript.err.audio_lib_not_found": {
        "uk": "Запис в Аудіотеці не знайдено",
        "en": "Audio library entry not found",
    },
    "transcript.err.audio_unavailable": {
        "uk": "Аудіо-файл недоступний",
        "en": "The audio file is unavailable",
    },
    "transcript.err.bulk_delete_failed": {
        "uk": "Помилка масового видалення",
        "en": "Bulk deletion failed",
    },
    "transcript.err.bulk_export_failed": {
        "uk": "Помилка масового експорту",
        "en": "Bulk export failed",
    },
    "transcript.err.claude_api": {
        "uk": "Помилка Claude API.",
        "en": "Claude API error.",
    },
    "transcript.err.claude_api_logs": {
        "uk": "Помилка Claude API. Перевірте логи.",
        "en": "Claude API error. Check the logs.",
    },
    "transcript.err.claude_empty": {
        "uk": "Claude повернув порожній текст",
        "en": "Claude returned empty text",
    },
    "transcript.err.db_error": {
        "uk": "Помилка бази даних",
        "en": "Database error",
    },
    "transcript.err.delete_failed": {
        "uk": "Помилка видалення транскрибування",
        "en": "Failed to delete the transcription",
    },
    "transcript.err.delete_ids_missing": {
        "uk": "Не вказано ID для видалення",
        "en": "No IDs given for deletion",
    },
    "transcript.err.docx_missing": {
        "uk": "python-docx не встановлений",
        "en": "python-docx is not installed",
    },
    "transcript.err.docx_missing_hint": {
        "uk": "python-docx не встановлений. .venv/Scripts/python.exe -m pip install python-docx",
        "en": "python-docx is not installed. .venv/Scripts/python.exe -m pip install python-docx",
    },
    "transcript.err.download_id_invalid": {
        "uk": "Невірний ID завантаження",
        "en": "Invalid download ID",
    },
    "transcript.err.download_unfinished": {
        "uk": "Завантаження ще не завершено",
        "en": "The download is not finished yet",
    },
    "transcript.err.export_format_unsupported": {
        "uk": "Непідтримуваний формат експорту",
        "en": "Unsupported export format",
    },
    "transcript.err.export_ids_missing": {
        "uk": "Не вказано ID для експорту",
        "en": "No IDs given for export",
    },
    "transcript.err.file_format_unsupported": {
        "uk": "Непідтримуваний формат файлу",
        "en": "Unsupported file format",
    },
    "transcript.err.file_missing": {
        "uk": "Файл відсутній: {path}",
        "en": "File is missing: {path}",
    },
    "transcript.err.file_not_found": {
        "uk": "Файл не знайдено",
        "en": "File not found",
    },
    "transcript.err.file_not_selected": {
        "uk": "Файл не вибрано",
        "en": "No file selected",
    },
    "transcript.err.filename_invalid": {
        "uk": "Некоректне ім'я файлу",
        "en": "Invalid file name",
    },
    "transcript.err.format_unsupported": {
        "uk": "Непідтримуваний формат",
        "en": "Unsupported format",
    },
    "transcript.err.frame_cut_failed": {
        "uk": "Кадр не вдалося вирізати",
        "en": "Could not cut the frame",
    },
    "transcript.err.frames_cut_failed": {
        "uk": "Кадри не вдалося вирізати",
        "en": "Could not cut the frames",
    },
    "transcript.err.ids_format_invalid": {
        "uk": "Невірний формат ID",
        "en": "Invalid ID format",
    },
    "transcript.err.ids_limit_numbers": {
        "uk": "transcription_ids/limit мають бути числами",
        "en": "transcription_ids/limit must be numbers",
    },
    "transcript.err.index_range": {
        "uk": "index виходить за межі",
        "en": "index is out of range",
    },
    "transcript.err.indices_list": {
        "uk": "indices мусить бути списком ≥2 елементів",
        "en": "indices must be a list of at least 2 items",
    },
    "transcript.err.indices_numbers": {
        "uk": "indices — лише числа",
        "en": "indices must be numbers only",
    },
    "transcript.err.indices_range": {
        "uk": "indices виходять за межі",
        "en": "indices are out of range",
    },
    "transcript.err.indices_sequential": {
        "uk": "indices мусять бути послідовні",
        "en": "indices must be consecutive",
    },
    "transcript.err.json_object_expected": {
        "uk": "Очікується JSON-обʼєкт",
        "en": "A JSON object is expected",
    },
    "transcript.err.keyframe_not_found": {
        "uk": "Кейфрейм не знайдено",
        "en": "Keyframe not found",
    },
    "transcript.err.method_invalid": {
        "uk": "method: thumbs або bisect",
        "en": "method: thumbs or bisect",
    },
    "transcript.err.no_segments": {
        "uk": "Сегментів немає",
        "en": "There are no segments",
    },
    "transcript.err.no_video_in_record": {
        "uk": "у запису #{tid} немає відео",
        "en": "record #{tid} has no video",
    },
    "transcript.err.not_found": {
        "uk": "Не знайдено",
        "en": "Not found",
    },
    "transcript.err.nothing_to_restore": {
        "uk": "Немає що відновлювати",
        "en": "Nothing to restore",
    },
    "transcript.err.numeric_params": {
        "uk": "числові параметри мають бути числами",
        "en": "numeric parameters must be numbers",
    },
    "transcript.err.params_numbers": {
        "uk": "параметри мають бути числами",
        "en": "parameters must be numbers",
    },
    "transcript.err.rec_disabled": {
        "uk": "Recording feature вимкнено",
        "en": "The recording feature is disabled",
    },
    "transcript.err.rec_mp3_missing": {
        "uk": "Final MP3 не створений",
        "en": "The final MP3 was not created",
    },
    "transcript.err.rec_not_finalized": {
        "uk": "Recording ще не finalized",
        "en": "The recording is not finalized yet",
    },
    "transcript.err.rec_session_not_found": {
        "uk": "Recording сесія не знайдена",
        "en": "Recording session not found",
    },
    "transcript.err.rec_session_required": {
        "uk": "recording_session_id обов'язковий",
        "en": "recording_session_id is required",
    },
    "transcript.err.recording_active": {
        "uk": "Іде запис — підказка назви буде після його завершення, щоб не зупиняти копілот дзвінка",
        "en": "A recording is in progress — the title suggestion will be available after it ends, so the call co-pilot is not interrupted",
    },
    "transcript.err.reportlab_missing": {
        "uk": "reportlab не встановлений",
        "en": "reportlab is not installed",
    },
    "transcript.err.restore_failed": {
        "uk": "Помилка відновлення транскрибування",
        "en": "Failed to restore the transcription",
    },
    "transcript.err.sentiment_needs_diarization": {
        "uk": "Sentiment per speaker потребує діаризованого транскрипту",
        "en": "Sentiment per speaker requires a diarized transcript",
    },
    "transcript.err.source_type_invalid": {
        "uk": "Невірний тип джерела",
        "en": "Invalid source type",
    },
    "transcript.err.split_args_invalid": {
        "uk": "index/split_at_seconds невірні",
        "en": "index/split_at_seconds are invalid",
    },
    "transcript.err.split_inside": {
        "uk": "split_at ({split_at}s) має бути всередині сегменту [{start}, {end}]",
        "en": "split_at ({split_at}s) must be inside the segment [{start}, {end}]",
    },
    "transcript.err.t_number": {
        "uk": "t має бути числом",
        "en": "t must be a number",
    },
    "transcript.err.t_required": {
        "uk": "потрібен t",
        "en": "t is required",
    },
    "transcript.err.target_lang_required": {
        "uk": "target_lang обов'язковий (e.g. 'en')",
        "en": "target_lang is required (e.g. 'en')",
    },
    "transcript.err.tfl_numbers": {
        "uk": "t/from/to/limit мають бути числами",
        "en": "t/from/to/limit must be numbers",
    },
    "transcript.err.title_or_description": {
        "uk": "Потрібне хоча б одне поле: title або description",
        "en": "At least one field is required: title or description",
    },
    "transcript.err.transcribe_failed": {
        "uk": "Помилка транскрибування аудіо",
        "en": "Audio transcription failed",
    },
    "transcript.err.tx_empty": {
        "uk": "Транскрипт порожній",
        "en": "The transcript is empty",
    },
    "transcript.err.no_media": {
        "uk": "У цього запису немає медіафайлу для повторного транскрибування",
        "en": "This record has no media file to transcribe again",
    },
    "transcript.err.model_not_installed": {
        "uk": "Модель Whisper не встановлена локально",
        "en": "The Whisper model is not installed locally",
    },
    "transcript.err.retranscribe_recording": {
        "uk": "Іде запис — повторне транскрибування буде доступне після його завершення",
        "en": "A recording is in progress — re-transcribing is available after it ends",
    },
    "transcript.err.retranscribe_running": {
        "uk": "Для цього запису вже йде повторне транскрибування",
        "en": "A re-transcription is already running for this record",
    },
    "transcript.err.queue_unavailable": {
        "uk": "Черга завдань недоступна",
        "en": "The job queue is unavailable",
    },
    "transcript.err.version_not_found": {
        "uk": "Версію транскрипту не знайдено",
        "en": "Transcript version not found",
    },
    "transcript.err.bad_language": {
        "uk": "language має бути рядком",
        "en": "language must be a string",
    },
    "transcript.err.tx_not_found": {
        "uk": "Транскрибування не знайдено",
        "en": "Transcription not found",
    },
    "transcript.err.tx_not_found2": {
        "uk": "Транскрипт не знайдено",
        "en": "Transcript not found",
    },
    "transcript.err.video_lookup_failed": {
        "uk": "Не вдалося знайти відео",
        "en": "Could not find the video",
    },
    "transcript.err.video_not_found": {
        "uk": "Відео не знайдено",
        "en": "Video not found",
    },
    "transcript.err.video_unavailable": {
        "uk": "Файл відео недоступний",
        "en": "The video file is unavailable",
    },
    "transcript.err.vision_backend": {
        "uk": "vision_backend: local або claude",
        "en": "vision_backend: local or claude",
    },
    "transcript.msg.deleted_n": {
        "uk": "Видалено {n} транскрибувань",
        "en": "Deleted {n} transcriptions",
    },
    "transcript.err.job_active": {
        "uk": "Для цього запису вже виконується задача",
        "en": "A job is already active for this record",
    },
    "transcript.err.eviction_not_approved": {
        "uk": "Видалення версії транскрипту не схвалено",
        "en": "Dropping a transcript version was not approved",
    },
    "transcript.err.transcribe_recording": {
        "uk": "Іде запис — фонове транскрибування буде доступне після його завершення",
        "en": "A recording is in progress — background transcription is available after it ends",
    },
    "transcript.err.path_not_allowed": {
        "uk": "Шлях поза дозволеними теками (RECALL_IMPORT_ROOTS)",
        "en": "The path is outside the allowed folders (RECALL_IMPORT_ROOTS)",
    },
    "transcript.err.path_not_found": {
        "uk": "Файл за цим шляхом не знайдено",
        "en": "No file at this path",
    },
    "transcript.err.file_too_large": {
        "uk": "Файл завеликий",
        "en": "The file is too large",
    },
    "transcript.err.youtube_url_invalid": {
        "uk": "Невірне посилання YouTube",
        "en": "Invalid YouTube link",
    },
    "transcript.err.youtube_download_failed": {
        "uk": "Не вдалося завантажити аудіо з YouTube",
        "en": "Could not download the audio from YouTube",
    },
    "transcript.err.async_source_invalid": {
        "uk": "source: library, recording, youtube або path",
        "en": "source: library, recording, youtube or path",
    },
    "transcript.err.async_ref_required": {
        "uk": "Для source={source} потрібне поле {field}",
        "en": "source={source} needs the field {field}",
    },
    "transcript.err.category_id_invalid": {
        "uk": "category_id мусить бути числом",
        "en": "category_id must be a number",
    },
    "transcript.err.ai_kind_invalid": {
        "uk": "kind: polish, summarize, sentiment, translate, topics або enrich",
        "en": "kind: polish, summarize, sentiment, translate, topics or enrich",
    },
    "transcript.err.ai_target_lang_invalid": {
        "uk": "target_lang: en, uk або ru",
        "en": "target_lang: en, uk or ru",
    },
    "transcript.err.ai_unknown_field": {
        "uk": "Дозволені лише поля kind і target_lang",
        "en": "Only the fields kind and target_lang are allowed",
    },
    "transcript.err.ai_enrich_recording": {
        "uk": "Іде запис — збагачення буде доступне після його завершення",
        "en": "A recording is in progress — enrichment is available after it ends",
    },
    "transcript.bm.note_too_long": {
        "uk": "note завдовгий (макс. {max} символів)",
        "en": "note is too long (max {max} characters)",
    },
}
