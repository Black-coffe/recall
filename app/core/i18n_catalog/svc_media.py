"""Server text catalog: media area (YouTube, document import, transcription, speakers)."""
MESSAGES: dict = {
    # --- YouTube errors (classified pytubefix failures) ---
    "svc_media.yt.err.default": {
        "uk": "Помилка завантаження відео",
        "en": "Video download failed",
    },
    "svc_media.yt.err.bot": {
        "uk": "YouTube заблокував запит як автоматичний (bot detection)",
        "en": "YouTube blocked the request as automated (bot detection)",
    },
    "svc_media.yt.err.auth": {
        "uk": "YouTube вимагає авторизації",
        "en": "YouTube requires sign-in",
    },
    "svc_media.yt.err.unavailable": {
        "uk": "Відео недоступне або видалене",
        "en": "The video is unavailable or has been removed",
    },
    "svc_media.yt.err.private": {"uk": "Приватне відео", "en": "Private video"},
    "svc_media.yt.err.copyright": {
        "uk": "Відео заблоковано через авторські права",
        "en": "The video is blocked due to copyright",
    },
    "svc_media.yt.err.no_file": {
        "uk": "Файл не знайдено після завантаження",
        "en": "File not found after the download",
    },
    # --- YouTube progress / process-log lines ---
    "svc_media.yt.log.trim_planned": {
        "uk": "Запланована обрізка: {start}s - {end}s",
        "en": "Trim planned: {start}s - {end}s",
    },
    "svc_media.yt.log.start": {
        "uk": "Початок завантаження відео з YouTube...",
        "en": "Starting the video download from YouTube...",
    },
    "svc_media.yt.log.fallback": {
        "uk": "YouTube не віддав потік напряму — пробуємо запасний завантажувач...",
        "en": "YouTube did not serve the stream directly - trying the fallback downloader...",
    },
    "svc_media.yt.log.convert": {"uk": "Конвертація в MP3...", "en": "Converting to MP3..."},
    "svc_media.yt.log.done_size": {
        "uk": "Завантаження завершено. Розмір: {size} MB",
        "en": "Download complete. Size: {size} MB",
    },
    "svc_media.yt.log.trim_start": {
        "uk": "Початок обрізки аудіо...",
        "en": "Starting the audio trim...",
    },
    "svc_media.yt.log.trim_ok": {
        "uk": "Обрізка завершена успішно",
        "en": "Trim completed successfully",
    },
    "svc_media.yt.log.trim_fail": {
        "uk": "Обрізка не вдалась, використовується повний файл",
        "en": "Trim failed, using the full file",
    },
    "svc_media.yt.log.complete": {
        "uk": "Процес завершено успішно",
        "en": "Process completed successfully",
    },
    # --- Document parser errors (shown through the documents blueprint) ---
    "svc_media.doc.err.ocr_py_unavailable": {
        "uk": "pytesseract/Pillow недоступні: {error}",
        "en": "pytesseract/Pillow are unavailable: {error}",
    },
    "svc_media.doc.err.tesseract_missing": {
        "uk": "системний Tesseract не знайдено. Встановіть Tesseract OCR "
        "(+ мовні пакети ukr/rus/eng) і додайте його у PATH.",
        "en": "system Tesseract not found. Install Tesseract OCR "
        "(+ ukr/rus/eng language packs) and add it to PATH.",
    },
    "svc_media.doc.err.ocr_unavailable": {"uk": "OCR недоступний", "en": "OCR is unavailable"},
    "svc_media.doc.err.docx_missing": {
        "uk": "python-docx недоступний: {error}",
        "en": "python-docx is unavailable: {error}",
    },
    "svc_media.doc.err.pdf_missing": {
        "uk": "PyMuPDF не встановлено — PDF-парсинг недоступний. Встановіть: pip install PyMuPDF",
        "en": "PyMuPDF is not installed - PDF parsing is unavailable. Install: pip install PyMuPDF",
    },
    "svc_media.doc.err.pptx_missing": {
        "uk": "python-pptx не встановлено — PPTX-парсинг недоступний. Встановіть: pip install python-pptx",
        "en": "python-pptx is not installed - PPTX parsing is unavailable. Install: pip install python-pptx",
    },
    "svc_media.doc.err.xlsx_missing": {
        "uk": "openpyxl не встановлено — XLSX-парсинг недоступний. Встановіть: pip install openpyxl",
        "en": "openpyxl is not installed - XLSX parsing is unavailable. Install: pip install openpyxl",
    },
    "svc_media.doc.err.image_ocr_off": {
        "uk": "OCR недоступний — розпізнавання зображень вимкнено. {reason}",
        "en": "OCR is unavailable - image recognition is disabled. {reason}",
    },
    "svc_media.doc.err.pillow_missing": {
        "uk": "Pillow недоступний: {error}",
        "en": "Pillow is unavailable: {error}",
    },
    "svc_media.doc.err.image_open": {
        "uk": "Не вдалося відкрити зображення: {error}",
        "en": "Could not open the image: {error}",
    },
    "svc_media.doc.err.unsupported_format": {
        "uk": "Непідтримуваний формат документа: '.{ext}'",
        "en": "Unsupported document format: '.{ext}'",
    },
    "svc_media.doc.err.pdf_no_text_layer": {
        "uk": "У PDF не знайдено текстового шару (схоже на скан), а OCR недоступний. "
        "Встановіть Tesseract OCR (+ мовні пакети ukr/rus/eng) у PATH.",
        "en": "No text layer was found in the PDF (it looks like a scan) and OCR is unavailable. "
        "Install Tesseract OCR (+ ukr/rus/eng language packs) in PATH.",
    },
    "svc_media.doc.err.ocr_no_text": {
        "uk": "OCR не знайшов тексту на зображенні.",
        "en": "OCR found no text in the image.",
    },
    "svc_media.doc.err.empty": {
        "uk": "Документ порожній або не містить тексту",
        "en": "The document is empty or contains no text",
    },
    # --- Transcription process-log lines (polled by the UI) ---
    "svc_media.tx.log.start": {
        "uk": "Начало транскрибации аудио...",
        "en": "Starting the audio transcription...",
    },
    "svc_media.tx.log.processing": {"uk": "Обработка...", "en": "Processing..."},
    "svc_media.tx.log.failed": {"uk": "Помилка: {error}", "en": "Error: {error}"},
    "svc_media.tx.log.done": {"uk": "Транскрибация завершена", "en": "Transcription finished"},
    "svc_media.tx.log.complete": {
        "uk": "Процесс завершен успешно!",
        "en": "Process completed successfully!",
    },
    # --- transcript export files (txt/md/docx/pdf, single and bulk) ---
    "svc_media.export.source": {"uk": "Джерело", "en": "Source"},
    "svc_media.export.date": {"uk": "Дата", "en": "Date"},
    "svc_media.export.language": {"uk": "Мова", "en": "Language"},
    "svc_media.export.model": {"uk": "Модель", "en": "Model"},
    "svc_media.export.speakers": {"uk": "Спікери", "en": "Speakers"},
    "svc_media.export.transcript": {"uk": "Транскрипт", "en": "Transcript"},
    "svc_media.export.you": {"uk": "Ви", "en": "You"},
    "svc_media.export.speaker_n": {"uk": "Спікер {n}", "en": "Speaker {n}"},
    "svc_media.export.comments.heading": {"uk": "Коментарі власника", "en": "Owner's comments"},
    "svc_media.export.comments.note": {
        "uk": "Написані про запис уже після нього. При суперечності з транскриптом "
              "нижче правильні саме вони.",
        "en": "Written about the recording after it took place. Where they contradict "
              "the transcript below, they are the ones that are right.",
    },
    "svc_media.export.comments.pinned": {"uk": "закріплено", "en": "pinned"},
    "svc_media.export.kind.correction": {"uk": "ВИПРАВЛЕННЯ", "en": "CORRECTION"},
    "svc_media.export.kind.decision": {"uk": "РІШЕННЯ", "en": "DECISION"},
    "svc_media.export.kind.note": {"uk": "НОТАТКА", "en": "NOTE"},
    "svc_media.export.kind.context": {"uk": "КОНТЕКСТ", "en": "CONTEXT"},
    "svc_media.export.kind.question": {"uk": "ПИТАННЯ", "en": "QUESTION"},
}
