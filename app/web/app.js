const $ = (id) => document.getElementById(id);

let boot = null;
let current = "dash";
let dir = { path: "", parent: null, entries: [], drives: [] };
let selected = "";
let procs = [];
let muted = false;
let downloadsSet = false;

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function toast(text, bad) {
  const node = $("toast");
  node.textContent = text || "";
  node.className = bad ? "bad" : "";
}

function confirmed() { return $("confirm").checked; }

function disarm() { $("confirm").checked = false; }

async function call(name, ...args) {
  if (!window.pywebview || !window.pywebview.api) {
    return { ok: false, error: "Окно ещё загружается" };
  }
  try {
    return await window.pywebview.api[name](...args);
  } catch (err) {
    return { ok: false, error: String(err) };
  }
}

function done(res, text) {
  if (!res || !res.ok) {
    toast((res && res.error) || "Ошибка", true);
    return false;
  }
  if (text) toast(text, false);
  return true;
}

function formatBytes(size) {
  let value = Number(size) || 0;
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  const shown = index === 0 ? Math.round(value) : value.toFixed(1);
  return shown + " " + units[index];
}

function formatUptime(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days) return days + " д " + hours + " ч " + minutes + " мин";
  return hours + " ч " + minutes + " мин";
}

function show(page) {
  current = page;
  document.querySelectorAll(".page").forEach((node) => node.classList.toggle("active", node.id === page));
  document.querySelectorAll("#nav button").forEach((node) => node.classList.toggle("active", node.dataset.page === page));
  if (page === "files") loadDir(dir.path || "");
  if (page === "proc") loadProcs();
  if (page === "launch") renderLaunchers();
  if (page === "svc") loadServices();
  if (page === "wins") loadWindows();
  if (page === "sys") loadVolume();
  if (page === "audit") loadAudit();
  if (page === "settings") renderSettings();
  if (page === "dash") renderDash();
}

function renderHeader() {
  const bot = boot.bot || {};
  $("bot-dot").className = "dot " + (bot.ready ? "ok" : "bad");
  $("bot-label").textContent = bot.ready ? (bot.using || "бот онлайн") : (bot.error || "бот не подключён");
  const metrics = boot.metrics || {};
  $("host").textContent = (metrics.host || "") + (metrics.user ? " · " + metrics.user : "");
  $("pause-btn").textContent = boot.paused ? "Продолжить" : "Пауза";
}

function renderChecklist() {
  const box = $("checklist");
  const items = [];
  if (!boot.has_token) items.push("Вставьте токен бота в настройках.");
  if (!boot.users.length) {
    items.push(boot.pair_code ? "Код привязки: " + boot.pair_code + ". В Telegram: /pair " + boot.pair_code : "Откройте настройки и выдайте код привязки.");
  }
  box.hidden = items.length === 0;
  box.replaceChildren();
  items.forEach((text) => box.append(el("li", text)));
}

function renderDash() {
  const metrics = boot.metrics;
  if (!metrics) return;
  const cards = [
    ["CPU", Math.round(metrics.cpu) + "%"],
    ["ОЗУ", Math.round(metrics.ram.percent) + "%"],
    ["Аптайм", formatUptime(metrics.uptime)],
    ["Сеть", "↑ " + formatBytes(metrics.net.up) + "/с"],
  ];
  if (metrics.battery) {
    cards.push(["Батарея", Math.round(metrics.battery.percent) + "% " + (metrics.battery.plugged ? "сеть" : "батарея")]);
  }
  const wrap = $("cards");
  wrap.replaceChildren();
  cards.forEach(([label, value]) => {
    const card = el("div", "", "card");
    card.append(el("span", label), el("strong", value));
    wrap.append(card);
  });
  const disks = $("disks");
  disks.replaceChildren();
  (metrics.disks || []).forEach((disk) => {
    const row = el("div", "", "disk");
    row.append(el("div", disk.mount + " · занято " + Math.round(disk.percent) + "% · свободно " + formatBytes(disk.free)));
    const bar = el("div", "", "bar");
    const fill = document.createElement("i");
    fill.style.width = Math.min(100, disk.percent) + "%";
    bar.append(fill);
    row.append(bar);
    disks.append(row);
  });
}

const ROLE_NAME = { owner: "владелец", operator: "оператор", viewer: "наблюдатель" };
const ROLE_DIFF = {
  viewer: "Наблюдатель: только состояние ПК и скриншот. Нет файлов, процессов, окон, звука, буфера, запуска программ и настроек.",
  operator: "Оператор: файлы, процессы, окна, звук, буфер и запуск программ. Нет удаления, питания, остановки служб, командной строки, журнала и настроек.",
  owner: "Владелец: всё, включая удаление, питание, остановку служб, командную строку, журнал и настройки. Опасные действия спрашивают подтверждение.",
};

