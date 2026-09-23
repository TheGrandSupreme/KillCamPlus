"""Per-application volume mixer (Windows audio sessions via pycaw).

Loopback capture receives the already-mixed system output, so per-app
levels are applied at the source: each app's WASAPI session volume.
The mixed result flows through the normal recording pipeline untouched.
"""

import threading

try:
    import comtypes
    from pycaw.pycaw import AudioUtilities
    _HAVE_PYCAW = True
except (ImportError, OSError):
    comtypes = None
    AudioUtilities = None
    _HAVE_PYCAW = False


def available():
    return _HAVE_PYCAW


def _com_init():
    if comtypes is not None:
        try:
            comtypes.CoInitialize()
        except (OSError, ValueError):
            pass


def _display_name(session, exe):
    """Human-readable row label: DisplayName, else process name (.exe
    stripped), else the raw exe id. Resource refs (@%SystemRoot%...) are
    never shown."""
    try:
        disp = session.DisplayName
        if disp and not str(disp).strip().startswith("@"):
            return str(disp).strip()
    except (AttributeError, OSError, ValueError):
        pass
    try:
        proc = session.Process
        if proc is not None:
            name = proc.name() if callable(getattr(proc, "name", None)) else None
            if name:
                name = str(name)
                if name.lower().endswith(".exe"):
                    name = name[:-4]
                if name:
                    return name
    except (AttributeError, OSError, ValueError, Exception):
        pass
    if exe:
        return exe[:-4] if exe.lower().endswith(".exe") else exe
    return "Unknown app"


def _exe_of_session(session):
    """Process exe name for a session; always returns a usable label."""
    try:
        pid = session.ProcessId
    except (AttributeError, OSError, ValueError):
        return "Unknown app"
    if not pid:
        return "System Sounds"
    try:
        import psutil
        name = psutil.Process(pid).name()
        if name:
            return name
    except (ImportError, Exception):
        pass
    try:
        proc = session.Process
        if proc is not None:
            name = proc.name()
            if name:
                return name
    except (AttributeError, Exception):
        pass
    return "Unknown app (pid %d)" % pid


class AppVolumeMixer:
    """Enumerate/set per-app session volumes. Thread-safe."""

    def __init__(self, saved_volumes=None):
        self._lock = threading.Lock()
        self.saved = dict(saved_volumes or {})
        self._seen = set(self.saved)
        self.last_error = None

    def _sessions(self):
        _com_init()
        try:
            return list(AudioUtilities.GetAllSessions())
        except (OSError, ValueError, Exception) as exc:
            self.last_error = str(exc)[:200]
            return []

    def refresh(self):
        """Current per-app levels: [{exe, volume (0-100), pid, active}].

        READ-ONLY w.r.t. the system mixer: live volumes are never
        modified here (see set_volume with live=True, opt-in only).
        Newly seen apps keep their current live level; saved values are
        user intent applied by the recording pipeline when available.
        """
        if not _HAVE_PYCAW:
            return []
        by_exe = {}
        for session in self._sessions():
            try:
                exe = _exe_of_session(session)
                try:
                    vol = session.SimpleAudioVolume
                except (AttributeError, OSError, ValueError):
                    continue
                try:
                    pid = session.ProcessId
                except (AttributeError, OSError, ValueError):
                    pid = None
                try:
                    active = int(session.State) == 1
                except (AttributeError, OSError, ValueError, TypeError):
                    active = True
                try:
                    level = int(round(vol.GetMasterVolume() * 100))
                except (OSError, ValueError, Exception):
                    continue
                # One row per app; keep the loudest session's level.
                # PIDs: keep the sounding (active) process when known.
                prev = by_exe.get(exe)
                if prev is None or level > prev[0]:
                    by_exe[exe] = (max(0, min(100, level)), pid, active,
                                   _display_name(session, exe))
                elif active and not prev[2] and pid:
                    by_exe[exe] = (prev[0], pid, True, prev[3])
            except (OSError, ValueError, Exception):
                continue
        with self._lock:
            for exe in by_exe:
                self._seen.add(exe)
        return [{"exe": exe, "volume": vol, "pid": pid, "active": active,
                 "display": display}
                for exe, (vol, pid, active, display) in sorted(
                    by_exe.items(), key=lambda kv: kv[0].lower())]

    def set_volume(self, exe, volume):
        """Record clip-only volume intent (0-100), always persisted.

        NEVER touches Windows session volumes: with only a single mixed
        loopback tap, live session edits glitch the capture stream, and
        per-app gains need per-app stems (unavailable: process-loopback
        capture requires an interface ID this system cannot provide).
        Intent is stored for the recording pipeline (recorder.app_volumes).
        Returns True when stored (False only without pycaw present).
        """
        if not _HAVE_PYCAW:
            return False
        volume = max(0, min(100, int(volume)))
        with self._lock:
            self.saved[exe] = volume
            self._seen.add(exe)
        return True
