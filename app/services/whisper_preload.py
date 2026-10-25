"""Background warm-up of the default Whisper model at boot.

Post-wave finding 03.07.2026: `/api/transcribe` does not hold any
pre-loaded whisper model — it is loaded lazily,
inside the HTTP request itself (`WhisperManager.load_model()`, called
from `transcribe_with_progress()`). The first request after a restart (or after
a model change) hangs for 30-60s+ on "Loading model...", while
the weights load from disk/HF cache into RAM/VRAM. A client with a sensible HTTP
timeout drops the connection and naturally retries — and that is the typical
source of duplicates (see the anti-dup guard in `app/blueprints/transcription.py`,
`transcribe()`).

Fix: warm up the model in a background daemon thread right after the app starts
(in parallel with the boot of Flask/blueprints) — by the time of the first real
request the model is already resident in the `WhisperManager` cache. If there is no GPU,
the model is not downloaded or the warm-up failed with an exception — just a WARNING in the log,
the app stays usable (`/api/transcribe` will load
the model the old lazy way).

Gated by `RECALL_PRELOAD_WHISPER` (default '1' — enabled); '0'/'false'/
'no' — disable.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

from app.core import settings as _settings


logger = logging.getLogger(__name__)

# Default model for warm-up. There is no single backend config for the
# "default" whisper model (config.py WHISPER_MODELS is only a catalog of
# usage hints for the UI) — the actual default lives in the frontend
# (static/js/recall/views/{upload,youtube,record,audio,settings}.js: `const
# DEFAULT_MODEL = 'large-v3'`) and is repeated in the settings registry
# (TELEGRAM_WHISPER_MODEL, RECORDING_AUTOSAVE_MODEL). We mirror the same value,
# with the option to override via env if someone sets a different default.
DEFAULT_PRELOAD_MODEL = 'large-v3'


def preload_enabled() -> bool:
    """config-registry-fix-r3-01: reading moved to `settings.env_bool`
    (the registry is the single source of parsing). This is a documented flip of
    behaviour on garbage values: the old parser (`not in ('0','false','no')`)
    treated any unrecognized value (e.g. 'ага') as True (enabled);
    `env_bool` treats it as False (truthy set: '1'/'true'/'yes'/'on')."""
    return _settings.env_bool('RECALL_PRELOAD_WHISPER')


def resolve_preload_model() -> str:
    return os.environ.get('RECALL_PRELOAD_WHISPER_MODEL', DEFAULT_PRELOAD_MODEL)


def _warm_up(whisper_manager, model_name: str) -> None:
    t0 = time.time()
    logger.info("Whisper preload: loading model %s in the background...", model_name)
    try:
        whisper_manager.load_model(model_name)
        logger.info(
            "Whisper preload: model %s ready in %.1fs",
            model_name, time.time() - t0,
        )
    except Exception as e:
        # No GPU / model not downloaded / OOM — not critical, /api/transcribe
        # will simply load the model the old lazy way as before.
        logger.warning("Whisper preload: failed to warm up %s: %s", model_name, e, exc_info=True)


def start_background_preload(
    whisper_manager,
    model_name: Optional[str] = None,
) -> Optional[threading.Thread]:
    """Starts a daemon thread that warms up `model_name` (default — env/
    :data:`DEFAULT_PRELOAD_MODEL`). Does not block the call.

    Returns ``None`` (nothing starts) if:
    - ``whisper_manager`` is not created (falsy);
    - ``RECALL_PRELOAD_WHISPER=0`` (disabled).
    """
    if not whisper_manager:
        logger.debug("Whisper preload: skipped — whisper_manager is not created")
        return None
    if not preload_enabled():
        logger.info("Whisper preload: disabled (RECALL_PRELOAD_WHISPER=0)")
        return None

    resolved_model = model_name or resolve_preload_model()
    t = threading.Thread(
        target=_warm_up, args=(whisper_manager, resolved_model),
        name="whisper-preload", daemon=True,
    )
    t.start()
    return t


def start_tracked_preload(whisper_manager, робота: dict) -> Optional[threading.Thread]:
    """Starts the warm-up and at the same time drives `робота['whisper']` through the whole
    state cycle (`loading` → `ready`|`failed`|`disabled`) — this code used to
    live inline in `app.py:613-658` (config-registry-fix-r3-01).

    The order of states is the same as it was in app.py: `'loading'` is set BEFORE
    the background warm-up thread starts, `'ready'`/`'failed'` — only after
    `.join()` by the model-cache membership criterion (`_m in
    whisper_manager._models`, finding 8 of the previous round — NOT
    `current_model_name`, because that is a "last used" pointer that is
    moved by any parallel transcription with a different model).

    Returns the watcher thread (`.join()` on it waits for both the warm-up
    and the робота update to finish) or ``None`` if the warm-up
    was not started (disabled / no manager / exception at start).
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
        # T7.4: not a bare except — log before reducing робота to
        # 'failed'; /api/transcribe will still load the model lazily.
        logger.warning(
            "Whisper preload: failed to start tracking the warm-up: %s", e, exc_info=True,
        )
        робота['whisper'] = {'preload': 'failed', 'model': None}
        return None
