"use strict";

const $ = (id) => document.getElementById(id);
const form = $("job-form");
const modes = {
  grok: { title: "План Grok", button: "Получить план", label: "Что нужно разработать?", placeholder: "Например: продумай импорт CSV с проверкой ошибок" },
  devin: { title: "Devin", button: "Запустить Devin", label: "Что должен сделать Devin?", placeholder: "Опиши результат и критерии готовности" },
  image: { title: "Картинка", button: "Создать картинку", label: "Что должно быть на картинке?", placeholder: "Сюжет, стиль, освещение и композиция" },
  video: { title: "Видео", button: "Создать видео", label: "Что происходит в кадре?", placeholder: "Сцена, действие, движение камеры и стиль" },
};
const labels = { sending: "Отправляется", running: "В работе", paused: "На паузе", done: "Готово", ended: "Завершено", failed: "Ошибка" };
const defaults = { image: "grok-imagine-image-2.0", video: "grok-imagine-video-1.5" };
const state = { config: null, jobs: [], selected: null, source: null, busy: false, refreshing: false };
const project = { tree: null, selected: new Set(), context: null, version: 0 };

async function api(path, payload) {
  const init = payload === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Bridge-Request": "1" },
    body: JSON.stringify(payload),
  };
  const response = await fetch(path, init);
  const data = await response.json();
  if (!response.ok) {
    const detail = typeof data.detail === "string" ? data.detail : "Проверь поля запроса.";
    throw new Error(detail);
  }
  return data;
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function statusText(job) {
  return labels[job.status] + (job.status === "running" && job.progress != null ? " · " + job.progress + "%" : "");
}

function message(id, value) {
  const field = $(id);
  field.hidden = !value;
  field.textContent = value || "";
}

function setMode() {
  const mode = form.elements.mode.value;
  const metadata = modes[mode];
  const usesModel = mode === "image" || mode === "video";
  $("project-fields").hidden = usesModel;
  $("repo").disabled = $("git-ref").disabled = usesModel;
  $("repo-browser").hidden = mode !== "grok";
  $("devin-fields").hidden = mode !== "devin";
  $("max-acu").disabled = mode !== "devin";
  $("model-field").hidden = !usesModel;
  $("model").disabled = !usesModel;
  if (usesModel) {
    const select = $("model");
    const previous = select.value;
    const choices = state.config?.models[mode] || [];
    select.replaceChildren(...choices.map((name) => {
      const option = element("option", name);
      option.value = name;
      return option;
    }));
    select.value = choices.includes(previous) ? previous : defaults[mode];
  }
  if (mode !== "devin") state.source = null;
  $("handoff").hidden = !state.source;
  $("prompt-label").textContent = metadata.label;
  $("prompt").placeholder = metadata.placeholder;
  const service = mode === "devin" ? "devin" : "antig";
  const enabled = state.config?.[service];
  $("submit").textContent = state.busy ? "Отправка…" : metadata.button;
  $("submit").disabled = state.busy || !enabled;
  $("form-hint").textContent = !state.config ? "Загрузка…" : !enabled ? "Нет ключа API" :
    mode === "grok" ? "grok-4.7" : mode === "devin" ? "API v3" : "AntiG Imagine";
}

function resetContext() {
  project.context = null;
  $("share-context").checked = false;
  $("context-confirm").hidden = true;
  $("context-preview").textContent = "";
}

function resetProject() {
  project.version += 1;
  project.tree = null;
  project.selected.clear();
  resetContext();
  $("repo-tree").hidden = true;
  $("file-filter").value = "";
  $("file-list").replaceChildren();
  message("repo-error", "");
  $("repo-hint").textContent = "Необязательно. Выбранный код можно проверить перед отправкой.";
}

function displayFiles() {
  const query = $("file-filter").value.toLowerCase().trim();
  const shown = project.tree.paths.filter((path) => path.toLowerCase().includes(query));
  const rows = shown.map((path) => {
    const item = element("label");
    const checkbox = element("input");
    checkbox.type = "checkbox";
    checkbox.checked = project.selected.has(path);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked && project.selected.size >= 6) {
        checkbox.checked = false;
        message("repo-error", "Можно выбрать до шести файлов.");
        return;
      }
      if (checkbox.checked) project.selected.add(path);
      else project.selected.delete(path);
      project.version += 1;
      resetContext();
      message("repo-error", "");
      displayFileCount();
    });
    item.append(checkbox, element("span", path));
    return item;
  });
  $("file-list").replaceChildren(...(rows.length ? rows : [element("span", "Нет подходящих файлов.")]));
  displayFileCount();
}

