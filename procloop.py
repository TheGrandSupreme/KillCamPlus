"""Native per-process loopback capture (WASAPI application loopback).

Activates an IAudioClient on the process-loopback virtual device for one
target PID, then reads its render mix directly. Each sounding app gets an
independent stem: no system volumes are touched, gains apply per stream.

Recovered interface IDs are validated at runtime (GetService must return
S_OK); every COM call is HRESULT-checked with clean RuntimeError failures.
Only stdlib ctypes + comtypes are used.
"""

import ctypes
import threading
import time
from ctypes import (c_void_p, c_uint32, c_int32, c_ushort, c_ulong,
                    POINTER, Structure, byref, cast, WINFUNCTYPE)
from ctypes.wintypes import DWORD, LPCWSTR
HRESULT = c_int32


def _coinit():
    try:
        import comtypes
        comtypes.CoInitialize()
    except (ImportError, OSError, ValueError):
        pass


# ---- activation plumbing ----
IID_IAUDIOCLIENT = "{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}"
# Recovered from on-disk audio binaries, validated live via GetService.
IID_IAUDIOCAPTURECLIENT = "{C8ADBD64-E71E-48A0-A4DE-185C395CD317}"

VAD_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
ACTIVATION_PROCESS_LOOPBACK = 1

VT_BLOB = 0x0041


class _BLOB(Structure):
    _fields_ = [("cbSize", DWORD), ("pBlobData", c_void_p)]


class _PROPVARIANT(Structure):
    _fields_ = [("vt", c_ushort), ("wReserved1", c_ushort),
                ("wReserved2", c_ushort), ("wReserved3", c_ushort),
                ("blob", _BLOB)]


class _ACTPARAMS(Structure):
    _fields_ = [("ActivationType", c_int32),
                ("ProcessId", DWORD),
                ("IncludeTargetProcessTree", DWORD)]


class _CompletionHandler:
    """Catch-all IUnknown: the system only ever QIs this object for the
    async-completion interface, so every QI is answered affirmatively."""

    def __init__(self):
        self.event = threading.Event()
        self.async_op = None
        from comtypes import GUID
        _QI = WINFUNCTYPE(HRESULT, c_void_p, POINTER(GUID), POINTER(c_void_p))
        _AR = WINFUNCTYPE(c_ulong, c_void_p)
        _DN = WINFUNCTYPE(HRESULT, c_void_p, c_void_p)

        def _qi(this, riid, ppv):
            ppv[0] = this
            return 0

        def _ar(this):
            return 2

        def _dn(this, asyncop):
            self.async_op = asyncop
            self.event.set()
            return 0

        class _VTBL(Structure):
            _fields_ = [("qi", _QI), ("ar", _AR),
                        ("rl", _AR), ("dn", _DN)]

        class _OBJ(Structure):
            _fields_ = [("lpVtbl", POINTER(_VTBL))]

        self._vtbl = _VTBL(_QI(_qi), _AR(_ar), _AR(_ar), _DN(_dn))
        self._obj = _OBJ(ctypes.pointer(self._vtbl))

    @property
    def address(self):
        return ctypes.addressof(self._obj)


def _activate_client(pid):
    """Activate IAudioClient for a process-loopback stream. Returns pointer."""
    from comtypes import GUID
    _coinit()
    params = _ACTPARAMS(ACTIVATION_PROCESS_LOOPBACK, int(pid), 0)
    blob = (ctypes.c_ubyte * ctypes.sizeof(params))()
    ctypes.memmove(blob, byref(params), ctypes.sizeof(params))
    prop = _PROPVARIANT()
    prop.vt = VT_BLOB
    prop.blob.cbSize = ctypes.sizeof(params)
    prop.blob.pBlobData = ctypes.cast(blob, c_void_p).value
    handler = _CompletionHandler()
    mmdev = ctypes.WinDLL("Mmdevapi.dll")
    fn = mmdev.ActivateAudioInterfaceAsync
    fn.restype = HRESULT
    fn.argtypes = [LPCWSTR, POINTER(GUID), POINTER(_PROPVARIANT),
                   c_void_p, POINTER(c_void_p)]
    op = c_void_p()
    hr = fn(VAD_PROCESS_LOOPBACK, byref(GUID(IID_IAUDIOCLIENT)),
            byref(prop), handler.address, byref(op))
    if hr != 0:
        raise RuntimeError("ActivateAudioInterfaceAsync failed: 0x%08X"
                           % (hr & 0xFFFFFFFF))
    if not handler.event.wait(timeout=10):
        raise RuntimeError("loopback activation timed out")
    GETRES = WINFUNCTYPE(HRESULT, c_void_p, POINTER(HRESULT), POINTER(c_void_p))
    vtbl = cast(handler.async_op, POINTER(POINTER(c_void_p)))
    hres = HRESULT()
    cli = c_void_p()
    hr = cast(vtbl[0][3], GETRES)(handler.async_op, byref(hres), byref(cli))
    if hr != 0 or hres.value != 0 or not cli.value:
        raise RuntimeError("loopback activation failed: 0x%08X"
                           % ((hres.value if hr == 0 else hr) & 0xFFFFFFFF))
    return cli.value, handler  # handler kept alive by caller


