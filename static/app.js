"use strict";

const $ = (id) => document.getElementById(id);
const form = $("job-form");
const routes = {
  auto: "Grok распределяет", answer: "Ответ Grok", clarify: "Уточнение Grok",
  inspect: "Разбор Devin", implement: "Реализация Devin", grok: "План Grok", devin: "Devin",
  image: "Изображение AntiG", video: "Видео AntiG",
};
const labels = {
  sending: "Запуск", awaiting: "Нужно подтверждение", running: "В работе",
  paused: "На паузе", reviewing: "Проверка отчёта", done: "Готово", ended: "Завершено", failed: "Ошибка",
};
const devinDetails = {
  waiting_for_user: "Ждёт твоего ответа в сессии",
  waiting_for_approval: "Ждёт разрешения в сессии",
  working: "Работает в репозитории",
  usage_limit_exceeded: "Достигнут лимит ACU",
  out_of_credits: "Закончились кредиты",
  out_of_quota: "Квота исчерпана",
  inactivity: "Приостановлен из-за бездействия",
};
const state = { config: null, jobs: [], selected: null, detail: null, busy: false, loading: 0 };
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

function updateForm() {
  const hasRepo = Boolean($("repo").value.trim());
  $("devin-fields").hidden = !hasRepo;
  $("devin-fields").disabled = !hasRepo;
  $("submit").textContent = state.busy ? "Отправка…" : "Запустить задачу";
  $("submit").disabled = state.busy || !state.config?.antig;
  $("form-hint").textContent = !state.config ? "Подключение…" : !state.config.antig ? "Нет ключа AntiG" :
    hasRepo && !state.config.devin ? "Ключ Devin не настроен" : "Исполнитель выбирается автоматически";
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
  $("repo-hint").textContent = "Необязательно. Grok увидит только файлы, которые разрешишь отправить.";
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

$("repo").addEventListener("input", () => {
  resetProject();
  updateForm();
});
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
    button.querySelector(".job-meta").textContent = routeTitle(job) + " · " + at;
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

function taskRoute(job) {
  return job.route || job.mode;
}

function routeTitle(job) {
  return routes[taskRoute(job)] || "Задача";
}

function currentStage(job) {
  const route = taskRoute(job);
  if (job.status === "reviewing") return ["Grok", "Разбирает отчёт Devin"];
  if (["done", "ended", "failed"].includes(job.status)) {
    return [job.reviewed_at ? "Grok" : routeTitle(job), labels[job.status]];
  }
  if (route === "auto" || route === "grok") return ["Grok", "Распределяет задачу"];
  if (["image", "video"].includes(route)) {
    return ["AntiG Imagine", job.status === "awaiting" ? "Ждёт подтверждения генерации" : "Генерирует медиа"];
  }
  return ["Devin", devinDetails[job.activity?.detail] ||
    (job.status === "sending" ? "Запускает сессию" : job.status === "paused" ? "Сессия на паузе" : "Сессия запущена")];
}

function logEntry(list, actor, text, timestamp, isMessage = false) {
  const row = element("li");
  const date = new Date(timestamp);
  const at = timestamp && !Number.isNaN(date.valueOf()) ?
    " · " + date.toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" }) : "";
  row.append(element("div", actor + at, "mono muted"));
  row.append(element(isMessage ? "pre" : "p", text));
  list.append(row);
}

function showActivity(root, job) {
  const activity = job.activity || {};
  const messages = activity.messages || [];
  const isDevin = ["implement", "inspect", "devin"].includes(taskRoute(job));
  const latest = messages.at(-1);
  if (latest) {
    root.append(element("h3", "Последнее сообщение Devin"), element("pre", latest.text));
  }
  if (activity.pr) {
    const pr = activity.pr;
    root.append(element("h3", "Файлы в GitHub PR"));
    root.append(element("p", [pr.head, pr.base].filter(Boolean).join(" → "), "mono"));
    const paths = element("ul", "", "changed-files mono");
    for (const path of pr.files) paths.append(element("li", path));
    root.append(paths);
    if (pr.total > pr.files.length) root.append(element("p", "Показано " + pr.files.length + " из " + pr.total + " файлов.", "muted"));
  }
  if (isDevin && (job.session_url || state.config?.demo)) {
    root.append(element("p", "Команды и незакоммиченные файлы видны в самой сессии Devin.", "muted"));
  }
  for (const warning of [...(activity.warnings || []), activity.pr_error].filter(Boolean)) {
    root.append(element("p", warning, "muted"));
  }
  if (activity.report?.summary) {
    root.append(element("h3", "Отчёт Devin"));
    const names = { summary: "Итог", checks: "Проверки", remaining: "Осталось" };
    root.append(element("pre", Object.entries(names).filter(([key]) => activity.report[key])
      .map(([key, title]) => title + ": " + activity.report[key]).join("\n\n")));
  }
  if (job.mode !== "auto") return;
  const journal = element("details", "", "job-journal");
  journal.id = "job-journal";
  journal.append(element("summary", "Журнал работы"));
  const list = element("ol", "", "activity-list");
  if (job.reviewed_at) logEntry(list, "Grok", "Разобрал отчёт Devin.", job.reviewed_at);
  if (job.finished_at) logEntry(list, isDevin ? "Devin" : routeTitle(job), "Работа завершилась.", job.finished_at);
  for (const entry of [...messages].reverse()) {
    logEntry(list, "Devin" + (state.config?.demo ? " (демо)" : ""), entry.text,
      entry.at < 1e12 ? entry.at * 1000 : entry.at, true);
  }
  if (job.reason) logEntry(list, "Grok", routeTitle(job) + ": " + job.reason, job.routed_at);
  logEntry(list, "Панель", "Задача принята.", job.created_at);
  journal.append(list);
  root.append(journal);
}

function confirmMedia(root, job) {
  const button = element("button", "Подтвердить генерацию", "primary");
  button.type = "button";
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await api("/api/jobs/" + job.id + "/generate", {});
      await load();
    } catch (error) {
      message("history-error", error.message);
      button.disabled = false;
    }
  });
  root.append(element("p", "Модель: " + job.model + ". В реальном режиме генерация расходует квоту.", "muted"), button);
}

