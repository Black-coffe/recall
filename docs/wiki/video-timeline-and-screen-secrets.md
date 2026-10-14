---
domain: video
tags: [video, ocr, secrets, mcp]
related: [rag-context-integrity.md, recording-sources-and-sessions.md]
last-verified: 2026-10-14
---

# Візуальна стрічка відео і секрети з екрана

Відео є лише в записах екрана Recall (`audio_downloads.has_video=1` -> `recording_video_tracks`); `video_analysis.resolve_recording_video` - єдине джерело правди, нема відео -> `NO_VIDEO`.

- **Зображення кадрів не зберігаються.** `video_keyframes.image_path` = NULL для нових рядків (лишилось від Phase 23; перерахунок видаляє старі jpg). Кадр ріжеться з відео на запит (`video_frames.grab_frame`, `video_timeline.render_frame`), тож `/frame` і `/frames?t=` працюють без аналізу; `/keyframe/<id>` теж ріже на льоту, але потребує рядка.
- **Час.** `video_keyframes.ts_offset_sec/end_sec` - час стрічки (відео + `start_offset_sec` доріжки); `video_time_sec` - час представника в самому відео. Кадр за `t` = `t - start_offset_sec`.
- **Кеш = параметри.** `transcriptions.video_analysis_params` (JSON: `algo`=`video_frames.ALGO_VERSION`, method, step, min_res, threshold, settle, ignore_regions, max_frames, ocr, describe, vision_backend). Збіг без `force` -> готовий результат, без декодування. Інші параметри або версія -> повний перерахунок (DELETE keyframes + чанків `speaker='екран'`).
- **Секрети маскуються ДО будь-якого запису чи опису.** `screen_secrets.scan_all` по словах OCR; у `ocr_text`, `vision_text`, `ocr_boxes_json` і чанках RAG (`speaker='екран'`, секція «кадр ГГ:ХХ:СС») лишається `[🔒 секрет]`. Сирі значення - лише таблиця `screen_secrets` (v49), її не читає жоден HTTP-ендпоінт чи MCP-тулза. Єдиний шлях до сирого: `python -m app.services.screen_secrets reveal <id>` з TTY + підтвердження, у файл.
- **Картинка назовні - тільки із зафарбованими рамками** (`screen_secrets.redact`). Кадр, що не є представником відрізка, OCR-иться на льоту (`_live_secret_boxes`); якщо OCR недоступний - кадр віддається повністю чорним. Опис vision-моделлю (`describe=True`, дефолт вимкнено) теж іде з уже зафарбованого кадру.
- **Назовні через MCP - ще й фільтр приватного** (`app/services/privacy.py`, 4.11.0; UI Recall бачить усе). `RECALL_MCP_VISUAL`: `images` (дефолт) | `text` (`get_frame` відмовляє) | `off` (усі 5 відео-тулз відмовляють, чанки `speaker='екран'` зникають із `search_archive`/`grep_archive`/`ask_archive`); невідоме значення = `off`. Запис у напрямку з `RECALL_PRIVATE_CATEGORIES` (дефолт `Юридичне,Особисте`; назва на будь-якому рівні дерева + нащадки, тезки ховаються всі) -> `PRIVATE`. Слово з `private.txt` (підрядок `casefold`, без морфології) ховає відрізок/кадр/хіт/чанк, лічильник `private_hidden`. Fail closed: збій перевірки або нечитабельний файл -> ховати. Кадр без OCR (`frame_text=None`) через MCP не віддається. Той самий стоп-лист бере бот K4.
- **Чанки кадрів:** повтор за змістом (`duplicate_of`, dHash <= 4 біти, лише несусідні) у RAG не дублюється. Типи `camera_only`/`blank` приховані зі стрічки за замовчуванням (`include_camera`).
- **OCR-середовище:** `document_parser._configure_tesseract` (`OCR_TESSERACT_CMD`, `OCR_TESSDATA_DIR`); відео-OCR на час виклику ставить `OMP_THREAD_LIMIT=1` і `OCR_TESSDATA_FAST_DIR` (`video_analysis._VideoOcrEnv`).

Див. `docs/video-timeline.md`, [rag-context-integrity](rag-context-integrity.md) (чанки `екран` у пошуку), [recording-sources-and-sessions](recording-sources-and-sessions.md).

Рішення: ADR-030 (секрети), ADR-033 (фільтр приватного на виході MCP, один стоп-лист), ADR-034 (кадр перевіряється за власним текстом).
