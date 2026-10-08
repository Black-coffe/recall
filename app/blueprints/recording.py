"""Recording REST + SSE endpoints (Phase 9.5).

Тонкий HTTP-шар над :class:`RecordingService`. Усе heavy-lifting
(audio threads, fsync, finalize) виконує сервіс — тут лише валідація
вхідних параметрів і JSON-відповіді.

Endpoints:

- ``GET    /api/recording/devices``       — список mic + loopback (5s cache).
- ``POST   /api/recording/start``         — нова сесія.
- ``POST   /api/recording/<sid>/pause``   — пауза.
- ``POST   /api/recording/<sid>/resume``  — продовжити.
- ``POST   /api/recording/<sid>/stop``    — стоп + push finalize у фон.
- ``POST   /api/recording/<sid>/discard`` — скасувати без finalize.
- ``GET    /api/recording/<sid>/state``   — snapshot.
- ``GET    /api/recording/<sid>/live-transcript`` — снапшот live-preview сегментів (RAM).
- ``GET    /api/recording/<sid>/stream``  — SSE: level/status/chunk_saved/error.
- ``POST   /api/recording/<sid>/system-source`` — перенацілити системний звук (ADR-026 D3).
- ``GET    /api/recording/apps``          — застосунки, що звучать зараз (C3).
- ``GET    /api/recording/autoprompt``    — підказки дзвінка, що чекають відповіді (K2).
- ``GET    /api/recording/autoprompt/<token>/<action>`` — кнопки сповіщення (одноразово).
- ``POST   /api/recording/autoprompt/<id>/dismiss`` — «Не потрібно» з плашки.
- ``GET    /api/recordings/active``       — усі живі сесії (для reattach).

Якщо ``state.recording_service is None`` (RECORDING_ENABLED=False
або ImportError) — кожен endpoint повертає 503.
"""
from __future__ import annotations

import html
import logging
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

from flask import Blueprint, Response, current_app, g, jsonify, redirect, request, stream_with_context

from app import state
from app.services import record_meta
from app.services.recording.audio_source import SystemSpec, UnsupportedSourceSpecError
from app.services.recording.service import (
    NoSystemStreamError,
    RecordingError,
    SessionConflictError,
    SessionNotFoundError,
)
from app.services.recording.session_store import (
    STATUS_CRASHED,
    STATUS_FINALIZED,
    SessionStoreError,
)


logger = logging.getLogger(__name__)
recording_bp = Blueprint('recording', __name__)


# Cache для list_devices: енумерація WASAPI ~50ms. UI відкриває recorder-panel
# часто — без кешу буде нагрузка. Інвалідація по часу + при start (бо девайси
# можуть з'явитись/зникнути).
_DEVICES_CACHE_TTL = 5.0
_devices_cache: dict = {'ts': 0.0, 'data': []}
_devices_lock = threading.Lock()

# /save чекає на finalize. Зведення довгого запису (PCM→WAV→MP3, ~1 ГБ) для
# годинної розмови триває значно довше за початкові 30с → /save віддавав 504, і
# (у старому коді) запис не реєструвався. Бампнуто до 5 хв. Навіть якщо все одно
# не встигне — finalize-callback авто-зареєструє запис у бібліотеці.
_SAVE_FINALIZE_DEADLINE_SEC = 300.0


# ---------------------------------------------------------------- helpers

def _service_unavailable():
    return jsonify({
        'success': False,
        'error': 'Recording feature вимкнено (RECORDING_ENABLED=False або PyAudioWPatch не встановлено)',
        'error_code': 'RECORDING_DISABLED',
    }), 503


def _require_service():
    """Декоратор-helper — повертає 503 якщо service None, інакше None."""
    if state.recording_service is None:
        return _service_unavailable()
    return None


def _internal_error_response(error_code: str):
    """T7.4: відповідь на неочікуваний (не typed) виняток у recording-ендпоінтах.

    НЕ віддає str(exc) клієнту (могло текти шляхи файлів/деталі стеку) —
    лише error_code + request_id для кореляції з traceback у логах (сам
    traceback уже пішов через logger.exception(...) у виклику вище).
    """
    return jsonify({
        'success': False,
        'error': 'Внутрішня помилка сервера. Спробуйте пізніше.',
        'error_code': error_code,
        'request_id': getattr(g, 'request_id', '-'),
    }), 500


def _end_copilot_for(recording_session_id: str) -> None:
    """Phase 19: завершити копілот-сесію, прив'язану до запису (якщо є).
    Делегує єдиному хелперу копілота (C6); м'яко — не впливає на stop/discard."""
    try:
        from app.blueprints.copilot import end_for_recording
        end_for_recording(recording_session_id)
    except Exception:
        logger.warning("copilot end failed for %s", recording_session_id, exc_info=True)


def _unsupported_spec_response(e: Exception):
    return jsonify({
        'success': False,
        'error': str(e),
        'error_code': 'UNSUPPORTED_SOURCE_SPEC',
    }), 400


