"""profile_native_session.py — thread-level CPU profiler for a live session.

Runs a REAL recording (native core when engaged, else the Python path;
never fakes _nc_active) for --seconds, sampling per-thread CPU via
Win32 GetThreadTimes (true CPU, sleeps excluded by construction).
yappi is used for function-level detail only if already installed.

Usage:  py -3.11 profile_native_session.py [seconds] [--compressed]
Output: console table + log file in %TEMP%\\profile_native_session\\.
Settings.json is backed up and restored.
"""
import sys
import io
import os
import json
import time
import ctypes
import threading
import traceback
import subprocess

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\tomas\Desktop\KillCam+")

SP = r"C:\Users\tomas\Desktop\KillCam+\settings.json"
backup = open(SP, encoding="utf-8").read()
OUTDIR = os.path.join(os.environ["TEMP"], "profile_native_session")
os.makedirs(OUTDIR, exist_ok=True)

SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 60
COMPRESSED = "--compressed" in sys.argv
USE_NATIVE = "--native" in sys.argv

try:
    import yappi  # optional function-level layer
    HAVE_YAPPI = True
except ImportError:
    HAVE_YAPPI = False

_kernel32 = ctypes.windll.kernel32
try:
    _kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    _kernel32.OpenProcess.restype = ctypes.c_void_p
    _kernel32.OpenThread.restype = ctypes.c_void_p
except Exception:
    pass
_THREAD_QUERY = 0x0040


class _FILETIME(ctypes.Structure):
    _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]


try:
    _kernel32.GetThreadTimes.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME)]
    _kernel32.GetThreadTimes.restype = ctypes.c_bool
    _kernel32.OpenThread.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    _kernel32.OpenThread.restype = ctypes.c_void_p
    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
except Exception:
    pass


def thread_cpu_100ns(tid):
    """(kernel+user) CPU ticks for an OS thread id, or None."""
    try:
        h = _kernel32.OpenThread(_THREAD_QUERY, False, int(tid))
    except Exception:
        return None
    if not h:
        return None
    try:
        c, x, k, u = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
        if not _kernel32.GetThreadTimes(h, c, x, k, u):
            return None
        kernel = (k.high << 32) | k.low
        user = (u.high << 32) | u.low
        return kernel + user
    except Exception:
        return None
    finally:
        try:
            _kernel32.CloseHandle(h)
        except Exception:
            pass


class ThreadSampler:
    """Samples per-thread CPU every `interval` s; reports % of one core."""

    def __init__(self, interval=0.25):
        self.interval = interval
        self._stop = threading.Event()
        self.acc = {}  # tid -> [cpu_ticks, last_seen_name]
        self._prev = {}
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="prof-sampler")

    def _label(self, t):
        try:
            tgt = getattr(t, "_target", None)
            fn = getattr(tgt, "__qualname__", None) or getattr(tgt, "__name__", "?")
        except Exception:
            fn = "?"
        return "%s[%s]" % (t.name, fn)

    def start(self):
        self._t0 = time.monotonic()
        for t in threading.enumerate():
            try:
                tid = t.ident  # threading ident == OS TID on CPython/Windows
            except Exception:
                continue
            if not tid:
                continue
            v = thread_cpu_100ns(tid)
            if v is not None:
                self._prev[tid] = v
                self.acc.setdefault(tid, [0, self._label(t)])
        self._thread.start()

    def _run(self):
        while not self._stop.wait(self.interval):
            for t in threading.enumerate():
                try:
                    tid = t.ident
                except Exception:
                    continue
                if not tid:
                    continue
                v = thread_cpu_100ns(tid)
                if v is None:
                    continue
                p = self._prev.get(tid)
                self._prev[tid] = v
                if p is not None and v >= p:
                    e = self.acc.setdefault(tid, [0, self._label(t)])
                    e[0] += v - p
                    e[1] = self._label(t)

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)
        wall = max(0.25, time.monotonic() - self._t0)
        rows = []
        for tid, (ticks, name) in sorted(self.acc.items()):
            pct = ticks / 1e7 / wall * 100.0  # 100ns -> s -> % of 1 core
            rows.append((pct, name, tid))
        return sorted(rows, reverse=True), wall


class _PROC_T(ctypes.Structure):
    _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]


def proc_cpu_100ns(pid):
    """(kernel+user) CPU ticks for a process id, or None."""
    try:
        h = _kernel32.OpenProcess(0x0400, False, int(pid))
    except Exception:
        return None
    if not h:
        return None
    try:
        c, x, k, u = _PROC_T(), _PROC_T(), _PROC_T(), _PROC_T()
        if not _kernel32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(x),
                                         ctypes.byref(k), ctypes.byref(u)):
            return None
        return ((k.high << 32) | k.low) + ((u.high << 32) | u.low)
    except Exception:
        return None
    finally:
        try:
            _kernel32.CloseHandle(h)
        except Exception:
            pass