let rendered = "";
let renderedId = null;
function showResult() {
  const job = state.detail?.id === state.selected ? state.detail : state.jobs.find((item) => item.id === state.selected);
  const snapshot = JSON.stringify(job ?? null);
  if (rendered === snapshot) return;
  rendered = snapshot;
  const root = $("result");
  const expanded = renderedId === job?.id ? new Set([...root.querySelectorAll("details[open]")].map((node) => node.id)) : new Set();
  renderedId = job?.id;
  const status = $("result-status");
  status.hidden = !job;
  if (!job) {
    root.replaceChildren(element("p", "Выбери задачу в списке, чтобы увидеть ход работы.", "muted"));
    return;
  }
  const route = taskRoute(job);
  status.textContent = statusText(job);
  status.dataset.status = job.status;
  const [actor, stage] = currentStage(job);
  const stageNode = element("div", "", "current-stage");
  stageNode.setAttribute("aria-live", "polite");
  stageNode.append(element("strong", actor), element("span", stage));
  root.replaceChildren(stageNode);
  if (job.repo) root.append(element("p", [job.repo, job.ref || "база по умолчанию", job.commit_sha?.slice(0, 8)]
    .filter(Boolean).join(" · "), "mono muted"));
  root.append(element("p", job.prompt, "muted"));
  if (job.reason) root.append(element("p", job.reason));
  if (job.error) root.append(element("p", job.error, "error"));
  const actions = element("div", "", "result-actions");
  if (job.repo) {
    const workspace = element("a", "Открыть редактор", "action-link workspace-link");
    workspace.href = "/workspace/" + job.id;
    actions.append(workspace);
  }
  if (job.session_url) actions.append(link("Открыть сессию Devin", job.session_url));
  if (job.pr_url) actions.append(link("Открыть PR", job.pr_url));
  if (job.media_url) actions.append(link("Открыть медиа", job.media_url));
  if (actions.childElementCount) root.append(actions);
  if (job.plan) {
    const plan = element("details", "", "job-journal");
    plan.id = "job-plan";
    plan.append(element("summary", "Что Grok поручил Devin"), element("pre", job.plan));
    root.append(plan);
  }
  if (job.status === "awaiting") confirmMedia(root, job);
  showActivity(root, job);
  if (job.result) {
    if (job.reviewed_at) root.append(element("h3", "Разбор отчёта · Grok"));
    root.append(element("pre", job.result));
  }
  if (job.media_url) {
    const media = document.createElement(route === "video" ? "video" : "img");
    media.src = job.media_url;
    if (route === "video") {
      media.controls = true;
      media.preload = "none";
    } else {
      media.alt = "Изображение по запросу: " + job.prompt.slice(0, 120);
    }
    root.append(media);
    if (!state.config?.demo) root.append(element("p", "Ссылка может истечь — сохрани медиа после генерации.", "muted"));
  }
  for (const node of root.querySelectorAll("details")) node.open = expanded.has(node.id);
}

async function load() {
  const version = ++state.loading;
  const selected = state.selected;
  try {
    const [jobs, detail] = await Promise.all([
      api("/api/jobs"),
      selected ? api("/api/jobs/" + selected) : null,
    ]);
    if (version !== state.loading) return;
    state.jobs = jobs;
    state.detail = detail;
    message("history-error", "");
    showHistory();
    showResult();
  } catch (error) {
    if (version === state.loading) message("history-error", error.message);
  }
}

async function selectJob(id) {
  state.selected = id;
  showHistory();
  showResult();
  await load();
}

$("reload").addEventListener("click", () => void load());
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.busy || $("submit").disabled) return;
  const payload = { prompt: $("prompt").value.trim() };
  if (!payload.prompt) {
    message("form-error", "Введи задачу.");
    return;
  }
  const repo = $("repo").value.trim();
  const ref = $("git-ref").value.trim();
  if (repo) {
    payload.repo = repo;
    payload.max_acu = Number($("max-acu").value);
    payload.draft_pr = $("draft-pr").checked;
    payload.review = $("review").checked;
  }
  if (ref) payload.ref = ref;
  if (project.context && $("share-context").checked) {
    payload.context = { commit: project.context.commit, paths: project.context.files.map((file) => file.path) };
  }
  state.busy = true;
  message("form-error", "");
  updateForm();
  try {
    const job = await api("/api/jobs", payload);
    state.jobs.unshift(job);
    state.detail = job;
    await selectJob(job.id);
  } catch (error) {
    message("form-error", error.message);
  } finally {
    state.busy = false;
    updateForm();
  }
});

async function init() {
  try {
    state.config = await api("/api/config");
    const cfg = state.config;
    $("environment").textContent = cfg.demo ? "Демо · без внешних запросов" :
      "AntiG: " + (cfg.antig ? "ключ задан" : "нет ключа") + " · Devin: " + (cfg.devin ? "ключ задан" : "нет ключа");
    updateForm();
    await load();
    if (state.jobs.length) await selectJob(state.jobs[0].id);
    window.setInterval(() => {
      if (!document.hidden) void load();
    }, 4000);
  } catch (error) {
    $("environment").textContent = "Нет связи с панелью";
    message("form-error", error.message);
  }
}

updateForm();
void init();