function displayFileCount() {
  $("file-count").textContent = project.selected.size + " / 6";
  $("preview-files").disabled = !project.selected.size;
}

$("repo").addEventListener("input", resetProject);
$("git-ref").addEventListener("input", resetProject);
$("file-filter").addEventListener("input", displayFiles);
$("load-tree").addEventListener("click", async () => {
  const repo = $("repo").value.trim();
  const ref = $("git-ref").value.trim();
  resetProject();
  if (!repo || !$("repo").reportValidity()) {
    message("repo-error", "Укажи репозиторий в формате владелец/репозиторий.");
    return;
  }
  const version = project.version;
  $("load-tree").disabled = true;
  try {
    const tree = await api("/api/repo/tree", { repo, ref: ref || null });
    if (version !== project.version) return;
    project.tree = tree;
    $("git-ref").value = tree.ref;
    $("repo").value = tree.repo;
    $("repo-hint").textContent = (state.config?.demo ? "Демо: " : "") +
      tree.ref + " @ " + tree.commit.slice(0, 8) + (tree.truncated ? " · список неполный" : "");
    $("repo-tree").hidden = false;
    displayFiles();
  } catch (error) {
    if (version === project.version) message("repo-error", error.message);
  } finally {
    $("load-tree").disabled = false;
  }
});

$("preview-files").addEventListener("click", async () => {
  if (!project.tree || !project.selected.size) return;
  resetContext();
  message("repo-error", "");
  const version = project.version;
  const { repo, ref, commit } = project.tree;
  $("preview-files").disabled = true;
  try {
    const context = await api("/api/repo/preview", { repo, ref, commit, paths: [...project.selected] });
    if (version !== project.version) return;
    project.context = context;
    $("context-preview").textContent = context.files.map((file) => "=== " + file.path + " ===\n" + file.text).join("\n\n");
    $("context-confirm").hidden = false;
  } catch (error) {
    if (version === project.version) message("repo-error", error.message);
  } finally {
    displayFileCount();
  }
});

function showHistory() {
  $("history-count").textContent = String(state.jobs.length);
  const list = $("history-list");
  if (!state.jobs.length) {
    list.replaceChildren(element("p", "Здесь появятся запущенные задачи.", "history-empty"));
    return;
  }
  const oldRows = new Map(Array.from(list.children).map((row) => [row.dataset.id, row]));
  list.querySelector(".history-empty")?.remove();
  state.jobs.forEach((job, i) => {
    let button = oldRows.get(job.id);
    if (!button) {
      button = element("button", "", "job-row");
      button.type = "button";
      button.dataset.id = job.id;
      const summary = element("span");
      summary.append(element("span", "", "job-title"), element("span", "", "job-meta"));
      button.append(summary, element("span", "", "status"));
      button.addEventListener("click", () => void selectJob(job.id));
    }
    button.setAttribute("aria-current", String(state.selected === job.id));
    button.querySelector(".job-title").textContent = job.prompt;
    const at = new Date(job.created_at).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" });
    button.querySelector(".job-meta").textContent = modes[job.mode].title + " · " + at;
    const status = button.querySelector(".status");
    status.textContent = statusText(job);
    status.dataset.status = job.status;
    if (list.children[i] !== button) list.insertBefore(button, list.children[i] || null);
    oldRows.delete(job.id);
  });
  oldRows.forEach((row) => row.remove());
}

function link(title, url) {
  const node = element("a", title, "action-link");
  node.href = url;
  node.target = "_blank";
  node.rel = "noopener noreferrer";
  return node;
}

