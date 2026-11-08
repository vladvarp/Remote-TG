"""Команды Telegram. Доступ только у привязанных аккаунтов."""

from __future__ import annotations

import asyncio
import base64
import html
import logging
import secrets
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from app.actions import Actions, downloads_dir, format_bytes, format_uptime
from app.auth import AuthError, Store, User, actor_label, clean_name

SEND_LIMIT = 49 * 1024 * 1024
RECV_LIMIT = 20 * 1024 * 1024
ROLE_TITLE = {"owner": "владелец", "operator": "оператор", "viewer": "наблюдатель"}
BUTTONS = {
    "Статус": "status",
    "Скрин": "screenshot",
    "Процессы": "processes",
    "Диски": "files",
    "Окна": "windows",
    "Буфер": "clipboard",
    "Питание": "power",
}


def _redact(text: str, secrets_list: list[str]) -> str:
    cleaned = text
    for secret in secrets_list:
        if secret:
            cleaned = cleaned.replace(secret, "***")
    return cleaned[:500]


def h(text: object, limit: int = 3200) -> str:
    return html.escape(str(text)[:limit])


def rest(message: Message) -> str:
    parts = (message.text or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def keyboard(user: User) -> ReplyKeyboardMarkup:
    if user.role == "viewer":
        rows = [[KeyboardButton(text="Статус"), KeyboardButton(text="Скрин")]]
    elif user.role == "operator":
        rows = [
            [KeyboardButton(text="Статус"), KeyboardButton(text="Скрин")],
            [KeyboardButton(text="Процессы"), KeyboardButton(text="Диски")],
            [KeyboardButton(text="Окна"), KeyboardButton(text="Буфер")],
        ]
    else:
        rows = [
            [KeyboardButton(text="Статус"), KeyboardButton(text="Скрин")],
            [KeyboardButton(text="Процессы"), KeyboardButton(text="Диски")],
            [KeyboardButton(text="Окна"), KeyboardButton(text="Буфер")],
            [KeyboardButton(text="Питание")],
        ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text[:60], callback_data=data) for text, data in row]
            for row in rows
        ]
    )


def format_status(data: dict[str, Any]) -> str:
    lines = [
        f"<b>{h(data['host'])}</b> · {h(data['user'])}",
        f"CPU {data['cpu']:.0f}% · ОЗУ {data['ram']['percent']:.0f}% "
        f"({format_bytes(data['ram']['used'])} из {format_bytes(data['ram']['total'])})",
        f"Аптайм {format_uptime(data['uptime'])}",
    ]
    battery = data.get("battery")
    if battery:
        source = "сеть" if battery["plugged"] else "батарея"
        lines.append(f"Батарея {battery['percent']:.0f}% ({source})")
    for disk in data["disks"]:
        lines.append(
            f"{h(disk['mount'])} занято {disk['percent']:.0f}%, свободно {format_bytes(disk['free'])}"
        )
    lines.append(
        f"Сеть ↑ {format_bytes(data['net']['up'])}/с · ↓ {format_bytes(data['net']['down'])}/с"
    )
    return "\n".join(lines)


def help_text(user: User) -> str:
    common = "/status — состояние\n/shot — скриншот"
    if user.role == "viewer":
        return "<b>Наблюдатель</b>\n" + common
    operator = (
        "\n/ls [путь] — папка\n/cd путь\n/get путь — скачать в чат\n"
        "/mkdir путь\n/rename путь | новое имя\n"
        "Файл в чат — загрузить в текущую папку (до 20 МБ)\n"
        "/ps — процессы\n/kill PID\n/run команда\n/apps — быстрый запуск\n/go имя\n/services [фильтр]\n/svc start Имя\n"
        "/clip — буфер\n/clip set текст\n/vol 50\n/mute\n/windows\n/close заголовок\n"
        "/open путь или URL\n/wget URL | папка"
    )
    if user.role == "operator":
        return "<b>Оператор</b>\n" + common + operator
    return (
        "<b>Владелец</b>\n"
        + common
        + operator
        + "\n/rm путь — удалить файл\n/svc stop Имя\n/power — питание\n/sh команда\n/audit — журнал"
        + "\nОпасная команда спрашивает кнопками «Да» и «Нет»."
    )


