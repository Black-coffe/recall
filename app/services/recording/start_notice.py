"""Toast before an agent-started recording (spec mcp-recording-live, ADR-051).

An agent capture never starts without this notice: the owner sees what is about to be recorded
(microphone, applications or all system audio, language, name, screen, and who the agent claims to
be). ``notify_start`` returns ``True`` only after the toast was shown; every other outcome is
``False`` and the caller answers 503 instead of starting. It never raises.

No blueprint import and no ``app.state``. Toast texts come from the ``capture.rec.notice.*`` keys.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from app.core.i18n import tr
from app.services import approvals
from app.services.recording import toast

logger = logging.getLogger(__name__)


def _post(title: str, body: str, tag: str, icon_path: Optional[str]) -> bool:
    """Register the AUMID and post the toast. Test seam: tests stub this one name."""
    if not toast.ensure_registered('Recall', icon_path):
        return False
    return toast.show(toast.build_xml(title, body, []), tag)


def notify_start(*, mic_name: Optional[str], system_apps: list[str], system_all: bool,
                 language: str, name: Optional[str], with_screen: bool, claimed_label: str,
                 lang: str, icon_path: Optional[str] = None,
                 consent_reminder: bool = False) -> bool:
    if not approvals.station_visible():
        logger.info("start notice skipped: no visible desktop")
        return False
    if system_all:
        apps = tr('capture.rec.notice.all_system', lang=lang)
    elif system_apps:
        apps = ', '.join(str(a) for a in system_apps)
    else:
        apps = tr('capture.rec.notice.no_system', lang=lang)
    if with_screen:
        apps = f"{apps}, {tr('capture.rec.notice.with_screen', lang=lang)}"
    title = tr('capture.rec.notice.title', lang=lang)
    body = tr('capture.rec.notice.body', lang=lang,
              mic=mic_name or '', apps=apps, language=language,
              name=name or tr('capture.rec.notice.no_name', lang=lang), label=claimed_label)
    if consent_reminder:
        body = f"{body}\n{tr('svc_recording.consent.reminder', lang=lang)}"
    tag = f'rec-start-{uuid.uuid4().hex[:8]}'
    try:
        return _post(title, body, tag, icon_path) is True
    except Exception:  # noqa: BLE001 - a failed toast must refuse the start, not raise into it
        logger.warning("start notice failed to post", exc_info=True)
        return False
