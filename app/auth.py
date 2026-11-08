"""Привязка Telegram, роли и журнал."""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
_LEGACY_FILES = ("config.json", "config.json.tmp", "audit.log", "agent.log")
_data_ready = False


def data_dir() -> Path:
    """Папка пользователя: %APPDATA%\\Remote TG. В каталог программы ничего не пишется."""
    global _data_ready
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    folder = Path(base) / "Remote TG"
    folder.mkdir(parents=True, exist_ok=True)
    if not _data_ready:
        pending = False
        for name in _LEGACY_FILES:
            source = ROOT / name
            target = folder / name
            if not source.is_file() or target.exists():
                continue
            try:
                source.replace(target)
            except OSError:
                pending = True
        _data_ready = not pending
    return folder


CONFIG_PATH = data_dir() / "config.json"

ROLES = ("owner", "operator", "viewer")
MAX_FAILS = 5
LOCK_SECONDS = 15 * 60
PAIR_TTL = 10 * 60
FIRST_PAIR_TTL = 7 * 24 * 3600

VIEWER_ACTIONS = frozenset({"status", "screenshot"})
OPERATOR_ACTIONS = VIEWER_ACTIONS | frozenset(
    {
        "files",
        "upload",
        "mkdir",
        "rename",
        "processes",
        "kill",
        "start",
        "services",
        "service_start",
        "clipboard",
        "volume",
        "windows",
        "close_window",
        "open",
        "download_url",
    }
)
OWNER_ACTIONS = OPERATOR_ACTIONS | frozenset(
    {
        "shell",
        "delete",
        "power",
        "service_stop",
        "power_abort",
        "audit",
        "users",
        "settings",
    }
)
ROLE_ACTIONS = {
    "viewer": VIEWER_ACTIONS,
    "operator": OPERATOR_ACTIONS,
    "owner": OWNER_ACTIONS,
}
NEEDS_CONFIRM = frozenset({"delete", "power", "service_stop", "shell"})


class AuthError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class User:
    id: int
    name: str
    role: str


LOCAL_USER = User(0, "этот ПК", "owner")


def actor_label(user: User | None) -> str:
    if user is None:
        return "нет"
    if user.id == 0:
        return "этот ПК"
    return f"{user.name} ({user.id})"


def clean_name(name: str) -> str:
    cleaned = "".join(ch for ch in (name or "") if ch.isprintable()).strip()
    return cleaned[:64] or "user"