class BotRunner:
    def __init__(self, store: Store, actions: Actions) -> None:
        self.store = store
        self.actions = actions
        self.loop: asyncio.AbstractEventLoop | None = None
        self.bot: Bot | None = None
        self.dp: Dispatcher | None = None
        self.ready = False
        self.using = ""
        self.last_error = ""
        self._stop = False
        self.pending: dict[int, dict[str, Any]] = {}
        self.cwd: dict[int, str] = {}
        self.path_tokens: dict[str, str] = {}

    def remember(self, path: str) -> str:
        token = secrets.token_hex(4)
        self.path_tokens[token] = path
        if len(self.path_tokens) > 400:
            self.path_tokens.pop(next(iter(self.path_tokens)))
        return token

    def start(self) -> None:
        threading.Thread(target=self._thread, name="telegram", daemon=True).start()

    def _thread(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._loop())

    def stop(self) -> None:
        self._stop = True
        self.reload()

    def reload(self) -> None:
        loop = self.loop
        dp = self.dp
        if loop and dp and loop.is_running():
            asyncio.run_coroutine_threadsafe(dp.stop_polling(), loop)

    def broadcast(self, text: str, skip: int | None = None) -> None:
        loop = self.loop
        if loop is None or not loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(self._broadcast(text, skip), loop)

    async def _broadcast(self, text: str, skip: int | None = None) -> None:
        bot = self.bot
        if bot is None:
            return
        safe = h(text, 3500)
        for uid in self.store.notify_ids():
            if skip is not None and uid == skip:
                continue
            try:
                await bot.send_message(uid, safe)
            except Exception:
                logging.exception("notify %s", uid)

    def status(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "using": self.using,
            "error": self.last_error,
            "paused": self.store.is_paused(),
        }

    async def _commands(self, bot: Bot) -> None:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Статус"),
                BotCommand(command="pair", description="Привязать аккаунт"),
                BotCommand(command="help", description="Команды"),
                BotCommand(command="status", description="Состояние ПК"),
                BotCommand(command="shot", description="Скриншот"),
                BotCommand(command="ls", description="Папка"),
                BotCommand(command="cd", description="Текущая папка"),
                BotCommand(command="get", description="Скачать файл"),
                BotCommand(command="mkdir", description="Создать папку"),
                BotCommand(command="rename", description="Переименовать"),
                BotCommand(command="rm", description="Удалить файл"),
                BotCommand(command="ps", description="Процессы"),
                BotCommand(command="kill", description="Завершить процесс"),
                BotCommand(command="run", description="Запустить программу"),
                BotCommand(command="apps", description="Список быстрого запуска"),
                BotCommand(command="go", description="Запустить сохранённую программу"),
                BotCommand(command="services", description="Службы"),
                BotCommand(command="svc", description="Старт или стоп службы"),
                BotCommand(command="power", description="Питание"),
                BotCommand(command="clip", description="Буфер обмена"),
                BotCommand(command="vol", description="Громкость"),
                BotCommand(command="mute", description="Без звука"),
                BotCommand(command="windows", description="Окна"),
                BotCommand(command="close", description="Закрыть окно"),
                BotCommand(command="open", description="Открыть файл или ссылку"),
                BotCommand(command="wget", description="Скачать URL"),
                BotCommand(command="sh", description="Командная строка"),
                BotCommand(command="audit", description="Журнал"),
            ]
        )

    async def _connect(self) -> Bot | None:
        primary, backup = self.store.tokens()
        last = ""
        for label, token in (("основной", primary), ("запасной", backup)):
            if not token:
                continue
            bot = Bot(
                token,
                default=DefaultBotProperties(
                    parse_mode=ParseMode.HTML,
                    link_preview_is_disabled=True,
                ),
            )
            try:
                me = await bot.get_me()
                await bot.delete_webhook(drop_pending_updates=True)
                await self._commands(bot)
                self.using = f"{label}:@{me.username}" if me.username else label
                self.last_error = ""
                return bot
            except Exception as exc:
                last = _redact(str(exc), [primary, backup])
                logging.warning("bot %s: %s", label, last)
                await bot.session.close()
        self.using = ""
        self.last_error = "Токен бота не задан" if not primary and not backup else (last or "Не удалось подключиться")
        return None

    async def _loop(self) -> None:
        while not self._stop:
            self.ready = False
            bot = await self._connect()
            if bot is None:
                await asyncio.sleep(10)
                continue
            self.bot = bot
            dispatcher = Dispatcher()
            dispatcher.include_router(build_router(self))
            self.dp = dispatcher
            self.ready = True
            try:
                await dispatcher.start_polling(bot, handle_signals=False)
            except Exception as exc:
                self.last_error = _redact(str(exc), list(self.store.tokens()))
                logging.exception("polling")
            finally:
                self.ready = False
                try:
                    await bot.session.close()
                except Exception:
                    logging.exception("close bot")
            if self._stop:
                break
            await asyncio.sleep(3)


