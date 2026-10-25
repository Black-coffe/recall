"""Server text catalog: background job errors (svc_jobs)."""
MESSAGES: dict = {
    "svc_jobs.error.failed": {
        "uk": "Завдання завершилось помилкою. Подробиці — в журналі застосунку.",
        "en": "The job failed. See the application log for details.",
    },
    "svc_jobs.error.recording_failed": {
        "uk": "Обробка запису зупинилась через помилку. Подробиці — в журналі застосунку.",
        "en": "Recording processing stopped with an error. See the application log for details.",
    },
    "svc_jobs.error.stream_failed": {
        "uk": "Аудіопотік зламався. Подробиці — в журналі застосунку.",
        "en": "The audio stream failed. See the application log for details.",
    },
    "svc_jobs.error.crashed": {
        "uk": "Перервано рестартом застосунку",
        "en": "Interrupted by an application restart",
    },
}
