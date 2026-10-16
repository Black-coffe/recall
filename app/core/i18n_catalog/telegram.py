"""Catalog of server texts: the telegram area (brief, bot buttons, panel errors)."""
MESSAGES: dict = {
    "telegram.brief.title.weekly_digest": {
        "uk": "🗓 Звід тижня",
        "en": "🗓 Weekly digest",
    },
    "telegram.brief.title.dropped_commitments": {
        "uk": "🕳 Забуті обіцянки",
        "en": "🕳 Dropped commitments",
    },
    "telegram.brief.title.open_questions": {
        "uk": "❓ Питання без відповіді",
        "en": "❓ Unanswered questions",
    },
    "telegram.brief.tool.weekly_digest": {
        "uk": "Звід",
        "en": "Digest",
    },
    "telegram.brief.tool.dropped_commitments": {
        "uk": "Забуті обіцянки",
        "en": "Dropped commitments",
    },
    "telegram.brief.tool.open_questions": {
        "uk": "Питання з Telegram",
        "en": "Telegram questions",
    },
    "telegram.brief.notCollected": {
        "uk": "⚠️ {tool} не зібрано: {reason}",
        "en": "⚠️ {tool} not collected: {reason}",
    },
    "telegram.brief.reason.noOwner": {
        "uk": "невідомо, хто власник архіву: слухач вимкнений і немає ні TELEGRAM_SELF_NAME, ні приватних чатів",
        "en": "the archive owner is unknown: the listener is off and there is neither TELEGRAM_SELF_NAME nor any private chats",
    },
    "telegram.brief.noDir": {
        "uk": "Без напрямку",
        "en": "No category",
    },
    "telegram.brief.noText": {
        "uk": "(без тексту)",
        "en": "(no text)",
    },
    "telegram.brief.overdue": {
        "uk": "прострочено з {date}",
        "en": "overdue since {date}",
    },
    "telegram.brief.thisWeek": {
        "uk": "до {date}",
        "en": "due {date}",
    },
    "telegram.brief.noDate": {
        "uk": "без дати",
        "en": "no date",
    },
    "telegram.brief.silentSince": {
        "uk": "тиша з {date}",
        "en": "silent since {date}",
    },
    "telegram.brief.silentHours": {
        "uk": "тиша {h} год",
        "en": "silent {h} h",
    },
    "telegram.brief.inTelegram": {
        "uk": "у Telegram",
        "en": "in Telegram",
    },
    "telegram.brief.more": {
        "uk": "ще {n} →",
        "en": "{n} more →",
    },
    "telegram.brief.moreInRecall": {
        "uk": "…ще {n} у Recall",
        "en": "…{n} more in Recall",
    },
    "telegram.brief.stale": {
        "uk": "💤 Теми, що зникли",
        "en": "💤 Topics gone quiet",
    },
    "telegram.brief.staleDays": {
        "uk": "тиша {days} дн.",
        "en": "silent {days} d",
    },
    "telegram.brief.corrections": {
        "uk": "✏️ Виправлення архіву",
        "en": "✏️ Archive corrections",
    },
    "telegram.brief.recordWord": {
        "uk": "запис",
        "en": "record",
    },
    "telegram.brief.tasks": {
        "uk": "📌 Задачі",
        "en": "📌 Tasks",
    },
    "telegram.brief.dropped": {
        "uk": "🕳 Упущено — після зустрічі тиша",
        "en": "🕳 Dropped — silence after the meeting",
    },
    "telegram.brief.questions": {
        "uk": "❓ Питання без відповіді",
        "en": "❓ Unanswered questions",
    },
    "telegram.brief.coverage": {
        "uk": "З датою — {dated} із {total} задач архіву.",
        "en": "With a date — {dated} of {total} archive tasks.",
    },
    "telegram.brief.nothingNew": {
        "uk": "Нічого нового.",
        "en": "Nothing new.",
    },
    "telegram.brief.hidden": {
        "uk": {"one": "🔒 {n} пункт приховано, дивись у Recall", "few": "🔒 {n} пункти приховано, дивись у Recall", "many": "🔒 {n} пунктів приховано, дивись у Recall"},
        "en": {"one": "🔒 {n} item hidden, see Recall", "other": "🔒 {n} items hidden, see Recall"},
    },
    "telegram.brief.blocked": {
        "uk": "🔒 Брифінг «{name}» не надіслано: його зупинив фільтр приватного — {reason}. Подивись у Recall; деталі — у list_pipe_runs.",
        "en": "🔒 Briefing “{name}” was not sent: the privacy filter stopped it — {reason}. Check Recall; details in list_pipe_runs.",
    },
    "telegram.brief.blockedReason.stopWord": {
        "uk": "у готовому тексті лишилось слово зі стоп-листа",
        "en": "a stop-list word remained in the final text",
    },
    "telegram.brief.blockedReason.privateTxt": {
        "uk": "файл стоп-слів private.txt не читається",
        "en": "the stop-word file private.txt cannot be read",
    },
    "telegram.brief.blockedReason.dirsDb": {
        "uk": "не вдалося перевірити приватні напрямки (збій БД)",
        "en": "could not check private categories (DB failure)",
    },
    "telegram.brief.blockedReason.filter": {
        "uk": "фільтр приватного",
        "en": "privacy filter",
    },
    "telegram.brief.failed": {
        "uk": "⚠️ Труба «{name}» впала: {reason}.{tail} Деталі — у list_pipe_runs.",
        "en": "⚠️ Pipe “{name}” failed: {reason}.{tail} Details in list_pipe_runs.",
    },
    "telegram.brief.failRetry": {
        "uk": " Повторю через {minutes} хв (спроба {attempt} з {max}).",
        "en": " Retrying in {minutes} min (attempt {attempt} of {max}).",
    },
    "telegram.brief.failExhausted": {
        "uk": " Спроби вичерпано ({max}), до наступного розкладу не пробую.",
        "en": " Attempts exhausted ({max}); not retrying until the next scheduled run.",
    },
    "telegram.btn.footer": {
        "uk": "Кнопки спрацьовують, коли увімкнений домашній ПК.",
        "en": "Buttons work when the home PC is on.",
    },
    "telegram.btn.label.done": {
        "uk": "✅ зроблено",
        "en": "✅ done",
    },
    "telegram.btn.label.cancelled": {
        "uk": "🚫 неактуально",
        "en": "🚫 not relevant",
    },
    "telegram.btn.label.postpone1": {
        "uk": "⏭ на завтра",
        "en": "⏭ to tomorrow",
    },
    "telegram.btn.label.postpone7": {
        "uk": "⏭ на тиждень",
        "en": "⏭ by a week",
    },
    "telegram.btn.label.postpone30": {
        "uk": "⏭ на місяць",
        "en": "⏭ by a month",
    },
    "telegram.btn.tomorrow": {
        "uk": "завтра",
        "en": "tomorrow",
    },
    "telegram.btn.plusWeek": {
        "uk": "+тиждень",
        "en": "+week",
    },
    "telegram.btn.plusMonth": {
        "uk": "+місяць",
        "en": "+month",
    },
    "telegram.btn.postponedTo": {
        "uk": "⏭ до {date}",
        "en": "⏭ until {date}",
    },
    "telegram.btn.undo": {
        "uk": "↩ скасувати",
        "en": "↩ undo",
    },
    "telegram.btn.noAccess": {
        "uk": "Немає доступу",
        "en": "No access",
    },
    "telegram.btn.ok": {
        "uk": "готово",
        "en": "done",
    },
    "telegram.btn.undone": {
        "uk": "↩ скасовано",
        "en": "↩ undone",
    },
    "telegram.btn.unknown": {
        "uk": "Невідома кнопка",
        "en": "Unknown button",
    },
    "telegram.err.filePath": {
        "uk": "file_path відсутній або поза telegram_media/",
        "en": "file_path is missing or outside telegram_media/",
    },
    "telegram.err.queueDown": {
        "uk": "Черга завдань недоступна",
        "en": "Job queue unavailable",
    },
    "telegram.err.chatIdRequired": {
        "uk": "chat_id обов'язковий",
        "en": "chat_id is required",
    },
    "telegram.err.chatIdNumber": {
        "uk": "chat_id має бути числом",
        "en": "chat_id must be a number",
    },
    "telegram.err.categoryIdNumber": {
        "uk": "category_id має бути числом",
        "en": "category_id must be a number",
    },
    "telegram.err.tagsRequired": {
        "uk": "tags обов'язковий",
        "en": "tags is required",
    },
    "telegram.err.tagsList": {
        "uk": "tags: список назв",
        "en": "tags: a list of names",
    },
    "telegram.err.chatNotMonitored": {
        "uk": "чат не моніториться",
        "en": "chat is not monitored",
    },
    "telegram.err.needMessageId": {
        "uk": "потрібні chat_id і message_id",
        "en": "chat_id and message_id are required",
    },
    "telegram.err.needMessageIds": {
        "uk": "потрібні chat_id і message_ids",
        "en": "chat_id and message_ids are required",
    },
    "telegram.err.repairNumbers": {
        "uk": "chat_id/max_messages мають бути числами",
        "en": "chat_id/max_messages must be numbers",
    },
    "telegram.err.tagTooLong": {
        "uk": "назва тега довша за {max}: {name!r}",
        "en": "tag name is longer than {max}: {name!r}",
    },
    "telegram.err.repairSupergroup": {
        "uk": "ремонт по id доступний лише для супергруп (-100…): у решті чатів message_id з глобальної послідовності акаунта. Для них — /backfill за датами",
        "en": "repair by id is only available for supergroups (-100…): in other chats message_id comes from the account's global sequence. Use /backfill by date for them",
    },
    "telegram.coverage.note": {
        "uk": ("behind_messages рахується лише для супергруп (-100…); пропуски в id "
               "можуть бути стікерами/службовими повідомленнями, які інжест свідомо "
               "не бере. lag_days показує відставання ХВОСТА — дірки в середині "
               "історії ним не видно. not_listed при dialogs_seen == dialog_limit "
               "може означати просто «не вліз у вікно діалогів»"),
        "en": ("behind_messages is counted only for supergroups (-100…); gaps in ids "
               "may be stickers/service messages that ingest deliberately skips. "
               "lag_days shows the lag of the TAIL only; gaps in the middle of the "
               "history are not visible through it. not_listed when dialogs_seen == dialog_limit "
               "may simply mean \"did not fit in the dialog window\""),
    },
    "telegram.gaps.none": {
        "uk": "дірок немає (або чат порожній — тоді це backfill)",
        "en": "no gaps (or the chat is empty, in which case this is a backfill)",
    },
}