def _copilot_active(recording_session_id: str) -> bool:
    """Чи веде копілот цей запис (рядок copilot_sessions ще не 'ended')."""
    if state.copilot_service is None:
        return False
    try:
        cs = state.copilot_service.get_by_recording(recording_session_id)
    except Exception:
        logger.debug("copilot lookup failed for %s", recording_session_id, exc_info=True)
        return False
    return bool(cs and cs.get('status') == 'active')


# ---------------------------------------------------------------- devices

@recording_bp.route('/api/recording/screens', methods=['GET'])
def list_screens():
    """Список моніторів для video-capture з опціональними thumbnails.

    Query params:
    - ``thumbs=1`` — повертає data-uri thumbnail для кожного монітора (~320px).

    Ніколи не повертає 500: при будь-якій помилці (FFmpeg відсутній, ddagrab
    недоступний, Windows < 10) — ``video_available=False, screens=[]``.
    """
    err = _require_service()
    if err:
        return err
    try:
        from app.services.recording import screens as screens_mod
        from app.services.recording import video_probe
        cfg = current_app.config
        ffmpeg = video_probe.ffmpeg_path(cfg)        # may raise VideoUnavailable
        caps = video_probe.probe_capabilities(ffmpeg)
        mons = screens_mod.enumerate_monitors()
        mons = screens_mod.calibrate_output_idx(mons, ffmpeg, caps)
        if request.args.get('thumbs') == '1':
            for m in mons:
                m['thumbnail'] = screens_mod.thumbnail_for(m['output_idx'], ffmpeg)
        video_available = bool(caps.get('nvenc_h264') and caps.get('ddagrab'))
        return jsonify({'success': True, 'video_available': video_available, 'screens': mons})
    except Exception as e:
        logger.warning("list_screens degraded: %s", e)
        return jsonify({'success': True, 'video_available': False, 'screens': []})


@recording_bp.route('/api/recording/screens/<int:monitor_index>/preview', methods=['GET'])
def screen_preview(monitor_index: int):
    """Full-resolution preview image for a single monitor.

    Query params:
    - ``w`` (int, optional): desired width, default 1000, clamped to [320, 2560].

    Response:
    - ``{success, image, monitor_width, monitor_height}``

    Never returns 500 — degrades to ``{success: False, image: null, ...}`` on any error.
    """
    try:
        raw_w = request.args.get('w', 1000)
        try:
            max_w = int(raw_w)
        except (TypeError, ValueError):
            max_w = 1000
        max_w = max(320, min(max_w, 2560))

        from app.services.recording import screens as screens_mod
        from app.services.recording import video_probe
        try:
            ffmpeg = video_probe.ffmpeg_path(current_app.config)
        except Exception:
            logger.debug("ffmpeg_path resolution failed, falling back to default", exc_info=True)
            ffmpeg = None

        result = screens_mod.monitor_preview(monitor_index, ffmpeg=ffmpeg, max_w=max_w)
        return jsonify(result)
    except Exception as e:
        logger.warning('screen_preview degraded for monitor_index=%d: %s', monitor_index, e)
        return jsonify({'success': False, 'image': None, 'monitor_width': 0, 'monitor_height': 0})


@recording_bp.route('/api/recording/devices', methods=['GET'])
def list_devices():
    """Список input-девайсів (mic + loopback) з default-маркерами.

    Cache 5 секунд — UI не повинен дзвонити WASAPI на кожне відкриття
    panel'а. ``?force=1`` примусово оновлює.
    """
    err = _require_service()
    if err:
        return err

    force = request.args.get('force', '0') == '1'
    now = time.time()
    with _devices_lock:
        if not force and (now - _devices_cache['ts']) < _DEVICES_CACHE_TTL:
            return jsonify({
                'success': True,
                'devices': _devices_cache['data'],
                'cached': True,
            })
        try:
            devices = state.recording_service.list_devices()
        except Exception as e:
            logger.exception("list_devices error: %s", e)
            return jsonify({
                'success': False,
                'error': str(e),
                'error_code': 'DEVICE_ENUM_FAILED',
            }), 500
        _devices_cache['ts'] = now
        _devices_cache['data'] = devices

    return jsonify({
        'success': True,
        'devices': devices,
        'cached': False,
    })


# ---------------------------------------------------------------- start

@recording_bp.route('/api/recording/start', methods=['POST'])
def start_recording():
    """Розпочати нову сесію.

    Body (JSON):
    - ``mic_device_index`` (int|null): index з ``/devices``. Null = не пишемо mic.
    - ``system_device_index`` (int|null): index loopback. Null = не пишемо system.
    - ``mic_device_name`` (str, optional): для manifest.
    - ``system_device_name`` (str, optional): для manifest.

    Хоча б один з ``mic_device_index`` / ``system_device_index`` мусить
    бути ненульовим.

    Response:
    - 200 ``{success, session_id, sse_url, state_url}``
    - 400 якщо параметри невалідні
    - 409 якщо вже є активна сесія
    """
    return _start_session(request.get_json(silent=True) or {})


