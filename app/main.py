"""Окно, трей и запуск бота."""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import psutil
import webview
from PIL import Image

import app.autostart as autostart
from app.actions import Actions, downloads_dir, fail, ok
from app.auth import ROOT, LOCAL_USER, AuthError, Store, actor_label, data_dir
from app.bot import BotRunner
from app.notify import Watcher

WEB = ROOT / "app" / "web"
_MUTEX: Any = None


def setup_log() -> None:
    try:
        logging.basicConfig(
            filename=data_dir() / "agent.log",
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            encoding="utf-8",
        )
    except OSError:
        logging.basicConfig(level=logging.INFO)


def is_admin() -> bool:
    try:
        ctypes.windll.shell32.IsUserAnAdmin.restype = ctypes.c_bool
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def relaunch_as_admin() -> None:
    if is_admin():
        return
    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_int,
    ]
    shell32.ShellExecuteW.restype = ctypes.c_size_t
    params = subprocess.list2cmdline(sys.argv)
    result = shell32.ShellExecuteW(None, "runas", sys.executable, params, str(ROOT), 0)
    if result <= 32:
        ctypes.windll.user32.MessageBoxW(0, "Нужны права администратора. Запрос отклонён.", "Remote TG", 0x10)
        raise SystemExit(1)
    raise SystemExit(0)


def acquire_single_instance() -> bool:
    global _MUTEX
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.GetLastError.restype = ctypes.c_ulong
    _MUTEX = kernel32.CreateMutexW(None, False, "Local\\RemoteTG")
    if kernel32.GetLastError() == 183:
        ctypes.windll.user32.MessageBoxW(0, "Remote TG уже запущен. Откройте его из трея.", "Remote TG", 0x40)
        return False
    return True


def load_icon() -> Image.Image:
    path = ROOT / "assets" / "icon.ico"
    if path.is_file():
        return Image.open(path)
    return Image.new("RGB", (64, 64), (47, 111, 207))


def as_bool(value: bool | str | int) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


