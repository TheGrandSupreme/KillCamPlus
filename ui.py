import tkinter as tk
import ttkbootstrap as tb
from ttkbootstrap.constants import *
from tkinter import filedialog
import keyboard
import time
import os
import threading
import subprocess

from autostart import enable_autostart, disable_autostart, is_autostart_enabled


class BlacklineSlider(tk.Frame):
    """Volume slider with a guaranteed-visible fat black trough line.

    Theme-proof: the track is drawn explicitly on a canvas instead of
    relying on ttk theme styling (which renders trough colors
    inconsistently). API mirrors the used subset of ttk Scale:
    BlacklineSlider(parent, variable, from_=0, to=100, length=150,
    command=None, thumbcolor=...). command(str(value)) fires on user
    drags only, never on programmatic set. dragging is True mid-drag.
    """

    _HEIGHT = 26
    _TRACK_H = 5
    _THUMB_R = 10
    _TRACK_EMPTY = "#5a5f66"  # light gray remainder past the handle

    def __init__(self, parent, variable=None, from_=0, to=100, length=150,
                 command=None, thumbcolor="#58a6ff", **kw):
        try:
            bg = parent.cget("background")
        except (tk.TclError, TypeError):
            bg = "#1c2128"
        kw.setdefault("background", bg)
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("borderwidth", 0)
        super().__init__(parent, **kw)
        self._from = from_
        self._to = to
        self._command = command
        self._thumbcolor = thumbcolor
        self.variable = variable if variable is not None else tk.IntVar(value=from_)
        self.dragging = False
        self._ball = self._make_ball_photo(thumbcolor)
        self.canvas = tk.Canvas(self, height=self._HEIGHT, width=length,
                                background=bg, highlightthickness=0,
                                borderwidth=0)
        self.canvas.pack(fill=X, expand=True)
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        # NOTE: bound on the canvas itself (not the frame): the frame's
        # Configure fires before child geometry settles, leaving a stale
        # thumb; the canvas event carries the true new width.
        self.canvas.bind("<Configure>", lambda _e: self._draw())
        try:
            self._trace = self.variable.trace_add("write", lambda *_: self._draw())
        except (AttributeError, tk.TclError):
            self._trace = None
        self.after_idle(self._draw)

    @staticmethod
    def _make_ball_photo(thumbcolor):
        """Antialiased thumb: rendered 4x in PIL, downscaled LANCZOS.
        Returns PhotoImage or None (caller falls back to canvas oval)."""
        try:
            from PIL import Image, ImageDraw, ImageTk
            r = BlacklineSlider._THUMB_R
            big = (r * 2 + 6) * 4
            img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.ellipse([3, 3, big - 3, big - 3],
                         fill=thumbcolor, outline="#0b0e11", width=8)
            img = img.resize((r * 2 + 6, r * 2 + 6), Image.LANCZOS)
            return ImageTk.PhotoImage(img)
        except (ImportError, OSError, ValueError, tk.TclError):
            return None

    def _frac(self):
        try:
            val = float(self.variable.get())
        except (tk.TclError, ValueError, TypeError):
            val = float(self._from)
        span = float(self._to) - float(self._from)
        if span == 0:
            return 0.0
        return min(1.0, max(0.0, (val - self._from) / span))

    def _draw(self):
        try:
            w = self.canvas.winfo_width()
            if w < 10:
                w = int(self.canvas.cget("width") or 150)
            h = self._HEIGHT
            pad = self._THUMB_R + 2
            self.canvas.delete("all")
            y = h // 2
            right = max(pad + 1, w - pad)
            # Empty track (light gray) full width, then progressive black
            # fill from 0% up to the handle position.
            self.canvas.create_line(pad, y, right, y,
                                    fill=self._TRACK_EMPTY, width=self._TRACK_H,
                                    capstyle="round")
            cx = pad + self._frac() * max(1, w - 2 * pad)
            if cx > pad + 1:
                self.canvas.create_line(pad, y, cx, y,
                                        fill="black", width=self._TRACK_H,
                                        capstyle="round")
            r = self._THUMB_R
            if self._ball is not None:
                self.canvas.create_image(cx, y, image=self._ball)
            else:
                self.canvas.create_oval(cx - r, y - r, cx + r, y + r,
                                        fill=self._thumbcolor,
                                        outline="#0b0e11", width=2)
        except tk.TclError:
            pass

    def set(self, value):
        """Programmatic set: read a stored volume, move the handle to it.

        Clamps, updates the variable, redraws. Never fires command
        (no feedback loops with poll refresh or debounced saves).
        """
        try:
            val = int(round(float(value)))
        except (TypeError, ValueError):
            return
        val = max(self._from, min(self._to, val))
        try:
            self.variable.set(val)
        except (tk.TclError, ValueError):
            return
        self._draw()

    def _set_from_x(self, x):
        try:
            w = self.canvas.winfo_width()
        except tk.TclError:
            return
        pad = self._THUMB_R + 2
        frac = 0.0 if w <= 2 * pad else (x - pad) / (w - 2 * pad)
        frac = min(1.0, max(0.0, frac))
        val = int(round(self._from + frac * (self._to - self._from)))
        try:
            self.variable.set(val)
        except (tk.TclError, ValueError):
            return
        self._draw()
        if self._command is not None:
            try:
                self._command(str(val))
            except (tk.TclError, ValueError, TypeError):
                pass

    def _press(self, event):
        self.dragging = True
        self._set_from_x(event.x)

    def _drag(self, event):
        if self.dragging:
            self._set_from_x(event.x)

    def _release(self, _event):
        self.dragging = False

try:
    import pystray
    from PIL import Image, ImageDraw
    _HAVE_TRAY = True
except (ImportError, OSError):
    pystray = None
    Image = None
    ImageDraw = None
    _HAVE_TRAY = False

try:
    from screeninfo import get_monitors
    _HAVE_SCREENINFO = True
except (ImportError, OSError):
    get_monitors = None
    _HAVE_SCREENINFO = False

try:
    import appmixer
    _HAVE_MIXER = appmixer.available()
except (ImportError, OSError):
    appmixer = None
    _HAVE_MIXER = False