let rendered = "";
function showResult() {
  const job = state.jobs.find((item) => item.id === state.selected);
  const snapshot = JSON.stringify(job ?? null);
  if (rendered === snapshot) return;
  rendered = snapshot;
  const root = $("result");
  const status = $("result-status");
  status.hidden = !job;
  if (!job) {
    root.replaceChildren(element("p", "Выбери задачу в списке, чтобы открыть результат.", "muted"));
    return;
  }
  status.textContent = statusText(job);
  status.dataset.status = job.status;
  root.replaceChildren(element("h3", modes[job.mode].title + (job.model ? " · " + job.model : "")));
  if (job.repo) {
    root.append(element("p", [job.repo, job.ref, job.commit_sha?.slice(0, 8)].filter(Boolean).join(" · "), "mono muted"));
  }
  root.append(element("p", job.prompt, "muted"));
  if (job.error) root.append(element("p", job.error, "error"));
  if (job.result) root.append(element("pre", job.result));
  if (job.media_url) {
    const media = document.createElement(job.mode === "video" ? "video" : "img");
    media.src = job.media_url;
    if (job.mode === "video") {
      media.controls = true;
      media.preload = "none";
    } else {
      media.alt = "Изображение по запросу: " + job.prompt.slice(0, 120);
    }
    root.append(media);
    if (!state.config?.demo) root.append(element("p", "Ссылка может истечь — сохрани медиа после генерации.", "muted"));
  }
  const actions = element("div", "", "result-actions");
  if (job.mode === "grok" && job.status === "done") {
    const action = element("button", "Передать в Devin", "primary");
    action.type = "button";
    action.addEventListener("click", () => {
      state.source = job.id;
      resetProject();
      if (job.repo) {
        $("repo").value = job.repo;
        $("git-ref").value = job.ref || "";
      }
      form.querySelector('[value="devin"]').checked = true;
      $("prompt").value = job.prompt;
      setMode();
      $("prompt").focus();
    });
    actions.append(action);
  }
  if (job.session_url) actions.append(link("Открыть сессию", job.session_url));
  if (job.pr_url) actions.append(link("Открыть PR", job.pr_url));
  if (job.media_url) actions.append(link("Открыть медиа", job.media_url));
  if (actions.childElementCount) root.append(actions);
}

async function load({ refresh = false, paused = false } = {}) {
  if (state.refreshing) return;
  state.refreshing = true;
  try {
    let jobs = await api("/api/jobs");
    if (refresh) {
      const selected = state.selected;
      const pending = jobs.find((job) => job.id === selected &&
        job.remote_id && (job.status === "running" || (paused && job.status === "paused")));
      if (pending) {
        try {
          const current = await api("/api/jobs/" + pending.id + "/refresh", {});
          jobs = jobs.map((job) => job.id === current.id ? current : job);
          message("history-error", "");
        } catch (error) {
          message("history-error", error.message);
        }
      }
    }
    state.jobs = jobs;
    showHistory();
    showResult();
  } catch (error) {
    message("history-error", error.message);
  } finally {
    state.refreshing = false;
  }
}

async function selectJob(id) {
  state.selected = id;
  message("history-error", "");
  showHistory();
  showResult();
  await load({ refresh: true, paused: true });
}

form.querySelectorAll("[name=mode]").forEach((radio) => radio.addEventListener("change", () => {
  message("form-error", "");
  setMode();
}));
$("clear-source").addEventListener("click", () => {
  state.source = null;
  setMode();
  $("prompt").focus();
});
$("reload").addEventListener("click", () => {
  message("history-error", "");
  void load({ refresh: true, paused: true });
});
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.busy || $("submit").disabled) return;
  const mode = form.elements.mode.value;
  const payload = { mode, prompt: $("prompt").value.trim() };
  if (!payload.prompt) {
    message("form-error", "Введи задачу.");
    return;
  }
  if (mode === "grok" || mode === "devin") {
    const repo = $("repo").value.trim();
    const ref = $("git-ref").value.trim();
    if (repo) payload.repo = repo;
    if (ref) payload.ref = ref;
    if (mode === "grok" && repo) {
      if (!project.context || !$("share-context").checked) {
        message("form-error", "Выбери файлы, проверь код и подтверди отправку в AntiG.");
        return;
      }
      payload.context = project.context;
    }
  }
  if (mode === "devin") {
    payload.max_acu = Number($("max-acu").value);
    if (state.source) payload.source_id = state.source;
  } else if (mode !== "grok") {
    payload.model = $("model").value;
  }
  state.busy = true;
  message("form-error", "");
  setMode();
  try {
    const job = await api("/api/jobs", payload);
    state.jobs.unshift(job);
    state.selected = job.id;
    if (mode === "devin") state.source = null;
    showHistory();
    showResult();
    await load();
  } catch (error) {
    message("form-error", error.message);
  } finally {
    state.busy = false;
    setMode();
  }
});

async function init() {
  try {
    state.config = await api("/api/config");
    const cfg = state.config;
    $("environment").textContent = cfg.demo ? "Демо · без внешних запросов" :
      "AntiG: " + (cfg.antig ? "ключ задан" : "нет ключа") + " · Devin: " + (cfg.devin ? "ключ задан" : "нет ключа");
    setMode();
    await load();
    if (state.jobs.length) await selectJob(state.jobs[0].id);
    window.setInterval(() => {
      if (!document.hidden) void load({ refresh: true });
    }, 4500);
  } catch (error) {
    $("environment").textContent = "Нет связи с панелью";
    message("form-error", error.message);
  }
}

setMode();
void init();
