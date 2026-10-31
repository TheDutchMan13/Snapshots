"""Starting Snapshots when you sign in to Windows (optional, off unless you turn it on).

This uses the same place Windows keeps the "Startup apps" list in Settings: one value under the
current user's Run key. It needs no administrator rights, nothing is installed, and Windows' own
Settings > Apps > Startup can turn it off too. Snapshots then starts hidden ("--background"): the
server runs, there is no window. Open it by double-clicking Snapshots.exe (that shows the copy that
is already running) or by going to its address in a browser.

Everything that touches the registry goes through `Registry`, so tests can swap it for a fake.
"""

import os
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# Where Windows remembers that a startup app was switched off in Settings > Apps > Startup or Task
# Manager. The Run value stays; this one says "don't run it". The first byte is odd when it is off.
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE_NAME = "Snapshots"


def supported() -> bool:
    return os.name == "nt"


class Registry:
    """The one registry value that holds the start-up command."""

    def get(self) -> str | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                return str(winreg.QueryValueEx(key, VALUE_NAME)[0])
        except FileNotFoundError:
            return None

    def set(self, command: str) -> None:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)

    def delete(self) -> None:
        self._delete(RUN_KEY)

    def switched_off_in_windows(self) -> bool:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as key:
                data = winreg.QueryValueEx(key, VALUE_NAME)[0]
        except FileNotFoundError:
            return False
        return isinstance(data, bytes) and len(data) > 0 and data[0] % 2 == 1

    def clear_switched_off(self) -> None:
        """Forget a "switched off" from Windows' Startup list, so turning it on here really turns it on."""
        self._delete(APPROVED_KEY)

    @staticmethod
    def _delete(path: str) -> None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, VALUE_NAME)
        except FileNotFoundError:
            pass


registry = Registry()


def _quote(text: str) -> str:
    return '"' + text.replace('"', "") + '"'


def command(config_file: Path | None = None, library: Path | None = None, data_dir: Path | None = None) -> str:
    """The command Windows should run at sign-in for this copy of Snapshots.

    Windows starts programs in another folder, so everything is named in full: the config file if
    there is one, otherwise the photo and data folders this copy was started with.
    """
    if getattr(sys, "frozen", False):
        parts = [_quote(sys.executable)]
    else:
        python = Path(sys.executable)
        windowless = python.with_name("pythonw.exe")      # no console window when run from source
        script = Path(__file__).resolve().parent.parent / "run_snapshots.py"
        parts = [_quote(str(windowless if windowless.is_file() else python))]
        parts += [_quote(str(script))] if script.is_file() else ["-m", "snapshots"]
    parts.append("--background")
    if config_file:
        parts += ["--config", _quote(str(Path(config_file).resolve()))]
    else:
        if library:
            parts += ["--library", _quote(str(Path(library).resolve()))]
        if data_dir:
            parts += ["--data", _quote(str(Path(data_dir).resolve()))]
    return " ".join(parts)


def command_for(cfg) -> str:
    return command(cfg.config_file, cfg.library_path, cfg.data_dir)


def status(cfg) -> dict:
    """What the settings page shows: whether it is on, and whether it points at this copy."""
    if not supported():
        return {"supported": False, "enabled": False, "current": False, "command": None}
    try:
        stored = registry.get()
        off_in_windows = stored is not None and registry.switched_off_in_windows()
    except OSError:
        stored, off_in_windows = None, False
    return {"supported": True, "enabled": stored is not None and not off_in_windows,
            "current": stored == command_for(cfg), "command": stored}


def enable(cfg) -> None:
    registry.set(command_for(cfg))
    registry.clear_switched_off()


def disable() -> None:
    registry.delete()
    registry.clear_switched_off()