class HotkeyCapture:
    """Headless hold-to-capture hotkey logic (widget-free, unit-testable).

    Tracks held keys, latches the last non-empty combo as the candidate
    (so the user can release before pressing Confirm), and validates.
    """

    MODIFIERS = ("ctrl", "shift", "alt", "windows")
    _ALIASES = {
        "left ctrl": "ctrl", "right ctrl": "ctrl",
        "left shift": "shift", "right shift": "shift",
        "left alt": "alt", "right alt": "alt", "alt gr": "alt",
        "left windows": "windows", "right windows": "windows",
    }

    def __init__(self):
        self.held = set()
        self.candidate = ""

    @classmethod
    def normalize_key(cls, key):
        try:
            key = str(key).lower()
        except (TypeError, ValueError):
            return ""
        return cls._ALIASES.get(key, key)

    @classmethod
    def format_combo(cls, keys):
        """Canonical order: modifiers first, then the rest sorted."""
        keys = [k for k in keys if k]
        mods = [k for k in cls.MODIFIERS if k in keys]
        rest = sorted(k for k in keys if k not in cls.MODIFIERS)
        return "+".join(mods + rest)

    def on_key(self, name, event_type="down"):
        """Feed a keyboard event (name str, event_type 'down'/'up')."""
        key = self.normalize_key(name)
        if not key:
            return
        if event_type == "up":
            self.held.discard(key)
        else:
            self.held.add(key)
            if self.held:
                self.candidate = self.format_combo(self.held)

    def display(self):
        """Currently held combo (live), or '' when nothing held."""
        if not self.held:
            return ""
        return self.format_combo(self.held)

    def combo(self):
        """Latched candidate for Confirm."""
        return self.candidate

    @staticmethod
    def validate(combo, key_name, others):
        """Error string or None. others: {action: combo}."""
        combo = (combo or "").strip().lower()
        if not combo:
            return "Press a hotkey first."
        if combo in HotkeyCapture.MODIFIERS:
            return "Invalid hotkey."
        for name, existing in (others or {}).items():
            if name != key_name and (existing or "").strip().lower() == combo:
                return "Hotkey already in use."
        return None