def child_pids(exe_sub):
    """Direct-child PIDs whose exe name contains exe_sub (Toolhelp)."""
    try:
        import os as _os

        class _PE(ctypes.Structure):
            # th32DefaultHeapID is ULONG_PTR (8 bytes on x64).
            _fields_ = [("size", ctypes.c_ulong),
                        ("usage", ctypes.c_ulong),
                        ("pid", ctypes.c_ulong),
                        ("heap", ctypes.c_size_t),
                        ("mod", ctypes.c_ulong),
                        ("threads", ctypes.c_ulong),
                        ("ppid", ctypes.c_ulong),
                        ("pri", ctypes.c_long),
                        ("flags", ctypes.c_ulong),
                        ("exe", ctypes.c_wchar * 260)]

        snap = _kernel32.CreateToolhelp32Snapshot(0x2, 0)
        if snap is None or int(snap) < 0:
            return []
        out = []
        try:
            pe = _PE()
            pe.size = ctypes.sizeof(pe)
            ok = _kernel32.Process32FirstW(snap, ctypes.byref(pe))
            while ok:
                try:
                    if (int(pe.ppid) == int(_os.getpid())
                            and exe_sub in str(pe.exe).lower()):
                        out.append(int(pe.pid))
                except (TypeError, ValueError):
                    pass
                ok = _kernel32.Process32NextW(snap, ctypes.byref(pe))
        finally:
            try:
                _kernel32.CloseHandle(snap)
            except Exception:
                pass
        return out
    except Exception:
        return []


def main():
    global rec
    from settings import load_settings
    import recorder as recmod

    s = json.loads(backup)
    s["buffer_seconds"] = 10
    s["fps"] = 60
    s["resolution"] = "1920x1080"
    s["capture_source"] = "monitor"
    s["transport_mode"] = "compressed" if COMPRESSED else "raw"
    if not COMPRESSED and USE_NATIVE:
        s["use_native_core"] = True
    else:
        s["use_native_core"] = False
    json.dump(s, open(SP, "w", encoding="utf-8"), indent=4)

    rec = recmod.Recorder(load_settings())
    if HAVE_YAPPI:
        yappi.set_clock_type("cpu")
        yappi.start(builtins=False, profile_threads=True)
    sampler = ThreadSampler()
    rec.start()
    mode = ("NATIVE" if getattr(rec, "_nc_active", False)
            else "PYTHON-" + rec._transport_mode().upper())
    print("mode=%s enc=%s (profiling %ds)" % (
        mode, rec._live_encoder, SECONDS), flush=True)
    sampler.start()
    import os as _os
    self_pid = _os.getpid()
    proc_prev = {self_pid: proc_cpu_100ns(self_pid)}
    proc_acc = {self_pid: 0}
    proc_names = {self_pid: "python(KillCam+)"}
    t_end = time.monotonic() + SECONDS
    while time.monotonic() < t_end:
        time.sleep(2.0)
        for pid in [self_pid] + child_pids("ffmpeg"):
            v = proc_cpu_100ns(pid)
            if v is None:
                continue
            p = proc_prev.get(pid)
            proc_prev[pid] = v
            if p is not None and v >= p:
                proc_acc[pid] = proc_acc.get(pid, 0) + (v - p)
                if pid != self_pid:
                    proc_names[pid] = "ffmpeg.exe"
    rows, wall = sampler.stop()
    if HAVE_YAPPI:
        yappi.stop()
        fstats = yappi.get_func_stats()
        try:
            fstats.sort("ttot", "desc")
        except Exception:
            pass
    print("--- per-thread CPU (%% of one core, %.0fs wall) ---" % wall, flush=True)
    total = 0.0
    lines = []
    for pct, name, tid in rows:
        total += pct
        line = "%7.2f%%  %s" % (pct, name)
        lines.append(line)
        print(line, flush=True)
    print("TOTAL python process threads: %.2f%% of one core" % total, flush=True)
    print("--- whole-process CPU (%% of one core, GetProcessTimes) ---", flush=True)
    for pid in sorted(proc_acc):
        pct = proc_acc[pid] / 1e7 / wall * 100.0
        line = "%7.2f%%  %s (pid %d)" % (pct, proc_names.get(pid, "?"), pid)
        lines.append(line)
        print(line, flush=True)
    if HAVE_YAPPI:
        print("--- yappi top by ttot (cpu clock) ---", flush=True)
        for f in list(fstats)[:25]:
            line = ("ttot=%8.3fs tsub=%8.3fs n=%d  %s:%s:%s" % (
                f.ttot, f.tsub, f.ncall, f.module, f.lineno, f.name))
            lines.append(line)
            print(line, flush=True)
    else:
        msg = "yappi not installed: pip install yappi for function detail"
        lines.append(msg)
        print(msg, flush=True)
    log = os.path.join(OUTDIR, "profile_%s_%ds.log" % (mode, SECONDS))
    with open(log, "w", encoding="utf-8") as fh:
        fh.write("mode=%s enc=%s wall=%.1f\n" % (mode, rec._live_encoder, wall))
        fh.write("\n".join(lines) + "\n")
    print("log: %s" % log, flush=True)
    print("feed=%d fresh=%d blocks=%d" % (
        rec._feed_total, rec._fresh_total, len(rec._frag_deque)), flush=True)
    rec.stop()


rec = None
try:
    main()
    print("DONE", flush=True)
except Exception:
    traceback.print_exc()
    print("FAILED", flush=True)
finally:
    try:
        if "rec" in dir() and rec is not None:
            rec.stop()
    except Exception:
        pass
    open(SP, "w", encoding="utf-8").write(backup)
    print("settings restored", flush=True)
