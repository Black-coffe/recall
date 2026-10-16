"""Server text catalog: embeddings area (record title/description, title suggestions)."""
MESSAGES: dict = {
    # --- record title / description validation (record_meta) ---
    "svc_embed.meta.title.not_text": {
        "uk": "Назва має бути текстом або порожнім значенням",
        "en": "The title must be text or empty",
    },
    "svc_embed.meta.title.too_long": {
        "uk": "Назва задовга: максимум {limit} символів",
        "en": "The title is too long: at most {limit} characters",
    },
    "svc_embed.meta.description.not_text": {
        "uk": "Опис має бути текстом або порожнім значенням",
        "en": "The description must be text or empty",
    },
    "svc_embed.meta.description.too_long": {
        "uk": "Опис задовга: максимум {limit} символів",
        "en": "The description is too long: at most {limit} characters",
    },
    "svc_embed.meta.record_fallback": {
        "uk": "Запис #{id}",
        "en": "Record #{id}",
    },
    # --- title / description suggestions from the local model (meta_suggest) ---
    "svc_embed.suggest.not_found": {
        "uk": "Транскрибування не знайдено",
        "en": "Transcription not found",
    },
    "svc_embed.suggest.no_text": {
        "uk": "У запису ще нема тексту транскрипту",
        "en": "The record has no transcript text yet",
    },
    "svc_embed.suggest.model_unavailable": {
        "uk": "Локальна модель недоступна: {reason}",
        "en": "The local model is unavailable: {reason}",
    },
    "svc_embed.suggest.model_failed": {
        "uk": "Локальна модель не відповіла: {error}",
        "en": "The local model did not respond: {error}",
    },
    "svc_embed.suggest.empty": {
        "uk": "Модель повернула порожні варіанти — спробуйте ще раз",
        "en": "The model returned empty variants - try again",
    },
}
