"""Settings-backed config write endpoints (REMEDIATION_PLAN Волна 3, T5.1 + T5.2).

Two things a non-technical user must be able to do from Settings без
редагування ``.env`` руками:

  1. **T5.1** — вставити Anthropic API-ключ. Розблоковує polish/enrich/
     summarize/RAG-чат (усе, що йде через ``app.services.text_polishing`` /
     ``app.services.rag``, обидва читають ``os.environ["ANTHROPIC_API_KEY"]``).
  2. **T5.2** — увімкнути/вимкнути Co-pilot. Замінює стару
     developer-інструкцію «додайте COPILOT_ENABLED=1 у .env і перезапустіть»
     (``settings.js`` до цієї задачі).

Обидва пишуться у ``.env`` через ``python-dotenv``'s ``set_key`` — вона
чіпає ЛИШЕ названий ключ (решта файлу лишається як є). Це НЕ «записати
довільну env-змінну»: ендпоінти жорстко прибиті до ДВОХ конкретних імен
(``ANTHROPIC_API_KEY``, ``COPILOT_ENABLED``) — немає generic
``{key, value}``-body ендпоінта, яким клієнт міг би переписати будь-що
(``PYTHONPATH``, ``RECALL_API_KEY``, шлях до БД тощо). Див. ризик-нотатку в
REMEDIATION_PLAN T5.1.

**ANTHROPIC_API_KEY застосовується одразу, без рестарту.** Клієнт Claude у
``text_polishing._get_client()`` будується ліниво з ``os.environ`` при
першому виклику і мемоізується ЛИШЕ при успіху (немає ключа → RuntimeError,
``_client`` лишається ``None`` → наступний виклик пробує знову). Тож
записавши свіжий ключ у ``os.environ`` одразу після збереження, наступний-
таки polish/enrich viклик підхопить його без рестарту процесу.

**COPILOT_ENABLED softly-apply НЕ можна.** ``copilot_service`` створюється
ОДИН РАЗ при старті застосунку з ``cfg.COPILOT_ENABLED`` (``app.py`` ~808) —
рантайм-шляху ретроактивно створити/знищити його немає. Тому тумблер тут
чесно повертає ``restart_required: true`` і НІКОЛИ не бреше, що ввімкнулось
без перезапуску (REMEDIATION_PLAN T5.2 risk note).

Ключ ніколи не логується і не повертається назад клієнту (тільки
``configured: bool``) — див. ``get_anthropic_key_status``.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request


logger = logging.getLogger(__name__)
settings_bp = Blueprint('settings_api', __name__)

# sk-ant-api03-... — реальні ключі значно довші, але не прибиваємо цвяхами
# точний підформат (може змінитись); просто «схоже на ключ Anthropic»,
# без пробілів/переносів рядків (щоб не можна було injection-ом дописати
# зайвий рядок у .env через значення).
_KEY_RE = re.compile(r'^sk-ant-[A-Za-z0-9_\-]{8,300}$')


def _env_path() -> Path:
    """Шлях до .env у корені проєкту (app.root_path — там, де app.py)."""
    return Path(current_app.root_path) / '.env'


def _set_env_var(name: str, value: str) -> None:
    """Записати ОДНУ конкретну env-змінну в .env + оновити os.environ поточного
    процесу. ``name`` завжди приходить з жорстко прибитого виклику нижче —
    НІКОЛИ з request-body, щоб цей хелпер не перетворився на generic
    "напиши будь-яку env-змінну" примітив."""
    from dotenv import set_key
    path = _env_path()
    path.touch(exist_ok=True)  # set_key вимагає існуючого файлу
    set_key(str(path), name, value, quote_mode='always')
    os.environ[name] = value


def _unset_env_var(name: str) -> None:
    from dotenv import unset_key
    path = _env_path()
    if path.exists():
        unset_key(str(path), name)
    os.environ.pop(name, None)


# ---------------------------------------------------------------- T5.1: Anthropic API key

@settings_bp.route('/api/settings/anthropic-key/status', methods=['GET'])
def anthropic_key_status():
    """Чи налаштований ключ. НІКОЛИ не повертає сам ключ — лише bool."""
    from app.services import text_polishing
    return jsonify({'configured': text_polishing.is_available()})


@settings_bp.route('/api/settings/anthropic-key', methods=['POST'])
def save_anthropic_key():
    """Зберегти ANTHROPIC_API_KEY у .env + застосувати одразу (без рестарту).

    Body: {"api_key": "sk-ant-..."}. Валідація — лише формат (щоб відсіяти
    очевидний мусор/paste-помилку), не перевіряє валідність проти Anthropic
    API (для цього є /test окремо).
    """
    data = request.get_json(silent=True) or {}
    raw = data.get('api_key')
    if not isinstance(raw, str):
        return jsonify({'success': False, 'error': 'api_key мусить бути рядком'}), 400
    key = raw.strip()
    if not key:
        return jsonify({'success': False, 'error': 'Порожній ключ'}), 400
    if not _KEY_RE.match(key):
        return jsonify({
            'success': False,
            'error': 'Не схоже на ключ Anthropic (має починатись з "sk-ant-", без пробілів/переносів рядків)',
        }), 400

    try:
        _set_env_var('ANTHROPIC_API_KEY', key)
    except Exception as e:
        logger.error("Не вдалось зберегти ANTHROPIC_API_KEY у .env: %s", e)  # NB: помилка, НЕ сам ключ
        return jsonify({'success': False, 'error': 'Не вдалося записати .env'}), 500

    # Скидаємо мемоізований anthropic-клієнт (якщо стара збірка з іншим/
    # відсутнім ключем уже встигла закешуватись) — щоб наступний polish/
    # enrich-виклик одразу побудував клієнт зі свіжим ключем, без рестарту.
    from app.services import text_polishing
    text_polishing._client = None

    logger.info("ANTHROPIC_API_KEY оновлено через Settings (довжина=%d, ключ не логується)", len(key))
    return jsonify({'success': True, 'configured': True})


@settings_bp.route('/api/settings/anthropic-key', methods=['DELETE'])
def clear_anthropic_key():
    """Прибрати ключ (напр. користувач вирішив не використовувати Claude-фічі)."""
    try:
        _unset_env_var('ANTHROPIC_API_KEY')
    except Exception as e:
        logger.error("Не вдалось прибрати ANTHROPIC_API_KEY з .env: %s", e)
        return jsonify({'success': False, 'error': 'Не вдалося записати .env'}), 500
    from app.services import text_polishing
    text_polishing._client = None
    return jsonify({'success': True, 'configured': False})


@settings_bp.route('/api/settings/anthropic-key/test', methods=['POST'])
def test_anthropic_key():
    """Легкий тестовий виклик Claude (Haiku, коротке повідомлення) —
    перевіряє ЗБЕРЕЖЕНИЙ ключ (з os.environ), не приймає ключ у body (щоб не
    ганяти сирий секрет туди-сюди зайвий раз)."""
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return jsonify({'success': False, 'ok': False, 'error': 'Ключ ще не збережено'}), 400

    try:
        import anthropic
    except ImportError:
        return jsonify({'success': False, 'ok': False, 'error': 'Пакет anthropic не встановлено на сервері'}), 500

    from app.services import models as _models
    from app.services.claude_retry import call_with_retry

    try:
        client = anthropic.Anthropic(api_key=api_key)
        call_with_retry(
            lambda: client.messages.create(
                model=_models.HAIKU_4_5,
                max_tokens=8,
                messages=[{'role': 'user', 'content': 'ping'}],
            ),
            max_retries=1,
            what='settings-test-connection',
        )
        return jsonify({'success': True, 'ok': True})
    except anthropic.AuthenticationError:
        return jsonify({'success': True, 'ok': False, 'error': 'Ключ недійсний (401)'})
    except anthropic.PermissionDeniedError:
        return jsonify({'success': True, 'ok': False, 'error': 'Ключ без дозволу (403) — перевірте план/ліміти акаунту'})
    except Exception as e:
        # Мережа/timeout/несподіване — не витягуємо саму помилку SDK у сирому
        # вигляді (може містити частини запиту), лише коротке повідомлення.
        logger.warning("test_anthropic_key: помилка з'єднання: %s", e)
        return jsonify({'success': True, 'ok': False, 'error': 'Не вдалося з’єднатися з Anthropic API'})


# ---------------------------------------------------------------- T5.2: Co-pilot toggle

@settings_bp.route('/api/settings/copilot/toggle', methods=['POST'])
def toggle_copilot():
    """Записати COPILOT_ENABLED у .env. НЕ змінює поточний рантайм —
    ``copilot_service`` вже створено (або ні) при старті процесу і не може
    бути ретроактивно перестворено; відповідь чесно каже restart_required.
    """
    data = request.get_json(silent=True) or {}
    enabled = data.get('enabled')
    if not isinstance(enabled, bool):
        return jsonify({'success': False, 'error': 'enabled мусить бути true/false'}), 400

    try:
        _set_env_var('COPILOT_ENABLED', '1' if enabled else '0')
    except Exception as e:
        logger.error("Не вдалось записати COPILOT_ENABLED у .env: %s", e)
        return jsonify({'success': False, 'error': 'Не вдалося записати .env'}), 500

    from app import state
    live_now = state.copilot_service is not None
    return jsonify({
        'success': True,
        'requested_enabled': enabled,
        'live_enabled': live_now,
        'restart_required': live_now != enabled,
    })