def _start_session(data: dict):
    """Тіло старту — спільне для ``POST /start`` і кнопки «Записати» з підказки
    дзвінка (K2): одна копія логіки, а не дві. Повертає те саме, що маршрут."""
    err = _require_service()
    if err:
        return err

    mic_idx = data.get('mic_device_index')
    sys_idx = data.get('system_device_index')
    # Мова для live-preview transcribe. UI шле значення глобального
    # #languageSelect ('uk'/'ru'/'en'/'auto'). None / 'auto' / '' → auto-detect.
    live_lang = data.get('language')
    if isinstance(live_lang, str):
        live_lang = live_lang.strip() or None
    else:
        live_lang = None

    # ADR-026 D3: system_source {mode, apps}. 'all' (або відсутній) — сьогоднішній
    # шлях через endpoint system_device_index; include/exclude вмикають системний
    # потік і без девайса. Невалідний spec → 400 ДО будь-якого старту.
    sys_spec = None
    raw_source = data.get('system_source')
    if raw_source is not None:
        try:
            sys_spec = SystemSpec.from_dict(raw_source)
        except UnsupportedSourceSpecError as e:
            return _unsupported_spec_response(e)
        if sys_spec.mode == 'all':
            sys_spec = None

    if mic_idx is None and sys_idx is None and sys_spec is None:
        return jsonify({
            'success': False,
            'error': 'Потрібен хоча б один з mic_device_index/system_device_index',
            'error_code': 'NO_STREAMS',
        }), 400

    # Phase 22 (Story S4): optional video-capture spec.
    # A malformed/unavailable video block NEVER causes a 400 or blocks audio —
    # the mic/sys guard above is the only hard requirement.
    video_spec = data.get('video')
    video_summary = {'enabled': False}
    if video_spec and video_spec.get('enabled'):
        try:
            from app.services.recording import screens as screens_mod
            ok, tracks, reason = screens_mod.preflight(video_spec, current_app.config)
            if ok and tracks:
                video_spec = {'enabled': True, 'tracks': tracks}
                # Preserve mode and region in the summary so the supervisor
                # factory receives them when it spreads each track dict.
                _summary_tracks = []
                for t in tracks:
                    st = {'monitor_index': t['monitor_index']}
                    if 'mode' in t:
                        st['mode'] = t['mode']
                    if 'region' in t:
                        st['region'] = t['region']
                    _summary_tracks.append(st)
                video_summary = {
                    'enabled': True,
                    'tracks': _summary_tracks,
                }
            else:
                video_spec = None
                video_summary = {'enabled': False, 'reason': reason or 'video unavailable'}
        except Exception as e:
            logger.warning("video preflight failed, audio-only: %s", e)
            video_spec = None
            video_summary = {'enabled': False, 'reason': 'preflight error'}
    else:
        video_spec = None

    try:
        sid = state.recording_service.start(
            mic_device_index=int(mic_idx) if mic_idx is not None else None,
            system_device_index=int(sys_idx) if sys_idx is not None else None,
            mic_device_name=data.get('mic_device_name'),
            system_device_name=data.get('system_device_name'),
            system_source=sys_spec,
            video_spec=video_spec,
        )
    except SessionConflictError as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'error_code': 'SESSION_LIMIT',
            'active': e.active,
            'limit': e.limit,
        }), 409
    except UnsupportedSourceSpecError as e:
        return _unsupported_spec_response(e)
    except RecordingError as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'error_code': 'RECORDING_START_FAILED',
        }), 400
    except Exception as e:
        logger.exception("start_recording crashed: %s", e)
        return _internal_error_response('INTERNAL')

    # Інвалідація device cache — на наступному GET підвантажимо свіжі
    with _devices_lock:
        _devices_cache['ts'] = 0.0

    # Phase 12.26+12.27: live transcribe + diariz preview.
    # Запускаємо окремий worker per active stream — кожен emit-ує segments
    # з speaker_label ('self' для mic, 'other' для system).
    if state.live_transcribe_worker is not None:
        try:
            manifest = state.recording_service.store.read(sid)
            streams = manifest.get('streams') or {}
            for stream_name, on in (('mic', mic_idx is not None),
                                    ('system', sys_idx is not None or sys_spec is not None)):
                if not on:
                    continue
                stream_meta = streams.get(stream_name) or {}
                pcm_path = state.recording_service.store.pcm_path(sid, stream_name)
                rate = int(stream_meta.get('sample_rate') or 16000)
                ch = int(stream_meta.get('channels') or 1)
                state.live_transcribe_worker.start(
                    session_id=sid,
                    pcm_path=str(pcm_path),
                    sample_rate=rate,
                    channels=ch,
                    stream_name=stream_name,
                    language=live_lang,
                )
        except Exception as e:
            logger.warning("live_transcribe start failed: %s", e)

    # Phase 19 (Co-pilot, Крок 1): якщо UI передав налаштування ко-пілота —
    # створюємо копілот-сесію, прив'язану до запису. На Кроці 1 це лише
    # persist налаштувань (вектор/режим/важливість/бюджет/лише-локально);
    # live-аналіз додається у наступних кроках. М'яко: помилка тут не валить старт.
    # C6: копілот один на процес — зайнятий іншим живим записом → запис уже
    # стартував, відповідаємо 200 з copilot.error_code='COPILOT_BUSY'.
    copilot_session_id = None
    copilot_summary: dict = {'started': False}
    copilot_cfg = data.get('copilot')
    if copilot_cfg is not None and state.copilot_service is not None:
        from app.services.copilot.service import CopilotBusyError
        try:
            res = state.copilot_service.start(settings=copilot_cfg, recording_session_id=sid)
            copilot_session_id = res.get('copilot_session_id')
            # Phase 19 Крок 2: підняти топік-трекер (фоновий потік на ембеддингах).
            if copilot_session_id and state.copilot_worker is not None:
                state.copilot_worker.start(
                    copilot_session_id=copilot_session_id,
                    recording_session_id=sid,
                    config=res.get('config'),
                )
        except CopilotBusyError as e:
            copilot_summary = {'started': False, 'error_code': 'COPILOT_BUSY',
                               'owner_session_id': e.owner_session_id}
        except Exception as e:
            logger.warning("copilot start failed: %s", e)
        if copilot_session_id:
            copilot_summary = {'started': True}

    if sys_spec is not None:
        system_source = sys_spec.to_dict()
    elif sys_idx is not None:
        system_source = SystemSpec('all', device_index=int(sys_idx)).to_dict()
    else:
        system_source = None

    return jsonify({
        'success': True,
        'session_id': sid,
        'sse_url': f'/api/recording/{sid}/stream',
        'state_url': f'/api/recording/{sid}/state',
        'copilot_session_id': copilot_session_id,
        'copilot': copilot_summary,
        'system_source': system_source,
        'video': video_summary,
    })


