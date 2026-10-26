"""Approval primitive: a native Windows Yes/No dialog, fail closed, one dialog at a time.

Stdlib only, no Flask and no app state. Everything except an explicit "Yes" inside the
deadline is a denial. ``_show_dialog`` is the test seam (tests/conftest.py replaces it).
"""
from __future__ import annotations

import ctypes
import logging
import secrets
import sys
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

APPROVAL_TIMEOUT_S = 45.0
IDYES, IDNO = 6, 7

# Win32 constants (winuser.h)
MB_YESNO = 0x00000004
MB_ICONWARNING = 0x00000030
MB_DEFBUTTON2 = 0x00000100
MB_SYSTEMMODAL = 0x00001000
MB_SETFOREGROUND = 0x00010000
MB_TOPMOST = 0x00040000
WM_COMMAND = 0x0111
UOI_FLAGS = 1
WSF_VISIBLE = 1

_FLAGS = (MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2 | MB_SYSTEMMODAL
          | MB_SETFOREGROUND | MB_TOPMOST)

_slot = threading.Lock()
_nonce_lock = threading.Lock()
_last_nonce = ""


class DialogUnavailable(Exception):
    """The dialog cannot be shown; ``reason`` is not_windows | no_desktop | messagebox_failed."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Decision:
    status: str  # approved | denied | unavailable | pending
    reason: str | None
    elapsed_s: float


def _user32():
    return ctypes.windll.user32


def _station_visible(user32) -> bool:
    class _Flags(ctypes.Structure):
        _fields_ = [("fInherit", ctypes.c_int), ("fReserved", ctypes.c_int),
                    ("dwFlags", ctypes.c_uint32)]

    flags = _Flags()
    needed = ctypes.c_uint32(0)
    station = user32.GetProcessWindowStation()
    ok = user32.GetUserObjectInformationW(
        station, UOI_FLAGS, ctypes.byref(flags), ctypes.sizeof(flags), ctypes.byref(needed))
    return bool(ok) and bool(flags.dwFlags & WSF_VISIBLE)


def station_visible() -> bool:
    """Public no-desktop probe for other UI-less paths (the toast before an agent start).

    False off Windows and when the probe itself fails: no visible desktop, no toast."""
    if sys.platform != "win32":
        return False
    try:
        return _station_visible(_user32())
    except Exception:  # noqa: BLE001 - a failing probe means no visible desktop
        logger.debug("station_visible probe failed", exc_info=True)
        return False


def _show_dialog(title: str, text: str) -> int:
    """Show MessageBoxW on the calling thread and return IDYES / IDNO."""
    if sys.platform != "win32":
        raise DialogUnavailable("not_windows")
    try:
        user32 = _user32()
        if not _station_visible(user32):
            raise DialogUnavailable("no_desktop")
        result = user32.MessageBoxW(None, text, title, _FLAGS)
    except DialogUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - any ctypes failure is a denial
        logger.warning("MessageBoxW failed: %s", exc)
        raise DialogUnavailable("messagebox_failed") from exc
    if not result:
        raise DialogUnavailable("messagebox_failed")
    return int(result)


def _next_nonce() -> str:
    global _last_nonce
    with _nonce_lock:
        nonce = secrets.token_hex(2)
        while nonce == _last_nonce:
            nonce = secrets.token_hex(2)
        _last_nonce = nonce
        return nonce


def _close_window(title: str) -> None:
    """Best effort: press 'No' on the window that outlived the deadline."""
    if sys.platform != "win32":
        return
    try:
        user32 = _user32()
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            user32.PostMessageW(hwnd, WM_COMMAND, IDNO, 0)
        else:
            logger.debug("approval window not found for close: %s", title)
    except Exception as exc:  # noqa: BLE001
        logger.debug("closing the approval window failed (best effort): %s", exc)


def busy() -> bool:
    return _slot.locked()


def ask(text: str, *, timeout_s: float = APPROVAL_TIMEOUT_S) -> Decision:
    """Ask the owner; approved only on an explicit Yes within ``timeout_s``."""
    start = time.monotonic()
    if not _slot.acquire(blocking=False):
        return Decision("pending", "busy", 0.0)
    try:
        title = f"Recall - Confirm action - {_next_nonce()}"
        done = threading.Event()
        box: dict = {}

        def _run():
            try:
                box["value"] = _show_dialog(title, text)
            except DialogUnavailable as exc:
                box["unavailable"] = exc.reason
            except Exception as exc:  # noqa: BLE001
                logger.warning("approval dialog crashed: %s", exc)
                box["unavailable"] = "messagebox_failed"
            finally:
                done.set()

        threading.Thread(target=_run, name="approval-dialog", daemon=True).start()
        if not done.wait(timeout_s):
            _close_window(title)
            return Decision("denied", "timeout", time.monotonic() - start)
        elapsed = time.monotonic() - start
        if "unavailable" in box:
            return Decision("unavailable", box["unavailable"], elapsed)
        if box.get("value") == IDYES:
            return Decision("approved", None, elapsed)
        return Decision("denied", "no", elapsed)
    finally:
        _slot.release()
