"""Уведомления в Telegram: старт, диск и CPU."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from app.actions import Actions
from app.auth import Store


class Watcher:
    def __init__(self, store: Store, actions: Actions, ready: Callable[[], bool], send: Callable[[str], None]) -> None:
        self.store = store
        self.actions = actions
        self.ready = ready
        self.send = send

    def start(self) -> None:
        threading.Thread(target=self._run, name="watch", daemon=True).start()

    def _run(self) -> None:
        announced = False
        cpu_hits = 0
        hot_disks: set[str] = set()
        while True:
            if not announced and self.ready() and self.store.notify_ids():
                try:
                    host = self.actions.metrics()["host"]
                except (OSError, RuntimeError):
                    host = "ПК"
                self.send(f"Remote TG онлайн · {host}")
                announced = True
            time.sleep(30)
            if self.store.is_paused() or not self.ready():
                continue
            try:
                data = self.actions.metrics()
            except Exception:
                logging.exception("watch")
                continue
            cpu = float(data["cpu"])
            if cpu >= 90:
                cpu_hits += 1
                if cpu_hits == 2:
                    self.send(f"Высокая загрузка CPU: {cpu:.0f}%")
            else:
                cpu_hits = 0
            for disk in data["disks"]:
                mount = str(disk["mount"])
                percent = float(disk["percent"])
                if percent >= 90 and mount not in hot_disks:
                    hot_disks.add(mount)
                    self.send(f"Мало места на {mount}: занято {percent:.0f}%")
                elif percent < 85:
                    hot_disks.discard(mount)