# ---------------------------------------------------------------- pause/resume/stop/discard

@recording_bp.route('/api/recording/<session_id>/pause', methods=['POST'])
def pause_recording(session_id: str):
    err = _require_service()
    if err:
        return err
    try:
        state.recording_service.pause(session_id)
    except SessionNotFoundError as e:
        return jsonify({'success': False, 'error': str(e), 'error_code': 'NOT_FOUND'}), 404
    return jsonify({'success': True, 'session_id': session_id, 'status': 'paused'})


@recording_bp.route('/api/recording/<session_id>/resume', methods=['POST'])
def resume_recording(session_id: str):
    err = _require_service()
    if err:
        return err
    try:
        state.recording_service.resume(session_id)
    except SessionNotFoundError as e:
        return jsonify({'success': False, 'error': str(e), 'error_code': 'NOT_FOUND'}), 404
    return jsonify({'success': True, 'session_id': session_id, 'status': 'recording'})


_RETARGET_REASONS = ('user', 'auto_restore')


@recording_bp.route('/api/recording/<session_id>/system-source', methods=['POST'])
def set_system_source(session_id: str):
    """Перенацілити системний звук живої сесії (ADR-026 D3, контракт C5).

    Body: ``{mode: 'all'|'include'|'exclude', apps?: [exe], reason?: 'user'|'auto_restore'}``.
    """
    err = _require_service()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    reason = data.get('reason') or 'user'
    if reason not in _RETARGET_REASONS:
        return jsonify({
            'success': False,
            'error': f"reason має бути одним із {list(_RETARGET_REASONS)}",
            'error_code': 'INVALID_REASON',
        }), 400
    try:
        spec = SystemSpec.from_dict({'mode': data.get('mode'), 'apps': data.get('apps')})
    except UnsupportedSourceSpecError as e:
        return _unsupported_spec_response(e)

    try:
        res = state.recording_service.retarget_system(session_id, spec, reason)
    except SessionNotFoundError as e:
        return jsonify({'success': False, 'error': str(e), 'error_code': 'NOT_FOUND'}), 404
    except NoSystemStreamError as e:
        return jsonify({'success': False, 'error': str(e), 'error_code': 'NO_SYSTEM_STREAM'}), 409
    except UnsupportedSourceSpecError as e:
        return _unsupported_spec_response(e)
    except RecordingError as e:
        return jsonify({'success': False, 'error': str(e), 'error_code': 'RETARGET_FAILED'}), 400
    except Exception as e:
        logger.exception("set_system_source crashed: %s", e)
        return _internal_error_response('INTERNAL')

    return jsonify({
        'success': True,
        'session_id': session_id,
        'source': res.get('source'),
        'event': res.get('event'),
        'applied_on_resume': bool(res.get('applied_on_resume', False)),
    })


@recording_bp.route('/api/recording/apps', methods=['GET'])
def list_apps():
    """Застосунки, що звучать зараз, з позначкою, які живі записи їх пишуть (C3).
    Ніколи не 500: будь-яка помилка → ``available: false``."""
    err = _require_service()
    if err:
        return err
    try:
        from app.services.recording.app_audio import list_sounding_apps
        live_specs = {}
        for sid in state.recording_service.active_session_ids():
            try:
                live_specs[sid] = state.recording_service.get_state(sid).get('system_source') or {}
            except Exception:
                logger.debug("apps: state of %s unavailable", sid, exc_info=True)
        # Сесія без системного потоку нічого з динаміків не пише.
        live_specs = {sid: spec for sid, spec in live_specs.items() if spec}
        result = list_sounding_apps(live_specs=live_specs)
    except Exception as e:
        logger.warning("list_apps degraded: %s", e, exc_info=True)
        result = {'available': False, 'reason': 'enumeration error', 'apps': []}
    return jsonify({'success': True, **result})


