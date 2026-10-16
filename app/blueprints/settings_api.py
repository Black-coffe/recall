"""Settings-backed config write endpoints (REMEDIATION_PLAN Wave 3, T5.1 + T5.2).

Two things a non-technical user must be able to do from Settings without
editing ``.env`` by hand:

  1. **T5.1** — paste an Anthropic API key. Unblocks polish/enrich/
     summarize/RAG chat (everything that goes through ``app.services.text_polishing`` /
     ``app.services.rag``, both read ``os.environ["ANTHROPIC_API_KEY"]``).
  2. **T5.2** — turn the Co-pilot on/off. Replaces the old
     developer instruction "add COPILOT_ENABLED=1 to .env and restart"
     (``settings.js`` before this task).

Both are written to ``.env`` via ``python-dotenv``'s ``set_key`` — it
touches ONLY the named key (the rest of the file stays as is). This is NOT "write
an arbitrary env variable": the endpoints are hard-pinned to TWO specific names
(``ANTHROPIC_API_KEY``, ``COPILOT_ENABLED``) — there is no generic
``{key, value}`` body endpoint with which a client could overwrite anything
(``PYTHONPATH``, ``RECALL_API_KEY``, the DB path, etc.). See the risk note in
REMEDIATION_PLAN T5.1.

**ANTHROPIC_API_KEY is applied immediately, without a restart.** The Claude client in
``text_polishing._get_client()`` is built lazily from ``os.environ`` on the
first call and memoized ONLY on success (no key → RuntimeError,
``_client`` stays ``None`` → the next call tries again). So
by writing the fresh key to ``os.environ`` right after saving, the next
polish/enrich call picks it up without restarting the process.

**COPILOT_ENABLED cannot be softly applied.** ``copilot_service`` is created
ONCE at application startup from ``cfg.COPILOT_ENABLED`` (``app.py`` ~808) —
there is no runtime path to retroactively create/destroy it. So the toggle here
honestly returns ``restart_required: true`` and NEVER lies that it took effect
without a restart (REMEDIATION_PLAN T5.2 risk note).

The key is never logged and never returned back to the client (only
``configured: bool``) — see ``get_anthropic_key_status``.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from app.core.i18n import tr, ui_lang_setting


logger = logging.getLogger(__name__)
settings_bp = Blueprint('settings_api', __name__)

# sk-ant-api03-... — real keys are much longer, but we do not nail down
# the exact subformat (it may change); just "looks like an Anthropic key",
# without spaces/line breaks (so that an injection cannot append
# an extra line to .env through the value).
_KEY_RE = re.compile(r'^sk-ant-[A-Za-z0-9_\-]{8,300}$')


def _env_path() -> Path:
    """Path to .env in the project root (app.root_path — where app.py is)."""
    return Path(current_app.root_path) / '.env'


def _set_env_var(name: str, value: str) -> None:
    """Write ONE specific env variable to .env + update os.environ of the current
    process. ``name`` always comes from a hard-pinned call below —
    NEVER from the request body, so this helper does not turn into a generic
    "write any env variable" primitive."""
    from dotenv import set_key
    path = _env_path()
    path.touch(exist_ok=True)  # set_key requires an existing file
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
    """Whether the key is configured. NEVER returns the key itself — only a bool."""
    from app.services import text_polishing
    return jsonify({'configured': text_polishing.is_available()})


@settings_bp.route('/api/settings/anthropic-key', methods=['POST'])
def save_anthropic_key():
    """Save ANTHROPIC_API_KEY to .env + apply immediately (without a restart).

    Body: {"api_key": "sk-ant-..."}. Validation is format only (to weed out
    obvious garbage / a paste mistake), it does not check validity against the Anthropic
    API (there is a separate /test for that).
    """
    data = request.get_json(silent=True) or {}
    raw = data.get('api_key')
    if not isinstance(raw, str):
        return jsonify({'success': False, 'error': tr('core.settings.api_key_not_string')}), 400
    key = raw.strip()
    if not key:
        return jsonify({'success': False, 'error': tr('core.settings.api_key_empty')}), 400
    if not _KEY_RE.match(key):
        return jsonify({
            'success': False,
            'error': tr('core.settings.api_key_format'),
        }), 400

    try:
        _set_env_var('ANTHROPIC_API_KEY', key)
    except Exception as e:
        logger.error("Failed to save ANTHROPIC_API_KEY to .env: %s", e)  # NB: the error, NOT the key itself
        return jsonify({'success': False, 'error': tr('core.settings.env_write_failed')}), 500

    # Reset the memoized anthropic client (if an old build with a different/
    # missing key has already managed to get cached) — so the next polish/
    # enrich call immediately builds a client with the fresh key, without a restart.
    from app.services import text_polishing
    text_polishing._client = None

    logger.info("ANTHROPIC_API_KEY updated via Settings (length=%d, the key is not logged)", len(key))
    return jsonify({'success': True, 'configured': True})


@settings_bp.route('/api/settings/anthropic-key', methods=['DELETE'])
def clear_anthropic_key():
    """Remove the key (e.g. the user decided not to use Claude features)."""
    try:
        _unset_env_var('ANTHROPIC_API_KEY')
    except Exception as e:
        logger.error("Failed to remove ANTHROPIC_API_KEY from .env: %s", e)
        return jsonify({'success': False, 'error': tr('core.settings.env_write_failed')}), 500
    from app.services import text_polishing
    text_polishing._client = None
    return jsonify({'success': True, 'configured': False})


@settings_bp.route('/api/settings/anthropic-key/test', methods=['POST'])
def test_anthropic_key():
    """A light test call to Claude (Haiku, a short message) —
    checks the SAVED key (from os.environ), does not accept a key in the body (so as not to
    shuttle a raw secret back and forth needlessly)."""
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return jsonify({'success': False, 'ok': False, 'error': tr('core.settings.key_not_saved')}), 400

    try:
        import anthropic
    except ImportError:
        return jsonify({'success': False, 'ok': False, 'error': tr('core.settings.anthropic_missing')}), 500

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
        return jsonify({'success': True, 'ok': False, 'error': tr('core.settings.key_invalid')})
    except anthropic.PermissionDeniedError:
        return jsonify({'success': True, 'ok': False, 'error': tr('core.settings.key_forbidden')})
    except Exception as e:
        # Network/timeout/unexpected — we do not pull the SDK error itself out in raw
        # form (it may contain parts of the request), only a short message.
        logger.warning("test_anthropic_key: connection error: %s", e)
        return jsonify({'success': True, 'ok': False, 'error': tr('core.settings.anthropic_unreachable')})


# ---------------------------------------------------------------- T5.2: Co-pilot toggle

@settings_bp.route('/api/settings/copilot/toggle', methods=['POST'])
def toggle_copilot():
    """Write COPILOT_ENABLED to .env. Does NOT change the current runtime —
    ``copilot_service`` has already been created (or not) at process start and cannot
    be retroactively recreated; the response honestly says restart_required.
    """
    data = request.get_json(silent=True) or {}
    enabled = data.get('enabled')
    if not isinstance(enabled, bool):
        return jsonify({'success': False, 'error': tr('core.settings.enabled_not_bool')}), 400

    try:
        _set_env_var('COPILOT_ENABLED', '1' if enabled else '0')
    except Exception as e:
        logger.error("Failed to write COPILOT_ENABLED to .env: %s", e)
        return jsonify({'success': False, 'error': tr('core.settings.env_write_failed')}), 500

    from app import state
    live_now = state.copilot_service is not None
    return jsonify({
        'success': True,
        'requested_enabled': enabled,
        'live_enabled': live_now,
        'restart_required': live_now != enabled,
    })


# ---------------------------------------------------------------- UI language

@settings_bp.route('/api/settings/ui-lang', methods=['GET'])
def get_ui_lang():
    return jsonify({'success': True, 'value': ui_lang_setting()})


@settings_bp.route('/api/settings/ui-lang', methods=['POST'])
def set_ui_lang():
    """Write RECALL_UI_LANG (auto|uk|en) to .env; takes effect immediately (read from environ on every request)."""
    data = request.get_json(silent=True) or {}
    value = data.get('value')
    if value not in ('auto', 'uk', 'en'):
        return jsonify({'success': False, 'error': tr('core.settings.ui_lang_invalid')}), 400
    try:
        _set_env_var('RECALL_UI_LANG', value)
    except Exception as e:
        logger.error("Failed to write RECALL_UI_LANG to .env: %s", e)
        return jsonify({'success': False, 'error': tr('core.settings.env_write_failed')}), 500
    return jsonify({'success': True, 'value': value})
