"""Ярлык в папке автозагрузки текущего пользователя."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def shortcut_path() -> Path:
    appdata = os.environ.get("APPDATA", "")
    return (
        Path(appdata)
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
        / "Remote TG.lnk"
    )


def enabled() -> bool:
    return shortcut_path().is_file()


def install() -> None:
    script = ROOT / "install_autostart.vbs"
    subprocess.run(
        ["wscript.exe", str(script), "quiet"],
        check=False,
        timeout=20,
        creationflags=_NO_WINDOW,
    )
    if not enabled():
        raise OSError("Не удалось создать ярлык автозагрузки")


def remove() -> None:
    path = shortcut_path()
    if path.is_file():
        path.unlink()