function renderPairRoleHint() {
  const role = $("pair-role").value || "operator";
  const hint = $("pair-role-hint");
  const parts = [ROLE_DIFF[role] || ""];
  if (boot && boot.pair_code && boot.pair_role && boot.pair_role !== role) {
    parts.push("Сейчас действует код для роли «" + (ROLE_NAME[boot.pair_role] || boot.pair_role) + "». Новый код заменит его.");
  }
  hint.textContent = parts.join(" ");
  $("pair-role").title = ROLE_DIFF[role] || "Какие права получит человек, который введёт код";
}

function renderPair() {
  const box = $("pair-box");
  if (!boot) return;
  if (boot.pair_code) {
    const minutes = Math.ceil((boot.pair_seconds || 0) / 60);
    const role = ROLE_NAME[boot.pair_role] || boot.pair_role || "";
    box.textContent = boot.pair_code + " · " + role + " · ещё " + minutes + " мин";
  } else {
    box.textContent = "Код не выдан";
  }
  renderPairRoleHint();
}

function renderSettings() {
  renderPair();
  const box = $("users");
  box.replaceChildren();
  (boot.users || []).forEach((user) => {
    const row = el("div", "", "user");
    const title = el("div");
    title.append(el("div", user.name), el("small", String(user.id)));
    const role = document.createElement("select");
    ["owner", "operator", "viewer"].forEach((name) => {
      const option = el("option", name === "owner" ? "владелец" : name === "operator" ? "оператор" : "наблюдатель");
      option.value = name;
      role.append(option);
    });
    role.value = user.role;
    role.addEventListener("change", async () => {
      const res = await call("set_role", user.id, role.value);
      if (done(res, "Роль обновлена")) await refresh();
      else renderSettings();
    });
    const remove = el("button", "Удалить");
    remove.type = "button";
    remove.addEventListener("click", async () => {
      const res = await call("remove_user", user.id);
      if (done(res, "Аккаунт удалён")) {
        await refresh();
        renderSettings();
      }
    });
    row.append(title, role, remove);
    box.append(row);
  });
  $("autostart-btn").textContent = boot.autostart ? "Выключить автозагрузку" : "Включить автозагрузку";
}

async function refresh() {
  const res = await call("boot");
  if (!res.ok) {
    toast(res.error, true);
    return;
  }
  boot = res.data;
  if (!downloadsSet && boot.downloads) {
    $("wget-dir").value = boot.downloads;
    downloadsSet = true;
  }
  renderHeader();
  renderChecklist();
  if (current === "dash") renderDash();
  if (current === "settings") renderPair();
  if (current === "launch") renderLaunchers();
}

function renderLaunchers() {
  const box = $("launch-list");
  const rows = (boot && boot.launchers) || [];
  box.replaceChildren();
  if (!rows.length) {
    box.append(el("p", "Пока пусто. Введите имя и путь к программе.", "hint"));
    return;
  }
  rows.forEach((row) => {
    const card = el("article", "", "launch");
    card.append(el("strong", row.name));
    const code = el("code", row.command);
    code.title = row.command;
    card.append(code);
    const actions = el("div", "", "actions");
    const run = el("button", "Запустить");
    run.type = "button";
    run.title = "Запустить «" + row.name + "» на этом ПК";
    run.addEventListener("click", async () => {
      if (done(await call("launch_saved", row.id), "Запуск отправлен")) toast("Запуск отправлен");
    });
    const remove = el("button", "Удалить");
    remove.type = "button";
    remove.className = "danger";
    remove.title = "Убрать ярлык из списка. Файл программы не удаляется";
    remove.addEventListener("click", async () => {
      if (done(await call("remove_launcher", row.id), "Ярлык удалён")) await refresh();
    });
    actions.append(run, remove);
    card.append(actions);
    box.append(card);
  });
}

async function loadDir(path) {
  const res = await call("list_dir", path || "");
  if (!done(res)) return;
  dir = res.data;
  selected = "";
  $("dir-label").textContent = dir.path || "Диски";
  const drives = $("drive-row");
  drives.replaceChildren();
  (dir.drives || []).forEach((drive) => {
    const button = el("button", drive);
    button.type = "button";
    button.addEventListener("click", () => loadDir(drive));
    drives.append(button);
  });
  const list = $("file-list");
  list.replaceChildren();
  if (dir.truncated) list.append(el("p", "Показаны первые 400 имён.", "muted"));
  (dir.entries || []).forEach((entry) => {
    const row = el("div", "", "line");
    const label = el("div");
    label.append(el("div", entry.name), el("small", entry.dir ? "папка" : formatBytes(entry.size)));
    row.append(label);
    row.addEventListener("click", () => {
      selected = entry.path;
      document.querySelectorAll("#file-list .line").forEach((node) => node.classList.remove("selected"));
      row.classList.add("selected");
    });
    row.addEventListener("dblclick", () => {
      if (entry.dir) loadDir(entry.path);
    });
    list.append(row);
  });
}

