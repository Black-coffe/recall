"""
app/services/recording/app_audio.py  —  which apps are sounding right now (contract C3).

Enumerates audio sessions on EVERY active render endpoint (pycaw's ``GetAllSessions()`` sees the
default device only — spike measurement i), dedupes them by lowercase exe basename and marks which
live recordings capture each app. Never raises: any enumeration failure becomes
``{'available': False, 'reason': ..., 'apps': []}``.

pycaw/comtypes are imported lazily inside the default enumerator, so importing this module stays
cheap and works on machines without them. Deliberately independent of ``audio_source.py``:
``live_specs`` is a plain dict ``{sid: {'mode': 'all'|'include'|'exclude', 'apps': [exe, ...]}}``.
"""

from __future__ import annotations

import logging
import os

import psutil

logger = logging.getLogger(__name__)

# AudioSessionState: 0 Inactive, 1 Active, 2 Expired
_STATES = {0: 'inactive', 1: 'active', 2: 'expired'}


def _default_enumerate_sessions() -> list[tuple[int, str, str, str | None]]:
    """(pid, state, endpoint_name, None) for every session on every active render endpoint."""
    import comtypes
    from comtypes import CLSCTX_ALL, CoCreateInstance
    from pycaw.api.audiopolicy import IAudioSessionControl2, IAudioSessionManager2
    from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
    from pycaw.constants import CLSID_MMDeviceEnumerator
    from pycaw.utils import AudioUtilities

    comtypes.CoInitialize()
    try:
        rows = []
        en = CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_ALL)
        coll = en.EnumAudioEndpoints(0, 1)  # eRender, DEVICE_STATE_ACTIVE
        for i in range(coll.GetCount()):
            dev = coll.Item(i)
            try:
                name = AudioUtilities.CreateDevice(dev).FriendlyName or dev.GetId()
            except Exception:
                logger.debug('endpoint friendly name unavailable', exc_info=True)
                name = dev.GetId()
            mgr = dev.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None).QueryInterface(
                IAudioSessionManager2)
            se = mgr.GetSessionEnumerator()
            for j in range(se.GetCount()):
                try:
                    ctl = se.GetSession(j).QueryInterface(IAudioSessionControl2)
                    pid = int(ctl.GetProcessId())
                    state = _STATES.get(int(ctl.GetState()), 'expired')
                except Exception:
                    # A session that closes mid-enumeration: skip it, keep the rest.
                    logger.debug('audio session vanished during enumeration', exc_info=True)
                    continue
                rows.append((pid, state, str(name), None))
        return rows
    finally:
        comtypes.CoUninitialize()


#: Endpoint id → friendly name. pycaw's ``CreateDevice`` reads the whole property store
#: (~600 values per device) and was 92 % of a 63 ms watcher tick (K2 measurement); the name of an
#: endpoint does not change while it exists. A rename in Windows settings shows after a restart.
_endpoint_names: dict[str, str] = {}


def _endpoint_name(dev, audio_utilities) -> str:
    dev_id = dev.GetId()
    name = _endpoint_names.get(dev_id)
    if name is None:
        try:
            name = audio_utilities.CreateDevice(dev).FriendlyName or dev_id
        except Exception:
            logger.debug('endpoint friendly name unavailable', exc_info=True)
            return dev_id
        _endpoint_names[dev_id] = name
    return name


