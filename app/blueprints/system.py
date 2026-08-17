"""System & meta endpoints (Phase 5.5).

- /                          (index template)
- /api/health
- /api/models
- /api/download_model
- /api/system_info
- /api/system_stats
- /api/polish/availability
- /api/metrics
"""
import io
import json
import logging
import os
import platform
import re
import subprocess
import zipfile
from datetime import datetime, timezone

import torch
from flask import Blueprint, Response, abort, current_app, jsonify, render_template, request

from app import state
from app.services import text_polishing
from app.services.models import get_default_model
from app.utils.proc import NO_WINDOW


logger = logging.getLogger(__name__)
system_bp = Blueprint('system', __name__)

# Спільне з /api/health — одна версія на весь blueprint (T8.1).
APP_VERSION = "4.0.0"


@system_bp.route('/', defaults={'_path': ''}, methods=['GET'])
@system_bp.route('/<path:_path>', methods=['GET'])
def app_shell(_path):
    """Новий клієнтський каркас (shell). Віддається на будь-який не-API шлях,
    щоб клієнтський роутер міг обробити deep-links на холодному старті
    (напр. /transcript/2125-..., /entities/5).

    Точніші правила (Flask static, конкретні /api/... маршрути, /favicon.ico,
    /sw.js) мають вищий пріоритет за специфічністю і матчаться першими.
    Guard на 'api/' — страхувальна сітка: невідомий /api/... має повертати
    JSON-404, а не HTML-shell (інакше fetch-споживачі мовчки ламаються)."""
    if _path.startswith('api/'):
        abort(404)
    return render_template('shell.html')


@system_bp.route('/favicon.ico')
def favicon():
    """Favicon з static/ папки — usnuvaye 404 при default GET."""
    from flask import send_from_directory
    return send_from_directory(
        os.path.join(current_app.root_path, 'static'),
        'favicon.ico',
        mimetype='image/x-icon',
    )


@system_bp.route('/sw.js')
def service_worker():
    """Service worker — мусить віддаватись з кореня, щоб scope був '/'.
    Якщо віддавати з /static/sw.js — scope обмежується '/static/*'.
    Альтернатива: header Service-Worker-Allowed: /, але прямий маршрут чистіший.
    """
    from flask import send_from_directory
    resp = send_from_directory(
        os.path.join(current_app.root_path, 'static'),
        'sw.js',
        mimetype='application/javascript',
    )
    # Не кешувати самого SW — щоб оновлення коду SW долетіли швидко.
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Service-Worker-Allowed'] = '/'
    return resp


@system_bp.route('/api/health', methods=['GET'])
def health_check():
    """Стан системи."""
    health = {
        "status": "ok",
        "version": APP_VERSION,
        "ffmpeg_available": state.ffmpeg_available,
        "gpu_available": torch.cuda.is_available(),
        "database": "ok",
    }
    try:
        from app.db.connection import get_db_connection
        with get_db_connection(current_app.config['DATABASE']) as conn:
            conn.execute('SELECT 1')
    except Exception:
        health["database"] = "error"
        health["status"] = "degraded"
    if not state.ffmpeg_available:
        health["status"] = "degraded"
    return jsonify(health)


@system_bp.route('/api/models', methods=['GET'])
def get_models():
    """Список Whisper моделей."""
    return jsonify(state.whisper_manager.get_available_models())


@system_bp.route('/api/download_model', methods=['POST'])
def download_model():
    """Завантажити модель."""
    data = request.json
    model_name = data.get('model_name')
    if not model_name:
        return jsonify({"success": False, "error": "Не вказано модель"}), 400
    if state.whisper_manager.download_model(model_name):
        return jsonify({"success": True, "message": f"Модель {model_name} успішно завантажена"})
    return jsonify({"success": False, "error": "Помилка завантаження моделі"}), 500


@system_bp.route('/api/models/update-status', methods=['GET'])
def models_update_status():
    """Кешований статус оновлення faster-whisper (без мережі). Заповнюється
    фоновим стартовим чеком (раз на ~2 тижні). Header показує рядок, якщо є
    новіша версія (= можливі нові моделі)."""
    from app.services import model_updates
    path = current_app.config.get('MODEL_UPDATE_STATE', 'model_update_state.json')
    return jsonify(model_updates.read_status(path))


