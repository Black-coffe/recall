"""Catalog: server texts of the Library, the Audio library and Documents (audio_library, documents)."""


def _m(uk: str, en: str) -> dict:
    return {"uk": uk, "en": en}


MESSAGES: dict = {
    # documents.py
    "library.doc.err.no_file": _m("Файл не знайдено", "File not found"),
    "library.doc.err.no_file_selected": _m("Файл не вибрано", "No file selected"),
    "library.doc.err.unsupported": _m(
        "Непідтримуваний формат. Дозволені: {supported}",
        "Unsupported format. Allowed: {supported}"),
    "library.doc.err.bad_name": _m("Некоректне ім'я файлу", "Invalid file name"),
    "library.doc.err.save_failed": _m("Не вдалося зберегти файл", "Could not save the file"),
    "library.doc.err.parse_failed": _m("Помилка розбору документа", "Document parsing failed"),
    "library.doc.err.duplicate": _m("Такий документ уже є в архіві", "This document is already in the archive"),
    "library.doc.err.not_found": _m("Документ не знайдено", "Document not found"),
    "library.doc.err.no_original": _m(
        "Оригінал файлу відсутній — переразбір неможливий",
        "The original file is missing — re-parsing is not possible"),
    "library.doc.err.no_path": _m("Не вказано шлях до папки", "Folder path not provided"),
    "library.doc.err.import_disabled": _m(
        "Import-folder вимкнено: задайте RECALL_IMPORT_ROOTS у .env",
        "Import-folder is disabled: set RECALL_IMPORT_ROOTS in .env"),
    "library.doc.err.import_forbidden": _m(
        "Ця папка недоступна для імпорту (поза дозволеними директоріями)",
        "This folder is not available for import (outside the allowed directories)"),
    "library.doc.err.folder_missing": _m("Папку не знайдено", "Folder not found"),
    "library.doc.err.folder_empty": _m(
        "У папці немає підтримуваних документів", "The folder has no supported documents"),
    "library.doc.err.queue_unavailable": _m("Черга завдань недоступна", "The job queue is unavailable"),
    "library.doc.err.bad_paths": _m(
        "Очікується непорожній список шляхів до файлів", "A non-empty list of file paths is expected"),
    "library.doc.err.bulk_limit": _m(
        "Забагато файлів за раз: не більше {max}", "Too many files at once: at most {max}"),
    "library.doc.err.path_not_allowed": _m(
        "Файл поза дозволеними директоріями (RECALL_IMPORT_ROOTS)",
        "The file is outside the allowed directories (RECALL_IMPORT_ROOTS)"),
    "library.doc.err.file_too_large": _m("Файл завеликий", "The file is too large"),
    # audio_library.py
    "library.audio.err.no_url": _m("URL не вказано", "URL not provided"),
    "library.audio.err.bad_url": _m("Невірний YouTube URL", "Invalid YouTube URL"),
    "library.audio.err.already": _m("Це аудіо вже завантажено", "This audio has already been downloaded"),
    "library.audio.err.not_found": _m("Запис не знайдено", "Record not found"),
    "library.audio.err.json_object": _m("Очікується JSON-обʼєкт", "A JSON object is expected"),
    "library.audio.err.need_field": _m(
        "Потрібне хоча б одне поле: title або description",
        "At least one field is required: title or description"),
    "library.audio.err.title_empty": _m("Назва не може бути порожньою", "The title cannot be empty"),
    "library.audio.err.nothing_restore": _m("Немає що відновлювати", "Nothing to restore"),
    "library.audio.err.audio_missing": _m("Аудіо не знайдено", "Audio not found"),
    "library.audio.err.file_missing": _m("Файл не знайдено на диску", "File not found on disk"),
    "library.audio.err.opened": _m("Відкрито в провіднику", "Opened in Explorer"),
    "library.audio.err.open_failed": _m("Не вдалося відкрити провідник", "Could not open Explorer"),
    "library.audio.err.playing": _m("Відтворення: {title}", "Playing: {title}"),
    "library.audio.err.play_failed": _m("Не вдалося відтворити аудіо", "Could not play the audio"),
}