class Api:
    def __init__(self, store: Store, actions: Actions, runner: BotRunner) -> None:
        self.store = store
        self.actions = actions
        self.runner = runner
        self.quitting = False

    def boot(self) -> dict[str, Any]:
        try:
            metrics = self.actions.metrics()
        except (OSError, RuntimeError, psutil.Error) as exc:
            return fail(str(exc))
        view = self.store.public_view()
        view.update(
            {
                "autostart": autostart.enabled(),
                "bot": self.runner.status(),
                "metrics": metrics,
                "home": str(Path.home()),
                "downloads": str(downloads_dir()),
            }
        )
        return ok(view)

    def list_dir(self, path: str) -> dict[str, Any]:
        try:
            return ok(self.actions.list_directory(path))
        except (ValueError, OSError) as exc:
            return fail(str(exc))

    def make_dir(self, path: str) -> dict[str, Any]:
        return self.actions.run(LOCAL_USER, "mkdir", path, lambda: self.actions.make_dir(path))

    def rename_path(self, src: str, dst: str) -> dict[str, Any]:
        return self.actions.run(LOCAL_USER, "rename", src, lambda: self.actions.rename(src, dst))

    def delete_file(self, path: str, pin: str, confirm: bool | str) -> dict[str, Any]:
        return self.actions.run(
            LOCAL_USER,
            "delete",
            path,
            lambda: self.actions.delete_file(path),
            pin=pin,
            confirm=as_bool(confirm),
        )

    def copy_in(self, dest: str, source: str) -> dict[str, Any]:
        return self.actions.run(
            LOCAL_USER,
            "upload",
            source,
            lambda: self.actions.copy_files(dest, [source]),
        )

    def pick_copy(self, dest: str) -> dict[str, Any]:
        try:
            picked = webview.windows[0].create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True)
        except Exception as exc:
            logging.exception("dialog")
            return fail(str(exc)[:300])
        if not picked:
            return fail("Файл не выбран")
        return self.actions.run(
            LOCAL_USER,
            "upload",
            dest,
            lambda: self.actions.copy_files(dest, list(picked)),
        )

    def processes(self) -> dict[str, Any]:
        try:
            return ok(self.actions.processes())
        except (psutil.Error, OSError) as exc:
            return fail(str(exc))

    def kill_process(self, pid: int, confirm: bool | str) -> dict[str, Any]:
        if not as_bool(confirm):
            return fail("Подтвердите действие")
        return self.actions.run(LOCAL_USER, "kill", str(pid), lambda: self.actions.kill_process(int(pid)))

    def start_program(self, command: str) -> dict[str, Any]:
        return self.actions.run(LOCAL_USER, "start", command, lambda: self.actions.start_program(command))

    def pick_path(self) -> dict[str, Any]:
        try:
            picked = webview.windows[0].create_file_dialog(webview.FileDialog.OPEN)
        except Exception as exc:
            logging.exception("dialog")
            return fail(str(exc)[:300])
        if not picked:
            return fail("Файл не выбран")
        return ok(picked[0])

    def add_launcher(self, name: str, command: str) -> dict[str, Any]:
        try:
            row = self.store.add_launcher(name, command)
        except AuthError as exc:
            return fail(exc.message)
        self.store.audit(actor_label(LOCAL_USER), "start", True, f"ярлык {row['name']}")
        return ok(row)

    def remove_launcher(self, launcher_id: str) -> dict[str, Any]:
        self.store.remove_launcher(str(launcher_id))
        self.store.audit(actor_label(LOCAL_USER), "start", True, "ярлык удалён")
        return ok(True)

    def launch_saved(self, launcher_id: str) -> dict[str, Any]:
        row = next((item for item in self.store.launchers() if item["id"] == str(launcher_id)), None)
        if row is None:
            return fail("Ярлык не найден")
        command = row["command"]
        return self.actions.run(LOCAL_USER, "start", command, lambda: self.actions.start_program(command))

    def services(self, query: str) -> dict[str, Any]:
        return self.actions.run(LOCAL_USER, "services", query or "список", lambda: self.actions.services(query))

    def service_start(self, name: str) -> dict[str, Any]:
        return self.actions.run(
            LOCAL_USER,
            "service_start",
            name,
            lambda: self.actions.service_control(name, True),
        )

    def service_stop(self, name: str, pin: str, confirm: bool | str) -> dict[str, Any]:
        return self.actions.run(
            LOCAL_USER,
            "service_stop",
            name,
            lambda: self.actions.service_control(name, False),
            pin=pin,
            confirm=as_bool(confirm),
        )

    def screenshot(self) -> dict[str, Any]:
        return self.actions.run(LOCAL_USER, "screenshot", "скриншот", self.actions.take_screenshot)

    def power(self, mode: str, pin: str, confirm: bool | str) -> dict[str, Any]:
        action = "power_abort" if mode == "abort" else "power"
        return self.actions.run(
            LOCAL_USER,
            action,
            mode,
            lambda: self.actions.power(mode),
            pin=pin,
            confirm=True if mode == "abort" else as_bool(confirm),
        )

    def clipboard_get(self) -> dict[str, Any]:
        return self.actions.run(LOCAL_USER, "clipboard", "чтение", self.actions.clipboard_get)

    def clipboard_set(self, text: str) -> dict[str, Any]:
        return self.actions.run(
            LOCAL_USER,
            "clipboard",
            f"запись {len(text)}",
            lambda: self.actions.clipboard_set(text),
        )

    def volume(self) -> dict[str, Any]:
        return self.actions.run(LOCAL_USER, "volume", "чтение", self.actions.volume_state)

    def volume_set(self, level: int) -> dict[str, Any]:
        return self.actions.run(
            LOCAL_USER,
            "volume",
            str(level),
            lambda: self.actions.set_volume(int(level)),
        )

    def volume_mute(self, mute: bool | str) -> dict[str, Any]:
        flag = as_bool(mute)
        return self.actions.run(LOCAL_USER, "volume", "mute", lambda: self.actions.set_mute(flag))

    def windows(self) -> dict[str, Any]:
        try:
            return ok(self.actions.list_windows())
        except OSError as exc:
            return fail(str(exc))

    def close_window(self, hwnd: str, confirm: bool | str) -> dict[str, Any]:
        if not as_bool(confirm):
            return fail("Подтвердите действие")
        return self.actions.run(LOCAL_USER, "close_window", hwnd, lambda: self.actions.close_window(str(hwnd)))

    def open_target(self, value: str) -> dict[str, Any]:
        return self.actions.run(LOCAL_USER, "open", value, lambda: self.actions.open_target(value))

    def download_url(self, url: str, folder: str) -> dict[str, Any]:
        return self.actions.run(LOCAL_USER, "download_url", url, lambda: self.actions.download_url(url, folder))

    def shell(self, command: str, pin: str) -> dict[str, Any]:
        return self.actions.run(LOCAL_USER, "shell", command, lambda: self.actions.shell(command), pin=pin)

    def audit(self) -> dict[str, Any]:
        return ok(self.store.recent_audit(80))

    def save_tokens(self, token: str, backup: str, pin: str, clear_backup: bool | str) -> dict[str, Any]:
        try:
            self.store.require_settings_pin(pin)
            current, current_backup = self.store.tokens()
            new_token = token.strip() or current
            if as_bool(clear_backup):
                new_backup = ""
            else:
                new_backup = backup.strip() or current_backup
            self.store.set_tokens(new_token, new_backup)
        except AuthError as exc:
            self.store.audit(actor_label(LOCAL_USER), "settings", False, "токен")
            return fail(exc.message)
        self.store.audit(actor_label(LOCAL_USER), "settings", True, "токен обновлён")
        self.runner.reload()
        return ok(True)

    def save_pin(self, new_pin: str, old_pin: str) -> dict[str, Any]:
        try:
            self.store.set_pin(new_pin, old_pin)
        except AuthError as exc:
            self.store.audit(actor_label(LOCAL_USER), "settings", False, "PIN")
            return fail(exc.message)
        self.store.audit(actor_label(LOCAL_USER), "settings", True, "PIN изменён")
        return ok(True)

    def issue_pair(self, role: str, pin: str) -> dict[str, Any]:
        try:
            code = self.store.new_pair_code(role, pin)
        except AuthError as exc:
            return fail(exc.message)
        self.store.audit(actor_label(LOCAL_USER), "pair", True, role)
        return ok({"code": code, "role": role, "seconds": 600})

    def set_role(self, user_id: int, role: str, pin: str) -> dict[str, Any]:
        try:
            self.store.set_role(int(user_id), role, pin)
        except AuthError as exc:
            return fail(exc.message)
        self.store.audit(actor_label(LOCAL_USER), "users", True, f"{user_id} {role}")
        return ok(True)

    def remove_user(self, user_id: int, pin: str) -> dict[str, Any]:
        try:
            self.store.remove_user(int(user_id), pin)
        except AuthError as exc:
            return fail(exc.message)
        self.store.audit(actor_label(LOCAL_USER), "users", True, f"удалён {user_id}")
        return ok(True)

    def set_paused(self, paused: bool | str) -> dict[str, Any]:
        self.store.set_paused(as_bool(paused))
        return ok(self.store.is_paused())

    def set_autostart(self, enabled: bool | str, pin: str) -> dict[str, Any]:
        try:
            self.store.require_settings_pin(pin)
            if as_bool(enabled):
                autostart.install()
            else:
                autostart.remove()
        except (AuthError, OSError) as exc:
            message = exc.message if isinstance(exc, AuthError) else str(exc)
            return fail(message)
        self.store.audit(actor_label(LOCAL_USER), "settings", True, "автозагрузка")
        return ok(autostart.enabled())


