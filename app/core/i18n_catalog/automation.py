"""Server text catalog: automation endpoints (automation)."""
MESSAGES: dict = {
    "automation.err.invalidChannel": {
        "uk": "Невідомий канал подій",
        "en": "Unknown event channel",
    },
    "automation.err.notFound": {
        "uk": "Сесію запису не знайдено",
        "en": "Recording session not found",
    },
    "automation.err.tooManyWaiters": {
        "uk": "Забагато одночасних очікувань подій",
        "en": "Too many concurrent event waiters",
    },
    "automation.err.invalidSection": {
        "uk": "Невідома секція статусу автоматики",
        "en": "Unknown automation status section",
    },
    "automation.err.pipeNotFound": {
        "uk": "Трубу не знайдено",
        "en": "Pipe not found",
    },
    "automation.err.pipesDisabled": {
        "uk": "Труби вимкнені (PIPES_ENABLED=0)",
        "en": "Pipes are disabled (PIPES_ENABLED=0)",
    },
    "automation.err.notConfigured": {
        "uk": "Бот Telegram не налаштований",
        "en": "Telegram bot is not configured",
    },
    "automation.err.alreadyRunning": {
        "uk": "Труба вже виконується",
        "en": "This pipe is already running",
    },
    "automation.err.cooldown": {
        "uk": "Труба щойно виконувалась, зачекайте паузу",
        "en": "This pipe ran recently, wait for the cooldown",
    },
}
