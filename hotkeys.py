import os
import time
import keyboard


class HotkeyManager:
    """Keeps global hotkeys in sync with the saved settings."""
    def __init__(self, recorder, app):
        self.recorder, self.app, self.settings = recorder, app, recorder.settings
        self.registered = []

    def clear(self):
        for handle in self.registered:
            try:
                keyboard.remove_hotkey(handle)
            except (KeyError, ValueError):
                pass
        self.registered = []

    def reload(self):
        self.clear()
        actions = {"save_clip": self.save_clip, "toggle_mic": self.toggle_mic, "toggle_system_audio": self.toggle_system}
        try:
            for name, action in actions.items():
                hotkey = self.settings["hotkeys"].get(name, "")
                if hotkey:
                    self.registered.append(keyboard.add_hotkey(hotkey, action))
            return True
        except Exception as exc:
            self.clear()
            self.app.ui.notify(f"Could not register hotkeys: {exc}")
            return False

    def save_clip(self):
        folder = self.settings.get("save_folder") or os.getcwd()
        path = os.path.join(folder, f"clip_{time.strftime('%Y%m%d_%H%M%S')}.mp4")
        self.app.root.after(0, lambda: self.app.ui.save_clip(path))

    def toggle_mic(self):
        vol = self.recorder.toggle_mic_volume()
        self.app.root.after(0, lambda v=vol: self.app.ui.set_audio_toggle("mic", v))

    def toggle_system(self):
        vol = self.recorder.toggle_system_volume()
        self.app.root.after(0, lambda v=vol: self.app.ui.set_audio_toggle("system", v))
