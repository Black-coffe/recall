"""Catalog: the knowledge area (memory, research, comments blueprints)."""
MESSAGES: dict = {
    'knowledge.memory.err.channel': {
        "uk": 'Невідомий channel: {channels}',
        "en": 'Unknown channel: {channels}',
    },
    'knowledge.memory.err.tag_name_long': {
        "uk": 'Назва тега довша за {max} символів',
        "en": 'Tag name is longer than {max} characters',
    },
    'knowledge.memory.err.dir_not_found': {
        "uk": 'Директорію не знайдено: {path}',
        "en": 'Directory not found: {path}',
    },
    'knowledge.memory.err.project_bad': {
        "uk": 'project: очікую текст або список текстів',
        "en": 'project: expected text or a list of texts',
    },
    'knowledge.memory.err.parent_id_bad': {
        "uk": 'parent_id: очікую id або null',
        "en": 'parent_id: expected an id or null',
    },
    'knowledge.memory.err.name_empty': {
        "uk": 'Порожня назва',
        "en": 'Name is empty',
    },
    'knowledge.memory.err.sort_order_bad': {
        "uk": 'sort_order: очікую ціле число',
        "en": 'sort_order: expected an integer',
    },
    'knowledge.memory.err.nothing_to_update': {
        "uk": 'Нічого оновлювати',
        "en": 'Nothing to update',
    },
    'knowledge.memory.err.target_id_bad': {
        "uk": 'target_id: очікую id або null',
        "en": 'target_id: expected an id or null',
    },
    'knowledge.memory.err.merge_self': {
        "uk": 'Не можна обʼєднати напрямок із самим собою',
        "en": 'Cannot merge a category into itself',
    },
    'knowledge.memory.err.category_id_bad': {
        "uk": 'category_id: очікую id або null',
        "en": 'category_id: expected an id or null',
    },
    'knowledge.memory.err.category_not_found': {
        "uk": 'Напрямок не знайдено',
        "en": 'Category not found',
    },
    'knowledge.memory.err.transcript_not_found': {
        "uk": 'Транскрипт не знайдено',
        "en": 'Transcript not found',
    },
    'knowledge.memory.err.suggest_failed': {
        "uk": 'Помилка підказки. Перевірте логи.',
        "en": 'Suggestion failed. Check the logs.',
    },
    'knowledge.memory.err.ids_bad': {
        "uk": 'ids: очікую список цілих чисел',
        "en": 'ids: expected a list of integers',
    },
    'knowledge.memory.err.ids_or_source': {
        "uk": 'Вкажіть ids або source_type',
        "en": 'Provide ids or source_type',
    },
    'knowledge.memory.err.tag_name_empty': {
        "uk": 'Порожня назва тега',
        "en": 'Tag name is empty',
    },
    'knowledge.memory.err.color_bad': {
        "uk": 'color: очікую #RGB/#RGBA/#RRGGBB/#RRGGBBAA',
        "en": 'color: expected #RGB/#RGBA/#RRGGBB/#RRGGBBAA',
    },
    'knowledge.memory.err.tag_merge_ids': {
        "uk": 'Вкажіть keep_id і merge_id',
        "en": 'Provide keep_id and merge_id',
    },
    'knowledge.memory.err.tag_add_remove': {
        "uk": 'Вкажіть add і/або remove',
        "en": 'Provide add and/or remove',
    },
    'knowledge.memory.err.tag_accept_dismiss': {
        "uk": 'Вкажіть accept і/або dismiss',
        "en": 'Provide accept and/or dismiss',
    },
    'knowledge.memory.err.no_key_no_embeddings': {
        "uk": 'Немає ні ANTHROPIC_API_KEY, ні локальних embeddings',
        "en": 'Neither ANTHROPIC_API_KEY nor local embeddings are available',
    },
    'knowledge.memory.err.enrich_failed': {
        "uk": 'Помилка збагачення. Перевірте логи.',
        "en": 'Enrichment failed. Check the logs.',
    },
    'knowledge.memory.err.backfill_running': {
        "uk": 'Backfill вже виконується',
        "en": 'Backfill is already running',
    },
    'knowledge.memory.err.import_path': {
        "uk": 'Вкажіть path до директорії meeting_archive',
        "en": 'Provide the path to the meeting_archive directory',
    },
    'knowledge.memory.err.query_empty': {
        "uk": 'Порожній запит',
        "en": 'Empty query',
    },
    'knowledge.memory.err.no_api_key': {
        "uk": 'ANTHROPIC_API_KEY не налаштовано',
        "en": 'ANTHROPIC_API_KEY is not configured',
    },
    'knowledge.memory.err.question_empty': {
        "uk": 'Порожнє питання',
        "en": 'Empty question',
    },
    'knowledge.memory.err.rag_failed': {
        "uk": 'Помилка RAG. Перевірте логи.',
        "en": 'RAG error. Check the logs.',
    },
    'knowledge.memory.err.rag_stream_failed': {
        "uk": 'Помилка RAG.',
        "en": 'RAG error.',
    },
    'knowledge.memory.err.rating_bad': {
        "uk": 'rating має бути 1 або -1',
        "en": 'rating must be 1 or -1',
    },
    'knowledge.memory.err.question_not_found': {
        "uk": 'Питання не знайдено',
        "en": 'Question not found',
    },
    'knowledge.memory.err.entity_not_found': {
        "uk": 'Сутність не знайдено',
        "en": 'Entity not found',
    },
    'knowledge.memory.err.status_bad': {
        "uk": 'Невірний статус',
        "en": 'Invalid status',
    },
    'knowledge.memory.err.task_not_found': {
        "uk": 'Задачу не знайдено',
        "en": 'Task not found',
    },
    'knowledge.research.err.query_empty': {
        "uk": 'Порожній запит',
        "en": 'Empty query',
    },
    'knowledge.research.err.summary_failed': {
        "uk": 'Помилка саммарі.',
        "en": 'Summary error.',
    },
    'knowledge.research.err.no_api_key': {
        "uk": 'ANTHROPIC_API_KEY не налаштовано',
        "en": 'ANTHROPIC_API_KEY is not configured',
    },
    'knowledge.research.err.no_fragments': {
        "uk": 'Не знайдено фрагментів для саммарі',
        "en": 'No fragments found for the summary',
    },
    'knowledge.research.err.generate_failed': {
        "uk": 'Помилка генерації саммарі. Перевірте логи.',
        "en": 'Summary generation failed. Check the logs.',
    },
    'knowledge.research.progress.analyzing': {
        "uk": 'Аналізую джерела…',
        "en": 'Analysing sources…',
    },
    'knowledge.research.progress.analyzing_part': {
        "uk": 'Аналізую джерела (частина {part}/{total})…',
        "en": 'Analysing sources (part {part}/{total})…',
    },
    'knowledge.research.progress.reducing': {
        "uk": 'Зводжу в підсумковий звіт…',
        "en": 'Combining into the final report…',
    },
    'knowledge.memory.ans.no_results': {
        "uk": 'У архіві не знайдено релевантної інформації за цим запитом.',
        "en": 'No relevant information was found in the archive for this query.',
    },
    'knowledge.comments.err.unknown_target': {
        "uk": 'невідомий тип цілі: {target_type}',
        "en": 'unknown target type: {target_type}',
    },
    'knowledge.comments.err.target_required': {
        "uk": 'потрібні target_type і target_id',
        "en": 'target_type and target_id are required',
    },
    'knowledge.comments.err.ids_array': {
        "uk": 'ids мусить бути масивом',
        "en": 'ids must be an array',
    },
    'knowledge.comments.err.target_id_bad': {
        "uk": 'некоректний target_id',
        "en": 'invalid target_id',
    },
    'knowledge.comments.err.not_found': {
        "uk": 'Коментар не знайдено',
        "en": 'Comment not found',
    },
    'knowledge.comments.err.no_api_key': {
        "uk": 'Немає ANTHROPIC_API_KEY — розбір недоступний',
        "en": 'ANTHROPIC_API_KEY is missing — analysis is unavailable',
    },
    'knowledge.comments.err.no_transcript': {
        "uk": 'Немає транскрипта, до якого прикріпити задачі',
        "en": 'There is no transcript to attach the tasks to',
    },
    'knowledge.comments.err.claude_silent': {
        "uk": 'Claude не відповів — спробуйте ще раз',
        "en": 'Claude did not respond — please try again',
    },
    'knowledge.comments.err.nothing_to_restore': {
        "uk": 'Немає що відновлювати',
        "en": 'Nothing to restore',
    },
    "knowledge.memory.err.not_mcp_event": {
        "uk": "Цю подію створив не агент, її не можна скасувати",
        "en": "This event was not made by an agent and cannot be undone",
    },
    "knowledge.memory.err.undo_not_last": {
        "uk": "Скасувати можна лише останню подію задачі",
        "en": "Only the last event of an item can be undone",
    },
    "knowledge.memory.err.bulk_limit": {
        "uk": "Забагато елементів за раз: дозволено лише id, не більше 5",
        "en": "Too many items at once: only ids, at most 5 are allowed",
    },
    "knowledge.comments.err.owner_authored": {
        "uk": "Коментар написав власник, агент його не змінює",
        "en": "The comment was written by the owner; an agent cannot change it",
    },
    "knowledge.memory.err.category_name_long": {
        "uk": "Назва напряму довша за {max} символів",
        "en": "Direction name is longer than {max} characters",
    },
    "knowledge.memory.err.private_category_change": {
        "uk": "Запис не можна вивести з приватного напряму: це робить лише власник в інтерфейсі",
        "en": "A record cannot be moved out of a private direction: only the owner does that in the UI",
    },
    "knowledge.memory.err.tag_unknown": {
        "uk": "Тег «{name}» не існує: агент призначає лише наявні теги",
        "en": "Tag \"{name}\" does not exist: an agent assigns existing tags only",
    },
    "knowledge.memory.err.postpone_days_bad": {
        "uk": "days: очікую 1, 7 або 30",
        "en": "days: expected 1, 7 or 30",
    },
    "knowledge.memory.err.action_bad": {
        "uk": "action: очікую approve, reject або edit, і без status поруч",
        "en": "action: expected approve, reject or edit, and no status next to it",
    },
    "knowledge.memory.err.edit_bad": {
        "uk": "edit: потрібен непорожній task або due_date у форматі РРРР-ММ-ДД",
        "en": "edit: a non-empty task or a due_date as YYYY-MM-DD is required",
    },
}
