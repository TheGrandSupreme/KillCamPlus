import os
import sys

def get_startup_folder():
    return os.path.join(os.getenv("APPDATA"), r"Microsoft\Windows\Start Menu\Programs\Startup")

def get_shortcut_path(app_name="KillCam"):
    return os.path.join(get_startup_folder(), f"{app_name}.lnk")

def enable_autostart(app_name="KillCam"):
    try:
        from win32com.client import Dispatch

        startup = get_startup_folder()
        shortcut_path = get_shortcut_path(app_name)

        app_dir = os.path.dirname(os.path.abspath(__file__))
        python_exe = sys.executable
        script_path = os.path.join(app_dir, "main.py")

        shell = Dispatch("WScript.Shell")
        shortcut = shell.CreateShortCut(shortcut_path)
        shortcut.Targetpath = python_exe
        shortcut.Arguments = f'"{script_path}"'
        shortcut.WorkingDirectory = app_dir
        shortcut.IconLocation = os.path.join(app_dir, "icons", "killcam.ico")
        shortcut.save()
        return True
    except Exception as e:
        print("Auto-start enable error:", e)
        return False

def disable_autostart(app_name="KillCam"):
    try:
        shortcut_path = get_shortcut_path(app_name)
        if os.path.exists(shortcut_path):
            os.remove(shortcut_path)
        return True
    except Exception as e:
        print("Auto-start disable error:", e)
        return False

def is_autostart_enabled(app_name="KillCam"):
    return os.path.exists(get_shortcut_path(app_name))
