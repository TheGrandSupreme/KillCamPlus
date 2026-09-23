import ctypes
import socket
import sys
import tkinter as tk

from hotkeys import HotkeyManager
from recorder import Recorder
from settings import RESOURCE_DIR, load_settings
from ui import UI

# Localhost lock: a second instance fails to bind and exits safely.
_SINGLE_INSTANCE_PORT = 46731
_single_instance_socket = None


def _acquire_single_instance():
    global _single_instance_socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", _SINGLE_INSTANCE_PORT))
    except OSError:
        try:
            ctypes.windll.user32.MessageBoxW(
                None, "KillCam+ is already running.", "KillCam+", 0x40)
        except (AttributeError, OSError):
            pass
        sys.exit(0)
    _single_instance_socket = sock  # held open for app lifetime

class KillCamApp:
    def __init__(self):
        _acquire_single_instance()
        # Per-monitor DPI awareness: without this Windows bitmap-scales
        # the whole process on scaled displays, making every widget
        # (including slider handles) look blocky/pixelated.
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except (AttributeError, OSError):
                pass
        self.root = tk.Tk()
        self.settings = load_settings()
        self.recorder = Recorder(self.settings, RESOURCE_DIR)
        try:
            self.is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            self.is_admin = False
        self.hotkeys = HotkeyManager(self.recorder, self)
        self.ui = UI(self.root, self.recorder, self.save_clip_callback, self)
        try:
            self.recorder.start()
        except RuntimeError as exc:
            self.ui.notify(str(exc))
        self.hotkeys.reload()

    def save_clip_callback(self, path=None):
        return self.recorder.save_clip(path)

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    KillCamApp().run()
