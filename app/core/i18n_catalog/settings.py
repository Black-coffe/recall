"""Catalog of server texts: the settings area (speakers.py)."""
MESSAGES: dict = {
    'settings.speakers.err.name_taken': {
        "uk": 'Ім\'я "{name}" вже використовується',
        "en": 'Name "{name}" is already in use',
    },
    'settings.speakers.err.name_type': {
        "uk": 'name мусить бути string',
        "en": 'name must be a string',
    },
    'settings.speakers.err.name_empty': {
        "uk": 'name не може бути порожнім',
        "en": 'name must not be empty',
    },
    'settings.speakers.err.name_long': {
        "uk": 'name занадто довгий (max {max})',
        "en": 'name is too long (max {max})',
    },
    'settings.speakers.err.color_type': {
        "uk": "color мусить бути string '#RRGGBB' або null",
        "en": "color must be a string '#RRGGBB' or null",
    },
    'settings.speakers.err.color_hex': {
        "uk": "color має бути hex '#RRGGBB' або 'RRGGBB'",
        "en": "color must be hex '#RRGGBB' or 'RRGGBB'",
    },
    'settings.speakers.err.exists': {
        "uk": 'Спікер з таким іменем уже існує',
        "en": 'A speaker with this name already exists',
    },
    'settings.speakers.err.nothing_to_update': {
        "uk": 'Нічого не задано для оновлення',
        "en": 'Nothing to update',
    },
    'settings.speakers.err.not_found': {
        "uk": 'Спікер не знайдено',
        "en": 'Speaker not found',
    },
    'settings.speakers.err.self_delete': {
        "uk": '"Ви" не можна видалити — використовується для mic-стріму',
        "en": '"You" cannot be deleted — it is used for the mic stream',
    },
    'settings.speakers.err.mapping_type': {
        "uk": 'mapping має бути обʼєктом',
        "en": 'mapping must be an object',
    },
    'settings.speakers.err.tx_not_found': {
        "uk": 'Транскрипт не знайдено',
        "en": 'Transcript not found',
    },
    'settings.speakers.err.bad_name_for': {
        "uk": 'Невірне імʼя для {label}: {err}',
        "en": 'Invalid name for {label}: {err}',
    },
    'settings.speakers.err.keep_id_int': {
        "uk": 'keep_id мусить бути позитивним int',
        "en": 'keep_id must be a positive int',
    },
    'settings.speakers.err.merge_ids_list': {
        "uk": 'merge_ids мусить бути непорожнім списком',
        "en": 'merge_ids must be a non-empty list',
    },
    'settings.speakers.err.merge_ids_nums': {
        "uk": 'merge_ids має містити лише числа',
        "en": 'merge_ids must contain only numbers',
    },
    'settings.speakers.err.keep_in_merge': {
        "uk": 'keep_id не може бути у merge_ids',
        "en": 'keep_id must not be in merge_ids',
    },
    'settings.speakers.err.keep_not_found': {
        "uk": 'keep_id не знайдено',
        "en": 'keep_id not found',
    },
    'settings.speakers.err.some_not_found': {
        "uk": 'Деякі merge_ids не знайдено',
        "en": 'Some merge_ids were not found',
    },
    'settings.speakers.err.self_merge': {
        "uk": '"Ви" (is_self=1) не можна злити з іншим спікером',
        "en": '"You" (is_self=1) cannot be merged into another speaker',
    },
    'settings.speakers.err.merge_not_found': {
        "uk": 'Merge не знайдено (можливо, прострочено)',
        "en": 'Merge not found (it may have expired)',
    },
    'settings.speakers.err.merge_restored': {
        "uk": 'Цей merge вже відновлено',
        "en": 'This merge has already been restored',
    },
    'settings.speakers.err.snapshot_bad': {
        "uk": 'Пошкоджений снапшот merge',
        "en": 'Corrupted merge snapshot',
    },
    "settings.polish.slow": {
        "uk": 'повільно (1-3 хв)',
        "en": 'slow (1-3 min)',
    },
    "settings.polish.cost_opus": {
        "uk": '≈ $0.20 / 15K симв.',
        "en": '≈ $0.20 / 15K chars',
    },
    "settings.polish.note_opus": {
        "uk": 'Найвища якість. Adaptive thinking. Дорого і повільно для звичайної редактури.',
        "en": 'Highest quality. Adaptive thinking. Expensive and slow for routine editing.',
    },
    "settings.polish.medium": {
        "uk": 'середньо (30-60с)',
        "en": 'medium (30-60s)',
    },
    "settings.polish.cost_sonnet": {
        "uk": '≈ $0.10 / 15K симв.',
        "en": '≈ $0.10 / 15K chars',
    },
    "settings.polish.note_sonnet": {
        "uk": 'Збалансовано. Підходить для більшості постпроцесингу.',
        "en": 'Balanced. Suits most post-processing.',
    },
    "settings.polish.fast": {
        "uk": 'швидко (10-20с)',
        "en": 'fast (10-20s)',
    },
    "settings.polish.cost_haiku": {
        "uk": '≈ $0.025 / 15K симв.',
        "en": '≈ $0.025 / 15K chars',
    },
    "settings.polish.note_haiku": {
        "uk": 'Швидко і дешево. Без adaptive thinking. Якість 90%+ на простих задачах редактури.',
        "en": 'Fast and cheap. No adaptive thinking. 90%+ quality on simple editing tasks.',
    },
    "settings.speakers.stats_note": {
        "uk": ("`speakers` — голоси з діаризації: {diarized} записів із {total_tx}. "
               "Решта архіву — переписка, де автор відомий точно і рахується в "
               "`tg_senders` (без секунд і слів мовлення). Одиниці шарів різні, "
               "їх не додають. Спільна людина видна через `entity_id`: він "
               "проставлений у {linked} із {senders} відправників, "
               "решту граф просто не знає під цим написанням"),
        "en": ("`speakers` are diarized voices: {diarized} recordings out of {total_tx}. "
               "The rest of the archive is chat, where the author is known exactly and is counted in "
               "`tg_senders` (no seconds or spoken words). The layers use different units, "
               "do not add them up. A shared person is visible via `entity_id`: it "
               "is set for {linked} of {senders} senders, "
               "the graph simply does not know the rest under this spelling"),
    },
    "settings.speakers.err.use_ui": {
        "uk": "Ця дія доступна лише в інтерфейсі",
        "en": "This action is available only in the UI",
    },
    "settings.speakers.err.speaker_id_required": {
        "uk": "Потрібен speaker_id",
        "en": "speaker_id is required",
    },
    "settings.speakers.err.is_self": {
        "uk": "Не можна перейменувати власника (себе)",
        "en": "The owner (self) speaker cannot be renamed",
    },
}
