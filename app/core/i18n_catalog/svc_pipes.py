"""Server text catalog: area svc_pipes (comment errors, video vision availability reasons, visual timeline markdown)."""
MESSAGES: dict = {
    "svc_pipes.comments.err.empty_key": {
        "uk": "порожній ключ цілі",
        "en": "empty target key",
    },
    "svc_pipes.comments.err.target_numeric_got": {
        "uk": "ціль {target_type} адресується числом, отримано {target_id!r}",
        "en": "target {target_type} is addressed by a number, got {target_id!r}",
    },
    "svc_pipes.comments.err.target_numeric": {
        "uk": "ціль {target_type} адресується числом",
        "en": "target {target_type} is addressed by a number",
    },
    "svc_pipes.comments.err.unknown_target": {
        "uk": "невідомий тип цілі: {target_type!r} (відомі: {known})",
        "en": "unknown target type: {target_type!r} (known: {known})",
    },
    "svc_pipes.comments.err.unknown_target_to": {
        "uk": "невідомий тип цілі: {to_type!r}",
        "en": "unknown target type: {to_type!r}",
    },
    "svc_pipes.comments.err.unknown_kind": {
        "uk": "невідомий тип коментаря: {kind!r} (відомі: {known})",
        "en": "unknown comment kind: {kind!r} (known: {known})",
    },
    "svc_pipes.comments.err.unknown_kind_short": {
        "uk": "невідомий тип коментаря: {kind!r}",
        "en": "unknown comment kind: {kind!r}",
    },
    "svc_pipes.comments.err.unknown_source": {
        "uk": "невідоме джерело: {source!r}",
        "en": "unknown source: {source!r}",
    },
    "svc_pipes.comments.err.empty_body": {
        "uk": "порожній коментар",
        "en": "empty comment",
    },
    "svc_pipes.comments.err.too_long": {
        "uk": "коментар довший за {max} символів",
        "en": "comment is longer than {max} characters",
    },
    "svc_pipes.comments.err.no_card": {
        "uk": "немає картки {target_type}#{target_id}",
        "en": "no card {target_type}#{target_id}",
    },
    "svc_pipes.comments.reason.no_transcript": {
        "uk": "{target_type}#{target_id} без транскрипта",
        "en": "{target_type}#{target_id} has no transcript",
    },
    "svc_pipes.vision.off": {
        "uk": "вимкнено (VIDEO_VISION_BACKEND=off)",
        "en": "disabled (VIDEO_VISION_BACKEND=off)",
    },
    "svc_pipes.vision.noKey": {
        "uk": "немає ANTHROPIC_API_KEY (Claude vision вимкнено)",
        "en": "no ANTHROPIC_API_KEY (Claude vision disabled)",
    },
    "svc_pipes.vision.noSdk": {
        "uk": "anthropic SDK не встановлено (pip install anthropic)",
        "en": "anthropic SDK is not installed (pip install anthropic)",
    },
    "svc_pipes.vision.localLlmUnavailable": {
        "uk": "local_llm недоступний",
        "en": "local_llm is unavailable",
    },
    "svc_pipes.vision.okLocal": {
        "uk": "OK (локальна VL: {model})",
        "en": "OK (local VL: {model})",
    },
    "svc_pipes.vision.modelMissing": {
        "uk": "VL-модель '{model}' не знайдена в Ollama. Виконайте: ollama pull {model}",
        "en": "VL model '{model}' not found in Ollama. Run: ollama pull {model}",
    },
    "svc_pipes.timeline.hint.notAnalyzed": {
        "uk": "спершу video_analyze — нарізка ще не робилась",
        "en": "run video_analyze first — the frames have not been cut yet",
    },
    "svc_pipes.timeline.md.title": {
        "uk": "# Візуальна стрічка — {title} (#{tid})",
        "en": "# Visual timeline — {title} (#{tid})",
    },
    "svc_pipes.timeline.md.frame": {
        "uk": "кадр",
        "en": "frame",
    },
    "svc_pipes.timeline.md.repeat": {
        "uk": "_Повтор екрана з кадру #{frame}._",
        "en": "_Screen repeat of frame #{frame}._",
    },
    "svc_pipes.timeline.md.onScreen": {
        "uk": "На екрані: {text}",
        "en": "On screen: {text}",
    },
    "svc_pipes.timeline.md.description": {
        "uk": "Опис: {text}",
        "en": "Description: {text}",
    },
    "svc_pipes.timeline.md.spoken": {
        "uk": "Говорили:",
        "en": "Spoken:",
    },
    "svc_pipes.timeline.md.next": {
        "uk": "_Далі: offset={offset} (усього {total})._",
        "en": "_Next: offset={offset} (total {total})._",
    },
}
