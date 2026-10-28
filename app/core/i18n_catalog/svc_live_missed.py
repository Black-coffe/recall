"""Server text catalog: the svc_live_missed area ("what did I miss" live summary)."""
MESSAGES: dict = {
    "svc_live_missed.err.window": {
        "uk": "Невідоме вікно. Допустимі: since_self, 2m, 5m, 10m, whole.",
        "en": "Unknown window. Allowed: since_self, 2m, 5m, 10m, whole.",
    },
    "svc_live_missed.err.not_live": {
        "uk": "Немає живого запису з транскриптом для цієї сесії.",
        "en": "There is no live recording with a transcript for this session.",
    },
    "svc_live_missed.err.ambiguous": {
        "uk": "Іде кілька записів — вкажіть session_id.",
        "en": "Several recordings are live: pass session_id.",
    },
    "svc_live_missed.err.agent_session": {
        "uk": "Для запису, запущеного агентом, резюме недоступне.",
        "en": "The summary is not available for a recording started by an agent.",
    },
    "svc_live_missed.err.budget": {
        "uk": "Вичерпано ліміт витрат на резюме для цього дзвінка.",
        "en": "The summary spend limit for this call is exhausted.",
    },
    "svc_live_missed.err.unavailable": {
        "uk": "Claude недоступний: немає ключа ANTHROPIC_API_KEY або запит не вдався.",
        "en": "Claude is unavailable: no ANTHROPIC_API_KEY or the request failed.",
    },
}
