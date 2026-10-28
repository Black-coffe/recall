"""One-time local connect pages: ``GET/POST /connect/<kind>/<token>``.

Secrets (the Gmail app password, the Anthropic key, a secret calendar ICS address) are typed here, on this computer, and
never travel through chat. Direct loopback requests only. The submitted secret is never
logged and never rendered back.
"""
from __future__ import annotations

import logging
import time

from flask import Blueprint, current_app, make_response, render_template, request

from app.core import i18n
from app.core.auth import _PROXY_HEADERS
from app.core.i18n import tr
from app.services import calendar_ics, connect_links, mail_imap, settings_secrets
from app.services import mail_accounts as ma

logger = logging.getLogger(__name__)

connect_bp = Blueprint('connect', __name__)

_LOOPBACK_ADDRS = {'127.0.0.1', '::1'}
_LOOPBACK_HOSTS = {'127.0.0.1', 'localhost', '[::1]'}


def _host_name(host: str) -> str:
    if host.startswith('['):
        return host.split(']')[0] + ']'
    return host.rsplit(':', 1)[0] if ':' in host else host


def _guard_ok() -> bool:
    if request.remote_addr not in _LOOPBACK_ADDRS:
        return False
    if any(request.headers.get(h) for h in _PROXY_HEADERS):
        return False
    host = request.host or ''
    if _host_name(host).lower() not in _LOOPBACK_HOSTS:
        return False
    if request.method == 'POST':
        origin = request.headers.get('Origin')
        if origin == 'null':
            # An opaque origin: browsers send it for their own form under a strict referrer
            # policy. Accept it only when Fetch Metadata does not say the request is cross-site.
            if request.headers.get('Sec-Fetch-Site') not in ('same-origin', 'none'):
                return False
        elif origin is not None and origin != f'http://{host}':
            return False
    return True


def _page(status: int, **ctx):
    kind = ctx.pop('kind')
    ctx.setdefault('title', tr(f'connect.title.{kind}'))
    ctx.setdefault('lang', i18n.request_lang())
    for name in ('gone', 'done', 'labels', 'error', 'address', 'requested_by_line', 'configured_line'):
        ctx.setdefault(name, None)
    resp = make_response(render_template('connect.html', **ctx), status)
    resp.headers['Cache-Control'] = 'no-store'
    resp.headers['Referrer-Policy'] = 'same-origin'
    resp.headers['X-Frame-Options'] = 'DENY'
    return resp


def _gone(kind: str):
    return _page(410, kind=kind, gone=tr('connect.gone'))


def _form(kind: str, link: dict, status: int = 200, error: str | None = None):
    requested = None
    if link.get('requested_by') == 'mcp':
        requested = tr('connect.requestedByMcp',
                       time=time.strftime('%H:%M', time.localtime(link['created_at'])))
    if kind == 'mail':
        configured = ma.has_password(link['subject'])
    elif kind == 'calendar':
        configured = calendar_ics.has_source(current_app.config['DATABASE'], link['subject'])
    else:
        configured = settings_secrets.anthropic_key_configured()
    secret_label = {'mail': 'connect.label.mailSecret',
                    'calendar': 'connect.label.calendarSecret'}.get(kind, 'connect.label.keySecret')
    return _page(
        status, kind=kind, error=error,
        warning=tr('connect.warning.' + kind),
        requested_by_line=requested,
        configured_line=tr('connect.alreadyConfigured') if configured else None,
        address=link.get('subject') if kind in ('mail', 'calendar') else None,
        address_label=tr('connect.label.calendarName' if kind == 'calendar' else 'connect.label.address'),
        secret_label=tr(secret_label),
        submit=tr('connect.submit'))


def _attempt_failed(kind: str, token: str, link: dict, status: int, message: str):
    left = connect_links.record_failure(kind, token)
    if left <= 0:
        return _gone(kind)
    return _form(kind, link, status, f"{message} {tr('connect.attemptsLeft', left=left)}")


