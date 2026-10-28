"""Catalog of server texts: the approval dialog in front of destructive routes and its refusals."""
MESSAGES: dict = {
    # who asks: always the first line of the dialog
    "approvals.who.ui": {
        "uk": "Запит зі сторінки Recall (токен інтерфейсу). "
              "Якщо ви зараз нічого не видаляли — натисніть «Ні».",
        "en": "Request from a Recall page (interface token). "
              "If you are not deleting anything right now, press «No».",
    },
    "approvals.who.unknown": {
        "uk": "⚠ НЕ з інтерфейсу Recall: MCP, скрипт або програма. "
              "Якщо ви зараз нічого не видаляли — натисніть «Ні».",
        "en": "⚠ NOT from the Recall interface: MCP, a script or a program. "
              "If you are not deleting anything right now, press «No».",
    },
    "approvals.who.claims": {
        "uk": "Заявляє про себе (не перевірено): «{actor}» / «{tool}».",
        "en": "Claims to be (not verified): «{actor}» / «{tool}».",
    },
    # verb
    "approvals.action.transcript_delete": {"uk": "Видалити запис.", "en": "Delete a record."},
    "approvals.action.transcript_bulk_delete": {
        "uk": "Остаточно видалити кілька записів.",
        "en": "Permanently delete several records.",
    },
    "approvals.action.taxonomy_delete": {"uk": "Видалити з довідника.", "en": "Delete from the dictionary."},
    "approvals.action.taxonomy_merge": {"uk": "Злити в довіднику.", "en": "Merge in the dictionary."},
    "approvals.action.speaker_delete": {"uk": "Видалити спікера.", "en": "Delete a speaker."},
    "approvals.action.speaker_merge": {"uk": "Злити спікерів.", "en": "Merge speakers."},
    "approvals.action.enrich_backfill": {
        "uk": "Запустити платне збагачення архіву через Claude.",
        "en": "Start the paid enrichment of the archive through Claude.",
    },
    "approvals.action.telegram_backfill": {
        "uk": "Дозавантажити історію чату Telegram.",
        "en": "Load the history of a Telegram chat.",
    },
    "approvals.action.settings_remove_anthropic_key": {
        "uk": "Видалити ключ Anthropic з налаштувань.",
        "en": "Remove the Anthropic key from the settings.",
    },
    "approvals.action.mailbox_disconnect": {
        "uk": "Відключити поштову скриньку (видалити пароль, вимкнути папки).",
        "en": "Disconnect a mailbox (remove the password, disable the folders).",
    },
    "approvals.action.calendar_disconnect": {
        "uk": "Відключити календар (видалити секретну адресу і його події).",
        "en": "Disconnect a calendar (remove the secret address and its events).",
    },
    "approvals.action.mailbox_delete": {
        "uk": "Видалити поштову скриньку з Recall.",
        "en": "Delete a mailbox from Recall.",
    },
    "approvals.action.mailbox_backfill": {
        "uk": "Догрузити старі листи (глибше ніж за 12 місяців).",
        "en": "Load older mail (deeper than 12 months).",
    },
    "approvals.action.mailbox_label_enrich": {
        "uk": "Увімкнути платний розбір Claude для поштової папки.",
        "en": "Turn paid Claude enrichment on for a mail folder.",
    },
    "approvals.action.audio_delete": {"uk": "Видалити аудіо.", "en": "Delete an audio file."},
    "approvals.action.transcript_version_evict": {
        "uk": "Перетранскрибувати запис із видаленням найстарішої версії.",
        "en": "Re-transcribe a record, dropping its oldest version.",
    },
    "approvals.action.document_import_folder": {
        "uk": "Імпортувати документи з папки в архів.",
        "en": "Import documents from a folder into the archive.",
    },
    "approvals.action.document_reparse": {
        "uk": "Повторно розібрати документ з оригіналу.",
        "en": "Re-parse a document from its original.",
    },
    "approvals.action.video_describe_claude": {
        "uk": "Описати кадри відео через Claude.",
        "en": "Describe the video frames through Claude.",
    },
    "approvals.action.telegram_enable_chat": {
        "uk": "Увімкнути слухання чату Telegram.",
        "en": "Start listening to a Telegram chat.",
    },
    # object kind
    "approvals.kind.record": {"uk": "Запис", "en": "Record"},
    "approvals.kind.records": {"uk": "Записи", "en": "Records"},
    "approvals.kind.direction": {"uk": "Напрямок", "en": "Direction"},
    "approvals.kind.tag": {"uk": "Тег", "en": "Tag"},
    "approvals.kind.speaker": {"uk": "Спікер", "en": "Speaker"},
    "approvals.kind.speakers": {"uk": "Спікери", "en": "Speakers"},
    "approvals.kind.enrichment": {
        "uk": "Збагачення (лише необроблені записи)",
        "en": "Enrichment (unprocessed records only)",
    },
    "approvals.kind.enrichment_force": {
        "uk": "Збагачення повторно для всіх записів",
        "en": "Enrichment again for every record",
    },
    "approvals.kind.chat": {"uk": "Чат", "en": "Chat"},
    "approvals.kind.setting": {"uk": "Параметр", "en": "Setting"},
    "approvals.kind.mailbox": {"uk": "Скринька", "en": "Mailbox"},
    "approvals.kind.calendar": {"uk": "Календар", "en": "Calendar"},
    "approvals.kind.mailboxes": {"uk": "Усі скриньки", "en": "All mailboxes"},
    "approvals.kind.mail_label": {"uk": "Поштова папка", "en": "Mail folder"},
    "approvals.kind.audio": {"uk": "Аудіо", "en": "Audio"},
    "approvals.kind.folder": {"uk": "Папка", "en": "Folder"},
    "approvals.kind.folder_recursive": {"uk": "Папка з підпапками", "en": "Folder with subfolders"},
    "approvals.object": {"uk": "{kind} «{name}» (#{id})", "en": "{kind} «{name}» (#{id})"},
    "approvals.object_noid": {"uk": "{kind} «{name}»", "en": "{kind} «{name}»"},
    "approvals.object_bare": {"uk": "{kind}", "en": "{kind}"},
    "approvals.object_unverified": {
        "uk": "{kind} «{name}» (не перевірено: такого чату немає серед чатів Recall)",
        "en": "{kind} «{name}» (not verified: no such chat among Recall's chats)",
    },
    # impact
    "approvals.impact.transcript_delete": {
        "uk": "Сегментів у записі: {count}.", "en": "Segments in the record: {count}.",
    },
    "approvals.impact.transcript_bulk_delete": {
        "uk": "Записів буде видалено: {count}.", "en": "Records to delete: {count}.",
    },
    "approvals.impact.taxonomy_delete": {
        "uk": "Записів, яких це торкнеться: {count}.", "en": "Records affected: {count}.",
    },
    "approvals.impact.taxonomy_merge": {
        "uk": "Записів буде перенесено: {count}.", "en": "Records to move: {count}.",
    },
    "approvals.impact.speaker_delete": {
        "uk": "Привʼязок у записах: {count}.", "en": "Links in records: {count}.",
    },
    "approvals.impact.speaker_merge": {
        "uk": "Використань буде перенесено: {count}.", "en": "Uses to move: {count}.",
    },
    "approvals.impact.enrich_backfill": {
        "uk": "Записів до обробки: {count}.", "en": "Records to process: {count}.",
    },
    "approvals.impact.telegram_backfill": {
        "uk": "Повідомлень до завантаження (до): {count}.", "en": "Messages to load (up to): {count}.",
    },
    "approvals.impact.settings_remove_anthropic_key": {
        "uk": "Записів, яких це торкнеться: {count}.", "en": "Records affected: {count}.",
    },
    "approvals.impact.mailbox_disconnect": {
        "uk": "Увімкнених папок буде вимкнено: {count}. Архів і позначки синхронізації лишаться; "
              "після повторного підключення папки треба ввімкнути вручну.",
        "en": "Enabled folders to disable: {count}. The archive and sync cursors stay; "
              "after a reconnect the folders must be enabled by hand.",
    },
    "approvals.impact.calendar_disconnect": {
        "uk": "Подій буде видалено: {count}. Записи лишаться, але втратять привʼязку до цих подій.",
        "en": "Events to remove: {count}. Recordings stay but lose their link to these events.",
    },
    "approvals.impact.mailbox_delete": {
        "uk": "Ярликів буде видалено: {count}. Архівні листи лишаться.",
        "en": "Labels to remove: {count}. Archived mail stays.",
    },
    "approvals.impact.mailbox_backfill": {
        "uk": "Місяців назад: {count}. Нові листи підуть в архів (і на розбір Claude там, де він увімкнений).",
        "en": "Months back: {count}. New mail goes into the archive (and to Claude where enrichment is on).",
    },
    "approvals.impact.mailbox_label_enrich": {
        "uk": "Кожен новий лист цієї папки піде на платний розбір Claude.",
        "en": "Every new message in this folder goes to paid Claude enrichment.",
    },
    "approvals.impact.transcript_version_evict": {
        "uk": "Версій лишиться: {count}.", "en": "Versions kept: {count}.",
    },
    "approvals.impact.audio_delete": {
        "uk": "Файлів: {count}.", "en": "Files: {count}.",
    },
    "approvals.impact.document_import_folder": {
        "uk": "Файлів до імпорту: {count}. Нові документи підуть на збагачення "
              "(платний розбір Claude, якщо є ключ).",
        "en": "Files to import: {count}. New documents go to enrichment "
              "(paid Claude enrichment when a key is set).",
    },
    "approvals.impact.document_reparse": {
        "uk": "Текст запису буде замінено новим розбором, запис буде збагачено повторно "
              "(платно, якщо є ключ Claude).",
        "en": "The record's text is replaced by the new parse and the record is enriched again "
              "(paid when a Claude key is set).",
    },
    "approvals.impact.video_describe_claude": {
        "uk": "Кадри відео покинуть компʼютер і підуть в Anthropic (Claude) на опис; це платно.",
        "en": "The video frames leave this computer for Anthropic (Claude) to be described; this is paid.",
    },
    "approvals.impact.telegram_enable_chat": {
        "uk": "Нові повідомлення чату підуть в архів. Історію ця дія не завантажує.",
        "en": "New messages of the chat go into the archive. This action does not load the history.",
    },
    # extras
    "approvals.extra.tg_chat_new": {
        "uk": "Чат новий для Recall: буде додано.", "en": "The chat is new to Recall: it will be added.",
    },
    "approvals.extra.tg_chat_known": {
        "uk": "Чат уже є в Recall.", "en": "The chat is already in Recall.",
    },
    "approvals.extra.merge_into": {"uk": "Куди: «{name}».", "en": "Into: «{name}»."},
    "approvals.extra.merge_into_none": {"uk": "Куди: без напрямку.", "en": "Into: no direction."},
    "approvals.extra.model": {"uk": "Модель: {model}.", "en": "Model: {model}."},
    "approvals.extra.model_default": {"uk": "Модель: за замовчуванням.", "en": "Model: the default."},
    "approvals.extra.version_drop": {
        "uk": "Буде видалено версію: {version}.", "en": "Version to drop: {version}.",
    },
    "approvals.extra.direction": {"uk": "Напрямок: «{name}».", "en": "Direction: «{name}»."},
    "approvals.extra.direction_none": {"uk": "Напрямок: без напрямку.", "en": "Direction: none."},
    "approvals.extra.source_file": {"uk": "Файл: «{name}».", "en": "File: «{name}»."},
    "approvals.extra.mailbox": {"uk": "Скринька: «{name}».", "en": "Mailbox: «{name}»."},
    "approvals.extra.key_present": {"uk": "Ключ зараз збережено.", "en": "A key is stored now."},
    "approvals.extra.key_absent": {"uk": "Ключа зараз немає.", "en": "No key is stored now."},
    # undo
    "approvals.undo.yes": {"uk": "Це можна скасувати.", "en": "This can be undone."},
    "approvals.undo.no": {"uk": "Скасувати це не можна.", "en": "This cannot be undone."},
    # note from the requester, always quoted
    "approvals.note": {
        "uk": "Примітка запитувача (не перевірено): «{note}»",
        "en": "Requester's note (not verified): «{note}»",
    },
    "approvals.deadline": {
        "uk": "Без відповіді за {seconds} с — відмова.",
        "en": "No answer in {seconds} s means no.",
    },
    "approvals.question": {"uk": "Дозволити?", "en": "Allow?"},
    # refusals in the response body
    "approvals.err.private_category": {
        "uk": "Видалення або злиття приватного напрямку через MCP заборонене.",
        "en": "Deleting or merging a private direction over MCP is refused.",
    },
    "approvals.err.privacy_check": {
        "uk": "Не вдалося перевірити приватні напрямки.",
        "en": "Could not check private directions.",
    },
    "approvals.err.ceiling": {
        "uk": "Забагато видалень поспіль. Спробуйте через {seconds} с.",
        "en": "Too many destructive actions in a row. Try again in {seconds} s.",
    },
    "approvals.err.recording": {
        "uk": "Іде запис дзвінка: видалення не з інтерфейсу зараз заборонені.",
        "en": "A call is being recorded: destructive requests from outside the interface are refused now.",
    },
    "approvals.err.pending": {
        "uk": "Уже відкрито інше вікно підтвердження.",
        "en": "Another confirmation window is already open.",
    },
    "approvals.err.unavailable": {
        "uk": "Вікно підтвердження не вдалося показати, дію відхилено.",
        "en": "The confirmation window could not be shown; the action is refused.",
    },
    "approvals.err.malformed": {
        "uk": "Запит не вдалося описати у вікні підтвердження, тому його відхилено без виконання.",
        "en": "The request could not be described in the confirmation window, so it is refused without running.",
    },
    "approvals.err.denied": {
        "uk": "Дію не підтверджено у вікні Windows.",
        "en": "The action was not confirmed in the Windows window.",
    },
    "approvals.err.timeout": {
        "uk": "Немає відповіді у вікні Windows, дію відхилено.",
        "en": "No answer in the Windows window; the action is refused.",
    },
    # mcp-recording-live: stop and cancel of a recording
    "approvals.action.recording_stop": {
        "uk": "Зупинити запис дзвінка.", "en": "Stop a call recording.",
    },
    "approvals.action.recording_cancel_own": {
        "uk": "Скасувати запис дзвінка без збереження.", "en": "Cancel a call recording without saving it.",
    },
    "approvals.kind.recording": {"uk": "Запис дзвінка", "en": "Call recording"},
    "approvals.impact.recording_stop": {
        "uk": "Записано хвилин: {count}; запис завершиться.",
        "en": "{count} min recorded so far; the capture ends.",
    },
    "approvals.impact.recording_cancel_own": {
        "uk": "Буде видалено {count} с аудіо, відновити не можна.",
        "en": "{count} s of audio deleted, no restore.",
    },
    "approvals.extra.started_by_owner": {"uk": "Запустив: власник.", "en": "Started by: owner."},
    "approvals.extra.started_by_agent": {"uk": "Запустив: агент.", "en": "Started by: agent."},
}
