"""Gmail ingest HTTP API (/api/mail/*): accounts, labels, sync trigger, status.

The app password is read from the request body once, handed to the keyring and the
IMAP login, and never returned, logged or stored anywhere else. IMAP work for sync
runs on the poller thread; the routes here only call ``poller.trigger``.
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify, request

from app.core import settings as _settings
from app.core.i18n import tr
from app.db.connection import get_db_connection
from app.services import mail_accounts as ma
from app.services import connect_links, mail_imap, privacy
from app.services.secret_shape import looks_like_secret

logger = logging.getLogger(__name__)

mail_bp = Blueprint('mail', __name__)

_MAIL_ERRORS = {
    mail_imap.AUTH_FAILED: (400, 'mail.err.authFailed'),
    mail_imap.CONNECT_FAILED: (502, 'mail.err.connectFailed'),
    mail_imap.FOLDER_MISSING: (502, 'mail.err.folderMissing'),
    mail_imap.PROTOCOL: (502, 'mail.err.protocol'),
}


def _db() -> str:
    return current_app.config['DATABASE']


def _err(code: str, key: str, status: int, **vars):
    return jsonify({'success': False, 'error': tr(key, **vars), 'error_code': code}), status


def _mail_error(exc: mail_imap.MailError):
    status, key = _MAIL_ERRORS.get(exc.code, (502, 'mail.err.protocol'))
    # Only the code is logged: the message may have been built near the credentials.
    logger.warning("Mail IMAP call failed: %s", exc.code)
    return _err(exc.code, key, status)


def _body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def _account(account_id: int):
    return next((a for a in ma.list_accounts(_db()) if a['id'] == account_id), None)


def _poller():
    return current_app.extensions.get('mail_poller')


MCP_MAX_MONTHS = 12
SYNC_MAX_MONTHS = 12        # /sync for every caller; deeper is /backfill behind the dialog
BACKFILL_MAX_MONTHS = 120


def _is_mcp() -> bool:
    return request.headers.get('X-Recall-Actor', '').strip().lower() == 'mcp'


@mail_bp.after_request
def _log_mcp_request(response):
    if _is_mcp():
        tool = ''.join(ch for ch in request.headers.get('X-Recall-Tool', '')[:64]
                       if ch.isalnum() or ch in '_-.')
        logger.info("actor=mcp tool=%s route=%s status=%s", tool, request.endpoint, response.status_code)
    return response


def _trigger(account_id=None, months=None, first_months_cap=None) -> None:
    poller = _poller()
    if poller is None:
        return
    try:
        if first_months_cap is None:
            poller.trigger(account_id, months)
        else:
            poller.trigger(account_id, months, first_months_cap=first_months_cap)
    except Exception:
        logger.warning("Mail poller trigger failed", exc_info=True)


def _list_remote_folders(address: str, password: str) -> list[dict]:
    with mail_imap.connect(address, password) as client:
        return client.list_folders()


@mail_bp.route('/api/mail/accounts', methods=['GET'])
def mail_accounts_list():
    return jsonify({'success': True, 'accounts': ma.list_accounts(_db())})


@mail_bp.route('/api/mail/accounts', methods=['POST'])
def mail_accounts_add():
    data = _body()
    if data is None:
        return _err('BAD_REQUEST', 'mail.err.badJson', 400)
    try:
        result = ma.connect_account(_db(), str(data.get('address') or ''), str(data.get('app_password') or ''))
    except ValueError as exc:
        if str(exc) == 'INVALID_ADDRESS':
            return _err('INVALID_ADDRESS', 'mail.err.invalidAddress', 400)
        if str(exc) == 'EMPTY_PASSWORD':
            return _err('EMPTY_PASSWORD', 'mail.err.emptyPassword', 400)
        logger.exception("Mail account could not be stored")
        return _err('INTERNAL', 'mail.err.internal', 500)
    except mail_imap.MailError as exc:
        return _mail_error(exc)
    except Exception:
        logger.exception("Mail account could not be stored")
        return _err('INTERNAL', 'mail.err.internal', 500)
    account, labels = result['account'], result['labels']
    return jsonify({'success': True, 'account': account, 'labels': labels}), 201


@mail_bp.route('/api/mail/accounts/<int:account_id>', methods=['DELETE'])
def mail_accounts_delete(account_id: int):
    """Hard delete (labels and cursors too). Gated as ``mailbox_delete``; MCP has no tool for it."""
    if not ma.delete_account(_db(), account_id):
        return _err('NOT_FOUND', 'mail.err.accountNotFound', 404)
    return jsonify({'success': True})


@mail_bp.route('/api/mail/accounts/<int:account_id>/credentials', methods=['DELETE'])
def mail_accounts_disconnect(account_id: int):
    """Soft disconnect, gated as ``mailbox_disconnect``: password gone, labels off, cursors kept."""
    disabled = ma.disconnect_account(_db(), account_id)
    if disabled is None:
        return _err('NOT_FOUND', 'mail.err.accountNotFound', 404)
    return jsonify({'success': True, 'labels_disabled': len(disabled), 'disabled_label_ids': disabled,
                    'records_kept': True, 'recovery': tr('mail.disconnect.recovery')})


@mail_bp.route('/api/mail/accounts/<int:account_id>/labels', methods=['GET'])
def mail_labels_list(account_id: int):
    account = _account(account_id)
    if account is None:
        return _err('NOT_FOUND', 'mail.err.accountNotFound', 404)
    if request.args.get('refresh') == '1':
        password = ma.get_password(account['address'])
        if not password:
            return _err('NO_PASSWORD', 'mail.err.noPassword', 409)
        try:
            folders = _list_remote_folders(account['address'], password)
        except mail_imap.MailError as exc:
            return _mail_error(exc)
        ma.upsert_folders(_db(), account_id, folders)
    return jsonify({'success': True, 'labels': ma.list_labels(_db(), account_id)})


@mail_bp.route('/api/mail/accounts/<int:account_id>/labels/refresh', methods=['POST'])
def mail_labels_refresh(account_id: int):
    account = _account(account_id)
    if account is None:
        return _err('NOT_FOUND', 'mail.err.accountNotFound', 404)
    password = ma.get_password(account['address'])
    if not password:
        return _err('NO_PASSWORD', 'mail.err.noPassword', 409)
    try:
        folders = _list_remote_folders(account['address'], password)
    except mail_imap.MailError as exc:
        return _mail_error(exc)
    ma.upsert_folders(_db(), account_id, folders)
    return jsonify({'success': True, 'labels': ma.list_labels(_db(), account_id)})


@mail_bp.route('/api/mail/labels/<int:label_id>', methods=['PATCH'])
def mail_label_patch(label_id: int):
    data = _body()
    if data is None:
        return _err('BAD_REQUEST', 'mail.err.badJson', 400)
    mcp = _is_mcp()
    # Turning Claude enrichment on costs money: it has its own gated route, for every caller.
    if data.get('enrich') is True:
        return _err('USE_GATED_ROUTE', 'mail.err.enrichGated', 400, route=f'/api/mail/labels/{label_id}/enrich')
    fields = {}
    for name in ('enabled', 'enrich'):
        if name in data:
            if not isinstance(data[name], bool):
                return _err('BAD_FLAG', 'mail.err.badFlag', 400, field=name)
            fields[name] = data[name]
    if 'category_id' in data:
        cid = data['category_id']
        if cid is not None:
            if isinstance(cid, bool) or not isinstance(cid, int):
                return _err('BAD_CATEGORY', 'mail.err.badCategory', 400)
            with get_db_connection(_db()) as conn:
                row = conn.execute("SELECT 1 FROM categories WHERE id = ? AND parent_id IS NULL",
                                   (cid,)).fetchone()
            if row is None:
                return _err('BAD_CATEGORY', 'mail.err.badCategory', 400)
        fields['category_id'] = cid
        if mcp:
            current = next((lb for lb in ma.list_labels(_db()) if lb['id'] == label_id), None)
            if current is None:
                return _err('NOT_FOUND', 'mail.err.labelNotFound', 404)
            try:
                exposes = privacy.category_change_exposes(_db(), current['category_id'], cid)
            except privacy.PrivacyError:
                return _err('PRIVACY_CHECK_FAILED', 'mail.err.privacyCheck', 503)
            if exposes:
                return _err('PRIVATE_CATEGORY_CHANGE', 'mail.err.privateCategory', 403)
    try:
        label = ma.update_label(_db(), label_id, **fields)
    except KeyError:
        return _err('NOT_FOUND', 'mail.err.labelNotFound', 404)
    out = {'success': True, 'label': label}
    if fields.get('enabled'):
        if mcp:
            _trigger(label['account_id'], first_months_cap=MCP_MAX_MONTHS)
            if not label.get('synced_since') or label.get('last_uid') is None:
                out['backfill_months'] = min(_settings.env_int('MAIL_BACKFILL_MONTHS'), MCP_MAX_MONTHS)
        else:
            _trigger(label['account_id'])
    return jsonify(out)


@mail_bp.route('/api/mail/sync', methods=['POST'])
def mail_sync():
    data = request.get_json(silent=True)
    data = data if isinstance(data, dict) else {}
    account_id = data.get('account_id')
    if account_id is not None and (isinstance(account_id, bool) or not isinstance(account_id, int)):
        return _err('BAD_ACCOUNT', 'mail.err.badAccountId', 400)
    months = data.get('months')
    if months is not None and (isinstance(months, bool) or not isinstance(months, int)
                               or not 1 <= months <= 120):
        return _err('BAD_MONTHS', 'mail.err.badMonths', 400)
    # A window deeper than 12 months is a backfill: its own gated route, for every caller.
    if months is not None and months > SYNC_MAX_MONTHS:
        return _err('USE_GATED_ROUTE', 'mail.err.backfillGated', 400, route='/api/mail/backfill')
    return _queue_sync(account_id, months, 202)


def _queue_sync(account_id, months, status: int):
    poller = _poller()
    if poller is None:
        return _err('MAIL_POLLER_OFF', 'mail.err.pollerOff', 503)
    if account_id is not None and _account(account_id) is None:
        return _err('NOT_FOUND', 'mail.err.accountNotFound', 404)
    poller.trigger(account_id, months)
    return jsonify({'success': True, 'queued': True}), status


@mail_bp.route('/api/mail/backfill', methods=['POST'])
def mail_backfill():
    """Sync 13..120 months back, gated as ``mailbox_backfill`` (the gate validated the body)."""
    data = request.get_json(silent=True)
    data = data if isinstance(data, dict) else {}
    account_id = data.get('account_id')
    if account_id is not None and (isinstance(account_id, bool) or not isinstance(account_id, int)):
        return _err('BAD_ACCOUNT', 'mail.err.badAccountId', 400)
    months = data.get('months')
    if isinstance(months, bool) or not isinstance(months, int) or not SYNC_MAX_MONTHS < months <= BACKFILL_MAX_MONTHS:
        return _err('BAD_MONTHS', 'mail.err.badBackfillMonths', 400)
    return _queue_sync(account_id, months, 200)


@mail_bp.route('/api/mail/labels/<int:label_id>/enrich', methods=['POST'])
def mail_label_enrich(label_id: int):
    """Turn Claude enrichment on for a label, gated as ``mailbox_label_enrich``."""
    try:
        label = ma.update_label(_db(), label_id, enrich=True)
    except KeyError:
        return _err('NOT_FOUND', 'mail.err.labelNotFound', 404)
    return jsonify({'success': True, 'label': label})


@mail_bp.route('/api/mail/status', methods=['GET'])
def mail_status():
    poller = _poller()
    running = bool(poller is not None and poller.is_alive())
    interval = getattr(poller, 'interval_min', None) if poller is not None else None
    if interval is None:
        interval = _settings.env_int('MAIL_POLL_MINUTES')
    out = ma.status(_db())
    return jsonify({'success': True, 'accounts': out['accounts'],
                    'poller': {'running': running, 'interval_min': interval}})


@mail_bp.route('/api/mail/connect-link', methods=['POST'])
def mail_connect_link():
    """Issue a one-time link to the local page where the app password is typed."""
    data = _body()
    if data is None:
        return _err('BAD_REQUEST', 'mail.err.badJson', 400)
    raw = str(data.get('address') or '')
    if looks_like_secret(raw):
        return _err('SECRET_IN_ARGUMENT', 'connect.err.secretInArgument', 400)
    try:
        address = ma.normalize_address(raw)
    except ValueError:
        return _err('INVALID_ADDRESS', 'mail.err.invalidAddress', 400)
    link = connect_links.issue('mail', subject=address, requested_by='mcp' if _is_mcp() else 'unknown')
    return jsonify({
        'success': True,
        'connect_url': connect_links.url_for('mail', link['token'], connect_links.port_from_host(request.host)),
        'expires_in': connect_links.TTL_SECONDS,
        'already_configured': ma.has_password(address),
        'address': address,
    })