@connect_bp.route('/connect/<kind>/<token>', methods=['GET', 'POST'])
def connect_page(kind: str, token: str):
    if not _guard_ok():
        return make_response('Forbidden', 403)
    if kind not in connect_links.KINDS:
        return make_response('Not found', 404)
    link = connect_links.peek(kind, token)
    if link is None:
        return _gone(kind)
    if request.method == 'GET':
        return _form(kind, link)

    secret = request.form.get('secret', '')
    if kind == 'mail':
        return _post_mail(token, link, secret)
    if kind == 'calendar':
        return _post_calendar(token, link, secret)
    return _post_key(token, link, secret)


def _post_mail(token: str, link: dict, secret: str):
    kind = 'mail'
    try:
        result = ma.connect_account(current_app.config['DATABASE'], link['subject'], secret)
    except ValueError:
        return _attempt_failed(kind, token, link, 400, tr('connect.err.rejected'))
    except mail_imap.MailError as exc:
        # Only the code is logged: the message may have been built near the credentials.
        logger.warning("Connect page: IMAP call failed: %s", exc.code)
        if exc.code == mail_imap.AUTH_FAILED:
            return _attempt_failed(kind, token, link, 400, tr('mail.err.authFailed'))
        return _attempt_failed(kind, token, link, 502, tr('connect.err.unreachable'))
    except Exception as exc:
        logger.warning("Connect page: mail connect failed: %s", type(exc).__name__)
        return _attempt_failed(kind, token, link, 500, tr('connect.err.internal'))
    connect_links.consume(kind, token)
    labels = [str(lb.get('display_name') or lb.get('folder') or '') for lb in result['labels']]
    return _page(200, kind=kind, done=tr('connect.done.mail'), labels=labels,
                 done_hint=tr('connect.done.mailHint'), close_hint=tr('connect.done.close'))


def _post_key(token: str, link: dict, secret: str):
    kind = 'anthropic_key'
    try:
        settings_secrets.save_anthropic_key(secret)
    except ValueError as exc:
        key = str(exc)
        status = 500 if key == settings_secrets.ERR_ENV_WRITE else 400
        return _attempt_failed(kind, token, link, status, tr(key))
    connect_links.consume(kind, token)
    return _page(200, kind=kind, done=tr('connect.done.anthropic_key'), labels=None,
                 close_hint=tr('connect.done.close'))


_CALENDAR_ERRORS = {
    calendar_ics.BAD_URL: (400, 'connect.err.calendarUrl'),
    calendar_ics.HOST_NOT_ALLOWED: (400, 'connect.err.calendarHost'),
    calendar_ics.FETCH_FAILED: (502, 'connect.err.unreachable'),
    calendar_ics.PARSE_FAILED: (400, 'connect.err.calendarParse'),
}


def _post_calendar(token: str, link: dict, secret: str):
    kind = 'calendar'
    try:
        result = calendar_ics.connect_source(current_app.config['DATABASE'], link['subject'], secret)
    except ValueError:
        return _attempt_failed(kind, token, link, 400, tr('connect.err.rejected'))
    except calendar_ics.CalendarError as exc:
        # Only the code is logged: the address is the secret.
        logger.warning("Connect page: calendar connect failed: %s", exc.code)
        status, key = _CALENDAR_ERRORS.get(exc.code, (500, 'connect.err.internal'))
        return _attempt_failed(kind, token, link, status, tr(key))
    except Exception as exc:
        logger.warning("Connect page: calendar connect failed: %s", type(exc).__name__)
        return _attempt_failed(kind, token, link, 500, tr('connect.err.internal'))
    connect_links.consume(kind, token)
    return _page(200, kind=kind, labels=None, close_hint=tr('connect.done.close'),
                 done=tr('connect.done.calendar', events=result['events'],
                         with_attendees=result['with_attendees']))