@system_bp.route('/api/models/check-updates', methods=['POST'])
def models_check_updates():
    """Примусово перевірити PyPI зараз (кнопка «перевірити» у Налаштуваннях)."""
    from app.services import model_updates
    path = current_app.config.get('MODEL_UPDATE_STATE', 'model_update_state.json')
    return jsonify(model_updates.check_now(path))


@system_bp.route('/api/system_info', methods=['GET'])
def system_info():
    """Інформація про систему."""
    return jsonify(state.whisper_manager.get_system_info())


# ---- T8.1 (Волна 4): діагностичний пакет ------------------------------
# Простий варіант з REMEDIATION_PLAN (без Sentry): один zip з останніми
# рядками файлових логів трьох процесів (app.py/telegram_listener.py/
# mcp_server.py — див. app/core/logger.py, telegram_listener.py:43-70,
# mcp_server.py:41-74) + system_info + версія/коміт, для ручної відправки
# при жалобі. НІКОЛИ не включає транскрипти/БД/.env — лише короткий
# хвіст логів, пропущений через redaction нижче.
DIAGNOSTIC_LOG_TAIL_LINES = 500

# arcname у zip -> шлях відносно кореня проєкту (current_app.root_path).
# whisper_ui.log — конфігурований (config.LOG_FILE), інші два — фіксовані
# імена окремих процесів (навмисно різні файли, щоб уникнути WinError 32
# при одночасному rollover з кількох процесів — див. коментарі там).
_DIAGNOSTIC_LOG_SOURCES = (
    ("whisper_ui.log", None),  # None -> читається з current_app.config['LOG_FILE']
    ("telegram_listener.log", "telegram_listener.log"),
    ("mcp_server.log", "mcp_server.log"),
)

# Значення, схожі на ключі/токени/хеші — маскуємо перед пакуванням.
# 1) "голі" Anthropic-ключі (sk-ant-...), навіть якщо трапляються поза
#    парою ім'я=значення (напр. у тексті помилки SDK).
_RE_BARE_ANTHROPIC_KEY = re.compile(r'sk-ant-[A-Za-z0-9_\-]{10,}')
# 2) пари ІМ'Я=значення / ІМ'Я: значення, де ІМ'Я виглядає як секрет:
#    *_KEY, *_TOKEN, *_HASH, *_SECRET, *_PASSWORD, а також api_id/api_hash
#    (TELEGRAM_API_ID, TELEGRAM_API_HASH, ANTHROPIC_API_KEY, SECRET_KEY, …).
_RE_SECRET_ASSIGNMENT = re.compile(
    r'(?i)\b([A-Za-z][\w.]*(?:api[_-]?key|api[_-]?hash|api[_-]?id|secret|token|'
    r'password|passwd|_key|_hash)[\w.]*)\s*([:=])\s*(\S+)'
)


def _redact_secrets(line: str) -> str:
    """Маскує значення, схожі на ключі/токени/хеші, в одному рядку логу."""
    line = _RE_BARE_ANTHROPIC_KEY.sub('sk-ant-***REDACTED***', line)
    line = _RE_SECRET_ASSIGNMENT.sub(r'\1\2***REDACTED***', line)
    return line


def _tail_redacted(path: str, max_lines: int) -> str:
    """Останні max_lines рядків файлу, кожен пропущений через redaction."""
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()
    tail = lines[-max_lines:]
    return ''.join(_redact_secrets(l) for l in tail)


def _git_commit_short():
    """Короткий git-коміт, якщо дешево дістати (repo є, git у PATH). None інакше."""
    try:
        res = subprocess.run(
            ['git', 'rev-parse', '--short', 'HEAD'],
            cwd=current_app.root_path, capture_output=True, text=True, timeout=2,
            creationflags=NO_WINDOW,
        )
        if res.returncode == 0:
            return res.stdout.strip() or None
    except Exception:
        pass
    return None


