"use strict";

const $ = (id) => document.getElementById(id);
const id = window.location.pathname.startsWith("/workspace/") ? window.location.pathname.split("/").filter(Boolean).at(-1) : null;
const endpoint = "/api/jobs/" + id;
const state = { job: null, tree: null, tabs: new Map(), active: null, expanded: new Set(), request: 0 };
const statuses = {
  sending: "Запуск", awaiting: "Ждёт подтверждения", running: "В работе", paused: "На паузе",
  reviewing: "Разбор Grok", done: "Готово", ended: "Завершено", failed: "Ошибка",
};
const details = {
  waiting_for_user: "Ждёт ответа", waiting_for_approval: "Ждёт разрешения", usage_limit_exceeded: "Лимит ACU",
};
const languages = {
  py: ["Python", "python"], js: ["JavaScript", "js"], mjs: ["JavaScript", "js"], cjs: ["JavaScript", "js"],
  ts: ["TypeScript", "js"], tsx: ["TypeScript JSX", "js"], jsx: ["JSX", "js"],
  html: ["HTML", "web"], xml: ["XML", "web"], svg: ["SVG", "web"], css: ["CSS", "web"],
  json: ["JSON", "data"], yml: ["YAML", "data"], yaml: ["YAML", "data"], toml: ["TOML", "data"],
  md: ["Markdown", "markdown"], sh: ["Shell", "shell"], ps1: ["PowerShell", "shell"],
};
const scanners = {
  python: /(?<comment>#[^\n]*)|(?<string>"""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')|(?<keyword>\b(?:async|await|class|def|from|import|return|raise|yield|if|elif|else|except|finally|for|in|is|not|and|or|try|def|with|as|while|pass|None|False|True)\b)|(?<number>\b\d+(?:\.\d+)?\b)/g,
  js: /(?<comment>\/\/[^\n]*|\/\*[\s\S]*?\*\/)|(?<string>"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|\x60(?:\\.|[^\x60\\])*\x60)|(?<keyword>\b(?:async|await|class|const|let|var|function|export|import|from|return|new|if|else|for|of|while|try|catch|throw|extends|void|typeof|false|true|null|undefined|type|interface)\b)|(?<number>\b\d+(?:\.\d+)?\b)/g,
  web: /(?<comment><!--[\s\S]*?-->|\/\*[\s\S]*?\*\/)|(?<string>"[^"\n]*"|'[^'\n]*')|(?<tag><\/?[\w:-]+|\b[\w-]+(?=:))|(?<number>\b\d+(?:\.\d+)?(?:px|em|rem|%)?)/g,
  data: /(?<comment>#[^\n]*)|(?<tag>"(?:\\.|[^"\\\n])*"(?=\s*:)|\b[\w-]+(?=:))|(?<string>"(?:\\.|[^"\\\n])*"|'[^'\n]*')|(?<keyword>\b(?:true|false|null)\b)|(?<number>\b\d+(?:\.\d+)?\b)/g,
  markdown: /(?<tag>^#{1,6} [^\n]+)|(?<string>\x60[^\x60\n]+\x60)|(?<keyword>\*\*[^*\n]+\*\*)/gm,
  shell: /(?<comment>#[^\n]*)|(?<string>"(?:\\.|[^"\\\n])*"|'[^'\n]*')|(?<tag>\$[\w{}:]+)|(?<keyword>\b(?:if|else|for|in|then|fi|do|done|function|export)\b)/g,
};

function make(tag, text = "", cls = "") {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}

function icon(name, cls = "") {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  const use = document.createElementNS(ns, "use");
  svg.setAttribute("aria-hidden", "true");
  if (cls) svg.setAttribute("class", cls);
  use.setAttribute("href", "/static/workspace-icons.svg#" + name);
  svg.append(use);
  return svg;
}

async function api(path) {
  const response = await fetch(path, { cache: "no-store" });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Не удалось получить данные.");
  return data;
}

const basename = (path) => path.split("/").at(-1);
const language = (path) => languages[basename(path).toLowerCase().split(".").at(-1)] || ["Текст", "plain"];
function fileIcon(path) {
  const node = icon("file", "file-icon");
  node.dataset.language = language(path)[1];
  return node;
}

function notice(text, error = false) {
  const item = $("tree-notice");
  item.textContent = text;
  item.dataset.error = String(error);
  item.hidden = !text;
}

function renderTree() {
  const tree = $("tree");
  if (!state.tree) {
    tree.replaceChildren();
    return;
  }
  const root = { dirs: new Map(), files: [] };
  const query = $("file-search").value.trim().toLowerCase();
  let count = 0;
  for (const entry of state.tree.files) {
    if (!entry.path.toLowerCase().includes(query)) continue;
    count++;
    const parts = entry.path.split("/");
    let parent = root;
    for (const part of parts.slice(0, -1)) {
      if (!parent.dirs.has(part)) parent.dirs.set(part, { dirs: new Map(), files: [] });
      parent = parent.dirs.get(part);
    }
    parent.files.push(entry);
  }
  const changed = new Set($("revision").value === "pr" ? state.job.activity?.pr?.files || [] : []);
  function listFor(node, prefix = "") {
    const list = make("ul", "", "file-list");
    for (const [name, branch] of [...node.dirs].sort(([a], [b]) => a.localeCompare(b))) {
      const li = make("li");
      const folder = make("details", "", "folder");
      const label = make("summary", "", "tree-row");
      const path = prefix + name + "/";
      label.append(icon("chevron", "chevron"), icon("folder", "folder-icon"), make("span", name, "filename"));
      folder.append(label);
      folder.open = Boolean(query) || state.expanded.has(path);
      if (folder.open) folder.append(listFor(branch, path));
      folder.addEventListener("toggle", () => {
        if (!query) {
          if (folder.open) state.expanded.add(path);
          else state.expanded.delete(path);
        }
        if (folder.open && folder.childElementCount === 1) folder.append(listFor(branch, path));
      });
      li.append(folder);
      list.append(li);
    }
    for (const entry of [...node.files].sort((a, b) => basename(a.path).localeCompare(basename(b.path)))) {
      const li = make("li");
      const button = make("button", "", "tree-row file");
      button.type = "button";
      button.dataset.path = entry.path;
      button.title = entry.stamp ? entry.path : "Файл превышает 128 КиБ";
      button.disabled = !entry.stamp;
      button.setAttribute("aria-current", String(entry.path === state.active));
      button.append(fileIcon(entry.path), make("span", basename(entry.path), "filename"));
      if (changed.has(entry.path)) {
        const mark = make("span", "M", "modified");
        mark.title = "Изменено в PR";
        button.append(mark);
      }
      button.addEventListener("click", () => openFile(entry, true));
      li.append(button);
      list.append(li);
    }
    return list;
  }
  tree.replaceChildren(listFor(root));
  const hint = state.tree.truncated ? "GitHub вернул неполное дерево." : "";
  notice(count ? hint : (query ? "Подходящих файлов нет." : "Нет файлов для просмотра."));
  $("footer-count").textContent = count + (query ? " / " + state.tree.files.length : "") + " файлов";
}

function showCode(text, kind) {
  const source = $("source");
  const parts = document.createDocumentFragment();
  let end = 0;
  let count = 0;
  if (scanners[kind]) {
    for (const match of text.slice(0, 48_000).matchAll(scanners[kind])) {
      if (++count > 5000) break;
      parts.append(text.slice(end, match.index));
      const group = Object.keys(match.groups).find((key) => match.groups[key] !== undefined);
      parts.append(make("span", match[0], "syntax-" + group));
      end = match.index + match[0].length;
    }
  }
  parts.append(text.slice(end));
  source.replaceChildren(parts);
  const lines = text.split("\n").length;
  $("line-numbers").textContent = Array.from({ length: lines }, (_, i) => i + 1).join("\n");
}

function renderEditor() {
  const tab = state.tabs.get(state.active);
  const chosen = tab && typeof tab.text === "string";
  $("editor").hidden = !chosen;
  $("editor-empty").hidden = Boolean(chosen);
  $("copy").disabled = !chosen;
  $("footer-language").textContent = tab ? language(tab.file.path)[0] : "";
  const crumbs = $("breadcrumbs");
  crumbs.replaceChildren();
  if (!tab) {
    crumbs.append(make("span", "Файл не выбран", "dim"));
    $("editor-title").textContent = "Рабочая область";
    $("editor-hint").textContent = "Выбери файл в проводнике.";
    return;
  }
  const parts = tab.file.path.split("/");
  parts.forEach((part, i) => {
    if (i) crumbs.append(icon("chevron"));
    crumbs.append(make("span", part, i === parts.length - 1 ? "filename" : "folder-name"));
  });
  if (chosen) {
    showCode(tab.text, language(tab.file.path)[1]);
    $("editor-scroll").scrollTo(tab.left || 0, tab.top || 0);
  } else {
    $("editor-title").textContent = tab.error ? "Файл не открылся" : "Загрузка файла…";
    $("editor-hint").textContent = tab.error || tab.file.path;
  }
}

function renderTabs() {
  const strip = $("tabs");
  strip.replaceChildren();
  for (const [path] of state.tabs) {
    const tab = make("div", "", "tab");
    tab.dataset.active = String(path === state.active);
    const show = make("button", "", "tab-action");
    show.type = "button";
    show.title = path;
    show.setAttribute("aria-current", String(path === state.active));
    show.append(fileIcon(path), make("span", basename(path), "tab-name"));
    show.addEventListener("click", () => {
      void openFile(state.tabs.get(path).file);
      strip.querySelector("[aria-current=true]")?.focus();
    });
    const close = make("button", "", "tab-close icon-button");
    close.type = "button";
    close.title = "Закрыть " + basename(path);
    close.setAttribute("aria-label", close.title);
    close.append(icon("close"));
    close.addEventListener("click", () => {
      const paths = [...state.tabs.keys()];
      state.tabs.delete(path);
      if (state.active === path) activate(paths[paths.indexOf(path) + 1] || [...state.tabs.keys()].at(-1) || null);
      else renderTabs();
      ($("tabs").querySelector("[aria-current=true]") || $("file-search")).focus();
    });
    tab.append(show, close);
    strip.append(tab);
  }
}

function activate(path) {
  const editor = $("editor-scroll");
  const old = state.tabs.get(state.active);
  if (old && !$("editor").hidden) {
    old.left = editor.scrollLeft;
    old.top = editor.scrollTop;
  }
  state.active = path;
  for (const file of $("tree").querySelectorAll("[data-path]")) {
    file.setAttribute("aria-current", String(file.dataset.path === path));
  }
  renderTabs();
  renderEditor();
}

async function openFile(file, fromTree = false) {
  if (!state.tree || !file.stamp) return;
  const path = file.path;
  if (!state.tabs.has(path)) state.tabs.set(path, { file });
  const tab = state.tabs.get(path);
  activate(path);
  if (fromTree && window.matchMedia("(max-width: 670px)").matches) {
    setExplorer(false);
    $("editor-scroll").focus();
  }
  if (typeof tab.text === "string" || tab.loading) return;
  tab.loading = true;
  tab.error = "";
  renderEditor();
  try {
    const query = new URLSearchParams({
      repo: state.tree.repo, commit: state.tree.commit, path, blob: file.blob, stamp: file.stamp,
    });
    const data = await api(endpoint + "/workspace/file?" + query);
    tab.text = data.text;
  } catch (error) {
    tab.error = error.message;
  } finally {
    tab.loading = false;
    if (state.active === path && state.tabs.get(path) === tab) renderEditor();
  }
}

async function loadTree() {
  const request = ++state.request;
  const revision = $("revision").value;
  const previous = state.tree;
  if (!state.job?.repo) return;
  notice("Загружаю снимок GitHub…");
  $("reload-tree").disabled = true;
  try {
    const tree = await api(endpoint + "/workspace/tree?revision=" + revision);
    if (request !== state.request) return;
    state.tree = { ...tree, revision };
    $("tree-ref").textContent = tree.ref + " · " + tree.commit.slice(0, 8);
    $("tree-ref").title = tree.ref + " · " + tree.commit;
    $("footer-revision").replaceChildren(icon("branch"), make("span", tree.ref + "@" + tree.commit.slice(0, 8)));
    $("footer-revision").title = tree.commit;
    $("file-search").disabled = false;
    const changed = tree.commit !== previous?.commit || tree.repo !== previous?.repo;
    const snapshot = new Map(tree.files.map((entry) => [entry.path, entry]));
    if (changed) {
      const last = state.active;
      state.tabs.clear();
      activate(null);
      const requested = previous ? null : new URLSearchParams(window.location.search).get("file");
      const next = snapshot.get(last || requested) || tree.files.find((entry) => /^readme\.(md|txt)$/i.test(entry.path));
      if (next?.stamp) void openFile(next);
      if (!previous && tree.files.length < 40) {
        for (const entry of tree.files) if (entry.path.includes("/")) state.expanded.add(entry.path.split("/")[0] + "/");
      }
    } else {
      for (const tab of state.tabs.values()) tab.file = snapshot.get(tab.file.path) || tab.file;
    }
    renderTree();
  } catch (error) {
    if (request !== state.request) return;
    notice(error.message, true);
    $("revision").value = state.tree?.revision || "base";
    if (!state.tree) {
      $("editor-title").textContent = "Снимок недоступен";
      $("editor-hint").textContent = "Проверь доступ к GitHub и обнови проводник.";
    }
  } finally {
    if (request === state.request) {
      $("reload-tree").disabled = false;
    }
  }
}

function updateLink(id, url) {
  const link = $(id);
  link.hidden = !url;
  if (url) link.href = url;
  else link.removeAttribute("href");
}

let renderedLog = "";
function renderJob(job) {
  $("top-repo").textContent = job.repo || "Без репозитория";
  $("repo-name").textContent = job.repo || "Нет GitHub-репозитория";
  updateLink("session-link", job.session_url);
  updateLink("pr-link", job.pr_url);
  $("revision").disabled = !job.repo;
  $("revision").options[1].disabled = !job.pr_url;
  const visibleStatus = details[job.activity?.detail] || statuses[job.status] || "Загрузка";
  if ($("job-state").dataset.state !== job.status || $("job-state").textContent !== visibleStatus) {
    $("job-state").dataset.state = job.status;
    $("job-state").replaceChildren(make("span", "", "state-dot"), make("span", visibleStatus));
  }
  const events = [];
  if (job.skills?.length) events.push({ actor: "Панель", at: Date.parse(job.created_at),
    text: "Для задачи: " + job.skills.map((skill) => skill.title).join(" · ") });
  if (job.reason) events.push({ actor: "Grok", text: job.reason, at: Date.parse(job.routed_at) });
  for (const message of job.activity?.messages || []) {
    events.push({ actor: "Devin", text: message.text, at: message.at < 1e12 ? message.at * 1000 : message.at });
  }
  if (job.activity?.report?.summary) events.push({ actor: "Devin", at: Date.parse(job.finished_at), text:
    [job.activity.report.summary, job.activity.report.checks].filter(Boolean).join("\n") });
  if (job.result && job.reviewed_at) events.push({ actor: "Grok", text: job.result, at: Date.parse(job.reviewed_at) });
  if (job.error) events.push({ actor: "Панель", text: job.error });
  if (!events.length) events.push({ actor: "Панель", text: "Здесь появятся сообщения Grok и Devin." });
  const snapshot = JSON.stringify(events);
  if (renderedLog === snapshot) return;
  renderedLog = snapshot;
  const scroll = $("log-scroll");
  const follows = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 25;
  $("log").replaceChildren(...events.map(({ actor, text, at }) => {
    const row = make("li", "", "log-entry");
    row.dataset.actor = actor;
    const date = at ? new Date(at).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }) : "—";
    row.append(make("time", date), make("strong", actor, "log-actor"), make("pre", text, "log-text"));
    return row;
  }));
  if (follows) scroll.scrollTop = scroll.scrollHeight;
}

function setExplorer(visible) {
  document.body.dataset.explorer = String(visible);
  $("toggle-explorer").setAttribute("aria-pressed", String(visible));
}

function setJournal(visible) {
  $("journal").hidden = !visible;
  $("toggle-journal").setAttribute("aria-pressed", String(visible));
}

function focusSearch() {
  setMarket(false);
  setExplorer(true);
  $("file-search").focus();
}

function setMarket(visible) {
  document.body.dataset.market = String(visible);
  $("skills-market").hidden = !visible;
  document.querySelector(".market-footer").hidden = !visible;
  $("toggle-skills").setAttribute("aria-pressed", String(visible));
  $("toggle-explorer").setAttribute("aria-pressed", String(!visible && document.body.dataset.explorer === "true"));
  if (visible) window.BridgeMarketplace.open(() => state.job);
}

$("toggle-skills").addEventListener("click", () => {
  if (id) setMarket($("skills-market").hidden);
});
$("toggle-explorer").addEventListener("click", () => {
  const visible = !$("skills-market").hidden;
  setMarket(false);
  setExplorer(visible || document.body.dataset.explorer !== "true");
});
$("focus-search").addEventListener("click", focusSearch);
$("toggle-journal").addEventListener("click", () => {
  const visible = !$("skills-market").hidden;
  setMarket(false);
  setJournal(visible || $("journal").hidden);
});
$("close-journal").addEventListener("click", () => {
  setJournal(false);
  $("toggle-journal").focus();
});
$("revision").addEventListener("change", () => void loadTree());
$("reload-tree").addEventListener("click", () => void loadTree());
$("file-search").addEventListener("input", renderTree);
$("file-search").addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  $("tree").querySelector("button[data-path]:not(:disabled)")?.click();
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "/" || event.ctrlKey || event.metaKey || event.altKey ||
      event.target.closest("input, textarea, select, [contenteditable]") || $("file-search").disabled) return;
  event.preventDefault();
  focusSearch();
});
$("copy").addEventListener("click", async () => {
  const text = state.tabs.get(state.active)?.text;
  if (typeof text !== "string") return;
  try {
    await navigator.clipboard.writeText(text);
    $("copy-note").textContent = "Код скопирован.";
  } catch {
    $("copy-note").textContent = "Скопировать не получилось. Можно выделить код вручную.";
  }
});

async function init() {
  try {
    state.job = await api(endpoint);
    renderJob(state.job);
    if (state.job.repo) void loadTree();
    else {
      $("reload-tree").disabled = true;
      notice("У задачи нет выбранного GitHub-репозитория.");
    }
  } catch (error) {
    notice(error.message, true);
    return;
  }
  let refreshing = false;
  window.setInterval(async () => {
    if (document.hidden || refreshing) return;
    refreshing = true;
    try {
      state.job = await api(endpoint);
      renderJob(state.job);
    } catch {
      $("job-state").replaceChildren(make("span", "", "state-dot"), make("span", "Связь потеряна"));
    } finally {
      refreshing = false;
    }
  }, 4000);
}

if (!id) {
  document.body.dataset.marketOnly = "true";
  $("top-repo").textContent = "Маркетплейс";
  document.title = "Скиллы · Мост";
}
if (!id || new URLSearchParams(location.search).get("panel") === "skills") setMarket(true);
if (id) void init();
