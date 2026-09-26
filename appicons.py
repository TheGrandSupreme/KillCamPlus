"""Process-icon extraction: exe path -> tkinter PhotoImage (32px), cached.

SHGetFileInfoW for the HICON, GetIconInfo + GetDIBits to read pixels.
Tk photo put() has no reliable per-pixel alpha path, so pixels are
composited onto the row background up front. All GDI objects are
released; any failure returns None (caller falls back to text).
"""
import ctypes
from ctypes import wintypes

_SHGFI_ICON = 0x100
_SHGFI_LARGEICON = 0x0
_BI_RGB = 0
_DIB_RGB_COLORS = 0


class _SHFILEINFOW(ctypes.Structure):
    _fields_ = [("hIcon", wintypes.HICON),
                ("iIcon", ctypes.c_int),
                ("dwAttributes", wintypes.DWORD),
                ("szDisplayName", wintypes.WCHAR * 260),
                ("szTypeName", wintypes.WCHAR * 80)]


class _BITMAP(ctypes.Structure):
    _fields_ = [("bmType", wintypes.LONG),
                ("bmWidth", wintypes.LONG),
                ("bmHeight", wintypes.LONG),
                ("bmWidthBytes", wintypes.LONG),
                ("bmPlanes", wintypes.WORD),
                ("bmBitsPixel", wintypes.WORD),
                ("bmBits", wintypes.LPVOID)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD),
                ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


_cache = {}  # (exe_path.lower(), bg) -> PhotoImage or None
_procs_ready = False


def _setup_procs():
    """Declare ctypes signatures: without restype, 64-bit HDC/HICON
    handles truncate to c_int and every GDI call silently fails."""
    global _procs_ready
    if _procs_ready:
        return True
    try:
        shell32 = ctypes.windll.shell32
        gdi32 = ctypes.windll.gdi32
        user32 = ctypes.windll.user32
    except (AttributeError, OSError):
        return False
    try:
        shell32.SHGetFileInfoW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_void_p, wintypes.UINT, wintypes.UINT]
        shell32.SHGetFileInfoW.restype = ctypes.c_void_p
        user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.c_void_p]
        user32.GetIconInfo.restype = wintypes.BOOL
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.GetDC.restype = wintypes.HDC
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.ReleaseDC.restype = ctypes.c_int
        user32.DestroyIcon.argtypes = [wintypes.HICON]
        user32.DestroyIcon.restype = wintypes.BOOL
        gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                     wintypes.LPVOID]
        gdi32.GetObjectW.restype = ctypes.c_int
        gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP,
                                    wintypes.UINT, wintypes.UINT,
                                    wintypes.LPVOID, ctypes.c_void_p,
                                    wintypes.UINT]
        gdi32.GetDIBits.restype = ctypes.c_int
        gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
        gdi32.DeleteObject.restype = wintypes.BOOL
    except (AttributeError, OSError):
        return False
    _procs_ready = True
    return True


def _exe_path_for_pid(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        import psutil
        return psutil.Process(pid).exe()
    except Exception:
        return None


def get_icon_for_pid(pid, bg="#060606"):
    """PhotoImage for the process icon, or None. Cached per exe path."""
    path = _exe_path_for_pid(pid)
    if not path:
        return None
    bg = str(bg or "#060606")
    key = (path.lower(), bg)
    if key in _cache:
        return _cache[key]
    try:
        img = _extract(path, bg)
    except Exception:
        img = None
    _cache[key] = img
    return img


def _hex_to_rgb(value):
    try:
        h = str(value).lstrip("#")
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except (ValueError, TypeError, IndexError):
        return (6, 6, 6)


def _extract(exe_path, bg):
    if not _setup_procs():
        return None
    try:
        shell32 = ctypes.windll.shell32
        gdi32 = ctypes.windll.gdi32
        user32 = ctypes.windll.user32
    except (AttributeError, OSError):
        return None
    hicon = None
    hbm_color = None
    hbm_mask = None
    try:
        info = _SHFILEINFOW()
        ret = shell32.SHGetFileInfoW(exe_path, 0, ctypes.byref(info),
                                     ctypes.sizeof(info),
                                     _SHGFI_ICON | _SHGFI_LARGEICON)
        if not ret or not info.hIcon:
            return None
        hicon = info.hIcon

        class _ICONINFO(ctypes.Structure):
            _fields_ = [("fIcon", wintypes.BOOL),
                        ("xHotspot", wintypes.DWORD),
                        ("yHotspot", wintypes.DWORD),
                        ("hbmMask", wintypes.HBITMAP),
                        ("hbmColor", wintypes.HBITMAP)]

        ii = _ICONINFO()
        if not user32.GetIconInfo(hicon, ctypes.byref(ii)):
            return None
        hbm_color, hbm_mask = ii.hbmColor, ii.hbmMask
        if not hbm_color or not hbm_mask:
            return None

        bm = _BITMAP()
        if not gdi32.GetObjectW(hbm_color, ctypes.sizeof(bm),
                                ctypes.byref(bm)):
            return None
        w, h = int(bm.bmWidth), int(bm.bmHeight)
        if w <= 0 or h <= 0 or w > 256 or h > 256:
            return None

        screen_dc = user32.GetDC(None)
        try:
            color = (ctypes.c_ubyte * (w * h * 4))()
            hdr = _BITMAPINFOHEADER()
            hdr.biSize = ctypes.sizeof(hdr)
            hdr.biWidth = w
            hdr.biHeight = -h  # top-down
            hdr.biPlanes = 1
            hdr.biBitCount = 32
            hdr.biCompression = _BI_RGB
            if not gdi32.GetDIBits(screen_dc, hbm_color, 0, h,
                                   color, ctypes.byref(hdr),
                                   _DIB_RGB_COLORS):
                return None
            stride = ((w + 31) // 32) * 4
            mask = (ctypes.c_ubyte * (stride * h))()
            hdr.biBitCount = 1
            if not gdi32.GetDIBits(screen_dc, hbm_mask, 0, h,
                                   mask, ctypes.byref(hdr),
                                   _DIB_RGB_COLORS):
                return None
        finally:
            try:
                user32.ReleaseDC(None, screen_dc)
            except (OSError, AttributeError):
                pass

        br, bg_, bb = _hex_to_rgb(bg)
        has_alpha = any(color[i * 4 + 3] != 0 for i in range(w * h))
        rows = []
        for y in range(h):
            cells = []
            for x in range(w):
                o = (y * w + x) * 4
                b, g, r, a = color[o], color[o + 1], color[o + 2], color[o + 3]
                if has_alpha:
                    alpha = a / 255.0
                else:
                    byte = mask[y * stride + x // 8]
                    alpha = 0.0 if (byte >> (7 - (x % 8))) & 1 else 1.0
                fr = int(r * alpha + br * (1.0 - alpha))
                fg = int(g * alpha + bg_ * (1.0 - alpha))
                fb = int(b * alpha + bb * (1.0 - alpha))
                cells.append("#%02x%02x%02x" % (fr, fg, fb))
            # NOTE: put() wants a tuple-of-tuples; brace-delimited row
            # strings are parsed as a single (invalid) color.
            rows.append(tuple(cells))
        try:
            import tkinter as tk
            photo = tk.PhotoImage(width=w, height=h)
            photo.put(tuple(rows))
        except Exception:
            return None
        return photo
    finally:
        try:
            if hicon:
                user32.DestroyIcon(hicon)
            if hbm_color:
                gdi32.DeleteObject(hbm_color)
            if hbm_mask:
                gdi32.DeleteObject(hbm_mask)
        except (OSError, AttributeError):
            pass
