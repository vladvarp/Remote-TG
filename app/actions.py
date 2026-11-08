"""Файлы, процессы, экран и питание этого ПК."""

from __future__ import annotations

import base64
import ctypes
import io
import json
import logging
import os
import re
import shutil
import socket
import string
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psutil
from PIL import Image

from app.auth import NEEDS_CONFIRM, AuthError, Store, User, actor_label

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MAX_DOWNLOAD = 500 * 1024 * 1024
_SERVICE_NAME = re.compile(r"^[A-Za-z0-9_.\- ]{1,80}$")
_ENUM = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
_USER32: ctypes.WinDLL | None = None


def ok(data: Any = None) -> dict[str, Any]:
    return {"ok": True, "data": data}


def fail(error: str) -> dict[str, Any]:
    return {"ok": False, "error": error}


def format_bytes(size: int | float) -> str:
    value = float(size)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            if unit == "Б":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{int(size)} Б"


def format_uptime(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, _seconds = divmod(rest, 60)
    if days:
        return f"{days} д {hours} ч {minutes} мин"
    return f"{hours} ч {minutes} мин"


def downloads_dir() -> Path:
    folder = Path.home() / "Downloads"
    if folder.is_dir():
        return folder
    return Path.home()


def as_path(path: str, base: Path | None = None) -> Path:
    if not path or "\x00" in path:
        raise ValueError("Пустой путь")
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raw = (base or Path.home()) / raw
    return raw.resolve(strict=False)


def safe_name(name: str) -> str:
    cleaned = "".join(ch for ch in Path(name or "").name if ch not in '<>:"/\\|?*' and ord(ch) >= 32)
    cleaned = cleaned.strip(" .")
    return cleaned[:180] or "file"


def unique_path(folder: Path, name: str) -> Path:
    target = folder / safe_name(name)
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for index in range(1, 1000):
        candidate = folder / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    raise ValueError("Слишком много файлов с таким именем")


def _decode(data: bytes) -> str:
    if not data:
        return ""
    oem = f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    try:
        return data.decode(oem)
    except UnicodeDecodeError:
        return data.decode("utf-8", "replace")


def _user32() -> ctypes.WinDLL:
    global _USER32
    if _USER32 is not None:
        return _USER32
    lib = ctypes.WinDLL("user32", use_last_error=True)
    lib.IsWindowVisible.argtypes = [ctypes.c_void_p]
    lib.IsWindowVisible.restype = ctypes.c_bool
    lib.IsWindow.argtypes = [ctypes.c_void_p]
    lib.IsWindow.restype = ctypes.c_bool
    lib.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    lib.GetWindowTextLengthW.restype = ctypes.c_int
    lib.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    lib.GetWindowTextW.restype = ctypes.c_int
    lib.EnumWindows.argtypes = [_ENUM, ctypes.c_void_p]
    lib.EnumWindows.restype = ctypes.c_int
    lib.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    lib.PostMessageW.restype = ctypes.c_bool
    lib.LockWorkStation.restype = ctypes.c_bool
    lib.OpenClipboard.argtypes = [ctypes.c_void_p]
    lib.OpenClipboard.restype = ctypes.c_bool
    lib.CloseClipboard.restype = ctypes.c_bool
    lib.EmptyClipboard.restype = ctypes.c_bool
    lib.GetClipboardData.argtypes = [ctypes.c_uint]
    lib.GetClipboardData.restype = ctypes.c_void_p
    lib.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    lib.SetClipboardData.restype = ctypes.c_void_p
    _USER32 = lib
    return lib


def _kernel32() -> ctypes.WinDLL:
    lib = ctypes.WinDLL("kernel32", use_last_error=True)
    lib.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    lib.GlobalAlloc.restype = ctypes.c_void_p
    lib.GlobalLock.argtypes = [ctypes.c_void_p]
    lib.GlobalLock.restype = ctypes.c_void_p
    lib.GlobalUnlock.argtypes = [ctypes.c_void_p]
    lib.GlobalUnlock.restype = ctypes.c_bool
    lib.GlobalFree.argtypes = [ctypes.c_void_p]
    lib.GlobalFree.restype = ctypes.c_void_p
    return lib


class Actions:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._net_prev: Any = None
        self._net_at = 0.0
        self._lock = threading.Lock()
        threading.Thread(target=self._prime, name="prime-cpu", daemon=True).start()

    def _prime(self) -> None:
        try:
            psutil.cpu_percent(interval=None)
            for proc in psutil.process_iter():
                try:
                    proc.cpu_percent(interval=None)
                except (psutil.Error, OSError):
                    continue
        except (psutil.Error, OSError):
            logging.exception("prime")

    def run(
        self,
        user: User | None,
        action: str,
        detail: str,
        fn: Callable[[], Any],
        confirm: bool = True,
    ) -> dict[str, Any]:
        if action in NEEDS_CONFIRM and not confirm:
            return fail("Подтвердите действие")
        try:
            self.store.require(user, action)
        except AuthError as exc:
            self.store.audit(actor_label(user), action, False, detail)
            return fail(exc.message)
        try:
            data = fn()
        except (ValueError, OSError, psutil.Error) as exc:
            self.store.audit(actor_label(user), action, False, detail)
            return fail(str(exc)[:400])
        except Exception:
            logging.exception("action %s", action)
            self.store.audit(actor_label(user), action, False, detail)
            return fail("Не удалось выполнить действие")
        self.store.audit(actor_label(user), action, True, detail)
        return ok(data)

    def metrics(self) -> dict[str, Any]:
        now = time.time()
        cpu = float(psutil.cpu_percent(interval=None))
        memory = psutil.virtual_memory()
        counters = psutil.net_io_counters()
        with self._lock:
            up = down = 0.0
            sent = recv = 0
            if counters is not None:
                sent = int(counters.bytes_sent)
                recv = int(counters.bytes_recv)
                if self._net_prev is not None:
                    delta = max(now - self._net_at, 0.001)
                    up = max(0, sent - int(self._net_prev.bytes_sent)) / delta
                    down = max(0, recv - int(self._net_prev.bytes_recv)) / delta
                self._net_prev = counters
                self._net_at = now
        disks: list[dict[str, Any]] = []
        for part in psutil.disk_partitions(all=False):
            if "cdrom" in (part.opts or "") or not part.fstype:
                continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except OSError:
                continue
            disks.append(
                {
                    "mount": part.mountpoint,
                    "percent": usage.percent,
                    "free": usage.free,
                    "total": usage.total,
                }
            )
        battery_data = None
        try:
            battery = psutil.sensors_battery()
        except (OSError, RuntimeError):
            battery = None
        if battery is not None:
            battery_data = {
                "percent": round(float(battery.percent), 1),
                "plugged": bool(battery.power_plugged),
            }
        try:
            account = os.getlogin()
        except OSError:
            account = os.environ.get("USERNAME", "")
        return {
            "host": socket.gethostname(),
            "user": account,
            "cpu": round(cpu, 1),
            "ram": {
                "percent": memory.percent,
                "used": memory.used,
                "total": memory.total,
            },
            "disks": disks,
            "net": {"sent": sent, "recv": recv, "up": up, "down": down},
            "uptime": int(now - psutil.boot_time()),
            "battery": battery_data,
        }

    def drive_letters(self) -> list[str]:
        return [f"{letter}:\\" for letter in string.ascii_uppercase if Path(f"{letter}:\\").exists()]

    def list_directory(self, path: str) -> dict[str, Any]:
        if not str(path).strip():
            return {
                "path": "",
                "parent": None,
                "entries": [],
                "drives": self.drive_letters(),
                "truncated": False,
            }
        folder = as_path(path)
        if not folder.is_dir():
            raise ValueError("Папка не найдена")
        parent = "" if folder.parent == folder else str(folder.parent)
        try:
            children = list(folder.iterdir())
        except OSError as exc:
            raise ValueError("Нет доступа к папке") from exc
        children.sort(key=lambda item: (not item.is_dir(), item.name.casefold()))
        truncated = len(children) > 400
        entries: list[dict[str, Any]] = []
        for child in children[:400]:
            try:
                is_dir = child.is_dir()
                size = 0 if is_dir else child.stat().st_size
            except OSError:
                continue
            entries.append(
                {"name": child.name, "path": str(child), "dir": is_dir, "size": size}
            )
        return {
            "path": str(folder),
            "parent": parent,
            "entries": entries,
            "drives": self.drive_letters(),
            "truncated": truncated,
        }

    def make_dir(self, path: str) -> str:
        folder = as_path(path)
        folder.mkdir(parents=True, exist_ok=True)
        return str(folder)

    def rename(self, src: str, dst: str) -> str:
        source = as_path(src)
        if not source.exists():
            raise ValueError("Не найдено")
        raw = Path(dst)
        if raw.is_absolute() or (len(dst) > 1 and dst[1] == ":"):
            target = as_path(dst)
        else:
            target = (source.parent / dst).resolve(strict=False)
        if target.exists():
            raise ValueError("Уже существует")
        source.rename(target)
        return str(target)

    def delete_file(self, path: str) -> str:
        target = as_path(path)
        if not target.is_file():
            raise ValueError("Можно удалить только файл")
        target.unlink()
        return str(target)

    def copy_files(self, dest: str, sources: list[str]) -> list[str]:
        folder = as_path(dest)
        if not folder.is_dir():
            raise ValueError("Папка не найдена")
        saved: list[str] = []
        for source in sources:
            src = as_path(source)
            if not src.is_file():
                raise ValueError(f"Не файл: {src.name}")
            target = unique_path(folder, src.name)
            shutil.copy2(src, target)
            saved.append(str(target))
        return saved

    def reserve_upload(self, folder: str, name: str) -> Path:
        directory = as_path(folder)
        if not directory.is_dir():
            raise ValueError("Папка не найдена")
        return unique_path(directory, name)

    def file_info(self, path: str) -> dict[str, Any]:
        target = as_path(path)
        if not target.is_file():
            raise ValueError("Это не файл")
        return {"path": str(target), "name": target.name, "size": target.stat().st_size}

    def processes(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for proc in psutil.process_iter(["pid", "name", "username"]):
            try:
                info = proc.info
                memory = proc.memory_info().rss
                cpu = proc.cpu_percent(interval=None)
            except (psutil.Error, OSError):
                continue
            rows.append(
                {
                    "pid": int(info["pid"]),
                    "name": str(info.get("name") or "")[:80],
                    "user": str(info.get("username") or "")[:80],
                    "ram": int(memory),
                    "cpu": round(float(cpu), 1),
                }
            )
        rows.sort(key=lambda item: item["ram"], reverse=True)
        return rows[:150]

    def kill_process(self, pid: int) -> str:
        proc = psutil.Process(int(pid))
        name = proc.name()
        proc.kill()
        return f"{name} ({pid})"

    def start_program(self, command: str) -> str:
        command = command.strip()
        if not command:
            raise ValueError("Пустая команда")
        if len(command) > 500:
            raise ValueError("Слишком длинная команда")
        candidate = Path(command)
        if candidate.exists():
            os.startfile(str(candidate))  # noqa: S606 - запуск файла, который выбрал владелец
            return str(candidate)
        subprocess.Popen(  # noqa: S602 - строка запуска программы на своём ПК
            command,
            shell=True,
            cwd=str(Path.home()),
            creationflags=NO_WINDOW,
            close_fds=True,
        )
        return command

    def services(self, query: str = "") -> list[dict[str, str]]:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                "Get-Service | Select-Object Name, DisplayName, "
                "@{Name='Status';Expression={$_.Status.ToString()}} | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            timeout=45,
            creationflags=NO_WINDOW,
            check=False,
        )
        text = completed.stdout.decode("utf-8", "replace").strip()
        if not text:
            detail = _decode(completed.stderr).strip() or "Не удалось получить службы"
            raise ValueError(detail[:300])
        raw = json.loads(text)
        if isinstance(raw, dict):
            raw = [raw]
        needle = query.casefold().strip()
        rows: list[dict[str, str]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("Name") or "")
            display = str(item.get("DisplayName") or name)
            status = str(item.get("Status") or "")
            if needle and needle not in name.casefold() and needle not in display.casefold():
                continue
            rows.append({"name": name, "display": display, "status": status})
        rows.sort(key=lambda item: item["display"].casefold())
        return rows[:300]

    def service_control(self, name: str, start: bool) -> str:
        if not _SERVICE_NAME.fullmatch(name or ""):
            raise ValueError("Некорректное имя службы")
        completed = subprocess.run(
            ["sc", "start" if start else "stop", name],
            capture_output=True,
            timeout=30,
            creationflags=NO_WINDOW,
            check=False,
        )
        output = _decode(completed.stdout + completed.stderr).strip()
        if completed.returncode != 0:
            raise ValueError(output[:300] or "Служба не переключилась")
        return output[:300] or ("запущена" if start else "остановка отправлена")

    def take_screenshot(self) -> str:
        import mss

        with mss.mss() as grabber:
            shot = grabber.grab(grabber.monitors[0])
            image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        image.thumbnail((2560, 1440))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=70)
        return base64.b64encode(buffer.getvalue()).decode("ascii")

    def power(self, mode: str) -> str:
        if mode == "lock":
            if not _user32().LockWorkStation():
                raise OSError("Не удалось заблокировать сеанс")
            return "Сеанс заблокирован"
        if mode == "sleep":
            timer = threading.Timer(1.5, _sleep_now)
            timer.daemon = True
            timer.start()
            return "Переход в сон через пару секунд"
        if mode == "shutdown":
            _shutdown("/s")
            return "Выключение через 5 секунд"
        if mode == "restart":
            _shutdown("/r")
            return "Перезагрузка через 5 секунд"
        if mode == "abort":
            completed = subprocess.run(
                ["shutdown", "/a"],
                capture_output=True,
                timeout=15,
                creationflags=NO_WINDOW,
                check=False,
            )
            if completed.returncode == 0:
                return "Запланированное выключение отменено"
            return "Нет запланированного выключения"
        raise ValueError("Неизвестное действие питания")

    def clipboard_get(self) -> str:
        user32 = _user32()
        kernel32 = _kernel32()
        if not user32.OpenClipboard(None):
            raise OSError("Буфер занят")
        try:
            handle = user32.GetClipboardData(13)
            if not handle:
                return ""
            locked = kernel32.GlobalLock(handle)
            if not locked:
                return ""
            try:
                return ctypes.wstring_at(locked)[:20000]
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    def clipboard_set(self, text: str) -> str:
        if len(text) > 1_000_000:
            raise ValueError("Слишком длинный текст")
        data = text.encode("utf-16-le") + b"\x00\x00"
        user32 = _user32()
        kernel32 = _kernel32()
        if not user32.OpenClipboard(None):
            raise OSError("Буфер занят")
        handle = None
        try:
            if not user32.EmptyClipboard():
                raise OSError("Не удалось очистить буфер")
            handle = kernel32.GlobalAlloc(0x0002, len(data))
            if not handle:
                raise OSError("Нет памяти для буфера")
            locked = kernel32.GlobalLock(handle)
            if not locked:
                raise OSError("Не удалось записать буфер")
            ctypes.memmove(locked, data, len(data))
            kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(13, handle):
                raise OSError("Не удалось записать буфер")
            handle = None
        finally:
            if handle:
                kernel32.GlobalFree(handle)
            user32.CloseClipboard()
        return f"{len(text)} символов"

    def volume_state(self) -> dict[str, Any]:
        device = _speakers()
        level = int(round(float(device.volume_percent)))
        muted = _is_muted(device)
        return {"level": max(0, min(100, level)), "mute": muted}

    def set_volume(self, level: int) -> dict[str, Any]:
        value = max(0, min(100, int(level)))
        device = _speakers()
        device.volume_percent = value
        return self.volume_state()

    def set_mute(self, mute: bool) -> dict[str, Any]:
        device = _speakers()
        device.EndpointVolume.SetMute(1 if mute else 0, None)
        return self.volume_state()

    def list_windows(self) -> list[dict[str, str]]:
        lib = _user32()
        found: list[dict[str, str]] = []

        def callback(hwnd: int, _param: int) -> int:
            if len(found) >= 120:
                return 1
            if not lib.IsWindowVisible(hwnd):
                return 1
            length = lib.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return 1
            buffer = ctypes.create_unicode_buffer(length + 1)
            lib.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value.strip()
            if title:
                found.append({"hwnd": str(int(hwnd)), "title": title[:180]})
            return 1

        if not lib.EnumWindows(_ENUM(callback), None):
            raise OSError("Не удалось получить список окон")
        return found

    def close_window(self, hwnd: str) -> str:
        handle = int(hwnd)
        lib = _user32()
        if not lib.IsWindow(handle):
            raise ValueError("Окно уже закрыто")
        if not lib.PostMessageW(handle, 0x0010, None, None):
            raise OSError("Не удалось закрыть окно")
        return hwnd

    def open_target(self, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("Пустое значение")
        parsed = urllib.parse.urlparse(text)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            webbrowser.open(text)
            return text
        if parsed.scheme:
            raise ValueError("Можно открыть файл или адрес http(s)")
        target = as_path(text)
        if not target.exists():
            raise ValueError("Не найдено")
        os.startfile(str(target))  # noqa: S606 - открытие выбранного файла
        return str(target)

    def download_url(self, url: str, folder: str) -> str:
        parsed = urllib.parse.urlparse(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Нужен адрес http или https")
        directory = as_path(folder)
        if not directory.is_dir():
            raise ValueError("Папка не найдена")
        name = safe_name(Path(urllib.parse.unquote(parsed.path)).name or "download")
        target = unique_path(directory, name)
        temporary = Path(str(target) + ".part")
        request = urllib.request.Request(url.strip(), headers={"User-Agent": "RemoteTG"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                final = urllib.parse.urlparse(response.geturl())
                if final.scheme not in {"http", "https"}:
                    raise ValueError("Разрешены только http и https")
                length = response.headers.get("Content-Length")
                if length and str(length).isdigit() and int(length) > MAX_DOWNLOAD:
                    raise ValueError("Файл больше 500 МБ")
                size = 0
                with temporary.open("wb") as handle:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_DOWNLOAD:
                            raise ValueError("Файл больше 500 МБ")
                        handle.write(chunk)
            temporary.replace(target)
        except urllib.error.URLError as exc:
            raise ValueError(f"Не удалось скачать: {exc.reason}") from exc
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)
        return str(target)

    def shell(self, command: str) -> str:
        command = command.strip()
        if not command:
            raise ValueError("Пустая команда")
        if len(command) > 1000:
            raise ValueError("Слишком длинная команда")
        try:
            completed = subprocess.run(  # noqa: S602 - оболочка владельца, команда пишется в журнал
                command,
                shell=True,
                capture_output=True,
                timeout=20,
                cwd=str(Path.home()),
                creationflags=NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("Таймаут 20 с") from exc
        output = _decode(completed.stdout).strip()
        error = _decode(completed.stderr).strip()
        text = output
        if error:
            text = f"{text}\n{error}".strip()
        return f"код {completed.returncode}\n{text[:20000]}".strip()


def _sleep_now() -> None:
    lib = ctypes.WinDLL("powrprof", use_last_error=True)
    lib.SetSuspendState.argtypes = [ctypes.c_bool, ctypes.c_bool, ctypes.c_bool]
    lib.SetSuspendState.restype = ctypes.c_bool
    lib.SetSuspendState(False, True, False)


def _shutdown(flag: str) -> None:
    completed = subprocess.run(
        ["shutdown", flag, "/t", "5", "/c", "Remote TG"],
        capture_output=True,
        timeout=15,
        creationflags=NO_WINDOW,
        check=False,
    )
    if completed.returncode not in {0, 1190}:
        detail = _decode(completed.stdout + completed.stderr).strip()
        raise ValueError(detail[:300] or "Команда питания не выполнена")


def _speakers() -> Any:
    try:
        import comtypes
        from pycaw.pycaw import AudioUtilities

        comtypes.CoInitialize()
        return AudioUtilities.GetSpeakers()
    except Exception as exc:
        raise ValueError("Громкость недоступна") from exc


def _is_muted(device: Any) -> bool:
    value = device.EndpointVolume.GetMute()
    if isinstance(value, (tuple, list)):
        value = value[0]
    return bool(value)