# ---------------------------------------------------------------- call autoprompt (K2)

def _call_watcher():
    """``CallWatcher`` живе в ``app.extensions`` лише в процесі, що обслуговує запити
    (app.py під ``__main__``, RECORDING_AUTOPROMPT); інакше ``None``."""
    return current_app.extensions.get('call_watcher')


def _autoprompt_page(title: str, text: str, code: int):
    """Маленька сторінка для вкладки, яку відкрила кнопка сповіщення."""
    body = (
        '<!doctype html><html lang="uk"><meta charset="utf-8"><title>Recall</title>'
        '<body style="font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem">'
        f'<h1 style="font-size:1.25rem">{html.escape(title)}</h1><p>{html.escape(text)}</p>'
        '<p><a href="/record">Відкрити запис у Recall</a></p></body></html>'
    )
    return Response(body, status=code, mimetype='text/html')


def _mic_for(endpoint: str | None) -> dict | None:
    """Мікрофон, з якого захоплює сам дзвінок (імʼя endpoint'а його сесії), інакше дефолтний."""
    try:
        mics = [d for d in state.recording_service.list_devices() if d.get('kind') == 'mic']
    except Exception as e:
        logger.warning("autoprompt: device enumeration failed: %s", e, exc_info=True)
        return None
    if endpoint:
        for d in mics:
            if d.get('name') == endpoint:
                return d
        for d in mics:
            name = str(d.get('name') or '')
            if name and (name.startswith(endpoint) or endpoint.startswith(name)):
                return d
    for d in mics:
        if d.get('is_default'):
            return d
    return mics[0] if mics else None


@recording_bp.route('/api/recording/autoprompt', methods=['GET'])
def autoprompt_pending():
    """Підказки дзвінка, що чекають відповіді (для плашки у вкладці, K2)."""
    watcher = _call_watcher()
    if watcher is None:
        return jsonify({'success': True, 'enabled': False, 'prompts': []})
    return jsonify({'success': True, 'enabled': True, 'prompts': watcher.pending()})


@recording_bp.route('/api/recording/autoprompt/<prompt_id>/dismiss', methods=['POST'])
def autoprompt_dismiss(prompt_id: str):
    """«Не потрібно» з плашки: підказка зникає, за цей дзвінок не повторюється."""
    watcher = _call_watcher()
    if watcher is None or not watcher.dismiss(prompt_id):
        return jsonify({'success': False, 'error': 'Підказки вже немає', 'error_code': 'NOT_FOUND'}), 404
    return jsonify({'success': True})


@recording_bp.route('/api/recording/autoprompt/<token>/<action>', methods=['GET'])
def autoprompt_action(token: str, action: str):
    """Кнопки сповіщення Windows відкривають цей URL у браузері. Токен = id підказки,
    одноразовий і живе, поки живе його дзвінок або запис: без нього нічого не
    стартує, тож чужий сайт запис не ввімкне. ``record`` стартує мікрофон + звук
    лише цього застосунку тим самим кодом, що ``POST /start``."""
    watcher = _call_watcher()
    info = watcher.consume(token, action) if watcher is not None else None
    if info is None:
        return _autoprompt_page('Посилання вже не дійсне',
                                'Цю підказку вже використано, або дзвінок закінчився.', 404)
    if action == 'sources':
        query = {'app': info['exe']}
        if info.get('mic_endpoint'):
            query['mic'] = info['mic_endpoint']
        return redirect('/record?' + urlencode(query))
    if action == 'keep':
        return redirect('/record')
    if action == 'stop':
        try:
            stop_session(info['session_id'])
        except SessionNotFoundError:
            return _autoprompt_page('Запис уже зупинено', 'Нічого робити не треба.', 200)
        except Exception as e:
            logger.exception("autoprompt stop crashed: %s", e)
            return _autoprompt_page('Не вдалося зупинити запис', 'Подробиці — у журналі Recall.', 500)
        return redirect('/record')

    body: dict = {'system_source': {'mode': 'include', 'apps': [info['exe']]}}
    mic = _mic_for(info.get('mic_endpoint'))
    if mic is not None:
        body['mic_device_index'] = mic['index']
        body['mic_device_name'] = mic.get('name')
    resp = _start_session(body)
    resp, code = resp if isinstance(resp, tuple) else (resp, resp.status_code)
    payload = resp.get_json(silent=True) or {}
    if payload.get('success'):
        return redirect('/record')
    if payload.get('error_code') == 'SESSION_LIMIT':
        return _autoprompt_page('Запис не почато',
                                f"Уже йде максимум записів ({payload.get('limit')}). Нічого не зупинено.", 409)
    return _autoprompt_page('Запис не почато', payload.get('error') or 'Подробиці — у журналі Recall.', code)


