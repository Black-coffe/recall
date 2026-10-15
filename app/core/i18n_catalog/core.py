"""Каталог: спільні серверні тексти (settings_api, system, app.py, error_handlers)."""
MESSAGES: dict = {
    "core.error.internal": {
        "uk": "Внутрішня помилка сервера. Спробуйте пізніше.",
        "en": "Internal server error. Please try again later.",
    },
    "core.error.rate_limit": {
        "uk": "Rate limit exceeded: {desc}. Спробуйте пізніше.",
        "en": "Rate limit exceeded: {desc}. Please try again later.",
    },
    "core.error.file_too_large": {
        "uk": "Файл занадто великий. Максимальний розмір: {max_gb} GB",
        "en": "File is too large. Maximum size: {max_gb} GB",
    },
    "core.settings.ui_lang_invalid": {
        "uk": "Невірне значення мови: очікується auto, uk або en",
        "en": "Invalid language value: expected auto, uk or en",
    },
    "core.settings.api_key_not_string": {
        "uk": "api_key мусить бути рядком",
        "en": "api_key must be a string",
    },
    "core.settings.api_key_empty": {
        "uk": "Порожній ключ",
        "en": "Empty key",
    },
    "core.settings.api_key_format": {
        "uk": 'Не схоже на ключ Anthropic (має починатись з "sk-ant-", без пробілів/переносів рядків)',
        "en": 'Does not look like an Anthropic key (must start with "sk-ant-", no spaces or line breaks)',
    },
    "core.settings.env_write_failed": {
        "uk": "Не вдалося записати .env",
        "en": "Could not write .env",
    },
    "core.settings.key_not_saved": {
        "uk": "Ключ ще не збережено",
        "en": "The key has not been saved yet",
    },
    "core.settings.anthropic_missing": {
        "uk": "Пакет anthropic не встановлено на сервері",
        "en": "The anthropic package is not installed on the server",
    },
    "core.settings.key_invalid": {
        "uk": "Ключ недійсний (401)",
        "en": "The key is invalid (401)",
    },
    "core.settings.key_forbidden": {
        "uk": "Ключ без дозволу (403) — перевірте план/ліміти акаунту",
        "en": "The key lacks permission (403) — check your plan/account limits",
    },
    "core.settings.anthropic_unreachable": {
        "uk": "Не вдалося з’єднатися з Anthropic API",
        "en": "Could not connect to the Anthropic API",
    },
    "core.settings.enabled_not_bool": {
        "uk": "enabled мусить бути true/false",
        "en": "enabled must be true or false",
    },
    "core.system.model_missing": {
        "uk": "Не вказано модель",
        "en": "No model specified",
    },
    "core.system.model_downloaded": {
        "uk": "Модель {model} успішно завантажена",
        "en": "Model {model} downloaded successfully",
    },
    "core.system.model_download_failed": {
        "uk": "Помилка завантаження моделі",
        "en": "Failed to download the model",
    },
    "core.system.stats_failed": {
        "uk": "Помилка отримання статистики системи",
        "en": "Failed to get system statistics",
    },
}