async function loadProcs() {
  const res = await call("processes");
  if (!done(res)) return;
  procs = res.data || [];
  paintProcs();
}

function paintProcs() {
  const needle = $("proc-filter").value.trim().toLowerCase();
  const list = $("proc-list");
  list.replaceChildren();
  procs.filter((proc) => !needle || String(proc.name).toLowerCase().includes(needle) || String(proc.pid).includes(needle)).slice(0, 80).forEach((proc) => {
    const row = el("div", "", "line");
    const label = el("div");
    label.append(el("div", proc.name || "без имени"), el("small", proc.pid + " · " + formatBytes(proc.ram) + " · CPU " + proc.cpu));
    const button = el("button", "Завершить");
    button.type = "button";
    button.className = "danger";
    button.addEventListener("click", async () => {
      const res = await call("kill_process", proc.pid, confirmed());
      if (done(res, "Процесс завершён")) {
        disarm();
        loadProcs();
      }
    });
    row.append(label, button);
    list.append(row);
  });
}

async function loadServices() {
  const res = await call("services", $("svc-filter").value.trim());
  if (!done(res)) return;
  const list = $("svc-list");
  list.replaceChildren();
  (res.data || []).slice(0, 80).forEach((item) => {
    const row = el("div", "", "line");
    const label = el("div");
    label.append(el("div", item.display), el("small", item.name + " · " + item.status));
    const start = el("button", "Старт");
    start.type = "button";
    start.addEventListener("click", async () => {
      if (done(await call("service_start", item.name), "Команда отправлена")) loadServices();
    });
    const stop = el("button", "Стоп");
    stop.type = "button";
    stop.className = "danger";
    stop.addEventListener("click", async () => {
      const res = await call("service_stop", item.name, confirmed());
      if (done(res, "Остановка отправлена")) {
        disarm();
        loadServices();
      }
    });
    const buttons = el("div", "", "actions");
    buttons.append(start, stop);
    row.append(label, buttons);
    list.append(row);
  });
}

async function loadWindows() {
  const res = await call("windows");
  if (!done(res)) return;
  const list = $("win-list");
  list.replaceChildren();
  (res.data || []).forEach((item) => {
    const row = el("div", "", "line");
    row.append(el("div", item.title));
    const button = el("button", "Закрыть");
    button.type = "button";
    button.addEventListener("click", async () => {
      const res = await call("close_window", item.hwnd, confirmed());
      if (done(res, "Закрытие отправлено")) {
        disarm();
        loadWindows();
      }
    });
    row.append(button);
    list.append(row);
  });
}

async function loadVolume() {
  const res = await call("volume");
  if (!res.ok) {
    toast(res.error, true);
    return;
  }
  $("vol").value = res.data.level;
  $("vol-label").textContent = res.data.level + "%";
  muted = !!res.data.mute;
  $("mute-btn").textContent = muted ? "Включить звук" : "Без звука";
}

async function loadAudit() {
  const res = await call("audit");
  if (!done(res)) return;
  const list = $("audit-list");
  list.replaceChildren();
  (res.data || []).forEach((row) => {
    const line = el("div", "", "line");
    const label = el("div");
    label.append(el("div", (row.action || "") + " · " + (row.ok ? "ок" : "ошибка")), el("small", (row.ts || "") + " · " + (row.who || "") + " · " + (row.detail || "")));
    line.append(label);
    list.append(line);
  });
}

document.querySelectorAll("#nav button").forEach((button) => {
  button.addEventListener("click", () => show(button.dataset.page));
});

$("pause-btn").addEventListener("click", async () => {
  const res = await call("set_paused", !(boot && boot.paused));
  if (done(res, res.ok && res.data ? "Бот на паузе" : "Бот снова принимает команды")) await refresh();
});

$("shot-btn").addEventListener("click", async () => {
  const res = await call("screenshot");
  if (!done(res, "Скриншот готов")) return;
  $("shot").src = "data:image/jpeg;base64," + res.data;
});

