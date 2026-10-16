"""Catalog of server texts: the mail (Gmail ingest) area."""
MESSAGES: dict = {
    "mail.err.invalidAddress": {
        "uk": "Вкажіть коректну адресу пошти.",
        "en": "Enter a valid email address.",
    },
    "mail.err.emptyPassword": {
        "uk": "Введіть пароль застосунку.",
        "en": "Enter the app password.",
    },
    "mail.err.authFailed": {
        "uk": "Gmail не прийняв адресу або пароль застосунку. Створіть пароль застосунку в налаштуваннях облікового запису Google.",
        "en": "Gmail rejected the address or the app password. Create an app password in your Google account settings.",
    },
    "mail.err.connectFailed": {
        "uk": "Не вдалося підключитися до Gmail. Перевірте мережу й спробуйте ще раз.",
        "en": "Could not connect to Gmail. Check the network and try again.",
    },
    "mail.err.folderMissing": {
        "uk": "Gmail не знайшов потрібний ярлик.",
        "en": "Gmail could not find the requested label.",
    },
    "mail.err.protocol": {
        "uk": "Gmail відповів несподівано. Спробуйте пізніше.",
        "en": "Gmail answered unexpectedly. Try again later.",
    },
    "mail.err.accountNotFound": {
        "uk": "Поштовий обліковий запис не знайдено.",
        "en": "Mail account not found.",
    },
    "mail.err.labelNotFound": {
        "uk": "Ярлик не знайдено.",
        "en": "Label not found.",
    },
    "mail.err.noPassword": {
        "uk": "Пароль застосунку не знайдено у сховищі системи. Додайте обліковий запис знову.",
        "en": "The app password is missing from the system store. Add the account again.",
    },
    "mail.err.badCategory": {
        "uk": "Невідомий напрямок.",
        "en": "Unknown direction.",
    },
    "mail.err.badFlag": {
        "uk": "Поле «{field}» має бути true або false.",
        "en": "The \"{field}\" field must be true or false.",
    },
    "mail.err.badMonths": {
        "uk": "Кількість місяців має бути цілим числом від 1 до 120.",
        "en": "Months must be an integer from 1 to 120.",
    },
    "mail.err.badAccountId": {
        "uk": "Некоректний ідентифікатор облікового запису.",
        "en": "Invalid account id.",
    },
    "mail.err.pollerOff": {
        "uk": "Фонове опитування пошти вимкнене.",
        "en": "Background mail polling is off.",
    },
    "mail.err.badJson": {
        "uk": "Очікується JSON-об'єкт.",
        "en": "A JSON object is expected.",
    },
    "mail.err.internal": {
        "uk": "Не вдалося виконати операцію з поштою.",
        "en": "The mail operation failed.",
    },
}