def build_router(runner: BotRunner) -> Router:
    router = Router()
    store = runner.store
    actions = runner.actions

    def private(message: Message) -> bool:
        return message.from_user is not None and message.chat.type == ChatType.PRIVATE

    async def access_id(tg_user: Any, action: str, send: Callable[[str], Awaitable[Any]]) -> User | None:
        if store.is_paused():
            await send("Бот на паузе. Снимите паузу в окне или в трее на ПК.")
            return None
        user = store.user_by_id(tg_user.id)
        if user is None:
            await send("Нет доступа. Код привязки показан в окне Remote TG на этом ПК.\nКоманда: /pair КОД")
            return None
        if not store.allows(user, action):
            await send("Этой роли действие недоступно.")
            return None
        return user

    async def access(message: Message, action: str) -> User | None:
        if not private(message):
            return None
        return await access_id(message.from_user, action, message.answer)

    async def say(message: Message, text: str, user: User | None = None) -> None:
        await message.answer(text, reply_markup=keyboard(user) if user else None)

    async def danger(message: Message, user: User, action: str, payload: dict[str, Any], prompt: str) -> None:
        token = secrets.token_hex(4)
        runner.pending[message.chat.id] = {
            "stage": "confirm",
            "action": action,
            "payload": payload,
            "token": token,
            "user_id": user.id,
            "expires": time.time() + 120,
        }
        await message.answer(prompt, reply_markup=inline([[("Да", f"ok:{token}"), ("Нет", f"no:{token}")]]))

    async def perform(
        message: Message,
        user: User,
        action: str,
        payload: dict[str, Any],
    ) -> None:
        if action == "status":
            result = actions.run(user, "status", "статус", actions.metrics)
            await say(message, format_status(result["data"]) if result["ok"] else h(result["error"]), user)
            return
        if action == "screenshot":
            result = actions.run(user, "screenshot", "скриншот", actions.take_screenshot)
            if not result["ok"]:
                await say(message, h(result["error"]), user)
                return
            raw = base64.b64decode(result["data"])
            await message.answer_photo(BufferedInputFile(raw, filename="screen.jpg"), reply_markup=keyboard(user))
            return
        if action == "files":
            await send_files(message, user, str(payload.get("path") or ""))
            return
        if action == "processes":
            await send_processes(message, user)
            return
        if action == "windows":
            await send_windows(message, user)
            return
        if action == "clipboard":
            result = actions.run(user, "clipboard", "чтение", actions.clipboard_get)
            if not result["ok"]:
                await say(message, h(result["error"]), user)
                return
            text = result["data"] or "Буфер пуст"
            await say(message, f"<b>Буфер</b>\n<pre>{h(text, 3000)}</pre>", user)
            return
        if action == "power":
            mode = str(payload.get("mode") or "")
            result = actions.run(
                user,
                "power",
                mode,
                lambda: actions.power(mode),
                confirm=True,
            )
            await say(message, h(result["data"] if result["ok"] else result["error"]), user)
            return
        if action == "power_abort":
            result = actions.run(user, "power_abort", "abort", lambda: actions.power("abort"))
            await say(message, h(result["data"] if result["ok"] else result["error"]), user)
            return
        if action == "shell":
            command = str(payload.get("command") or "")
            result = actions.run(
                user,
                "shell",
                command,
                lambda: actions.shell(command),
                confirm=True,
            )
            text = result["data"] if result["ok"] else result["error"]
            await say(message, f"<pre>{h(text, 3000)}</pre>", user)
            return
        if action == "delete":
            path = str(payload.get("path") or "")
            result = actions.run(
                user,
                "delete",
                path,
                lambda: actions.delete_file(path),
                confirm=True,
            )
            await say(message, h(result["data"] if result["ok"] else result["error"]), user)
            return
        if action == "service_stop":
            name = str(payload.get("name") or "")
            result = actions.run(
                user,
                "service_stop",
                name,
                lambda: actions.service_control(name, False),
                confirm=True,
            )
            await say(message, h(result["data"] if result["ok"] else result["error"]), user)
            return
        if action == "kill":
            pid = int(payload.get("pid") or 0)
            result = actions.run(user, "kill", str(pid), lambda: actions.kill_process(pid))
            await say(message, h(result["data"] if result["ok"] else result["error"]), user)
            return
        if action == "close_window":
            hwnd = str(payload.get("hwnd") or "")
            result = actions.run(user, "close_window", hwnd, lambda: actions.close_window(hwnd))
            await say(message, "Закрытие отправлено." if result["ok"] else h(result["error"]), user)

    async def send_files(message: Message, user: User, path: str, roots: bool = False) -> None:
        shown = "" if roots else (path or runner.cwd.get(message.chat.id, ""))
        result = actions.run(user, "files", shown or "диски", lambda: actions.list_directory(shown))
        if not result["ok"]:
            await say(message, h(result["error"]), user)
            return
        data = result["data"]
        if data["path"]:
            runner.cwd[message.chat.id] = data["path"]
        lines = [f"<b>{h(data['path'] or 'Диски')}</b>"]
        rows: list[list[tuple[str, str]]] = []
        if not data["path"]:
            row: list[tuple[str, str]] = []
            for drive in data["drives"]:
                row.append((drive, f"v:{drive[0]}"))
                if len(row) == 4:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
        else:
            rows.append([("Вверх", "o:" + runner.remember(data["parent"] or ""))])
            for entry in [item for item in data["entries"] if item["dir"]][:8]:
                rows.append([(entry["name"][:28], "o:" + runner.remember(entry["path"]))])
            for entry in [item for item in data["entries"] if not item["dir"]][:6]:
                rows.append([("Файл " + entry["name"][:24], "g:" + runner.remember(entry["path"]))])
            preview = data["entries"][:20]
            if preview:
                lines.append("")
            for entry in preview:
                mark = "папка" if entry["dir"] else format_bytes(entry["size"])
                lines.append(f"{h(entry['name'])} · {mark}")
            if data["truncated"]:
                lines.append("Показаны первые 400 имён.")
        if rows:
            await message.answer("Открыть", reply_markup=inline(rows))
        await message.answer("\n".join(lines), reply_markup=keyboard(user))

    async def send_file(message: Message, user: User, path: str) -> None:
        result = actions.run(user, "files", path, lambda: actions.file_info(path))
        if not result["ok"]:
            await say(message, h(result["error"]), user)
            return
        info = result["data"]
        if info["size"] > SEND_LIMIT:
            await say(message, "В чат уходит файл до 49 МБ.", user)
            return
        await message.answer_document(FSInputFile(info["path"]), reply_markup=keyboard(user))

    async def send_processes(message: Message, user: User) -> None:
        result = actions.run(user, "processes", "список", actions.processes)
        if not result["ok"]:
            await say(message, h(result["error"]), user)
            return
        lines = ["<b>Процессы</b>"]
        rows: list[list[tuple[str, str]]] = []
        for proc in result["data"][:12]:
            lines.append(f"{proc['pid']} · {h(proc['name'])} · {format_bytes(proc['ram'])}")
        if user.role != "viewer":
            for proc in result["data"][:6]:
                rows.append([(f"Завершить {proc['pid']}", f"k:{proc['pid']}")])
        if rows:
            await message.answer("Завершить", reply_markup=inline(rows))
        await message.answer("\n".join(lines), reply_markup=keyboard(user))

    async def send_windows(message: Message, user: User) -> None:
        result = actions.run(user, "windows", "список", actions.list_windows)
        if not result["ok"]:
            await say(message, h(result["error"]), user)
            return
        lines = ["<b>Окна</b>"]
        for item in result["data"][:25]:
            lines.append(f"{h(item['hwnd'])} · {h(item['title'])}")
        await say(message, "\n".join(lines), user)

    @router.message(CommandStart())
    async def start_cmd(message: Message) -> None:
        if not private(message):
            return
        runner.pending.pop(message.chat.id, None)
        assert message.from_user is not None
        if store.is_paused():
            await message.answer("Бот на паузе. Снимите паузу в окне или в трее на ПК.")
            return
        user = store.user_by_id(message.from_user.id)
        if user is None:
            await message.answer("Нет доступа. Код привязки показан в окне Remote TG на этом ПК.\nКоманда: /pair КОД")
            return
        result = actions.run(user, "status", "старт", actions.metrics)
        text = format_status(result["data"]) if result["ok"] else h(result["error"])
        await say(message, text, user)

    @router.message(Command("pair"))
    async def pair_cmd(message: Message) -> None:
        if not private(message):
            return
        runner.pending.pop(message.chat.id, None)
        assert message.from_user is not None
        if store.is_paused():
            await message.answer("Бот на паузе. Снимите паузу в окне или в трее на ПК.")
            return
        code = rest(message)
        if not code:
            await message.answer("Формат: /pair КОД")
            return
        try:
            user = store.pair(message.from_user.id, clean_name(message.from_user.full_name), code)
        except AuthError as exc:
            await message.answer(h(exc.message))
            return
        store.audit(actor_label(user), "pair", True, user.role)
        await say(message, f"Готово. Роль: {h(ROLE_TITLE.get(user.role, user.role))}.", user)
        runner.broadcast(
            f"Привязан аккаунт {user.name} ({user.id}), роль {ROLE_TITLE.get(user.role, user.role)}",
            skip=user.id,
        )

    @router.message(Command("help"))
    async def help_cmd(message: Message) -> None:
        user = await access(message, "status")
        if user:
            runner.pending.pop(message.chat.id, None)
            await say(message, help_text(user), user)

    @router.message(Command("status"))
    async def status_cmd(message: Message) -> None:
        runner.pending.pop(message.chat.id, None)
        user = await access(message, "status")
        if user:
            await perform(message, user, "status", {})

    @router.message(Command("shot"))
    async def shot_cmd(message: Message) -> None:
        runner.pending.pop(message.chat.id, None)
        user = await access(message, "screenshot")
        if user:
            await perform(message, user, "screenshot", {})

    @router.message(Command("ls"))
    async def ls_cmd(message: Message) -> None:
        runner.pending.pop(message.chat.id, None)
        user = await access(message, "files")
        if user:
            await send_files(message, user, rest(message))

    @router.message(Command("cd"))
    async def cd_cmd(message: Message) -> None:
        runner.pending.pop(message.chat.id, None)
        user = await access(message, "files")
        if not user:
            return
        path = rest(message)
        result = actions.run(user, "files", path, lambda: actions.list_directory(path))
        if not result["ok"] or not result["data"]["path"]:
            await say(message, h(result.get("error") or "Нужен путь к папке"), user)
            return
        runner.cwd[message.chat.id] = result["data"]["path"]
        await say(message, h(result["data"]["path"]), user)

    @router.message(Command("get"))
    async def get_cmd(message: Message) -> None:
        runner.pending.pop(message.chat.id, None)
        user = await access(message, "files")
        if user:
            await send_file(message, user, rest(message) or "")

    @router.message(Command("mkdir"))
    async def mkdir_cmd(message: Message) -> None:
        runner.pending.pop(message.chat.id, None)
        user = await access(message, "mkdir")
        if not user:
            return
        path = rest(message)
        result = actions.run(user, "mkdir", path, lambda: actions.make_dir(path))
        await say(message, h(result["data"] if result["ok"] else result["error"]), user)

    @router.message(Command("rename"))
    async def rename_cmd(message: Message) -> None:
        runner.pending.pop(message.chat.id, None)
        user = await access(message, "rename")
        if not user:
            return
        body = rest(message)
        if " | " not in body:
            await say(message, "Формат: /rename путь | новое имя", user)
            return
        src, dst = [part.strip() for part in body.split(" | ", 1)]
        result = actions.run(user, "rename", src, lambda: actions.rename(src, dst))
        await say(message, h(result["data"] if result["ok"] else result["error"]), user)

    @router.message(Command("rm"))
    async def rm_cmd(message: Message) -> None:
        runner.pending.pop(message.chat.id, None)
        user = await access(message, "delete")
        if user:
            await danger(message, user, "delete", {"path": rest(message)}, "Удалить этот файл?")

    @router.message(Command("ps"))
    async def ps_cmd(message: Message) -> None:
        runner.pending.pop(message.chat.id, None)
        user = await access(message, "processes")
        if user:
            await send_processes(message, user)

    @router.message(Command("kill"))
    async def kill_cmd(message: Message) -> None:
        runner.pending.pop(message.chat.id, None)
        user = await access(message, "kill")
        if not user:
            return
        pid_text = rest(message)
        if not pid_text.isdigit():
            await say(message, "Формат: /kill PID", user)
            return
        pid = int(pid_text)
        await danger(message, user, "kill", {"pid": pid}, f"Завершить процесс {pid}?")

    @router.message(Command("run"))
    async def run_cmd(message: Message) -> None:
        runner.pending.pop(message.chat.id, None)
        user = await access(message, "start")
        if not user:
            return
        command = rest(message)
        result = actions.run(user, "start", command, lambda: actions.start_program(command))
        await say(message, h(result["data"] if result["ok"] else result["error"]), user)

    @router.message(Command("apps"))
    async def apps_cmd(message: Message) -> None:
        runner.pending.pop(message.chat.id, None)
        user = await access(message, "start")
        if not user:
            return
        rows = store.launchers()
        if not rows:
            await say(message, "Быстрый запуск пуст. Добавьте программы в окне, раздел «Запуск».", user)
            return
        lines = ["<b>Быстрый запуск</b>"]
        for row in rows:
            lines.append(f"{h(row['name'])} · {h(row['command'])}")
        lines.append("\nЗапуск: /go имя")
        await say(message, "\n".join(lines), user)

    @router.message(Command("go"))
    async def go_cmd(message: Message) -> None:
        runner.pending.pop(message.chat.id, None)
        user = await access(message, "start")
        if not user:
            return
        title = rest(message).casefold()
        row = next((item for item in store.launchers() if item["name"].casefold() == title), None)
        if row is None:
            await say(message, "Нет такого имени. Список: /apps", user)
            return
        command = row["command"]
        result = actions.run(user, "start", command, lambda: actions.start_program(command))
        await say(message, h(result["data"] if result["ok"] else result["error"]), user)

    @router.message(Command("services"))
    async def services_cmd(message: Message) -> None:
        runner.pending.pop(message.chat.id, None)
        user = await access(message, "services")
        if not user:
            return
        query = rest(message)
        result = actions.run(user, "services", query or "список", lambda: actions.services(query))
        if not result["ok"]:
            await say(message, h(result["error"]), user)
            return
        lines = ["<b>Службы</b>"]
        for item in result["data"][:20]:
            lines.append(f"{h(item['status'])} · {h(item['name'])} · {h(item['display'])}")
        if not result["data"]:
            lines.append("Ничего не найдено.")
        await say(message, "\n".join(lines), user)

    @router.message(Command("svc"))
    async def svc_cmd(message: Message) -> None:
        runner.pending.pop(message.chat.id, None)
        body = rest(message)
        parts = body.split(maxsplit=1)
        if len(parts) < 2 or parts[0] not in {"start", "stop"}:
            if private(message):
                await message.answer("Формат: /svc start Имя или /svc stop Имя")
            return
        mode, name = parts[0], parts[1].strip()
        if mode == "start":
            user = await access(message, "service_start")
            if not user:
                return
            result = actions.run(user, "service_start", name, lambda: actions.service_control(name, True))
            await say(message, h(result["data"] if result["ok"] else result["error"]), user)
            return
        user = await access(message, "service_stop")
        if user:
            await danger(message, user, "service_stop", {"name": name}, f"Остановить службу {h(name)}?")

    @router.message(Command("power"))
    async def power_cmd(message: Message) -> None:
        runner.pending.pop(message.chat.id, None)
        user = await access(message, "status")
        if not user:
            return
        if user.role != "owner":
            await say(message, "Питание доступно владельцу.", user)
            return
        await message.answer("Питание", reply_markup=power_board())

    @router.message(Command("clip"))
    async def clip_cmd(message: Message) -> None:
        runner.pending.pop(message.chat.id, None)
        user = await access(message, "clipboard")
        if not user:
            return
        body = rest(message)
        if body.lower().startswith("set "):
            text = body[4:]
            result = actions.run(
                user,
                "clipboard",
                f"запись {len(text)}",
                lambda: actions.clipboard_set(text),
            )
            await say(message, h(result["data"] if result["ok"] else result["error"]), user)
            return
        await perform(message, user, "clipboard", {})

    @router.message(Command("vol"))
    async def vol_cmd(message: Message) -> None:
        runner.pending.pop(message.chat.id, None)
        user = await access(message, "volume")
        if not user:
            return
        body = rest(message)
        if not body:
            result = actions.run(user, "volume", "чтение", actions.volume_state)
        else:
            if not body.isdigit():
                await say(message, "Формат: /vol 50", user)
                return
            level = int(body)
            result = actions.run(user, "volume", body, lambda: actions.set_volume(level))
        if not result["ok"]:
            await say(message, h(result["error"]), user)
            return
        state = result["data"]
        mute = "выкл" if state["mute"] else "вкл"
        await say(message, f"Громкость {state['level']}% · звук {mute}", user)

    @router.message(Command("mute"))
    async def mute_cmd(message: Message) -> None:
        runner.pending.pop(message.chat.id, None)
        user = await access(message, "volume")
        if not user:
            return
        try:
            current = actions.volume_state()
        except ValueError as exc:
            await say(message, h(exc), user)
            return
        result = actions.run(user, "volume", "mute", lambda: actions.set_mute(not current["mute"]))
        if not result["ok"]:
            await say(message, h(result["error"]), user)
            return
        await say(message, "Без звука." if result["data"]["mute"] else "Звук включён.", user)

    @router.message(Command("windows"))
    async def windows_cmd(message: Message) -> None:
        runner.pending.pop(message.chat.id, None)
        user = await access(message, "windows")
        if user:
            await send_windows(message, user)

    @router.message(Command("close"))
    async def close_cmd(message: Message) -> None:
        runner.pending.pop(message.chat.id, None)
        user = await access(message, "close_window")
        if not user:
            return
        body = rest(message)
        if body.lstrip("-").isdigit():
            await danger(message, user, "close_window", {"hwnd": body}, "Закрыть это окно?")
            return
        try:
            listed = actions.list_windows()
        except OSError as exc:
            await say(message, h(exc), user)
            return
        matches = [item for item in listed if body.casefold() in item["title"].casefold()]
        if len(matches) != 1:
            await say(message, "Нужен точный заголовок или hwnd из /windows." if body else "Формат: /close заголовок", user)
            return
        hwnd = matches[0]["hwnd"]
        await danger(message, user, "close_window", {"hwnd": hwnd}, f"Закрыть «{h(matches[0]['title'])}»?")

    @router.message(Command("open"))
    async def open_cmd(message: Message) -> None:
        runner.pending.pop(message.chat.id, None)
        user = await access(message, "open")
        if not user:
            return
        value = rest(message)
        result = actions.run(user, "open", value, lambda: actions.open_target(value))
        await say(message, h(result["data"] if result["ok"] else result["error"]), user)

    @router.message(Command("wget"))
    async def wget_cmd(message: Message) -> None:
        runner.pending.pop(message.chat.id, None)
        user = await access(message, "download_url")
        if not user:
            return
        body = rest(message)
        if " | " in body:
            url, folder = [part.strip() for part in body.split(" | ", 1)]
        else:
            url, folder = body, str(downloads_dir())
        result = actions.run(user, "download_url", url, lambda: actions.download_url(url, folder))
        await say(message, h(result["data"] if result["ok"] else result["error"]), user)

    @router.message(Command("sh"))
    async def sh_cmd(message: Message) -> None:
        runner.pending.pop(message.chat.id, None)
        user = await access(message, "shell")
        if user:
            await danger(message, user, "shell", {"command": rest(message)}, "Выполнить команду? Она попадёт в журнал.")

    @router.message(Command("audit"))
    async def audit_cmd(message: Message) -> None:
        runner.pending.pop(message.chat.id, None)
        user = await access(message, "audit")
        if not user:
            return
        rows = store.recent_audit(15)
        if not rows:
            await say(message, "Журнал пуст.", user)
            return
        lines = ["<b>Журнал</b>"]
        for row in rows:
            mark = "ок" if row.get("ok") else "ошибка"
            lines.append(f"{h(row.get('ts', ''))} · {h(row.get('who', ''))} · {h(row.get('action', ''))} · {mark}")
        await say(message, "\n".join(lines), user)

    @router.message(F.document)
    async def on_document(message: Message) -> None:
        if not private(message) or message.document is None:
            return
        runner.pending.pop(message.chat.id, None)
        user = await access(message, "upload")
        if not user or message.bot is None:
            return
        document = message.document
        if document.file_size and document.file_size > RECV_LIMIT:
            await say(message, "Бот принимает файл до 20 МБ.", user)
            return
        caption = (message.caption or "").strip()
        folder = caption if (":\\" in caption or caption.startswith("\\\\")) else runner.cwd.get(message.chat.id, str(downloads_dir()))
        try:
            target = actions.reserve_upload(folder, document.file_name or "file")
            remote = await message.bot.get_file(document.file_id)
            if not remote.file_path:
                raise ValueError("Telegram не отдал файл")
            await message.bot.download_file(remote.file_path, destination=target)
        except Exception as exc:
            store.audit(actor_label(user), "upload", False, folder)
            await say(message, h(exc), user)
            return
        store.audit(actor_label(user), "upload", True, str(target))
        await say(message, f"Сохранено: {h(target)}", user)

    @router.message(F.text)
    async def on_text(message: Message) -> None:
        if not private(message):
            return
        text = (message.text or "").strip()
        if not text or text.startswith("/"):
            return
        pending = runner.pending.get(message.chat.id)
        if pending and time.time() > float(pending["expires"]):
            runner.pending.pop(message.chat.id, None)
            pending = None
        if pending and text in BUTTONS:
            runner.pending.pop(message.chat.id, None)
            pending = None
        if pending:
            await message.answer("Нажмите «Да» или «Нет» под вопросом.")
            return
        action = BUTTONS.get(text)
        if action == "status":
            user = await access(message, "status")
            if user:
                await perform(message, user, "status", {})
        elif action == "screenshot":
            user = await access(message, "screenshot")
            if user:
                await perform(message, user, "screenshot", {})
        elif action == "processes":
            user = await access(message, "processes")
            if user:
                await send_processes(message, user)
        elif action == "files":
            user = await access(message, "files")
            if user:
                await send_files(message, user, "", roots=True)
        elif action == "windows":
            user = await access(message, "windows")
            if user:
                await send_windows(message, user)
        elif action == "clipboard":
            user = await access(message, "clipboard")
            if user:
                await perform(message, user, "clipboard", {})
        elif action == "power":
            user = await access(message, "status")
            if user and user.role == "owner":
                await message.answer("Питание", reply_markup=power_board())
            elif user:
                await say(message, "Питание доступно владельцу.", user)

    @router.callback_query()
    async def on_callback(query: CallbackQuery) -> None:
        await query.answer()
        message = query.message
        data = query.data or ""
        if query.from_user is None or message is None or not hasattr(message, "answer"):
            return
        if message.chat.type != ChatType.PRIVATE:
            return
        if data.startswith(("ok:", "no:")):
            token = data[3:]
            pending = runner.pending.get(message.chat.id)
            if (
                not pending
                or pending.get("token") != token
                or time.time() > float(pending.get("expires") or 0)
            ):
                runner.pending.pop(message.chat.id, None)
                await message.answer("Подтверждение устарело. Повторите команду.")
                return
            runner.pending.pop(message.chat.id, None)
            if store.is_paused():
                await message.answer("Бот на паузе. Снимите паузу в окне или в трее на ПК.")
                return
            user = store.user_by_id(query.from_user.id)
            if user is None or user.id != int(pending.get("user_id") or 0) or not store.allows(user, str(pending["action"])):
                await message.answer("Нет доступа.")
                return
            if data.startswith("no:"):
                await message.answer("Отменено.", reply_markup=keyboard(user))
                return
            await perform(message, user, str(pending["action"]), pending["payload"])
            return
        runner.pending.pop(message.chat.id, None)

        async def send(text: str) -> None:
            await message.answer(text)

        if data.startswith("v:") and len(data) == 3:
            user = await access_id(query.from_user, "files", send)
            if user:
                await send_files(message, user, data[2] + ":\\")
            return
        if data.startswith("o:"):
            user = await access_id(query.from_user, "files", send)
            if not user:
                return
            path = runner.path_tokens.get(data[2:])
            if path is None:
                await send("Список устарел. Откройте /ls снова.")
                return
            await send_files(message, user, path)
            return
        if data.startswith("g:"):
            user = await access_id(query.from_user, "files", send)
            if not user:
                return
            path = runner.path_tokens.get(data[2:])
            if path is None:
                await send("Список устарел. Откройте /ls снова.")
                return
            await send_file(message, user, path)
            return
        if data.startswith("k:") and data[2:].isdigit():
            user = await access_id(query.from_user, "kill", send)
            if user:
                await danger(message, user, "kill", {"pid": int(data[2:])}, f"Завершить процесс {data[2:]}?")
            return
        if data.startswith("pw:"):
            mode = data[3:]
            if mode == "abort":
                user = await access_id(query.from_user, "power_abort", send)
                if user:
                    await perform(message, user, "power_abort", {"mode": "abort"})
                return
            if mode not in {"lock", "sleep", "restart", "shutdown"}:
                return
            user = await access_id(query.from_user, "power", send)
            if user:
                names = {"lock": "блокировку", "sleep": "сон", "restart": "перезагрузку", "shutdown": "выключение"}
                await danger(message, user, "power", {"mode": mode}, f"Подтвердите {names.get(mode, mode)}?")

    return router


def power_board() -> InlineKeyboardMarkup:
    return inline(
        [
            [("Блокировка", "pw:lock"), ("Сон", "pw:sleep")],
            [("Перезагрузка", "pw:restart"), ("Выключение", "pw:shutdown")],
            [("Отмена таймера", "pw:abort")],
        ]
    )