def stop_session(session_id: str, name: str | None = None) -> None:
    """Стоп запису + live-транскрипції й копілота цього запису. Спільне для
    ``POST /stop`` і автостопу K2, який кличе це з фонового потоку (без request-
    контексту). ``SessionNotFoundError`` та інші помилки ``service.stop``
    прокидаються викликачеві."""
    state.recording_service.stop(session_id, name=name)

    # Phase 12.26: Stop live transcribe worker ЦІЄЇ сесії (per-sid): живі воркери
    # іншого запису не чекаємо. Повна транскрипція серіалізується з ними
    # семафором WHISPER_MAX_PARALLEL=1 — їхня лента просто відстане (ADR-026, Ask 10).
    if state.live_transcribe_worker is not None:
        try:
            state.live_transcribe_worker.stop(session_id, wait=True)
        except Exception as e:
            logger.warning("live_transcribe stop failed: %s", e)

    # Phase 19: завершити копілот-сесію цього запису (якщо була).
    _end_copilot_for(session_id)


@recording_bp.route('/api/recording/<session_id>/stop', methods=['POST'])
def stop_recording(session_id: str):
    """Зупинити запис. Body може містити:
    - ``name`` (str|null) — назва запису, інакше використається auto_name.

    Finalize іде у фоні (JobQueue) — UI слідкує через polling /state
    або підписку SSE до приходу status='finalized'.
    """
    err = _require_service()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    name = data.get('name')
    if isinstance(name, str):
        name = name.strip() or None

    try:
        stop_session(session_id, name=name)
    except SessionNotFoundError as e:
        return jsonify({'success': False, 'error': str(e), 'error_code': 'NOT_FOUND'}), 404
    except Exception as e:
        logger.exception("stop_recording crashed: %s", e)
        return _internal_error_response('INTERNAL')

    return jsonify({
        'success': True,
        'session_id': session_id,
        'status': 'stopping',
        'message': 'Finalize пушено у JobQueue. Слідкуйте за status="finalized" через /state або SSE.',
    })


@recording_bp.route('/api/recording/<session_id>/save', methods=['POST'])
def save_recording(session_id: str):
    """Чекає на finalize і додає запис в Audio Library (audio_downloads).

    Body (JSON):
    - ``name`` (str, optional) — назва запису. Якщо порожній — використовується
      manifest.auto_name.

    Чекає до ``_SAVE_FINALIZE_DEADLINE_SEC`` секунд на ``status='finalized'``.
    Якщо за цей час не дочекалися — повертає 504. Якщо session crashed — 500.
    (Серверний finalize-callback однаково авто-реєструє запис у бібліотеці —
    навіть якщо тут 504, запис зʼявиться в Аудіотеці за ~хвилину.)

    Response:
    - 200 ``{success, download_id, file_path, name, duration_sec, file_size,
              recording_session_id}``
    - 404 sid не знайдено
    - 500 crashed або final.mp3 відсутній
    - 504 timeout
    """
    err = _require_service()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    name_input = (data.get('name') or '').strip() if isinstance(data.get('name'), str) else ''
    # editable-title-description-02: опис стоп-екрана — валідуємо одразу,
    # до очікування finalize.
    try:
        description_input = record_meta.normalize_description(data.get('description'))
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    store = state.recording_service.store
    deadline = time.time() + _SAVE_FINALIZE_DEADLINE_SEC
    manifest = None
    while time.time() < deadline:
        try:
            manifest = store.read(session_id)
        except SessionStoreError:
            return jsonify({
                'success': False, 'error': 'Сесія не знайдена',
                'error_code': 'NOT_FOUND',
            }), 404
        status = manifest.get('status')
        if status == STATUS_FINALIZED:
            break
        if status == STATUS_CRASHED:
            return jsonify({
                'success': False,
                'error': 'Сесія крашнулась — фінальний файл недоступний',
                'error_code': 'CRASHED',
            }), 500
        time.sleep(0.4)
    else:
        return jsonify({
            'success': False,
            'error': 'Finalize triває довше очікуваного. Спробуйте /save пізніше.',
            'error_code': 'TIMEOUT',
        }), 504

    final_mp3 = manifest.get('final_mp3_path')
    if not final_mp3 or not Path(final_mp3).is_file():
        return jsonify({
            'success': False,
            'error': 'Final MP3 не створений (можливо порожній запис)',
            'error_code': 'NO_FINAL',
        }), 500

    name = name_input or manifest.get('name') or manifest.get('auto_name') \
        or f'Запис {session_id[:8]}'

    # Persist назву в manifest (для recovery scenarios)
    try:
        store.set_name(session_id, name)
    except SessionStoreError:
        pass
    if description_input is not None:
        try:
            store.set_description(session_id, description_input)
        except SessionStoreError:
            pass
        else:
            manifest['description'] = description_input

    # Реєстрація в audio_downloads через спільний хелпер. Ідемпотентно:
    # серверний finalize-callback вже міг авто-зареєструвати цей запис —
    # тоді отримаємо created=False і (за потреби) оновлену назву. Логіка
    # insert'у єдина для обох шляхів (recording/library.py).
    from app.services.recording.library import register_recording
    db_path = current_app.config['DATABASE']
    try:
        reg = register_recording(db_path, session_id, manifest, name=name)
    except Exception as e:
        logger.exception("register_recording failed: %s", e)
        return _internal_error_response('DB_ERROR')

    if reg is None:
        return jsonify({
            'success': False,
            'error': 'Final MP3 не створений (можливо порожній запис)',
            'error_code': 'NO_FINAL',
        }), 500

    return jsonify({
        'success': True,
        'download_id': reg['download_id'],
        'file_path': reg['file_path'],
        'name': reg['name'],
        'duration_sec': reg['duration_sec'],
        'file_size': reg['file_size'],
        'recording_session_id': session_id,
        'already_registered': not reg['created'],
    })