$("up-btn").addEventListener("click", () => loadDir(dir.parent || ""));
$("drives-btn").addEventListener("click", () => loadDir(""));
$("mkdir-btn").addEventListener("click", async () => {
  const name = $("mkdir-name").value.trim();
  if (!dir.path || !name) { toast("Откройте папку и введите имя", true); return; }
  const res = await call("make_dir", dir.path + "\\" + name);
  if (done(res, "Папка создана")) loadDir(dir.path);
});
$("copy-btn").addEventListener("click", async () => {
  const res = await call("copy_in", dir.path, $("copy-source").value.trim());
  if (done(res, "Файл скопирован")) loadDir(dir.path);
});
$("pick-btn").addEventListener("click", async () => {
  const res = await call("pick_copy", dir.path);
  if (done(res, "Файлы скопированы")) loadDir(dir.path);
});
$("rename-btn").addEventListener("click", async () => {
  const res = await call("rename_path", selected, $("rename-name").value.trim());
  if (done(res, "Переименовано")) loadDir(dir.path);
});
$("delete-btn").addEventListener("click", async () => {
  const res = await call("delete_file", selected, confirmed());
  if (done(res, "Файл удалён")) { disarm(); loadDir(dir.path); }
});
$("open-file-btn").addEventListener("click", async () => {
  if (done(await call("open_target", selected), "Открыто")) toast("Открыто");
});
$("proc-filter").addEventListener("input", paintProcs);
$("proc-refresh").addEventListener("click", loadProcs);
$("run-btn").addEventListener("click", async () => {
  if (done(await call("start_program", $("run-cmd").value), "Запуск отправлен")) toast("Запуск отправлен");
});
$("launch-browse").addEventListener("click", async () => {
  const res = await call("pick_path");
  if (!done(res)) return;
  $("launch-cmd").value = res.data;
});
$("launch-add").addEventListener("click", async () => {
  const res = await call("add_launcher", $("launch-name").value, $("launch-cmd").value);
  if (!done(res, "Ярлык сохранён")) return;
  $("launch-name").value = "";
  $("launch-cmd").value = "";
  await refresh();
});
$("svc-refresh").addEventListener("click", loadServices);
$("win-refresh").addEventListener("click", loadWindows);
$("clip-get").addEventListener("click", async () => {
  const res = await call("clipboard_get");
  if (!done(res)) return;
  $("clip").value = res.data || "";
  toast("Буфер прочитан");
});
$("clip-set").addEventListener("click", async () => {
  if (done(await call("clipboard_set", $("clip").value), "Буфер записан")) toast("Буфер записан");
});
$("vol").addEventListener("change", async () => {
  const res = await call("volume_set", Number($("vol").value));
  if (!done(res)) return;
  $("vol-label").textContent = res.data.level + "%";
  muted = !!res.data.mute;
});
$("mute-btn").addEventListener("click", async () => {
  const res = await call("volume_mute", !muted);
  if (!done(res)) return;
  muted = !!res.data.mute;
  $("vol").value = res.data.level;
  $("vol-label").textContent = res.data.level + "%";
  $("mute-btn").textContent = muted ? "Включить звук" : "Без звука";
});
document.querySelectorAll("[data-power]").forEach((button) => {
  button.addEventListener("click", async () => {
    const mode = button.dataset.power;
    const res = await call("power", mode, mode === "abort" ? true : confirmed());
    if (done(res, res.data)) disarm();
  });
});
$("open-btn").addEventListener("click", async () => {
  if (done(await call("open_target", $("open-value").value), "Открыто")) toast("Открыто");
});
$("wget-btn").addEventListener("click", async () => {
  const res = await call("download_url", $("wget-url").value.trim(), $("wget-dir").value.trim());
  if (done(res)) toast(String(res.data));
});
$("shell-btn").addEventListener("click", async () => {
  const res = await call("shell", $("shell-cmd").value, confirmed());
  if (!res.ok) { toast(res.error, true); return; }
  $("shell-out").textContent = res.data || "";
  toast("Команда выполнена");
});
$("audit-refresh").addEventListener("click", loadAudit);
$("save-token").addEventListener("click", async () => {
  const res = await call("save_tokens", $("token").value, $("backup").value, $("clear-backup").checked);
  if (done(res, "Токены сохранены")) {
    $("token").value = "";
    $("backup").value = "";
    $("clear-backup").checked = false;
    await refresh();
  }
});
$("pair-role").addEventListener("change", renderPairRoleHint);
renderPairRoleHint();
$("issue-pair").addEventListener("click", async () => {
  const res = await call("issue_pair", $("pair-role").value);
  if (!done(res, "Код выдан")) return;
  await refresh();
  renderPair();
});
$("autostart-btn").addEventListener("click", async () => {
  const enable = !(boot && boot.autostart);
  const res = await call("set_autostart", enable);
  if (done(res, enable ? "Автозагрузка включена" : "Автозагрузка выключена")) {
    await refresh();
    renderSettings();
  }
});

window.addEventListener("pywebviewready", () => {
  refresh();
  setInterval(refresh, 3000);
});
