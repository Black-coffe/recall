"""Server text catalog: boot, Whisper manager and infrastructure progress lines (svc_boot)."""
MESSAGES: dict = {
    # --- app/utils/audio.py ---
    "svc_boot.audio.trim.loading": {
        "uk": "Загрузка аудіофайлу в память...",
        "en": "Loading the audio file into memory...",
    },
    "svc_boot.audio.trim.cutting": {
        "uk": "Обрізка аудіо: {start}с - {end}с",
        "en": "Trimming audio: {start}s - {end}s",
    },
    "svc_boot.audio.trim.saving": {
        "uk": "Збереження обрізаного файлу...",
        "en": "Saving the trimmed file...",
    },
    "svc_boot.audio.trim.done": {
        "uk": "Обрізка завершена. Розмір: {original}MB → {trimmed}MB",
        "en": "Trimming finished. Size: {original}MB → {trimmed}MB",
    },
    "svc_boot.audio.error": {"uk": "Помилка: {error}", "en": "Error: {error}"},
    "svc_boot.audio.extract.start": {
        "uk": "Витягування аудіо з відео...",
        "en": "Extracting audio from the video...",
    },
    "svc_boot.audio.extract.ffmpeg_error": {
        "uk": "Помилка FFmpeg: {error}",
        "en": "FFmpeg error: {error}",
    },
    "svc_boot.audio.extract.done": {
        "uk": "Аудіо успішно витягнуто",
        "en": "Audio extracted successfully",
    },
}