@recording_bp.route('/api/recording/<session_id>/discard', methods=['POST'])
def discard_recording(session_id: str):
    """Скасувати запис без збереження. Видаляє файли сесії повністю."""
    err = _require_service()
    if err:
        return err
    try:
        state.recording_service.discard(session_id)
    except Exception as e:
        logger.exception("discard_recording crashed: %s", e)
        return _internal_error_response('INTERNAL')
    # Phase 12.26: stop live worker (силою — discard не має finalize)
    if state.live_transcribe_worker is not None:
        try:
            state.live_transcribe_worker.stop(session_id, wait=False)
        except Exception:
            logger.debug(
                "live_transcribe_worker.stop failed for %s (best-effort, discard триває)",
                session_id, exc_info=True,
            )
    # Phase 19: завершити копілот-сесію цього запису (якщо була).
    _end_copilot_for(session_id)
    return jsonify({'success': True, 'session_id': session_id, 'status': 'discarded'})


# ---------------------------------------------------------------- state

@recording_bp.route('/api/recording/<session_id>/state', methods=['GET'])
def get_state_endpoint(session_id: str):
    """Snapshot стану сесії (active або finalized)."""
    err = _require_service()
    if err:
        return err
    try:
        snapshot = state.recording_service.get_state(session_id)
    except SessionNotFoundError as e:
        return jsonify({'success': False, 'error': str(e), 'error_code': 'NOT_FOUND'}), 404
    return jsonify({'success': True, 'state': snapshot})


# --------------------------------------------------------- live-transcript

# Потолок сегментів за один виклик (замір Трека 3: ~72 інсайти за дзвінок,
# з запасом на транскрипт). Contracts C1 (plan.md).
_LIVE_TRANSCRIPT_LIMIT = 400


@recording_bp.route('/api/recording/<session_id>/live-transcript', methods=['GET'])
def get_live_transcript(session_id: str):
    """Снапшот preview-сегментів `LiveTranscribeWorker` (RAM-only).

    Query:
    - ``since_seq`` (int, опц.) — **рекомендований курсор** для
      інкрементального опитування (MCP): повернути лише сегменти з
      ``seq > since_seq``. Номер спільний для обох доріжок (mic/system) і
      росте в порядку фактичного append'у, тому не губить сегменти
      повільнішої доріжки (Story 06). Бери ``next_seq`` з відповіді як
      ``since_seq`` наступного опиту.
    - ``since_sec`` (float, опц.) — фільтр по часу початку сегмента
      (``start > since_sec``). НЕ курсор для опитування: доріжки
      транскрибуються незалежно у своєму темпі, тому сегмент з меншим
      ``start`` може прийти ПІЗНІШЕ за вже відданий і буде втрачений при
      опитуванні по часу.
    """
    err = _require_service()
    if err:
        return err
    try:
        snapshot = state.recording_service.get_state(session_id)
    except SessionNotFoundError as e:
        return jsonify({'success': False, 'error': str(e), 'error_code': 'NOT_FOUND'}), 404

    status = snapshot.get('status')
    is_active = snapshot.get('is_active', False)
    elapsed_seconds = snapshot.get('elapsed_seconds', 0.0)

    def _unavailable(reason: str):
        return jsonify({
            'success': True,
            'session_id': session_id,
            'available': False,
            'reason': reason,
            'is_active': is_active,
            'status': status,
            'elapsed_seconds': elapsed_seconds,
            'segments': [],
            'count': 0,
            'last_sec': None,
            'next_seq': None,
            'truncated': False,
        })

    if state.live_transcribe_worker is None:
        return _unavailable('live-транскрипція вимкнена в цьому інстансі')

    if not state.live_transcribe_worker.is_active(session_id):
        if status == STATUS_FINALIZED:
            return _unavailable(
                'сесія завершена — повний текст доступний через транскрипт запису'
            )
        return _unavailable('live-прев\'ю для цієї сесії недоступне')

    since_sec = request.args.get('since_sec', type=float)
    since_seq = request.args.get('since_seq', type=int)
    # order_by='seq' — курсор рахує порядок append'у, не start (Story 06).
    segments = state.live_transcribe_worker.get_preview(session_id, order_by='seq')
    if since_seq is not None:
        segments = [s for s in segments if s.get('seq', 0) > since_seq]
    if since_sec is not None:
        segments = [s for s in segments if s.get('start', 0) > since_sec]

    truncated = len(segments) > _LIVE_TRANSCRIPT_LIMIT
    if truncated:
        segments = segments[:_LIVE_TRANSCRIPT_LIMIT]

    out_segments = [{
        'start': s.get('start'),
        'end': s.get('end'),
        'text': s.get('text'),
        'speaker_label': s.get('speaker'),
        'stream': s.get('stream'),
        'seq': s.get('seq'),
    } for s in segments]
    last_sec = max((s['start'] for s in out_segments), default=None)
    # next_seq — курсор для наступного опиту. Якщо нових сегментів немає,
    # лишаємо since_seq незмінним (а не max по всій сесії) — інакше усічені
    # `truncated`-сегменти "проскочать" повз клієнта назавжди.
    if out_segments:
        next_seq = out_segments[-1]['seq']
    else:
        next_seq = since_seq if since_seq is not None else 0

    return jsonify({
        'success': True,
        'session_id': session_id,
        'available': True,
        'reason': None,
        'is_active': is_active,
        'status': status,
        'elapsed_seconds': elapsed_seconds,
        'segments': out_segments,
        'count': len(out_segments),
        'last_sec': last_sec,
        'next_seq': next_seq,
        'truncated': truncated,
    })