def _same(left: str, right: str) -> bool:
    left = left.strip().casefold()
    right = right.strip().casefold()
    if len(left) != len(right):
        return False
    return secrets.compare_digest(left, right)


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or CONFIG_PATH
        self.audit_path = self.path.parent / "audit.log"
        self._lock = threading.Lock()
        self.data = self._load()

    def _blank(self) -> dict[str, Any]:
        return {
            "bot_token": "",
            "backup_bot_token": "",
            "users": [],
            "pair_code": "",
            "pair_role": "owner",
            "pair_until": 0,
            "failed_pairs": 0,
            "pair_lock_until": 0,
            "paused": False,
            "launchers": [],
        }

    def _load(self) -> dict[str, Any]:
        data = self._blank()
        existed = self.path.exists()
        if existed:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                raw = {}
            if isinstance(raw, dict):
                data.update(raw)
        changed = not existed
        for key in ("pin_hash", "pin_salt", "failed_pins", "lock_until"):
            if key in data:
                data.pop(key, None)
                changed = True
        if not data.get("pair_code"):
            data["pair_code"] = secrets.token_hex(4)
            changed = True
        if not isinstance(data.get("users"), list):
            data["users"] = []
            changed = True
        if not data["users"] and not data.get("pair_until"):
            data["pair_until"] = time.time() + FIRST_PAIR_TTL
            data["pair_role"] = "owner"
            changed = True
        if changed:
            self._write(data)
        return data

    def _write(self, data: dict[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(self.path)

    def save(self) -> None:
        with self._lock:
            self._write(self.data)

    def _users_raw(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in self.data.get("users") or []:
            if not isinstance(item, dict):
                continue
            try:
                role = str(item["role"])
                rows.append(
                    {
                        "id": int(item["id"]),
                        "name": clean_name(str(item.get("name") or item["id"])),
                        "role": role if role in ROLES else "viewer",
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
        return rows

    def users(self) -> list[User]:
        with self._lock:
            return [User(item["id"], item["name"], item["role"]) for item in self._users_raw()]

    def user_by_id(self, user_id: int) -> User | None:
        for user in self.users():
            if user.id == int(user_id):
                return user
        return None

    def allows(self, user: User, action: str) -> bool:
        return action in ROLE_ACTIONS.get(user.role, frozenset())

    def is_paused(self) -> bool:
        with self._lock:
            return bool(self.data.get("paused"))

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self.data["paused"] = bool(paused)
            self._write(self.data)

    def require(self, user: User | None, action: str) -> None:
        if user is None or not self.allows(user, action):
            raise AuthError("Нет доступа")

    def pair(self, user_id: int, name: str, code: str) -> User:
        with self._lock:
            now = time.time()
            if float(self.data.get("pair_lock_until") or 0) > now:
                raise AuthError("Привязка временно заблокирована")
            if float(self.data.get("pair_until") or 0) <= now:
                raise AuthError("Код истёк. Создайте новый в окне на ПК")
            if not _same(str(self.data.get("pair_code") or ""), code):
                fails = int(self.data.get("failed_pairs") or 0) + 1
                self.data["failed_pairs"] = fails
                if fails >= MAX_FAILS:
                    self.data["pair_lock_until"] = now + LOCK_SECONDS
                    self.data["failed_pairs"] = 0
                self._write(self.data)
                raise AuthError("Неверный код привязки")
            users = self._users_raw()
            if any(item["id"] == int(user_id) for item in users):
                raise AuthError("Этот аккаунт уже добавлен")
            role = str(self.data.get("pair_role") or "operator")
            if role not in ROLES:
                role = "operator"
            if not users:
                role = "owner"
            label = clean_name(name)
            users.append({"id": int(user_id), "name": label, "role": role})
            self.data["users"] = users
            self.data["pair_code"] = secrets.token_hex(4)
            self.data["pair_until"] = 0
            self.data["pair_role"] = "operator"
            self.data["failed_pairs"] = 0
            self._write(self.data)
            return User(int(user_id), label, role)

    def new_pair_code(self, role: str) -> str:
        if role not in ROLES:
            raise AuthError("Неизвестная роль")
        with self._lock:
            code = secrets.token_hex(4)
            self.data["pair_code"] = code
            self.data["pair_role"] = role
            self.data["pair_until"] = time.time() + PAIR_TTL
            self._write(self.data)
            return code

    def set_role(self, user_id: int, role: str) -> None:
        if role not in ROLES:
            raise AuthError("Неизвестная роль")
        with self._lock:
            users = self._users_raw()
            target = next((item for item in users if item["id"] == int(user_id)), None)
            if target is None:
                raise AuthError("Пользователь не найден")
            if target["role"] == "owner" and role != "owner":
                owners = [item for item in users if item["role"] == "owner"]
                if len(owners) <= 1:
                    raise AuthError("Нельзя снять роль с последнего владельца")
            target["role"] = role
            self.data["users"] = users
            self._write(self.data)

    def remove_user(self, user_id: int) -> None:
        with self._lock:
            users = self._users_raw()
            target = next((item for item in users if item["id"] == int(user_id)), None)
            if target is None:
                raise AuthError("Пользователь не найден")
            if target["role"] == "owner":
                owners = [item for item in users if item["role"] == "owner"]
                if len(owners) <= 1:
                    raise AuthError("Нельзя удалить последнего владельца")
            self.data["users"] = [item for item in users if item["id"] != int(user_id)]
            self._write(self.data)

    def set_tokens(self, token: str, backup: str) -> None:
        with self._lock:
            self.data["bot_token"] = token.strip()
            self.data["backup_bot_token"] = backup.strip()
            self._write(self.data)

    def _launchers_raw(self) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for item in self.data.get("launchers") or []:
            if not isinstance(item, dict):
                continue
            name = "".join(ch for ch in str(item.get("name") or "") if ch.isprintable()).strip()[:40]
            command = str(item.get("command") or "").strip()
            if not name or not command or "\x00" in command:
                continue
            rows.append({"id": str(item.get("id") or ""), "name": name, "command": command[:500]})
        return [row for row in rows if row["id"]]

    def launchers(self) -> list[dict[str, str]]:
        with self._lock:
            return self._launchers_raw()

    def add_launcher(self, name: str, command: str) -> dict[str, str]:
        title = "".join(ch for ch in name if ch.isprintable()).strip()[:40]
        command = command.strip()
        if not title or not command or "\x00" in command:
            raise AuthError("Нужны имя и команда")
        if len(command) > 500:
            raise AuthError("Слишком длинная команда")
        with self._lock:
            rows = self._launchers_raw()
            if any(row["name"].casefold() == title.casefold() for row in rows):
                raise AuthError("Такое имя уже есть")
            if len(rows) >= 24:
                raise AuthError("Не больше 24 ярлыков")
            row = {"id": secrets.token_hex(4), "name": title, "command": command}
            rows.append(row)
            self.data["launchers"] = rows
            self._write(self.data)
            return row

    def remove_launcher(self, launcher_id: str) -> None:
        with self._lock:
            rows = [row for row in self._launchers_raw() if row["id"] != launcher_id]
            self.data["launchers"] = rows
            self._write(self.data)

    def tokens(self) -> tuple[str, str]:
        with self._lock:
            return str(self.data.get("bot_token") or ""), str(self.data.get("backup_bot_token") or "")

    def public_view(self) -> dict[str, Any]:
        with self._lock:
            left = int(float(self.data.get("pair_until") or 0) - time.time())
            code = str(self.data.get("pair_code") or "") if left > 0 else ""
            return {
                "users": self._users_raw(),
                "pair_code": code,
                "pair_role": str(self.data.get("pair_role") or "operator"),
                "pair_seconds": max(0, left),
                "paused": bool(self.data.get("paused")),
                "has_token": bool(self.data.get("bot_token")),
                "has_backup": bool(self.data.get("backup_bot_token")),
                "launchers": self._launchers_raw(),
            }

    def audit(self, who: str, action: str, ok: bool, detail: str = "") -> None:
        row = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "who": who,
            "action": action,
            "ok": bool(ok),
            "detail": detail[:500],
        }
        line = json.dumps(row, ensure_ascii=False)
        with self._lock:
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            if self.audit_path.stat().st_size > 512_000:
                lines = self.audit_path.read_text(encoding="utf-8").splitlines()[-800:]
                self.audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def recent_audit(self, limit: int = 80) -> list[dict[str, Any]]:
        if not self.audit_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.audit_path.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
        rows.reverse()
        return rows

    def notify_ids(self) -> list[int]:
        return [user.id for user in self.users() if user.role in {"owner", "operator"}]
