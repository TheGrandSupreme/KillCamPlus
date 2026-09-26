"""Private-font loader: registers bundled fonts for this process only.

Uses AddFontResourceExW with FR_PRIVATE: no install, no admin, no
reboot, invisible to other apps. Undone automatically at process exit.
"""
import ctypes
import os

_FR_PRIVATE = 0x10
_loaded = []


def _fonts_dir():
    try:
        import sys
        base = getattr(sys, "_MEIPASS", None)
        if base is not None:
            cand = os.path.join(base, "fonts")
            if os.path.isdir(cand):
                return cand
    except (TypeError, AttributeError, OSError):
        pass
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")


def load_private_fonts(filenames=("Supreme-Regular.ttf", "Supreme-Bold.ttf")):
    """Register each bundled .ttf; returns [families now visible]."""
    try:
        gdi32 = ctypes.windll.gdi32
    except (AttributeError, OSError):
        return []
    d = _fonts_dir()
    for name in filenames:
        path = os.path.join(d, name)
        try:
            if os.path.isfile(path):
                added = gdi32.AddFontResourceExW(path, _FR_PRIVATE, 0)
                if added:
                    _loaded.append(path)
        except (OSError, TypeError, AttributeError):
            pass
    try:
        import tkinter.font as tkfont
        return [f for f in tkfont.families() if "Supreme" in f]
    except Exception:
        return list(_loaded)