def run_app() -> None:
    store = Store()
    actions = Actions(store)
    runner = BotRunner(store, actions)
    runner.start()
    Watcher(store, actions, lambda: runner.ready, runner.broadcast).start()
    api = Api(store, actions, runner)
    window = webview.create_window(
        "Remote TG",
        url=str(WEB / "index.html"),
        js_api=api,
        width=1120,
        height=760,
        min_size=(900, 640),
        background_color="#121417",
        text_select=True,
        confirm_close=False,
    )
    if window is None:
        raise RuntimeError("Не удалось создать окно")

    def on_closing() -> bool:
        if api.quitting:
            return True
        window.hide()
        return False

    window.events.closing += on_closing

    def open_window(_icon: Any, _item: Any) -> None:
        window.show()

    def toggle_pause(_icon: Any, _item: Any) -> None:
        store.set_paused(not store.is_paused())

    def quit_app(icon: Any, _item: Any) -> None:
        api.quitting = True
        runner.stop()
        icon.stop()
        window.destroy()

    import pystray

    menu = pystray.Menu(
        pystray.MenuItem("Открыть", open_window, default=True),
        pystray.MenuItem(lambda _item: "Продолжить" if store.is_paused() else "Пауза", toggle_pause),
        pystray.MenuItem("Выход", quit_app),
    )
    icon = pystray.Icon("RemoteTG", load_icon(), "Remote TG", menu)
    if hasattr(icon, "run_detached"):
        icon.run_detached()
    else:
        import threading

        threading.Thread(target=icon.run, name="tray", daemon=True).start()
    profile = data_dir() / "webview"
    profile.mkdir(parents=True, exist_ok=True)
    webview.start(
        gui="edgechromium",
        icon=str(ROOT / "assets" / "icon.ico"),
        storage_path=str(profile),
    )


def main() -> None:
    os.chdir(data_dir())
    setup_log()
    if sys.version_info < (3, 11):
        ctypes.windll.user32.MessageBoxW(0, "Нужен Python 3.11 или новее.", "Remote TG", 0x10)
        raise SystemExit(1)
    try:
        relaunch_as_admin()
        if not acquire_single_instance():
            raise SystemExit(0)
        logging.info("start")
        run_app()
    except SystemExit:
        raise
    except Exception as exc:
        logging.exception("start")
        ctypes.windll.user32.MessageBoxW(0, str(exc)[:800], "Remote TG", 0x10)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