class ProcessLoopbackStream:
    """One app's render mix. Read via get_read_available()/read_frames()
    (PortAudio-style polling, never blocks), close() to release."""

    def __init__(self, pid, sample_rate=48000, channels=2):
        self.pid = int(pid)
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self._cli = None
        self._cap = None
        self._handler = None
        self._closed = False
        self._open()

    def _v(self, slot):
        return cast(self._cli, POINTER(POINTER(c_void_p)))[0][slot]

    def _open(self):
        from comtypes import GUID
        import struct as _st
        cli, handler = _activate_client(self.pid)
        self._cli = cli
        self._handler = handler  # keep completion alive
        INIT = WINFUNCTYPE(HRESULT, c_void_p, c_int32, DWORD, ctypes.c_int64,
                           ctypes.c_int64, c_void_p, c_void_p)
        GETSVC = WINFUNCTYPE(HRESULT, c_void_p, POINTER(GUID), POINTER(c_void_p))
        tried = []
        for sr in (self.sample_rate, 44100):
            wfmt = _st.pack("<HHIIHHH", 3, self.channels, sr, sr * self.channels * 4,
                            self.channels * 4, 32, 0) + b"\x00" * 22
            wbuf = (ctypes.c_ubyte * 64)(*wfmt)
            hr = cast(self._v(3), INIT)(
                self._cli, 0, 0x00020000, 10000000, 0,
                ctypes.addressof(wbuf), None)
            tried.append((sr, hr))
            if hr == 0:
                self.sample_rate = sr
                break
        else:
            raise RuntimeError("loopback Initialize failed: %s" % tried)
        cap = c_void_p()
        hr = cast(self._v(14), GETSVC)(
            self._cli, byref(GUID(IID_IAUDIOCAPTURECLIENT)), byref(cap))
        if hr != 0 or not cap.value:
            raise RuntimeError("capture interface unavailable: 0x%08X"
                               % (hr & 0xFFFFFFFF))
        self._cap = cap.value

    def start(self):
        START = WINFUNCTYPE(HRESULT, c_void_p)
        hr = cast(self._v(10), START)(self._cli)
        if hr != 0:
            raise RuntimeError("loopback start failed: 0x%08X" % (hr & 0xFFFFFFFF))

    def stop(self):
        if self._cli:
            STOP = WINFUNCTYPE(HRESULT, c_void_p)
            try:
                cast(self._v(11), STOP)(self._cli)
            except (OSError, ValueError):
                pass

    def close(self):
        # Same-thread teardown only (cross-thread close segfaults).
        self._closed = True
        self.stop()
        self._cli = None
        self._cap = None
        self._handler = None

    @property
    def closed(self):
        return self._closed

    def get_read_available(self):
        """Frames ready in the next packet (0 when silent). Never blocks."""
        if not self._cap:
            return 0
        NEXTPKT = WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_uint32))
        cc = cast(self._cap, POINTER(POINTER(c_void_p)))
        try:
            n = c_uint32()
            if cast(cc[0][5], NEXTPKT)(self._cap, byref(n)) != 0:
                return 0
            return int(n.value)
        except (OSError, ValueError):
            return 0

    def read_frames(self, max_frames):
        """Read up to max_frames (float32 stereo bytes + actual count)."""
        import numpy as np
        if not self._cap or max_frames <= 0:
            return b"", 0
        GETBUF = WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_void_p),
                             POINTER(c_uint32), POINTER(DWORD),
                             POINTER(ctypes.c_int64), POINTER(ctypes.c_int64))
        RELBUF = WINFUNCTYPE(HRESULT, c_void_p, c_uint32)
        cc = cast(self._cap, POINTER(POINTER(c_void_p)))
        pdata, nfr, flags = c_void_p(), c_uint32(), DWORD()
        try:
            hr = cast(cc[0][3], GETBUF)(
                self._cap, byref(pdata), byref(nfr), byref(flags), None, None)
        except (OSError, ValueError):
            return b"", 0
        if hr != 0 or not pdata.value or not nfr.value:
            return b"", 0
        got = int(nfr.value)
        try:
            take = min(got, int(max_frames))
            raw = ctypes.string_at(
                pdata.value, take * self.channels * 4)
        finally:
            try:
                # Release the FULL packet even when taking a prefix.
                cast(cc[0][4], RELBUF)(self._cap, got)
            except (OSError, ValueError):
                pass
        return raw, take
