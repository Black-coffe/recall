"""Background warm-up of the default Whisper model at boot.

Пост-хвильова знахідка 03.07.2026: `/api/transcribe` не тримає жодної
попередньо завантаженої whisper-моделі — вона вантажиться ліниво,
всередині самого HTTP-запиту (`WhisperManager.load_model()`, викликається
з `transcribe_with_progress()`). Перший запит після рестарту (або після
зміни моделі) висить 30-60с+ на "Loading model...", поки триває
завантаження ваг з диска/HF-кешу в RAM/VRAM. Клієнт з розумним HTTP-
таймаутом обриває з'єднання і природно ретраїть — а це і є типове
джерело дублів (див. anti-dup guard у `app/blueprints/transcription.py`,
`transcribe()`).

Фікс: гріємо модель у фоновому daemon-потоці одразу після старту додатку
(паралельно з бутом Flask/blueprint'ів) — до моменту першого реального
запиту модель уже резидентна в кеші `WhisperManager`. Якщо GPU відсутній,
модель не скачана або прогрів упав з винятком — просто WARNING у лог,
додаток лишається придатним до роботи (`/api/transcribe` завантажить
модель по старому лінивому шляху).

Гейтиться `RECALL_PRELOAD_WHISPER` (дефолт '1' — увімкнено); '0'/'false'/
'no' — вимкнути.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

from app.core import settings as _settings


logger = logging.getLogger(__name__)

# Модель за замовчуванням для прогріву. Немає єдиного backend-конфіга для
# "дефолтної" whisper-моделі (config.py WHISPER_MODELS — лише каталог
# usage-довідки для UI) — фактичний дефолт живе у фронтенді
# (static/js/recall/views/{upload,record,audio,settings}.js: `const
# DEFAULT_MODEL = 'large-v3-turbo'`) і повторений тут же у
# `config.py TELEGRAM_WHISPER_MODEL`. Дзеркалимо те саме значення, з
# можливістю перевизначити через env якщо хтось поставить інший дефолт.
DEFAULT_PRELOAD_MODEL = 'large-v3-turbo'


def preload_enabled() -> bool:
    """config-registry-fix-r3-01: читання переведено на `settings.env_bool`
    (реєстр — єдине джерело парсингу). Це задокументоване перевертання
    поведінки на сміттєвих значеннях: старий парсер (`not in ('0','false','no')`)
    трактував будь-яке нерозпізнане значення (напр. 'ага') як True (увімкнено);
    `env_bool` трактує як False (truthy-набір: '1'/'true'/'yes'/'on')."""
    return _settings.env_bool('RECALL_PRELOAD_WHISPER')


def resolve_preload_model() -> str:
    return os.environ.get('RECALL_PRELOAD_WHISPER_MODEL', DEFAULT_PRELOAD_MODEL)


def _warm_up(whisper_manager, model_name: str) -> None:
    t0 = time.time()
    logger.info("Whisper preload: завантажую модель %s у фоні...", model_name)
    try:
        whisper_manager.load_model(model_name)
        logger.info(
            "Whisper preload: модель %s готова за %.1fs",
            model_name, time.time() - t0,
        )
    except Exception as e:
        # Нема GPU / модель не скачана / OOM — не критично, /api/transcribe
        # просто завантажить модель по старому лінивому шляху як раніше.
        logger.warning("Whisper preload: не вдалося прогріти %s: %s", model_name, e, exc_info=True)


def start_background_preload(
    whisper_manager,
    model_name: Optional[str] = None,
) -> Optional[threading.Thread]:
    """Запускає daemon-потік, що прогріває `model_name` (дефолт — env/
    :data:`DEFAULT_PRELOAD_MODEL`). Не блокує виклик.

    Повертає ``None`` (нічого не стартує) якщо:
    - ``whisper_manager`` не створений (falsy);
    - ``RECALL_PRELOAD_WHISPER=0`` (вимкнено).
    """
    if not whisper_manager:
        logger.debug("Whisper preload: пропущено — whisper_manager не створений")
        return None
    if not preload_enabled():
        logger.info("Whisper preload: вимкнено (RECALL_PRELOAD_WHISPER=0)")
        return None

    resolved_model = model_name or resolve_preload_model()
    t = threading.Thread(
        target=_warm_up, args=(whisper_manager, resolved_model),
        name="whisper-preload", daemon=True,
    )
    t.start()
    return t


def start_tracked_preload(whisper_manager, робота: dict) -> Optional[threading.Thread]:
    """Запускає прогрів і водночас веде `робота['whisper']` через увесь
    цикл станів (`loading` → `ready`|`failed`|`disabled`) — раніше цей код
    жив інлайном у `app.py:613-658` (config-registry-fix-r3-01).

    Порядок станів той самий, що був у app.py: `'loading'` виставляється ДО
    старту фонового потоку прогріву, `'ready'`/`'failed'` — лише після
    `.join()` за критерієм членства в кеші моделей (`_m in
    whisper_manager._models`, знахідка 8 попереднього раунду — НЕ
    `current_model_name`, бо це "останній використаний" покажчик, який
    рухає будь-яка паралельна транскрипція з іншою моделлю).

    Повертає потік-спостерігач (`.join()` на ньому чекає завершення і
    прогріву, і оновлення робота) або ``None``, якщо прогрів не
    запускався (вимкнено / немає менеджера / виняток на старті).
    """
    try:
        if not preload_enabled() or not whisper_manager:
            робота['whisper'] = {'preload': 'disabled', 'model': None}
            return None

        model_name = resolve_preload_model()
        робота['whisper'] = {'preload': 'loading', 'model': model_name}
        preload_thread = start_background_preload(whisper_manager, model_name)
        if preload_thread is None:
            робота['whisper'] = {'preload': 'disabled', 'model': None}
            return None

        def _watch(_t=preload_thread, _m=model_name):
            _t.join()
            ok = _m in getattr(whisper_manager, '_models', {})
            робота['whisper'] = {
                'preload': 'ready' if ok else 'failed',
                'model': _m if ok else None,
            }

        watcher = threading.Thread(
            target=_watch, name='whisper-preload-watch', daemon=True,
        )
        watcher.start()
        return watcher
    except Exception as e:
        # T7.4: не голий except — логуємо перед тим, як звести робота до
        # 'failed'; /api/transcribe все одно довантажить модель лінивo.
        logger.warning(
            "Whisper preload: не вдалося запустити відстеження прогріву: %s", e, exc_info=True,
        )
        робота['whisper'] = {'preload': 'failed', 'model': None}
        return None
