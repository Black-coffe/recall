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
- ``GET    /api/recording/<sid>/stream``  — SSE: level/status/chunk_saved/error.
- ``GET    /api/recordings/active``       — поточна active session (для reattach).

Якщо ``state.recording_service is None`` (RECORDING_ENABLED=False
або ImportError) — кожен endpoint повертає 503.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from flask import Blueprint, Response, current_app, g, jsonify, request, stream_with_context

from app import state
from app.services.recording.service import (
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
    М'яко — будь-яка помилка лише логується, не впливає на stop/discard запису."""
    # Спершу зупинити фоновий топік-трекер (Крок 2), тоді закрити сесію.
    if state.copilot_worker is not None:
        try:
            state.copilot_worker.stop(recording_session_id, wait=False)
        except Exception:
            logger.debug(
                "copilot_worker.stop failed for %s (best-effort, не блокує stop/discard)",
                recording_session_id, exc_info=True,
            )
    if state.copilot_service is None:
        return
    try:
        cs = state.copilot_service.get_by_recording(recording_session_id)
        if cs and cs.get('status') != 'ended':
            state.copilot_service.end(cs['id'])
    except Exception as e:
        logger.warning("copilot end failed for %s: %s", recording_session_id, e)


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
    err = _require_service()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    mic_idx = data.get('mic_device_index')
    sys_idx = data.get('system_device_index')
    # Мова для live-preview transcribe. UI шле значення глобального
    # #languageSelect ('uk'/'ru'/'en'/'auto'). None / 'auto' / '' → auto-detect.
    live_lang = data.get('language')
    if isinstance(live_lang, str):
        live_lang = live_lang.strip() or None
    else:
        live_lang = None

    if mic_idx is None and sys_idx is None:
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
            video_spec=video_spec,
        )
    except SessionConflictError as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'error_code': 'SESSION_CONFLICT',
            'active_session_id': state.recording_service.active_session_id,
        }), 409
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
            for stream_name, idx in (('mic', mic_idx), ('system', sys_idx)):
                if idx is None:
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
    copilot_session_id = None
    copilot_cfg = data.get('copilot')
    if copilot_cfg is not None and state.copilot_service is not None:
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
        except Exception as e:
            logger.warning("copilot start failed: %s", e)

    return jsonify({
        'success': True,
        'session_id': sid,
        'sse_url': f'/api/recording/{sid}/stream',
        'state_url': f'/api/recording/{sid}/state',
        'copilot_session_id': copilot_session_id,
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
    except SessionConflictError as e:
        return jsonify({'success': False, 'error': str(e), 'error_code': 'WRONG_SESSION'}), 409
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
    except SessionConflictError as e:
        return jsonify({'success': False, 'error': str(e), 'error_code': 'WRONG_SESSION'}), 409
    return jsonify({'success': True, 'session_id': session_id, 'status': 'recording'})


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
        state.recording_service.stop(session_id, name=name)
    except SessionNotFoundError as e:
        return jsonify({'success': False, 'error': str(e), 'error_code': 'NOT_FOUND'}), 404
    except SessionConflictError as e:
        return jsonify({'success': False, 'error': str(e), 'error_code': 'WRONG_SESSION'}), 409
    except Exception as e:
        logger.exception("stop_recording crashed: %s", e)
        return _internal_error_response('INTERNAL')

    # Phase 12.26: Stop live transcribe worker.
    # wait=True: дожидаемся завершения worker-потоков, чтобы они не дёргали GPU
    # (faster-whisper) в момент, когда сразу после stop пойдёт полный transcribe
    # с переключением модели.
    if state.live_transcribe_worker is not None:
        try:
            state.live_transcribe_worker.stop(session_id, wait=True)
        except Exception as e:
            logger.warning("live_transcribe stop failed: %s", e)

    # Phase 19: завершити копілот-сесію цього запису (якщо була).
    _end_copilot_for(session_id)

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
    """Повертає поточну active session (якщо є). Використовується UI на
    init — щоб reattach до триваючого запису."""
    err = _require_service()
    if err:
        return err
    sid = state.recording_service.active_session_id
    if sid is None:
        return jsonify({'success': True, 'active': False, 'session_id': None})
    snapshot = state.recording_service.get_state(sid)
    return jsonify({
        'success': True,
        'active': True,
        'session_id': sid,
        'state': snapshot,
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