def _default_enumerate_call_sessions() -> list[tuple[str, int, str, str, str | None, float]]:
    """(flow, pid, state, endpoint_name, None, peak) for every session on every active capture
    ('mic') and render ('out') endpoint. ``peak`` is the session's own meter (0..1): the K2 spike
    measured it per session on capture too (own mic stream 0.0069 vs the endpoint's 0.0050)."""
    import comtypes
    from comtypes import CLSCTX_ALL, CoCreateInstance
    from pycaw.api.audiopolicy import IAudioSessionControl2, IAudioSessionManager2
    from pycaw.api.endpointvolume import IAudioMeterInformation
    from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
    from pycaw.constants import CLSID_MMDeviceEnumerator
    from pycaw.utils import AudioUtilities

    comtypes.CoInitialize()
    try:
        rows = []
        en = CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_ALL)
        for flow, data_flow in (('mic', 1), ('out', 0)):  # eCapture, eRender
            coll = en.EnumAudioEndpoints(data_flow, 1)  # DEVICE_STATE_ACTIVE
            for i in range(coll.GetCount()):
                dev = coll.Item(i)
                name = _endpoint_name(dev, AudioUtilities)
                mgr = dev.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None).QueryInterface(
                    IAudioSessionManager2)
                se = mgr.GetSessionEnumerator()
                for j in range(se.GetCount()):
                    try:
                        sess = se.GetSession(j)
                        ctl = sess.QueryInterface(IAudioSessionControl2)
                        pid = int(ctl.GetProcessId())
                        state = _STATES.get(int(ctl.GetState()), 'expired')
                        if state == 'expired':
                            continue
                        peak = float(sess.QueryInterface(IAudioMeterInformation).GetPeakValue())
                    except Exception:
                        logger.debug('audio session vanished during enumeration', exc_info=True)
                        continue
                    rows.append((flow, pid, state, str(name), None, peak))
        return rows
    finally:
        comtypes.CoUninitialize()


def list_call_sessions(*, enumerate_sessions=None, own_pid: int | None = None) -> dict | None:
    """Per-exe microphone and output state for the call watcher (K2).

    ``{exe: {'mic': {'active', 'peak', 'endpoint'}, 'out': {'active', 'peak', 'endpoint'}}}`` —
    ``active`` if any session of that exe on that flow is Active, ``peak`` the loudest Active
    session, ``endpoint`` its device name. Recall's own process is skipped. ``None`` when the
    enumeration itself failed (the caller must not read that as «every call ended»)."""
    if own_pid is None:
        own_pid = os.getpid()
    try:
        sessions = list((enumerate_sessions or _default_enumerate_call_sessions)())
    except Exception as exc:
        logger.warning('call-session enumeration failed: %s', exc, exc_info=True)
        return None
    out: dict[str, dict] = {}
    for flow, pid, state, endpoint, exe, peak in sessions:
        if pid == 0 or pid == own_pid or state not in ('active', 'inactive'):
            continue
        if not exe:
            try:
                exe = psutil.Process(pid).name()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                logger.debug('call session pid %s not resolvable, skipped', pid)
                continue
        exe = os.path.basename(str(exe)).lower()
        app = out.setdefault(exe, {f: {'active': False, 'peak': 0.0, 'endpoint': None}
                                   for f in ('mic', 'out')})
        side = app[flow]
        if state != 'active':
            continue
        if not side['active'] or peak > side['peak']:
            side['peak'] = float(peak)
            side['endpoint'] = endpoint
        side['active'] = True
    return out


def _recorded_by(exe: str, live_specs: dict[str, dict]) -> list[str]:
    out = []
    for sid, spec in live_specs.items():
        spec = spec or {}
        mode = spec.get('mode', 'all')
        apps = {str(a).lower() for a in (spec.get('apps') or [])}
        if mode == 'all' or (mode == 'include' and exe in apps) or (mode == 'exclude' and exe not in apps):
            out.append(sid)
    return out


def _psutil_parents(pid: int) -> list[tuple[int, str]]:
    """(pid, name) of every ancestor of ``pid``, nearest first; raises psutil errors if gone."""
    out = []
    for p in psutil.Process(pid).parents():
        try:
            out.append((p.pid, p.name()))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            logger.debug('ancestor %s of pid %s not resolvable', p.pid, pid)
    return out