class UI:
    def __init__(self, root, recorder, save_clip_callback, app):
        self.root = root
        self.recorder = recorder
        self.save_clip_callback = save_clip_callback
        self.app = app

        self.settings = recorder.settings

        self.root.title("KillCam+")
        self.root.geometry("460x520")

        # Official window icon from the bundled icons/ folder
        try:
            icon_path = self.recorder.icon_path()
            if icon_path is not None:
                if icon_path.lower().endswith(".ico"):
                    self.root.iconbitmap(icon_path)
                else:
                    from PIL import ImageTk
                    _icon_img = ImageTk.PhotoImage(file=icon_path)
                    self.root.iconphoto(True, _icon_img)
                    self._window_icon = _icon_img  # keep a reference
        except (tk.TclError, OSError, ValueError, ImportError):
            pass

        # Tk variables
        self.buffer_var = tk.StringVar(value=str(self.settings["buffer_seconds"]))
        self.system_var = tk.BooleanVar(value=self.settings.get("record_system_audio", True))
        self.resolution_var = tk.StringVar(value=self.settings["resolution"])
        self.fps_var = tk.StringVar(value=str(self.settings["fps"]))
        self.audio_mode_var = tk.StringVar(value=str(self.settings["audio_mode"]).lower())
        self.compression_var = tk.StringVar(value=str(self.settings.get("compression", "Medium")))
        self.save_folder_var = tk.StringVar(value=self.settings.get("save_folder", ""))
        self.mic_device_var = tk.StringVar(value=self.settings.get("mic_audio_device", ""))
        self.system_device_var = tk.StringVar(value=self.settings.get("system_audio_device", ""))
        self.mic_volume_var = tk.IntVar(value=self.settings.get("mic_volume", 100))
        self.system_volume_var = tk.IntVar(value=self.settings.get("system_volume", 100))
        self.monitor_var = tk.StringVar()
        self.monitor_indices = [0]

        # Per-app mixer (session enumeration via pycaw; clip-only gains,
        # never writes live system volumes)
        self.app_mixer = appmixer.AppVolumeMixer(
            self.settings.get("app_volumes", {})) if _HAVE_MIXER else None
        self._app_rows = {}   # exe -> (frame, slider_var, slider, pct_label)
        self._app_poll_pending = False
        # Forcefully hydrate the recording engine with saved per-app
        # gains at startup so rows and engine agree before any drag.
        try:
            self.recorder.app_volumes = dict(self.settings.get("app_volumes", {}) or {})
        except AttributeError:
            pass

        # System tray state
        self._tray_icon = None
        self._tray_thread = None
        # Debounced settings persistence (slider drags fire dozens/sec)
        self._save_after_id = None

        # Hotkeys
        self.hk_save = tk.StringVar(value=self.settings["hotkeys"]["save_clip"])
        self.hk_mic = tk.StringVar(value=self.settings["hotkeys"]["toggle_mic"])
        self.hk_sys = tk.StringVar(value=self.settings["hotkeys"]["toggle_system_audio"])

        # Autostart
        self.autostart_var = tk.BooleanVar(
            value=is_autostart_enabled("KillCam+")
        )

        # Volume slider theme: solid black trough line with explicit
        # thickness, always visible regardless of app theme. Applies to
        # every volume slider (mic/system/per-app share these styles).
        try:
            _vol_style = tb.Style()
            for _sname in ("info.Horizontal.TScale",
                           "warning.Horizontal.TScale"):
                _vol_style.configure(_sname, troughcolor="black",
                                     troughrelief="flat", borderwidth=0,
                                     thickness=6, sliderthickness=16,
                                     foreground="black")
        except (tk.TclError, AttributeError):
            pass

        self.build_ui()

    # ---------------------------------------------------
    # Save settings
    # ---------------------------------------------------
    def save_settings(self):
        try:
            self.settings["buffer_seconds"] = max(1, int(self.buffer_var.get()))
            self.settings["fps"] = int(self.fps_var.get())
        except ValueError:
            self.notify("Buffer length and FPS must be numbers.")
            return False
        self.settings["record_system_audio"] = True  # always on; volume slider controls level
        self.settings["resolution"] = self.resolution_var.get()
        self.settings["audio_mode"] = self.audio_mode_var.get()
        comp = str(self.compression_var.get()).strip().capitalize()
        self.settings["compression"] = comp if comp in ("High", "Medium", "Low") else "Medium"
        self.settings["save_folder"] = self.save_folder_var.get()
        mic_device = self.mic_device_var.get().strip()
        system_device = self.system_device_var.get().strip()
        device_changed = (mic_device != self.recorder.mic_device or system_device != self.recorder.system_device)
        self.settings["mic_audio_device"] = mic_device
        self.settings["system_audio_device"] = system_device
        self.recorder.mic_device = self.settings["mic_audio_device"]
        self.recorder.system_device = self.settings["system_audio_device"]
        self.settings["mic_volume"] = self.mic_volume_var.get()
        self.settings["system_volume"] = self.system_volume_var.get()
        self.recorder.mic_volume = self.settings["mic_volume"]
        self.recorder.system_volume = self.settings["system_volume"]
        try:
            mon_idx = self.monitor_indices[
                self.monitor_labels().index(self.monitor_var.get())] \
                if self.monitor_var.get() else 0
        except (ValueError, IndexError, AttributeError):
            mon_idx = int(self.settings.get("monitor_index", 0))
        monitor_changed = (mon_idx != self.recorder.monitor_index)
        self.settings["monitor_index"] = mon_idx
        self.recorder.monitor_index = mon_idx
        if self.app_mixer is not None:
            self.settings["app_volumes"] = dict(self.app_mixer.saved)
        if device_changed and self.recorder.recording:
            self.recorder._restart_audio_capture()
        if monitor_changed and self.recorder.recording:
            try:
                self.recorder._restart_camera()
            except Exception as exc:
                self.notify("Monitor switch failed: %s" % str(exc)[:120])

        self.settings["hotkeys"]["save_clip"] = self.hk_save.get()
        self.settings["hotkeys"]["toggle_mic"] = self.hk_mic.get()
        self.settings["hotkeys"]["toggle_system_audio"] = self.hk_sys.get()

        self.settings["start_with_windows"] = bool(self.autostart_var.get())

        import json
        settings_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(self.settings, f, indent=4)
        self.recorder._reset_buffer_size()
        return True

    def _schedule_settings_save(self, delay_ms=400):
        """Debounced persistence for slider drags (fire dozens/sec).

        Recorder state + labels update synchronously in the drag
        handlers; only the file write + buffer rebuild wait here, so
        drags can never jank capture or the preview loop.
        """
        try:
            if self._save_after_id is not None:
                self.root.after_cancel(self._save_after_id)
        except (tk.TclError, ValueError, AttributeError):
            pass
        try:
            self._save_after_id = self.root.after(
                delay_ms, self._do_save_settings)
        except (tk.TclError, RuntimeError):
            self._save_after_id = None
            self.save_settings()

    def _do_save_settings(self):
        self._save_after_id = None
        try:
            self.save_settings()
        except (tk.TclError, ValueError, OSError):
            pass

    # ---------------------------------------------------
    # Notification popup
    # ---------------------------------------------------
    def notify(self, message, duration=2000):
        toast = tk.Toplevel(self.root)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)

        screen_width = toast.winfo_screenwidth()
        width = 163
        height = 44
        x = screen_width - width - 20
        y = 20

        toast.geometry(f"{width}x{height}+{x}+{y}")
        toast.configure(bg="black")

        frame = tk.Frame(toast, bg="black",
                         highlightbackground="#3a3a3a", highlightthickness=1)
        frame.pack(fill="both", expand=True)
        label = tk.Label(frame, text=message, font=("Segoe UI", 10),
                         bg="black", fg="white", wraplength=145,
                         justify="center")
        label.pack(fill="both", expand=True)
        # Re-assert after mapping: the ttkbootstrap theme overrides
        # classic-tk colors at creation time; post-creation configure
        # sticks (verified).
        try:
            frame.configure(bg="black")
            label.configure(bg="black", fg="white")
            toast.update_idletasks()
        except tk.TclError:
            pass

        toast.after(duration, toast.destroy)

    # ---------------------------------------------------
    # UI Layout
    # ---------------------------------------------------
    def build_ui(self):
        main_frame = tb.Frame(self.root, padding=10)
        main_frame.pack(fill=BOTH, expand=True)

        notebook = tb.Notebook(main_frame, bootstyle="dark")
        notebook.pack(fill=BOTH, expand=True)

        recording_tab = tb.Frame(notebook, padding=10)
        settings_tab = tb.Frame(notebook, padding=10)
        hotkeys_tab = tb.Frame(notebook, padding=10)
        output_tab = tb.Frame(notebook, padding=10)
        about_tab = tb.Frame(notebook, padding=10)

        notebook.add(recording_tab, text="Recording")
        notebook.add(settings_tab, text="Settings")
        notebook.add(hotkeys_tab, text="Hotkeys")
        notebook.add(output_tab, text="Output")
        notebook.add(about_tab, text="About")

        # ---- Recording Tab ----
        tb.Label(recording_tab, text="Replay Buffer (seconds):").pack(anchor=W)
        tb.Combobox(
            recording_tab, textvariable=self.buffer_var,
            values=["5", "10", "15", "20", "30", "45", "60", "90", "120"],
            state="readonly", width=10,
        ).pack(anchor=W, pady=(0, 8))

        # Video preview + audio levels
        preview_frame = tb.Labelframe(recording_tab, text="Preview")
        preview_frame.pack(fill=BOTH, expand=True, pady=(0, 5))

        self.preview_canvas = tk.Canvas(preview_frame, bg="#0d1117", highlightthickness=0)
        self.preview_canvas.pack(fill=BOTH, expand=True)

        # Smoothed audio levels (EMA)
        self._smooth_mic = 0.0
        self._smooth_sys = 0.0

        # ---- Settings Tab ----
        # Scrollable container for settings
        settings_canvas = tk.Canvas(settings_tab, highlightthickness=0)
        settings_scrollbar = tb.Scrollbar(settings_tab, orient="vertical", command=settings_canvas.yview)
        settings_inner = tb.Frame(settings_canvas)
        settings_inner.bind("<Configure>", lambda e: settings_canvas.configure(scrollregion=settings_canvas.bbox("all")))
        settings_canvas.create_window((0, 0), window=settings_inner, anchor="nw")
        settings_canvas.configure(yscrollcommand=settings_scrollbar.set)
        settings_scrollbar.pack(side=RIGHT, fill=Y)
        settings_canvas.pack(fill=BOTH, expand=True)

        # Recording toggle
        tb.Label(settings_inner, text="Recording", font=("Segoe UI", 10, "bold")).pack(anchor=W, pady=(0, 4))
        tb.Label(settings_inner, text="Microphone is always recording; mute it with the volume slider or hotkey.",
                 font=("Segoe UI", 8), foreground="#8b949e", wraplength=380,
                 justify=LEFT).pack(anchor=W, pady=(0, 6))

        # Audio devices
        tb.Label(settings_inner, text="Audio Devices", font=("Segoe UI", 10, "bold")).pack(anchor=W, pady=(8, 4))
        tb.Label(settings_inner, text="Microphone input:").pack(anchor=W)
        self.mic_device_box = tb.Combobox(
            settings_inner, textvariable=self.mic_device_var,
            values=[self.mic_device_var.get()], state="readonly", width=36,
            postcommand=self.refresh_audio_devices)
        self.mic_device_box.pack(anchor=W, pady=(0, 6))

        tb.Label(settings_inner, text="System-audio input:").pack(anchor=W)
        self.system_device_box = tb.Combobox(
            settings_inner, textvariable=self.system_device_var,
            values=[self.system_device_var.get()], state="readonly", width=36,
            postcommand=self.refresh_audio_devices)
        self.system_device_box.pack(anchor=W, pady=(0, 4))
        self.audio_status = tb.Label(settings_inner, text="", foreground="red", font=("Segoe UI", 9))
        self.audio_status.pack(anchor=W, pady=(0, 6))

        # Volume sliders
        tb.Label(settings_inner, text="Volume", font=("Segoe UI", 10, "bold")).pack(anchor=W, pady=(8, 4))

        mic_vol_frame = tb.Frame(settings_inner)
        mic_vol_frame.pack(fill=X, pady=(0, 2))
        tb.Label(mic_vol_frame, text="Microphone:").pack(side=LEFT)
        self.mic_vol_label = tb.Label(mic_vol_frame, text=f"{self.mic_volume_var.get()}%", width=4)
        self.mic_vol_label.pack(side=RIGHT)
        self.mic_vol_slider = BlacklineSlider(
            settings_inner, variable=self.mic_volume_var, from_=0, to=100,
            thumbcolor="#58a6ff",
            command=lambda v: self._on_mic_volume_change(v))
        self.mic_vol_slider.pack(fill=X, pady=(0, 6))

        sys_vol_frame = tb.Frame(settings_inner)
        sys_vol_frame.pack(fill=X, pady=(0, 2))
        tb.Label(sys_vol_frame, text="System:").pack(side=LEFT)
        self.sys_vol_label = tb.Label(sys_vol_frame, text=f"{self.system_volume_var.get()}%", width=4)
        self.sys_vol_label.pack(side=RIGHT)
        self.sys_vol_slider = BlacklineSlider(
            settings_inner, variable=self.system_volume_var, from_=0, to=100,
            thumbcolor="#d29922",
            command=lambda v: self._on_system_volume_change(v))
        self.sys_vol_slider.pack(fill=X, pady=(0, 6))

        # Video settings
        tb.Label(settings_inner, text="Video", font=("Segoe UI", 10, "bold")).pack(anchor=W, pady=(8, 4))

        fps_frame = tb.Frame(settings_inner)
        fps_frame.pack(fill=X, pady=(0, 4))
        tb.Label(fps_frame, text="FPS:").pack(side=LEFT)
        tb.Combobox(
            fps_frame, textvariable=self.fps_var,
            values=["30", "60", "120", "240"],
            state="readonly", width=8,
        ).pack(side=LEFT, padx=(8, 0))

        res_frame = tb.Frame(settings_inner)
        res_frame.pack(fill=X, pady=(0, 4))
        tb.Label(res_frame, text="Resolution:").pack(side=LEFT)
        tb.Combobox(
            res_frame, textvariable=self.resolution_var,
            values=["1280x720", "1600x900", "1920x1080", "2560x1440", "3840x2160"],
            state="readonly", width=14,
        ).pack(side=LEFT, padx=(8, 0))

        mon_frame = tb.Frame(settings_inner)
        mon_frame.pack(fill=X, pady=(0, 4))
        tb.Label(mon_frame, text="Monitor:").pack(side=LEFT)
        self.monitor_box = tb.Combobox(
            mon_frame, textvariable=self.monitor_var,
            values=self.monitor_labels(), state="readonly", width=28,
        )
        self.monitor_box.pack(side=LEFT, padx=(8, 0))
        self.monitor_box.bind("<<ComboboxSelected>>", lambda _e: self.on_monitor_selected())

        comp_frame = tb.Frame(settings_inner)
        comp_frame.pack(fill=X, pady=(0, 2))
        tb.Label(comp_frame, text="Compression:").pack(side=LEFT)
        tb.Combobox(
            comp_frame, textvariable=self.compression_var,
            values=["High", "Medium", "Low"],
            state="readonly", width=14,
        ).pack(side=LEFT, padx=(8, 0))
        tb.Label(
            settings_inner,
            text="High: Better performance/More quality loss (Low-end machines)\n"
                 "Medium: Medium performance/Medium quality loss (Mid-range machines)\n"
                 "Low: Worse Performance/Almost 0 quality loss (High-end machines)\n"
                 "Applies on restart.",
            font=("Segoe UI", 8), foreground="#8b949e",
        ).pack(anchor=W, pady=(0, 4))

        # Audio mode
        tb.Label(settings_inner, text="Audio Mode:").pack(anchor=W, pady=(8, 4))
        tb.Combobox(
            settings_inner, textvariable=self.audio_mode_var,
            values=["mixed", "separate"], state="readonly", width=15,
        ).pack(anchor=W, pady=(0, 6))

        # Per-app volume mixer
        tb.Label(settings_inner, text="App Volumes", font=("Segoe UI", 10, "bold")).pack(anchor=W, pady=(8, 4))
        if _HAVE_MIXER:
            tb.Label(
                settings_inner,
                text="Clip-only levels: Windows volumes are never touched.\n"
                     "Per-app gains apply inside the recording pipeline.",
                font=("Segoe UI", 8), foreground="#8b949e",
            ).pack(anchor=W, pady=(0, 4))
            self.app_mixer_frame = tb.Frame(settings_inner)
            self.app_mixer_frame.pack(fill=X, pady=(0, 6))
        else:
            tb.Label(
                settings_inner,
                text="Per-app mixer unavailable (pycaw not installed).",
                font=("Segoe UI", 8), foreground="#8b949e",
            ).pack(anchor=W, pady=(0, 6))
            self.app_mixer_frame = None

        # Autostart
        tb.Label(settings_inner, text="System", font=("Segoe UI", 10, "bold")).pack(anchor=W, pady=(8, 4))
        tb.Checkbutton(
            settings_inner, text="Start KillCam+ with Windows",
            variable=self.autostart_var, bootstyle="round-toggle",
            command=self.on_autostart_toggle,
        ).pack(anchor=W, pady=(0, 6))

        # ---- Hotkeys Tab ----
        tb.Label(hotkeys_tab, text="Global Hotkeys", font=("Segoe UI", 14, "bold")).pack(anchor=CENTER, pady=(0, 15))
        self.add_hotkey_editor(hotkeys_tab, "Save Clip", self.hk_save, "save_clip")
        self.add_hotkey_editor(hotkeys_tab, "Toggle Mic", self.hk_mic, "toggle_mic")
        self.add_hotkey_editor(hotkeys_tab, "Toggle System Audio", self.hk_sys, "toggle_system_audio")

        # ---- Output Tab ----
        tb.Label(output_tab, text="Save Folder:").pack(anchor=W)
        folder_frame = tb.Frame(output_tab)
        folder_frame.pack(fill=X, pady=(0, 10))
        self.folder_box = tb.Combobox(
            folder_frame, textvariable=self.save_folder_var,
            values=self.output_folders(), state="readonly", width=35,
            postcommand=self.refresh_output_folders)
        self.folder_box.pack(side=LEFT, fill=X, expand=True)
        tb.Button(folder_frame, text="Browse", command=self.choose_folder).pack(side=LEFT, padx=5)

        # ---- About Tab ----
        tb.Label(about_tab, text="KillCam+", font=("Segoe UI", 18, "bold")).pack(pady=10)
        tb.Label(about_tab, text="v 0.0.1", font=("Segoe UI", 10)).pack(pady=(0, 6))
        tb.Label(
            about_tab,
            text="Light-weight clipping software\nMade by TGS\nPowered by KillCam+",
            justify=CENTER,
        ).pack()

        # ---- Bottom controls ----
        controls = tb.Frame(main_frame)
        controls.pack(fill=X, pady=(10, 0))
        tb.Button(controls, text="Save clip now", bootstyle="primary", command=self.save_clip).pack(side=LEFT, padx=8)
        self.stream_status = tb.Label(controls, text="", font=("Consolas", 8), foreground="#8b949e")
        self.stream_status.pack(side=LEFT, padx=8)

        # ---- Wiring ----
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        for selector in (self.buffer_var, self.resolution_var, self.fps_var, self.audio_mode_var,
                         self.compression_var):
            selector.trace_add("write", lambda *_: self.save_settings())
        for selector in (self.mic_device_box, self.system_device_box, self.folder_box):
            selector.bind("<<ComboboxSelected>>", lambda _event: self.save_settings())
        self.refresh_audio_devices()
        self.root.after(10_000, self.schedule_device_refresh)
        self.root.after(500, self.update_preview)
        self.root.after(500, self.schedule_app_mixer_poll)
        # Explicit engine sync at startup: push every persisted volume
        # into the recorder + labels so UI, engine, and file agree from
        # frame one, regardless of construction order.
        self._push_volumes_to_engine()

    def _push_volumes_to_engine(self):
        """Sync persisted volumes into slider vars, labels, and recorder."""
        try:
            mic = max(0, min(100, int(self.settings.get("mic_volume", 100))))
        except (ValueError, TypeError):
            mic = 100
        try:
            sysv = max(0, min(100, int(self.settings.get("system_volume", 100))))
        except (ValueError, TypeError):
            sysv = 100
        try:
            self.mic_volume_var.set(mic)
            self.system_volume_var.set(sysv)
        except (tk.TclError, ValueError):
            pass
        for _slider, _val in ((getattr(self, "mic_vol_slider", None), mic),
                              (getattr(self, "sys_vol_slider", None), sysv)):
            try:
                if _slider is not None:
                    _slider.set(_val)
            except (tk.TclError, ValueError, AttributeError):
                pass
        self.recorder.mic_volume = mic
        self.recorder.system_volume = sysv
        self.settings["mic_volume"] = mic
        self.settings["system_volume"] = sysv
        try:
            self.mic_vol_label.config(text=f"{mic}%")
            self.sys_vol_label.config(text=f"{sysv}%")
        except (tk.TclError, AttributeError):
            pass
        try:
            self.recorder.app_volumes = dict(
                self.settings.get("app_volumes", {}) or {})
        except AttributeError:
            pass

    # ---------------------------------------------------
    # Preview updater
    # ---------------------------------------------------
    def update_preview(self):
        """Update the preview canvas with live video and smoothed audio levels."""
        try:
            canvas = self.preview_canvas
            canvas.delete("all")
            w = canvas.winfo_width()
            h = canvas.winfo_height()
            if w < 10 or h < 10:
                self.root.after(100, self.update_preview)
                return

            is_recording = self.recorder.recording

            if is_recording:
                canvas.configure(bg="#0d1117")

                # --- Video preview ---
                thumb_bytes = None
                with self.recorder.preview_lock:
                    thumb_bytes = self.recorder.preview_jpeg

                if thumb_bytes:
                    import io
                    from PIL import Image, ImageTk
                    img = Image.open(io.BytesIO(thumb_bytes))
                    # Scale to fit canvas while keeping aspect ratio
                    iw, ih = img.size
                    scale = min(w / iw, h * 0.75 / ih)  # leave room for audio bars
                    new_w = max(1, int(iw * scale))
                    new_h = max(1, int(ih * scale))
                    img = img.resize((new_w, new_h), Image.LANCZOS)
                    self._preview_photo = ImageTk.PhotoImage(img)
                    canvas.create_image(w // 2, int(h * 0.38), image=self._preview_photo, anchor=CENTER)
                else:
                    canvas.create_text(w // 2, int(h * 0.35), text="Starting...",
                                       fill="#484f58", font=("Segoe UI", 11))

                # --- Smoothed audio level bars ---
                alpha = 0.3  # EMA smoothing (lower = smoother, 0.1-0.4 good range)
                raw_mic = float(list(self.recorder.mic_audio_buffer)[-1]) if self.recorder.mic_audio_buffer else 0.0
                raw_sys = float(list(self.recorder.system_audio_buffer)[-1]) if self.recorder.system_audio_buffer else 0.0
                self._smooth_mic = alpha * raw_mic + (1 - alpha) * self._smooth_mic
                self._smooth_sys = alpha * raw_sys + (1 - alpha) * self._smooth_sys

                mic_pct = min(1.0, self._smooth_mic / 3000.0)
                sys_pct = min(1.0, self._smooth_sys / 3000.0)

                # Bar dimensions
                bar_x = int(w * 0.12)
                bar_w = int(w * 0.76)
                bar_h = 8
                gap = 22

                # Mic bar
                mic_y = int(h * 0.82)
                canvas.create_text(bar_x, mic_y - 8, text="MIC",
                                   fill="#58a6ff", font=("Consolas", 8), anchor=W)
                canvas.create_rectangle(bar_x, mic_y, bar_x + bar_w, mic_y + bar_h,
                                        fill="#161b22", outline="#30363d")
                fill_w = int(bar_w * mic_pct)
                canvas.create_rectangle(bar_x, mic_y, bar_x + fill_w, mic_y + bar_h,
                                        fill="#58a6ff", outline="")

                # System bar
                sys_y = mic_y + gap
                canvas.create_text(bar_x, sys_y - 8, text="SYS",
                                   fill="#d29922", font=("Consolas", 8), anchor=W)
                canvas.create_rectangle(bar_x, sys_y, bar_x + bar_w, sys_y + bar_h,
                                        fill="#161b22", outline="#30363d")
                fill_w = int(bar_w * sys_pct)
                canvas.create_rectangle(bar_x, sys_y, bar_x + fill_w, sys_y + bar_h,
                                        fill="#d29922", outline="")
            else:
                canvas.configure(bg="#1a1a2e")
                self._smooth_mic = 0.0
                self._smooth_sys = 0.0
                canvas.create_text(w // 2, h // 2, text="Not Recording",
                                   fill="#484f58", font=("Segoe UI", 12))

        except Exception:
            pass

        # Stream health: encoder, ring fill, audio dropouts, errors
        try:
            rec = self.recorder
            if getattr(rec, "recording", False):
                fill = rec.frag_fill_frac() * 100.0
                drops = getattr(rec, "audio_drops", {})
                try:
                    nmic = len(rec.mic_audio_replay)
                    nsys = len(rec.sys_audio_replay)
                except (AttributeError, TypeError):
                    nmic = nsys = -1
                serr = getattr(rec, "last_stream_error", None)
                sinfo = getattr(rec, "last_save_info", None)
                txt = "enc=%s ring=%.0f%% ach mic=%d sys=%d drops mic=%s sys=%s" % (
                    getattr(rec, "_live_encoder", "?"), fill, nmic, nsys,
                    drops.get("mic", "?"), drops.get("sys", "?"))
                if serr:
                    txt += " ERR: %s" % serr
                elif sinfo:
                    txt += " (%s)" % sinfo
                self.stream_status.configure(text=txt)
            else:
                self.stream_status.configure(text="")
        except Exception:
            pass

        self.root.after(100, self.update_preview)

    # ---------------------------------------------------
    # Monitor selection
    # ---------------------------------------------------
    def monitor_labels(self):
        """Dropdown labels for connected displays; indices align with dxcam."""
        try:
            if _HAVE_SCREENINFO:
                monitors = list(get_monitors())
            else:
                monitors = []
        except Exception:
            monitors = []
        if not monitors:
            self.monitor_indices = [max(0, int(self.settings.get("monitor_index", 0)))]
            return ["Monitor %d" % (self.monitor_indices[0] + 1)]
        labels = []
        self.monitor_indices = list(range(len(monitors)))
        for i, mon in enumerate(monitors):
            tag = "primary" if getattr(mon, "is_primary", False) else "secondary"
            labels.append("Monitor %d: %dx%d (%s)" % (i + 1, mon.width, mon.height, tag))
        idx = max(0, int(self.settings.get("monitor_index", 0)))
        if idx < len(labels):
            self.monitor_var.set(labels[idx])
        else:
            self.monitor_var.set(labels[0])
        return labels

    def on_monitor_selected(self):
        try:
            idx = self.monitor_labels().index(self.monitor_var.get())
            monitor_idx = self.monitor_indices[idx]
        except (ValueError, IndexError, AttributeError):
            return
        self.settings["monitor_index"] = monitor_idx
        self.recorder.monitor_index = monitor_idx
        self.save_settings()
        if self.recorder.recording:
            try:
                self.recorder._restart_camera()
            except Exception as exc:
                self.notify("Monitor switch failed: %s" % str(exc)[:120])

    # ---------------------------------------------------
    # Per-app volume mixer UI
    # ---------------------------------------------------
    def schedule_app_mixer_poll(self):
        if self.app_mixer is None or self.app_mixer_frame is None:
            return
        if not self._app_poll_pending:
            self._app_poll_pending = True
            threading.Thread(target=self._poll_app_volumes, daemon=True).start()
        try:
            self.root.after(2000, self.schedule_app_mixer_poll)
        except (RuntimeError, tk.TclError):
            pass

    def _poll_app_volumes(self):
        try:
            apps = self.app_mixer.refresh()
        except Exception:
            apps = []
        self._app_poll_pending = False
        # Feed {exe: (pid, volume, active)} for EVERY enumerated session
        # plus System Sounds: gains apply unconditionally, capture
        # threads additionally require a live, sounding PID.
        try:
            if self.recorder is not None:
                saved = (self.app_mixer.saved
                         if self.app_mixer is not None else {})
                self.recorder.sync_app_captures({
                    a.get("exe"): (a.get("pid"),
                                   saved.get(a.get("exe"), 100),
                                   a.get("active", True))
                    for a in apps if a.get("exe")})
        except Exception:
            pass
        try:
            self.root.after(0, lambda: self._rebuild_app_rows(apps))
        except (RuntimeError, tk.TclError):
            pass

    def _rebuild_app_rows(self, apps):
        frame = self.app_mixer_frame
        if frame is None:
            return
        try:
            live = {a["exe"]: a["volume"] for a in apps}
        except (TypeError, KeyError):
            return
        if not live and self._app_rows:
            return  # transient enumeration failure: keep existing rows
        # Rows cover every enumerated session plus anything with a saved
        # intent (native-mixer parity: sounding, idle, and System Sounds
        # all appear; expired clutter without intent is dropped). Labels
        # use the friendly display name; identity stays the exe key.
        saved = self.app_mixer.saved if self.app_mixer is not None else {}
        shown = set(live) | {exe for exe in saved if exe not in live}
        labels = {}
        try:
            for a in apps:
                if a.get("exe") in shown:
                    labels[a["exe"]] = a.get("display") or a["exe"]
        except (TypeError, KeyError, AttributeError):
            labels = {}
        for exe in sorted(set(saved) - set(live)):
            labels.setdefault(exe, exe)
        # Displayed value = clip-gain intent (persisted), defaulting to
        # 100% for newly detected apps. Never the live system level
        # (otherwise the poll snaps the handle back after every drag,
        # and live levels are not ours to display as clip gains).
        display = {}
        for exe in shown:
            display[exe] = saved.get(exe, 100)
        if set(display) != set(self._app_rows):
            # App set changed: rebuild rows
            for child in list(frame.winfo_children()):
                try:
                    child.destroy()
                except tk.TclError:
                    pass
            self._app_rows.clear()
            for exe in sorted(display, key=str.casefold):
                self._make_app_row(frame, exe, labels.get(exe, exe),
                                   display[exe])
        else:
            # Same apps: refresh handles to intent (skip mid-drag)
            for exe, (row, var, slider, pct) in self._app_rows.items():
                if getattr(slider, "dragging", False):
                    continue
                try:
                    var.set(display[exe])
                    pct.configure(text="%d%%" % display[exe])
                except (tk.TclError, KeyError):
                    pass

    def _make_app_row(self, parent, exe, label, volume):
        row = tb.Frame(parent)
        row.pack(fill=X, pady=(0, 2))
        tb.Label(row, text=label, width=22, anchor=W,
                 font=("Consolas", 8)).pack(side=LEFT)
        pct = tb.Label(row, text="%d%%" % volume, width=4,
                       font=("Consolas", 8))
        pct.pack(side=RIGHT)
        var = tk.IntVar(value=volume)
        slider = BlacklineSlider(
            row, variable=var, from_=0, to=100, length=150,
            thumbcolor="#58a6ff",
            command=lambda v, e=exe, p=pct: self._on_app_volume_change(e, v, p))
        slider.pack(side=RIGHT, padx=(0, 6), fill=X, expand=True)
        # Explicit read-then-set: handle follows the stored value.
        try:
            slider.set(volume)
        except (tk.TclError, ValueError, AttributeError):
            pass
        self._app_rows[exe] = (row, var, slider, pct)
        # Force the displayed (saved-or-live) value into recorder memory
        # right away so engine state never lags the UI.
        try:
            self.recorder.app_volumes[exe] = int(volume)
        except (AttributeError, TypeError, ValueError):
            pass

    def _on_app_volume_change(self, exe, value, pct_label):
        # Clip-only: record intent locally (+ recorder mirror dict) and
        # persist. NEVER touches live Windows sessions: session edits
        # mid-stream glitch the shared loopback capture (dropouts heard
        # as stutter across the whole clip).
        try:
            vol = max(0, min(100, int(float(value))))
        except (ValueError, TypeError):
            return
        try:
            pct_label.configure(text="%d%%" % vol)
        except tk.TclError:
            pass
        try:
            row = self._app_rows.get(exe)
            if row is not None:
                row[1].set(vol)  # keep Tk var in sync (no command refire)
        except (tk.TclError, ValueError, IndexError):
            pass
        if self.app_mixer is not None:
            if self.app_mixer.set_volume(exe, vol):
                self.settings["app_volumes"] = dict(self.app_mixer.saved)
                try:
                    self.recorder.app_volumes = dict(self.app_mixer.saved)
                except AttributeError:
                    pass
                # Debounced: recorder + labels already live; only the
                # file write + buffer rebuild wait (never janks capture).
                self._schedule_settings_save()

    # ---------------------------------------------------
    # System tray
    # ---------------------------------------------------
    def _tray_image(self):
        # Official icon asset from the bundled icons/ folder, crisp at
        # tray size. Falls back to a generated dot if missing/unreadable.
        try:
            path = self.recorder.icon_path()
            if path is not None and Image is not None:
                img = Image.open(path)
                img.load()
                if getattr(img, "is_animated", False):
                    img.seek(0)
                return img.convert("RGBA").resize((64, 64), Image.LANCZOS)
        except (OSError, ValueError, AttributeError):
            pass
        img = Image.new("RGB", (64, 64), "#0d1117")
        draw = ImageDraw.Draw(img)
        draw.ellipse([18, 18, 46, 46], fill="#f85149")
        return img

    def _ensure_tray(self):
        if not _HAVE_TRAY or self._tray_icon is not None:
            return self._tray_icon is not None
        try:
            menu = pystray.Menu(
                pystray.MenuItem("Open App", lambda: self.tray_show()),
                pystray.MenuItem(
                    lambda text: "Pause" if self.recorder.recording else "Resume",
                    lambda: self.tray_pause_resume()),
                pystray.MenuItem("Exit", lambda: self.tray_exit()),
            )
            self._tray_icon = pystray.Icon(
                "KillCam+", self._tray_image(), "KillCam+", menu)
            self._tray_thread = threading.Thread(
                target=self._tray_icon.run, daemon=True)
            self._tray_thread.start()
            return True
        except (OSError, ValueError, Exception):
            self._tray_icon = None
            return False

    def tray_show(self):
        try:
            self.root.after(0, self._show_window)
        except (RuntimeError, tk.TclError):
            pass

    def _show_window(self):
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:
            pass

    def tray_pause_resume(self):
        try:
            self.root.after(0, self._pause_resume_now)
        except (RuntimeError, tk.TclError):
            pass

    def _pause_resume_now(self):
        try:
            if self.recorder.recording:
                self.recorder.stop()
                self.notify("Recording paused.")
            else:
                self.recorder.start()
                self.notify("Recording resumed.")
        except RuntimeError as exc:
            self.notify(str(exc)[:120])

    def tray_exit(self):
        icon, self._tray_icon = self._tray_icon, None
        if icon is not None:
            try:
                icon.stop()
            except (OSError, ValueError, Exception):
                pass
        try:
            self.root.after(0, self._exit_now)
        except (RuntimeError, tk.TclError):
            pass

    def _exit_now(self):
        try:
            self.save_settings()
        except Exception:
            pass
        try:
            self.recorder.stop()
        except Exception:
            pass
        try:
            self.app.hotkeys.clear()
        except Exception:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    # ---------------------------------------------------
    # Audio device refresh
    # ---------------------------------------------------
    def refresh_audio_devices(self):
        try:
            import soundcard as sc
            microphones = sorted({device.name for device in sc.all_microphones(include_loopback=False)}, key=str.casefold)
            speakers = sorted({device.name for device in sc.all_microphones(include_loopback=True) if getattr(device, "isloopback", False)}, key=str.casefold)
        except Exception:
            microphones, speakers = [], []
        if not microphones:
            microphones = [self.mic_device_var.get()] if self.mic_device_var.get() else []
        if not speakers:
            speakers = [self.system_device_var.get()] if self.system_device_var.get() else []
        self.mic_device_box.configure(values=microphones)
        self.system_device_box.configure(values=speakers)
        err = getattr(self.recorder, "last_audio_error", None)
        self.audio_status.configure(text=err or "")

    def schedule_device_refresh(self):
        self.refresh_audio_devices()
        self.root.after(10_000, self.schedule_device_refresh)

    def output_folders(self):
        candidates = [self.save_folder_var.get(), os.path.join(os.path.expanduser("~"), "Videos", "Captures"), os.path.join(os.path.expanduser("~"), "Videos"), os.path.join(os.path.expanduser("~"), "Desktop")]
        return list(dict.fromkeys(path for path in candidates if path and os.path.isdir(path)))

    def refresh_output_folders(self):
        self.folder_box.configure(values=self.output_folders())

    # ---------------------------------------------------
    # Hotkey editor row
    # ---------------------------------------------------
    def add_hotkey_editor(self, parent, label, var, key_name):
        frame = tb.Frame(parent, padding=10)
        frame.pack(fill=X, pady=5)
        tb.Label(frame, text=label, font=("Segoe UI", 12, "bold")).pack(anchor=W)
        tb.Label(frame, textvariable=var, font=("Segoe UI", 10)).pack(anchor=W)
        tb.Button(
            frame, text="Change", bootstyle="secondary",
            command=lambda: self.open_hotkey_popup(var, key_name),
        ).pack(anchor=E, pady=5)

    # ---------------------------------------------------
    # Hotkey capture popup (hold combo, then Confirm)
    # ---------------------------------------------------
    def open_hotkey_popup(self, var, key_name):
        popup = tk.Toplevel(self.root)
        popup.title("Set Hotkey")
        popup.geometry("320x220")
        popup.grab_set()

        tb.Label(popup, text="Hold your new hotkey...", font=("Segoe UI", 12)).pack(pady=(10, 2))
        preview = tb.Label(popup, text="", font=("Segoe UI", 11, "bold"))
        preview.pack(pady=2)
        hint = tb.Label(popup, text="Release the keys, then press Confirm.",
                        font=("Segoe UI", 8), foreground="#8b949e")
        hint.pack(pady=(0, 6))

        capture = HotkeyCapture()
        closed = [False]
        popup.capture = capture  # reachable for tests/debugging

        def cleanup():
            if closed[0]:
                return
            closed[0] = True
            try:
                keyboard.unhook(hook)
            except (KeyError, ValueError, AttributeError):
                pass
            try:
                popup.grab_release()
            except tk.TclError:
                pass
            try:
                popup.destroy()
            except tk.TclError:
                pass

        def on_key(event):
            try:
                capture.on_key(event.name,
                               getattr(event, "event_type", "down"))
            except (AttributeError, ValueError):
                return
            try:
                held = capture.display()
                preview.config(text=held or capture.combo() or "...")
            except tk.TclError:
                pass

        def confirm():
            combo = capture.combo()
            others = {
                "save_clip": self.hk_save.get(),
                "toggle_mic": self.hk_mic.get(),
                "toggle_system_audio": self.hk_sys.get(),
            }
            err = HotkeyCapture.validate(combo, key_name, others)
            if err is not None:
                self.notify(err)
                return
            var.set(combo)
            self.save_settings()
            try:
                self.app.hotkeys.reload()
            except (AttributeError, RuntimeError):
                pass
            self.notify("Hotkey updated.")
            cleanup()

        btns = tb.Frame(popup)
        btns.pack(pady=8)
        tb.Button(btns, text="Confirm hotkey", bootstyle="primary",
                  command=confirm).pack(side=LEFT, padx=6)
        tb.Button(btns, text="Cancel", bootstyle="secondary",
                  command=cleanup).pack(side=LEFT, padx=6)
        popup.bind("<Escape>", lambda _e: cleanup())
        popup.protocol("WM_DELETE_WINDOW", cleanup)

        hook = keyboard.hook(on_key)

    # ---------------------------------------------------
    # Folder picker
    # ---------------------------------------------------
    def choose_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.save_folder_var.set(folder)
            self.settings["save_folder"] = folder
            self.save_settings()
            self.notify(f"Save folder updated:\n{folder}")

    # ---------------------------------------------------
    # Toggle handlers
    # ---------------------------------------------------
    def _on_mic_volume_change(self, value):
        try:
            vol = max(0, min(100, int(float(value))))
        except (ValueError, TypeError):
            return
        try:
            self.mic_vol_label.config(text=f"{vol}%")
        except tk.TclError:
            pass
        try:
            self.mic_volume_var.set(vol)  # keep Tk var in sync (no refire)
        except (tk.TclError, ValueError):
            pass
        self.recorder.mic_volume = vol
        self.settings["mic_volume"] = vol
        if vol > 0:
            self.recorder.last_mic_volume = vol
            self.settings["last_mic_volume"] = vol
        self._schedule_settings_save()

    def _on_system_volume_change(self, value):
        try:
            vol = max(0, min(100, int(float(value))))
        except (ValueError, TypeError):
            return
        try:
            self.sys_vol_label.config(text=f"{vol}%")
        except tk.TclError:
            pass
        try:
            self.system_volume_var.set(vol)  # keep Tk var in sync (no refire)
        except (tk.TclError, ValueError):
            pass
        self.recorder.system_volume = vol
        self.settings["system_volume"] = vol
        if vol > 0:
            self.recorder.last_system_volume = vol
            self.settings["last_system_volume"] = vol
        self._schedule_settings_save()

    def set_audio_toggle(self, source, vol):
        """Sync UI after a volume-toggle hotkey. vol is the new 0-100 level."""
        try:
            vol = max(0, min(100, int(vol)))
        except (ValueError, TypeError):
            return
        if source == "mic":
            try:
                self.mic_volume_var.set(vol)
            except (tk.TclError, ValueError):
                pass
            self._on_mic_volume_change(vol)
            self.notify("Microphone muted." if vol == 0
                        else f"Microphone unmuted ({vol}%).")
        elif source == "system":
            try:
                self.system_volume_var.set(vol)
            except (tk.TclError, ValueError):
                pass
            self._on_system_volume_change(vol)
            self.notify("System audio muted." if vol == 0
                        else f"System audio unmuted ({vol}%).")

    def save_clip(self, path=None):
        if not self.recorder.recording:
            self.notify("Start the replay buffer before saving a clip.")
            return
        folder = self.settings.get("save_folder") or os.getcwd()
        path = path or os.path.join(folder, f"clip_{time.strftime('%Y%m%d_%H%M%S')}.mp4")
        self.notify("Saving clip…")
        def save():
            success = self.save_clip_callback(path)
            self.root.after(0, lambda: self.notify("Clip saved!" if success else "Could not save the clip."))
        threading.Thread(target=save, daemon=True).start()

    def on_autostart_toggle(self):
        enabled = bool(self.autostart_var.get())
        self.settings["start_with_windows"] = enabled
        self.save_settings()
        if enabled:
            if enable_autostart("KillCam+"):
                self.notify("KillCam+ will now start with Windows.")
            else:
                self.notify("Failed to enable auto-start.")
        else:
            if disable_autostart("KillCam+"):
                self.notify("KillCam+ will no longer start with Windows.")
            else:
                self.notify("Failed to disable auto-start.")

    # ---------------------------------------------------
    # Close handler (minimize to tray; recording continues)
    # ---------------------------------------------------
    def on_close(self):
        self.save_settings()
        if _HAVE_TRAY and self._ensure_tray():
            try:
                self.root.withdraw()
            except tk.TclError:
                pass
            self.notify("KillCam+ minimized to tray.")
        else:
            # No tray support: fall back to full exit
            try:
                self.recorder.stop()
            except Exception:
                pass
            try:
                self.app.hotkeys.clear()
            except Exception:
                pass
            try:
                self.root.destroy()
            except tk.TclError:
                pass