@recording_bp.route('/api/recordings/recovered', methods=['GET'])
def get_recovered_sessions():
    """Список session_id що були recovery'ні при старті додатку.
    UI показує banner і одразу робить ack — після цього список очищується.
    """
    err = _require_service()
    if err:
        return err
    log = state.recording_recovery_log or []
    # Збираємо details із manifest'у для кожного recovered session
    details = []
    for sid in log:
        try:
            mf = state.recording_service.store.read(sid)
            details.append({
                'session_id': sid,
                'name': mf.get('name') or mf.get('auto_name'),
                'status': mf.get('status'),
                'started_at': mf.get('started_at'),
                'final_mp3_path': mf.get('final_mp3_path'),
                'total_duration_sec': mf.get('total_duration_sec'),
            })
        except Exception:
            logger.debug("recovered session manifest read failed for sid=%s", sid, exc_info=True)
    # Auto-ack: очищаємо log після першого read'у щоб banner не з'являвся знову
    if request.args.get('ack', '1') == '1':
        state.recording_recovery_log = []
    return jsonify({
        'success': True,
        'recovered': details,
        'count': len(details),
    })


@recording_bp.route('/api/recordings/active', methods=['GET'])
def get_active_session():
    """Усі живі сесії (C5). UI на init робить reattach до кожної; MCP обирає
    сесію явно. ``session_id``/``state`` заповнені лише коли сесія рівно одна."""
    err = _require_service()
    if err:
        return err
    sessions = []
    snapshots = {}
    for sid in state.recording_service.active_session_ids():
        try:
            snap = state.recording_service.get_state(sid)
        except SessionNotFoundError:
            continue  # зупинилась між переліком і знімком
        snapshots[sid] = snap
        streams = snap.get('streams') or {}
        sessions.append({
            'session_id': sid,
            'state': snap,  # повний знімок /state — SPA робить reattach по ньому
            'name': snap.get('name'),
            'auto_name': snap.get('auto_name'),
            'elapsed_seconds': snap.get('elapsed_seconds', 0.0),
            'streams': {name: bool((streams.get(name) or {}).get('enabled'))
                        for name in ('mic', 'system')},
            'system_source': snap.get('system_source'),
            'copilot_active': _copilot_active(sid),
        })
    sole = sessions[0]['session_id'] if len(sessions) == 1 else None
    return jsonify({
        'success': True,
        'active': bool(sessions),
        'count': len(sessions),
        'sessions': sessions,
        'session_id': sole,
        'state': snapshots[sole] if sole else None,
    })


# ---------------------------------------------------------------- SSE

@recording_bp.route('/api/recording/<session_id>/stream', methods=['GET'])
def stream_recording_events(session_id: str):
    """SSE-канал для конкретної сесії.

    Events:
    - ``level`` (10Hz): {streams: {mic: {peak, rms}, system: {...}}, elapsed}
    - ``status``: {status: 'recording'|'paused'|'stopping'|'finalized'}
    - ``chunk_saved``: {ts, streams: {mic: bytes, system: bytes}}
    - ``error``: {stream?, message}
    - ``video_status`` (Phase 22): {track_id, monitor_index, status, error?}
    - ``video_stats`` (Phase 22): {track_id, fps, dropped, bytes}
    """
    err = _require_service()
    if err:
        return err
    if state.sse_broker is None:
        return jsonify({'success': False, 'error': 'SSE broker недоступний'}), 503

    channel = f'recording:{session_id}'

    response = Response(
        stream_with_context(state.sse_broker.stream_events(channel)),
        mimetype='text/event-stream',
        direct_passthrough=True,
    )
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    response.headers['Connection'] = 'keep-alive'
    return response
