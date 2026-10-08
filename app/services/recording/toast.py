"""
app/services/recording/toast.py  —  Windows toast notifications for the call autoprompt (K2).

No pip dependency: the toast is posted through the WinRT ``ToastNotificationManager`` from a
hidden ``powershell.exe`` (``-EncodedCommand``, so Cyrillic survives; ``NO_WINDOW``, so nothing
flashes when app.py runs under ``pythonw.exe``). The sender is Recall's own AppUserModelID,
registered once under ``HKCU\\Software\\Classes\\AppUserModelId`` with a display name and icon.

Buttons use protocol activation: each opens a URL in the default browser. Every function here
returns ``bool`` and never raises — a missing toast must not break the watcher that called it.
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
from xml.sax.saxutils import escape, quoteattr

from app.utils.proc import NO_WINDOW

logger = logging.getLogger(__name__)

AUMID = 'Recall.CallPrompt'
GROUP = 'recall'
_REG_KEY = rf'Software\Classes\AppUserModelId\{AUMID}'
_TIMEOUT_SEC = 20


def ensure_registered(display_name: str = 'Recall', icon_path: str | None = None) -> bool:
    """Register the AUMID so toasts show «Recall» and its icon instead of a PowerShell sender."""
    try:
        import winreg
    except ImportError:
        logger.debug('winreg unavailable (not Windows): toast AUMID not registered')
        return False
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _REG_KEY) as k:
            winreg.SetValueEx(k, 'DisplayName', 0, winreg.REG_SZ, display_name)
            if icon_path and os.path.isfile(icon_path):
                winreg.SetValueEx(k, 'IconUri', 0, winreg.REG_SZ, os.path.abspath(icon_path))
        return True
    except OSError as exc:
        logger.warning('toast AUMID registration failed: %s', exc)
        return False


def build_xml(title: str, body: str, actions: list[tuple[str, str]], *,
              launch_url: str | None = None, reminder: bool = False) -> str:
    """Toast XML: two text lines, up to five protocol buttons ``(label, url)``.

    ``reminder`` keeps the toast on screen until the user acts (scenario="reminder")."""
    attrs = ''
    if launch_url:
        attrs += f' activationType="protocol" launch={quoteattr(launch_url)}'
    if reminder:
        attrs += ' scenario="reminder"'
    buttons = ''.join(
        f'<action content={quoteattr(label)} activationType="protocol" arguments={quoteattr(url)}/>'
        for label, url in actions[:5]
    )
    return (
        f'<toast{attrs}><visual><binding template="ToastGeneric">'
        f'<text>{escape(title)}</text><text>{escape(body)}</text>'
        f'</binding></visual>'
        + (f'<actions>{buttons}</actions>' if buttons else '')
        + '</toast>'
    )


def _ps_prelude() -> str:
    return (
        "$ErrorActionPreference = 'Stop'\n"
        "[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime]\n"
        "[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
        "ContentType = WindowsRuntime]\n"
    )


def _run_ps(script: str, runner=None) -> bool:
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    cmd = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
           '-EncodedCommand', encoded]
    try:
        res = (runner or subprocess.run)(cmd, capture_output=True, text=True,
                                         timeout=_TIMEOUT_SEC, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning('toast powershell failed to run: %s', exc)
        return False
    if res.returncode != 0:
        logger.warning('toast powershell exit %s: %s', res.returncode,
                       (res.stderr or '').strip()[:500])
        return False
    return True


def _ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def show(xml: str, tag: str, *, runner=None) -> bool:
    """Post (or replace — same tag) a toast from Recall's AUMID."""
    script = (
        _ps_prelude()
        + "$x = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
        + f"$x.LoadXml({_ps_quote(xml)})\n"
        + "$t = New-Object Windows.UI.Notifications.ToastNotification $x\n"
        + f"$t.Tag = {_ps_quote(tag)}; $t.Group = {_ps_quote(GROUP)}\n"
        + f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier({_ps_quote(AUMID)}).Show($t)\n"
    )
    return _run_ps(script, runner)


def remove(tag: str, *, runner=None) -> bool:
    """Withdraw a toast (on screen or in the Action Center) — the call ended, the prompt is moot."""
    script = (
        _ps_prelude()
        + "[Windows.UI.Notifications.ToastNotificationManager]::History.Remove("
        + f"{_ps_quote(tag)}, {_ps_quote(GROUP)}, {_ps_quote(AUMID)})\n"
    )
    return _run_ps(script, runner)