def session_pids(exe: str, *, enumerate_sessions=None,
                 own_pid: int | None = None) -> list[tuple[int, str]]:
    """``[(pid, state)]`` for every Active/Inactive session of ``exe`` on any active render
    endpoint (same enumerator seam as :func:`list_sounding_apps`). Never raises: an
    enumeration failure yields ``[]``."""
    if own_pid is None:
        own_pid = os.getpid()
    exe = os.path.basename(str(exe)).lower()
    try:
        sessions = list((enumerate_sessions or _default_enumerate_sessions)())
    except Exception as exc:
        logger.warning('session-pid enumeration for %s failed: %s', exe, exc, exc_info=True)
        return []
    states: dict[int, str] = {}
    for pid, state, _endpoint, name in sessions:
        if pid == 0 or pid == own_pid or state not in ('active', 'inactive'):
            continue
        if not name:
            try:
                name = psutil.Process(pid).name()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                logger.debug('session pid %s not resolvable, skipped', pid)
                continue
        if os.path.basename(str(name)).lower() != exe:
            continue
        if states.get(pid) != 'active':
            states[pid] = state
    return list(states.items())


def session_roots(exe: str, *, enumerate_sessions=None, own_pid: int | None = None,
                  process_parents=None, fallback=None) -> list[int]:
    """Process-tree roots of ``exe`` that hold an audio session, best target first.

    A session PID collapses to its topmost ancestor with the same exe name (a vanished
    process is skipped). Roots are ordered by active-session count, then by any-session
    count, then by PID. No session at all → ``fallback(exe)`` (e.g. psutil exe roots)."""
    if own_pid is None:
        own_pid = os.getpid()
    exe = os.path.basename(str(exe)).lower()
    parents = process_parents or _psutil_parents
    counts: dict[int, list[int]] = {}
    for pid, state in session_pids(exe, enumerate_sessions=enumerate_sessions, own_pid=own_pid):
        try:
            chain = parents(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            logger.debug('session pid %s vanished before root lookup, skipped', pid)
            continue
        root = pid
        for anc_pid, anc_name in chain:
            if os.path.basename(str(anc_name or '')).lower() == exe:
                root = int(anc_pid)
        if root == own_pid:
            continue
        c = counts.setdefault(root, [0, 0])
        c[1] += 1
        if state == 'active':
            c[0] += 1
    if not counts:
        return list(fallback(exe)) if fallback else []
    return sorted(counts, key=lambda r: (-counts[r][0], -counts[r][1], r))


def list_sounding_apps(live_specs: dict[str, dict] | None = None, *, enumerate_sessions=None,
                       own_pid: int | None = None) -> dict:
    """Apps holding an Active or Inactive audio session on any active render endpoint (C3)."""
    if own_pid is None:
        own_pid = os.getpid()
    enum = enumerate_sessions or _default_enumerate_sessions
    try:
        sessions = list(enum())
    except Exception as exc:
        logger.warning('sounding-apps enumeration failed: %s', exc, exc_info=True)
        return {'available': False, 'reason': f'{type(exc).__name__}: {exc}', 'apps': []}

    by_exe: dict[str, dict] = {}
    for pid, state, endpoint, exe in sessions:
        if pid == 0 or pid == own_pid or state not in ('active', 'inactive'):
            continue
        if not exe:
            try:
                exe = psutil.Process(pid).name()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                logger.debug('sounding app pid %s not resolvable, skipped', pid)
                continue
        exe = os.path.basename(str(exe)).lower()
        app = by_exe.setdefault(exe, {'exe': exe, 'label': os.path.splitext(exe)[0],
                                      'pids': [], 'state': 'inactive', 'endpoints': []})
        if pid not in app['pids']:
            app['pids'].append(pid)
        if endpoint not in app['endpoints']:
            app['endpoints'].append(endpoint)
        if state == 'active':
            app['state'] = 'active'

    apps = sorted(by_exe.values(), key=lambda a: (a['state'] != 'active', a['label']))
    for app in apps:
        app['recorded_by'] = _recorded_by(app['exe'], live_specs or {})
    return {'available': True, 'reason': None, 'apps': apps}
