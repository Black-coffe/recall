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
from app.services import mail_imap

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


def _trigger(account_id=None, months=None) -> None:
    poller = _poller()
    if poller is None:
        return
    try:
        poller.trigger(account_id, months)
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
        address = ma.normalize_address(str(data.get('address') or ''))
    except ValueError:
        return _err('INVALID_ADDRESS', 'mail.err.invalidAddress', 400)
    # Google shows app passwords in groups separated by spaces.
    password = ''.join(str(data.get('app_password') or '').split())
    if not password:
        return _err('EMPTY_PASSWORD', 'mail.err.emptyPassword', 400)
    try:
        folders = _list_remote_folders(address, password)
    except mail_imap.MailError as exc:
        return _mail_error(exc)
    try:
        account = ma.add_account(_db(), address, password)
        labels = ma.upsert_folders(_db(), account['id'], folders)
    except Exception:
        logger.exception("Mail account could not be stored")
        return _err('INTERNAL', 'mail.err.internal', 500)
    return jsonify({'success': True, 'account': account, 'labels': labels}), 201


@mail_bp.route('/api/mail/accounts/<int:account_id>', methods=['DELETE'])
def mail_accounts_delete(account_id: int):
    if not ma.delete_account(_db(), account_id):
        return _err('NOT_FOUND', 'mail.err.accountNotFound', 404)
    return jsonify({'success': True})


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


@mail_bp.route('/api/mail/labels/<int:label_id>', methods=['PATCH'])
def mail_label_patch(label_id: int):
    data = _body()
    if data is None:
        return _err('BAD_REQUEST', 'mail.err.badJson', 400)
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
    try:
        label = ma.update_label(_db(), label_id, **fields)
    except KeyError:
        return _err('NOT_FOUND', 'mail.err.labelNotFound', 404)
    if fields.get('enabled'):
        _trigger(label['account_id'])
    return jsonify({'success': True, 'label': label})


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
    poller = _poller()
    if poller is None:
        return _err('MAIL_POLLER_OFF', 'mail.err.pollerOff', 503)
    if account_id is not None and _account(account_id) is None:
        return _err('NOT_FOUND', 'mail.err.accountNotFound', 404)
    poller.trigger(account_id, months)
    return jsonify({'success': True, 'queued': True}), 202


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