@system_bp.route('/api/system/diagnostic-package', methods=['GET'])
def diagnostic_package():
    """Zip для ручної відправки при жалобі: хвости логів (redacted) +
    system_info + версія/коміт + manifest. Кнопка в Налаштуваннях (T8.1)."""
    buf = io.BytesIO()
    included_logs = []
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for arcname, relpath in _DIAGNOSTIC_LOG_SOURCES:
            relpath = relpath or current_app.config.get('LOG_FILE', 'whisper_ui.log')
            path = relpath if os.path.isabs(relpath) else os.path.join(current_app.root_path, relpath)
            if not os.path.isfile(path):
                continue
            try:
                content = _tail_redacted(path, DIAGNOSTIC_LOG_TAIL_LINES)
            except OSError as e:
                logger.warning(f"Не вдалося прочитати лог {path} для діагностичного пакету: {e}")
                continue
            zf.writestr(f"logs/{arcname}", content)
            included_logs.append(arcname)

        try:
            sys_info = state.whisper_manager.get_system_info() if state.whisper_manager else {}
        except Exception as e:
            sys_info = {"error": str(e)}
        zf.writestr("system_info.json", json.dumps(sys_info, ensure_ascii=False, indent=2, default=str))

        manifest = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "app_version": APP_VERSION,
            "git_commit": _git_commit_short(),
            "os": platform.platform(),
            "python_version": platform.python_version(),
            "included_logs": included_logs,
            "log_tail_lines": DIAGNOSTIC_LOG_TAIL_LINES,
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    buf.seek(0)
    filename = f"recall-diagnostic-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    resp = Response(buf.getvalue(), mimetype='application/zip')
    resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp


@system_bp.route('/api/system_stats', methods=['GET'])
def get_system_stats():
    """Snapshot CPU/GPU/RAM (Phase 2: non-blocking, читає з SystemMonitor)."""
    try:
        return jsonify(state.system_monitor.get_snapshot())
    except Exception as e:
        logger.error(f"Помилка отримання статистики: {e}")
        return jsonify({"success": False, "error": "Помилка отримання статистики системи"}), 500


@system_bp.route('/api/polish/availability', methods=['GET'])
def polish_availability():
    """Доступність Claude polish + перелік моделей (Phase 4.4+)."""
    return jsonify({
        "available": text_polishing.is_available(),
        "default_model": get_default_model(),
        "models": [
            {
                "id": "claude-opus-4-8",
                "label": "Opus 4.8",
                "tier": "premium",
                "speed": "повільно (1-3 хв)",
                "cost_hint": "≈ $0.20 / 15K симв.",
                "note": "Найвища якість. Adaptive thinking. Дорого і повільно для звичайної редактури.",
            },
            {
                "id": "claude-sonnet-4-6",
                "label": "Sonnet 4.6",
                "tier": "balanced",
                "speed": "середньо (30-60с)",
                "cost_hint": "≈ $0.10 / 15K симв.",
                "note": "Збалансовано. Підходить для більшості постпроцесингу.",
            },
            {
                "id": "claude-haiku-4-5",
                "label": "Haiku 4.5",
                "tier": "fast",
                "speed": "швидко (10-20с)",
                "cost_hint": "≈ $0.025 / 15K симв.",
                "note": "Швидко і дешево. Без adaptive thinking. Якість 90%+ на простих задачах редактури.",
            },
        ],
    })


@system_bp.route('/api/metrics', methods=['GET'])
def get_metrics():
    """Prometheus-сумісний metrics endpoint (Phase 6.3)."""
    active_jobs = len(state.job_queue.list(active_only=True))
    snap = state.system_monitor.get_snapshot()
    gauges = {
        "whisper_active_jobs": active_jobs,
        "whisper_gpu_memory_gb": snap.get("gpu_memory_used_gb", 0) or 0,
        "whisper_cpu_percent": snap.get("cpu_percent", 0) or 0,
    }
    body = state.metrics.render(gauges=gauges)
    return Response(body, mimetype="text/plain; version=0.0.4; charset=utf-8")
