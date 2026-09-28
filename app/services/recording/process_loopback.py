"""Windows process-loopback backend client (ADR-026 D2, plan C2).

One ``ProcessLoopbackClient`` = one native WASAPI process-loopback capture:
``ActivateAudioInterfaceAsync("VAD\\Process_Loopback", {pid, INCLUDE|EXCLUDE tree})`` →
``IAudioClient.Initialize(shared, LOOPBACK, <explicit format>)`` → ``IAudioCaptureClient``
polled on its own MTA thread. Promoted from the spike client
``scripts/spikes/process_loopback/ploop.py`` (spike-decision 2026-09-28: proc-tap is not
used — it only does INCLUDE, only float32, and silently falls back to whole-endpoint capture).

Nothing native is imported at module level: comtypes/pycaw load on the first
``ProcessLoopbackClient.start()`` (``_native()``), so importing this module (and
``audio_source``) stays cheap and platform-neutral.

Known behaviour (spike.md): a client on a dead or dead-born PID streams silence without an
error (f) — liveness is the caller's watcher's job; ``GetMixFormat`` is ``E_NOTIMPL`` (j) — the
format is always named explicitly; the INCLUDE tree reaches direct children only (c).
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Callable, Optional, Protocol

logger = logging.getLogger(__name__)

MODE_INCLUDE = 0  # PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
MODE_EXCLUDE = 1  # PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE
_MODES = {'include': MODE_INCLUDE, 'exclude': MODE_EXCLUDE}

_AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
_AUDCLNT_BUFFERFLAGS_SILENT = 0x2
_BUFFER_HNS = 2_000_000  # 200 ms engine buffer
_POLL_SECONDS = 0.005


class LoopbackClient(Protocol):
    """Backend client contract (C2)."""

    def start(self, on_frame: Callable[[bytes, float], None],
              on_error: Callable[[Exception], None]) -> None: ...

    def stop(self) -> None: ...


ClientFactory = Callable[[int, str, int, int], LoopbackClient]  # (pid, mode, rate, channels)


# ---------------------------------------------------------------- native (lazy)

_NATIVE = None
_NATIVE_LOCK = threading.Lock()


def _native():
    """Build the ctypes/comtypes bindings once, on first use."""
    global _NATIVE
    with _NATIVE_LOCK:
        if _NATIVE is not None:
            return _NATIVE
        if 'comtypes' not in sys.modules and not hasattr(sys, 'coinit_flags'):
            sys.coinit_flags = 0  # COINIT_MULTITHREADED for the importing thread

        import ctypes
        from ctypes import (POINTER, Structure, byref, c_long, c_ubyte, c_uint32, c_ulong,
                            c_ulonglong, c_ushort, c_void_p)
        from ctypes.wintypes import DWORD

        import comtypes
        from comtypes import COMMETHOD, COMObject, GUID, HRESULT, IUnknown
        from pycaw.api.audioclient import IAudioClient
        from pycaw.api.audioclient.depend import WAVEFORMATEX

        class IActivateAudioInterfaceAsyncOperation(IUnknown):
            _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
            _methods_ = [
                COMMETHOD([], HRESULT, "GetActivateResult",
                          (["out"], POINTER(c_long), "activateResult"),
                          (["out"], POINTER(POINTER(IUnknown)), "activatedInterface")),
            ]

        class IActivateAudioInterfaceCompletionHandler(IUnknown):
            _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
            _methods_ = [
                COMMETHOD([], HRESULT, "ActivateCompleted",
                          (["in"], POINTER(IActivateAudioInterfaceAsyncOperation),
                           "activateOperation")),
            ]

        class IAgileObject(IUnknown):
            _iid_ = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
            _methods_ = []

        class IAudioCaptureClient(IUnknown):
            _iid_ = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
            _methods_ = [
                COMMETHOD([], HRESULT, "GetBuffer",
                          (["out"], POINTER(POINTER(c_ubyte)), "ppData"),
                          (["out"], POINTER(c_uint32), "pNumFramesToRead"),
                          (["out"], POINTER(DWORD), "pdwFlags"),
                          (["out"], POINTER(c_ulonglong), "pu64DevicePosition"),
                          (["out"], POINTER(c_ulonglong), "pu64QPCPosition")),
                COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "NumFramesRead")),
                COMMETHOD([], HRESULT, "GetNextPacketSize",
                          (["out"], POINTER(c_uint32), "pNumFramesInNextPacket")),
            ]

        class _Blob(Structure):
            _fields_ = [("cbSize", c_ulong), ("pBlobData", c_void_p)]

        class _PropVariant(Structure):
            _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort),
                        ("r3", c_ushort), ("blob", _Blob)]

        class _ActivationParams(Structure):
            _fields_ = [("ActivationType", c_uint32), ("TargetProcessId", DWORD),
                        ("ProcessLoopbackMode", c_uint32)]

        class _Handler(COMObject):
            _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

            def __init__(self):
                super().__init__()
                self.done = threading.Event()
                self.hr = None
                self.unk = None
                self.error = None

            def ActivateCompleted(self, activateOperation):
                try:
                    self.hr, self.unk = activateOperation.GetActivateResult()
                except Exception as exc:  # handed to the caller through .error
                    self.error = exc
                finally:
                    self.done.set()
                return 0

        activate = ctypes.WinDLL("Mmdevapi.dll").ActivateAudioInterfaceAsync
        activate.restype = HRESULT
        activate.argtypes = [ctypes.c_wchar_p, POINTER(GUID), POINTER(_PropVariant),
                             POINTER(IActivateAudioInterfaceCompletionHandler),
                             POINTER(POINTER(IActivateAudioInterfaceAsyncOperation))]

        def activate_process_loopback(pid: int, mode: int, timeout: float = 5.0):
            params = _ActivationParams(1, pid, mode)  # AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
            pv = _PropVariant()
            pv.vt = 65  # VT_BLOB
            pv.blob.cbSize = ctypes.sizeof(params)
            pv.blob.pBlobData = ctypes.cast(byref(params), c_void_p)
            handler = _Handler()
            op = POINTER(IActivateAudioInterfaceAsyncOperation)()
            iid = IAudioClient._iid_
            activate("VAD\\Process_Loopback", byref(iid), byref(pv),
                     handler.QueryInterface(IActivateAudioInterfaceCompletionHandler), byref(op))
            if not handler.done.wait(timeout):
                raise TimeoutError("ActivateAudioInterfaceAsync did not complete")
            if handler.error is not None:
                raise handler.error
            if handler.hr is None or handler.hr < 0:
                raise OSError("process loopback activation failed "
                              f"hr=0x{(handler.hr or 0) & 0xFFFFFFFF:08X}")
            return handler.unk.QueryInterface(IAudioClient)

        def make_format(rate: int, channels: int):
            wf = WAVEFORMATEX()
            wf.wFormatTag = 1  # WAVE_FORMAT_PCM
            wf.nChannels = channels
            wf.nSamplesPerSec = rate
            wf.wBitsPerSample = 16
            wf.nBlockAlign = channels * 2
            wf.nAvgBytesPerSec = rate * wf.nBlockAlign
            wf.cbSize = 0
            return wf

        class _NS:
            pass

        ns = _NS()
        ns.ctypes = ctypes
        ns.byref = byref
        ns.comtypes = comtypes
        ns.IAudioCaptureClient = IAudioCaptureClient
        ns.activate_process_loopback = activate_process_loopback
        ns.make_format = make_format
        _NATIVE = ns
        return ns


# ---------------------------------------------------------------- client

class ProcessLoopbackClient:
    """One process-loopback capture of ``pid`` in ``mode`` at a fixed s16le format.

    ``start()`` blocks until the client is open (raises on activation/initialise failure);
    later capture errors go to ``on_error``. Frames go to ``on_frame(bytes, perf_counter_ts)``
    from the client's own thread.
    """

    def __init__(self, pid: int, mode: str, rate: int, channels: int,
                 *, open_timeout: float = 5.0):
        if mode not in _MODES:
            raise ValueError(f"unknown process-loopback mode: {mode!r}")
        self.pid = int(pid)
        self.mode = mode
        self.rate = int(rate)
        self.channels = int(channels)
        self._block = self.channels * 2
        self._open_timeout = open_timeout
        self._stop = threading.Event()
        self._opened = threading.Event()
        self._open_error: Optional[Exception] = None
        self._thread: Optional[threading.Thread] = None
        self._on_frame: Optional[Callable[[bytes, float], None]] = None
        self._on_error: Optional[Callable[[Exception], None]] = None

    def start(self, on_frame: Callable[[bytes, float], None],
              on_error: Callable[[Exception], None]) -> None:
        if self._thread is not None:
            return
        _native()
        self._on_frame, self._on_error = on_frame, on_error
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f'proc-loopback-{self.mode}-{self.pid}',
        )
        self._thread.start()
        if not self._opened.wait(self._open_timeout):
            self._stop.set()
            raise TimeoutError(f"process loopback client for pid {self.pid} did not open")
        if self._open_error is not None:
            raise self._open_error

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)

    def _run(self) -> None:
        nt = _native()
        nt.comtypes.CoInitializeEx(nt.comtypes.COINIT_MULTITHREADED)
        client = capture = None
        try:
            try:
                client = nt.activate_process_loopback(self.pid, _MODES[self.mode])
                wf = nt.make_format(self.rate, self.channels)
                client.Initialize(0, _AUDCLNT_STREAMFLAGS_LOOPBACK, _BUFFER_HNS, 0,
                                  nt.byref(wf), None)
                capture = client.GetService(
                    nt.byref(nt.IAudioCaptureClient._iid_)
                ).QueryInterface(nt.IAudioCaptureClient)
                client.Start()
            except Exception as exc:
                self._open_error = exc
                return
            finally:
                self._opened.set()
            while not self._stop.is_set():
                try:
                    n = capture.GetNextPacketSize()
                    while n:
                        data, frames, flags, _dev, _qpc = capture.GetBuffer()
                        nbytes = frames * self._block
                        if flags & _AUDCLNT_BUFFERFLAGS_SILENT:
                            chunk = bytes(nbytes)
                        else:
                            chunk = nt.ctypes.string_at(data, nbytes)
                        capture.ReleaseBuffer(frames)
                        if chunk and self._on_frame is not None:
                            self._on_frame(chunk, time.perf_counter())
                        n = capture.GetNextPacketSize()
                except Exception as exc:
                    logger.warning("process loopback pid=%s mode=%s failed: %s",
                                   self.pid, self.mode, exc)
                    if self._on_error is not None:
                        self._on_error(exc)
                    return
                time.sleep(_POLL_SECONDS)
        finally:
            try:
                if client is not None:
                    client.Stop()
            except Exception as exc:  # best-effort teardown; the client is being dropped
                logger.debug("process loopback Stop() failed: %s", exc)
            capture = client = None
            nt.comtypes.CoUninitialize()


def default_client_factory(pid: int, mode: str, rate: int, channels: int) -> LoopbackClient:
    """C2 default factory: a native process-loopback client."""
    return ProcessLoopbackClient(pid, mode, rate, channels)
