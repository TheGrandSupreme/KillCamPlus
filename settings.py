import json
import os
import sys


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RESOURCE_DIR = getattr(sys, "_MEIPASS", SCRIPT_DIR)
CONFIG_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else SCRIPT_DIR
SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")

DEFAULT_SETTINGS = {
    "mic_audio_device": "",
    "system_audio_device": "",
    "resolution": "1920x1080",
    "fps": 60,
    "buffer_seconds": 20,
    "record_microphone": True,
    "record_system_audio": True,
    "audio_mode": "mixed",
    "compression": "Medium",
    "save_folder": os.path.join(os.path.expanduser("~"), "Videos", "Captures"),
    "hotkeys": {"save_clip": "f9", "toggle_mic": "shift+alt+m", "toggle_system_audio": "shift+alt+s"},
    "start_with_windows": False,
    "mic_volume": 100,
    "system_volume": 100,
    "monitor_index": 0,
    "app_volumes": {},
    "last_mic_volume": 100,
    "last_system_volume": 100,
}

_KNOWN_KEYS = set(DEFAULT_SETTINGS) - {"hotkeys"}
_AUDIO_MODES = {"mixed", "separate"}
_COMPRESSION_LEVELS = {"High", "Medium", "Low"}
_HOTKEY_KEYS = set(DEFAULT_SETTINGS["hotkeys"])


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalize(settings):
    try:
        settings["buffer_seconds"] = max(1, int(str(settings.get("buffer_seconds", 20)).strip() or 20))
    except (ValueError, TypeError):
        settings["buffer_seconds"] = DEFAULT_SETTINGS["buffer_seconds"]
    try:
        settings["fps"] = int(str(settings.get("fps", 60)).strip() or 60)
    except (ValueError, TypeError):
        settings["fps"] = DEFAULT_SETTINGS["fps"]
    settings["record_microphone"] = _as_bool(settings.get("record_microphone", True))
    settings["record_system_audio"] = _as_bool(settings.get("record_system_audio", True))
    settings["start_with_windows"] = _as_bool(settings.get("start_with_windows", False))
    try:
        settings["monitor_index"] = max(0, int(settings.get("monitor_index", 0)))
    except (ValueError, TypeError):
        settings["monitor_index"] = 0
    app_vols = settings.get("app_volumes")
    clean_vols = {}
    if isinstance(app_vols, dict):
        for name, vol in app_vols.items():
            try:
                clean_vols[str(name)] = max(0, min(100, int(vol)))
            except (ValueError, TypeError):
                pass
    settings["app_volumes"] = clean_vols
    for _vkey in ("mic_volume", "system_volume"):
        try:
            settings[_vkey] = max(0, min(100, int(settings.get(_vkey, 100))))
        except (ValueError, TypeError):
            settings[_vkey] = 100
    for _vkey in ("last_mic_volume", "last_system_volume"):
        try:
            settings[_vkey] = max(1, min(100, int(settings.get(_vkey, 100))))
        except (ValueError, TypeError):
            settings[_vkey] = 100
    settings["mic_audio_device"] = str(settings.get("mic_audio_device", "") or "")
    settings["system_audio_device"] = str(settings.get("system_audio_device", "") or "")
    settings["resolution"] = str(settings.get("resolution", "") or DEFAULT_SETTINGS["resolution"])
    mode = str(settings.get("audio_mode", "mixed")).lower()
    settings["audio_mode"] = mode if mode in _AUDIO_MODES else "mixed"
    comp = str(settings.get("compression", "Medium")).strip().capitalize()
    settings["compression"] = comp if comp in _COMPRESSION_LEVELS else "Medium"
    save_folder = settings.get("save_folder")
    save_folder = str(save_folder) if save_folder else DEFAULT_SETTINGS["save_folder"]
    try:
        # Honor the configured folder (creating it if needed); fall back
        # to this machine's Captures folder only when unusable. This
        # self-heals installs copied from another PC.
        os.makedirs(save_folder, exist_ok=True)
        if not os.path.isdir(save_folder):
            raise OSError(save_folder)
    except (OSError, ValueError, TypeError):
        save_folder = DEFAULT_SETTINGS["save_folder"]
        try:
            os.makedirs(save_folder, exist_ok=True)
        except OSError:
            pass
    settings["save_folder"] = str(save_folder)
    for key in _HOTKEY_KEYS:
        value = settings.get("hotkeys", {}).get(key)
        settings["hotkeys"][key] = str(value).strip() if value else DEFAULT_SETTINGS["hotkeys"][key]


def load_settings(path=SETTINGS_PATH):
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        with open(path, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        if not isinstance(saved, dict):
            saved = {}
    except (OSError, ValueError, TypeError):
        saved = {}
    settings.update({key: saved[key] for key in _KNOWN_KEYS if key in saved})
    saved_hotkeys = saved.get("hotkeys")
    if isinstance(saved_hotkeys, dict):
        settings["hotkeys"].update({key: saved_hotkeys[key] for key in _HOTKEY_KEYS if key in saved_hotkeys})
    _normalize(settings)
    return settings


def save_settings(settings, path=SETTINGS_PATH):
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=4)
        return True
    except OSError:
        return False