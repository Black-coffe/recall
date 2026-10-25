"""Catalog of server texts: the one-time connect pages and link issuers."""
MESSAGES: dict = {
    "connect.err.secretInArgument": {
        "uk": "Схоже, що в аргумент потрапив пароль або ключ. Він відхилений; введіть його лише на сторінці підключення.",
        "en": "The argument looks like a password or a key. It was rejected; type secrets only on the connect page.",
    },
    "connect.title.mail": {
        "uk": "Підключення пошти",
        "en": "Connect a mailbox",
    },
    "connect.title.anthropic_key": {
        "uk": "Ключ Anthropic",
        "en": "Anthropic key",
    },
    "connect.warning": {
        "uk": "Продовжуйте лише якщо ви самі попросили підключити цю пошту або задати цей ключ.",
        "en": "Only continue if you asked to connect this mailbox / set this key.",
    },
    "connect.requestedByMcp": {
        "uk": "Запит від MCP-агента о {time}",
        "en": "Requested by MCP agent at {time}",
    },
    "connect.alreadyConfigured": {
        "uk": "Уже налаштовано; збереження замінить значення.",
        "en": "Already configured; saving replaces it.",
    },
    "connect.label.address": {
        "uk": "Адреса",
        "en": "Address",
    },
    "connect.label.mailSecret": {
        "uk": "Пароль застосунку",
        "en": "App password",
    },
    "connect.label.keySecret": {
        "uk": "Ключ API",
        "en": "API key",
    },
    "connect.submit": {
        "uk": "Зберегти",
        "en": "Save",
    },
    "connect.attemptsLeft": {
        "uk": "Залишилось спроб: {left}.",
        "en": "Attempts left: {left}.",
    },
    "connect.err.rejected": {
        "uk": "Значення не прийнято.",
        "en": "The value was not accepted.",
    },
    "connect.err.unreachable": {
        "uk": "Не вдалося зв'язатися із сервісом. Спробуйте ще раз.",
        "en": "Could not reach the service. Try again.",
    },
    "connect.err.internal": {
        "uk": "Внутрішня помилка. Спробуйте ще раз.",
        "en": "Internal error. Try again.",
    },
    "connect.gone": {
        "uk": "Це посилання вже недійсне. Попросіть у Claude нове.",
        "en": "This link is no longer valid. Ask Claude for a new one.",
    },
    "connect.done.mail": {
        "uk": "Пошту підключено. Знайдені ярлики:",
        "en": "Mailbox connected. Labels found:",
    },
    "connect.done.mailHint": {
        "uk": "Увімкніть ярлики з Claude через mailbox(label_set).",
        "en": "Enable labels from Claude with mailbox(label_set).",
    },
    "connect.done.anthropic_key": {
        "uk": "Ключ збережено.",
        "en": "The key is saved.",
    },
    "connect.done.close": {
        "uk": "Цю вкладку можна закрити.",
        "en": "You can close this tab.",
    },
}
