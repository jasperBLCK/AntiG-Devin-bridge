(() => {
  "use strict";

  const byId = (id) => document.getElementById(id);
  const input = byId("market-query");
  const dialog = byId("skill-dialog");
  const install = byId("skill-install");
  const reviewForm = byId("skill-review-form");
  const reviewSubmit = byId("skill-review-submit");
  const number = new Intl.NumberFormat("ru-RU");
  const tabLabels = [...document.querySelectorAll("[data-market-tab]")];
  let currentTab = "catalog";
  let results = [];
  let openSkill = null;
  let request = 0;
  let timer;
  let searchAbort;
  let detailAbort;
  let job;
  let saving = false;

  async function api(path, { payload, signal } = {}) {
    const response = await fetch("/api/skills" + path, {
      signal,
      ...(payload ? {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Bridge-Request": "1" },
        body: JSON.stringify(payload),
      } : {}),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Проверь данные и повтори.");
    return data;
  }

  function node(tag, content = "", css = "") {
    const element = document.createElement(tag);
    element.textContent = content;
    if (css) element.className = css;
    return element;
  }

  function ratings(item) {
    return item.votes ? "★ " + number.format(item.rating) + " / 5 · оценок: " + number.format(item.votes) : "Пока без оценок";
  }

  function renderResults() {
    const sort = byId("market-sort").value;
    const items = [...results];
    if (sort === "installs") items.sort((a, b) => (b.installs ?? -1) - (a.installs ?? -1));
    if (sort === "rating") items.sort((a, b) => (b.rating ?? -1) - (a.rating ?? -1) || b.votes - a.votes);
    const cards = items.map((item) => {
      const card = node("button", "", "market-card");
      card.type = "button";
      const status = item.recipe_title ? "Готово для агентов" : "Из Skills.sh";
      const badge = node("span", item.installed ? "В библиотеке" : status, "market-badge");
      badge.dataset.ready = String(Boolean(item.recipe_title));
      const title = node("strong", item.recipe_title || item.name, "market-card-name");
      const source = node("span", item.source, "market-card-source");
      const stats = node("span", ratings(item), "market-card-stats");
      if (item.installs != null) stats.append(node("span", "↓ " + number.format(item.installs), "market-installs"));
      card.append(badge, title, source, stats);
      card.addEventListener("click", () => void showInfo(item));
      return card;
    });
    byId("market-results").replaceChildren(...cards);
  }

  async function search() {
    clearTimeout(timer);
    const version = ++request;
    searchAbort?.abort();
    searchAbort = new AbortController();
    const status = byId("market-status");
    status.textContent = "Загружаю каталог…";
    const params = new URLSearchParams({ q: input.value.trim(), tab: currentTab });
    try {
      const data = await api("?" + params, { signal: searchAbort.signal });
      if (request !== version) return;
      results = data.items;
      renderResults();
      status.textContent = data.warning || (!results.length ? "Ничего не найдено. Попробуй другой запрос или раздел." :
        "Найдено в выдаче: " + results.length + (data.public ? "" : " · офлайн-демо"));
    } catch (error) {
      if (request !== version || error.name === "AbortError") return;
      results = [];
      renderResults();
      status.textContent = error.message;
    }
  }

  function detailError(text) {
    byId("skill-error").textContent = text;
    byId("skill-error").hidden = !text;
  }

  function drawInfo(item, resetForm) {
    byId("skill-title").textContent = item.recipe_title || item.name;
    byId("skill-source").textContent = item.source;
    byId("skill-description").textContent = item.description || "Описание открывается на странице источника.";
    byId("skill-note").textContent = item.recipe_title
      ? "Мост подключает свой проверенный текстовый набор для этой задачи. Скрипты и зависимости не устанавливаются."
      : "Сохраняется карточка. Внешние инструкции и скрипты не передаются агентам.";
    const details = byId("skill-recipe");
    details.hidden = !item.recipe;
    if (resetForm) details.open = false;
    if (item.recipe) {
      byId("skill-grok").textContent = item.recipe.grok;
      byId("skill-devin").textContent = item.recipe.devin;
    }
    byId("skill-score").textContent = ratings(item);
    install.disabled = saving;
    install.textContent = item.installed ? "Убрать из библиотеки" : item.recipe_title ? "Установить" : "Сохранить в библиотеку";
    const task = byId("skill-task");
    task.hidden = !item.installed || !item.recipe_title;
    const params = new URLSearchParams({ skill: item.id });
    const current = job?.();
    if (current?.repo) params.set("repo", current.repo);
    if (current?.ref) params.set("ref", current.ref);
    task.href = "/?" + params + "#job-form";
    const upstream = byId("skill-upstream");
    upstream.hidden = !item.url;
    if (item.url) upstream.href = item.url;
    else upstream.removeAttribute("href");
    reviewForm.hidden = !item.installed;
    if (resetForm) {
      byId("skill-stars").value = item.my_review?.stars ?? "";
      byId("skill-review-text").value = item.my_review?.text ?? "";
    }
    reviewSubmit.textContent = item.my_review ? "Обновить отзыв" : "Опубликовать отзыв";
    byId("skill-review-list").replaceChildren(...(item.reviews || []).map((review) => {
      const entry = node("li", "", "skill-review");
      const when = new Date(review.updated * 1000).toLocaleDateString("ru-RU");
      entry.append(node("strong", "★ " + review.stars + " / 5 · " + when + (review.mine ? " · Твой отзыв" : "")),
        node("p", review.text));
      return entry;
    }));
  }

  async function showInfo(item) {
    detailAbort?.abort();
    detailAbort = new AbortController();
    const { signal } = detailAbort;
    detailError("");
    openSkill = null;
    drawInfo(item, true);
    install.disabled = true;
    reviewForm.hidden = true;
    if (!dialog.open) dialog.showModal();
    try {
      const data = await api("/info?" + new URLSearchParams({ skill_id: item.id }), { signal });
      if (signal.aborted) return;
      openSkill = { ...item, ...data, installs: item.installs ?? data.installs };
      drawInfo(openSkill, true);
    } catch (error) {
      if (!signal.aborted) detailError(error.message);
    }
  }

  async function change(path, payload) {
    if (!openSkill || saving) return;
    const item = openSkill;
    saving = true;
    install.disabled = reviewSubmit.disabled = true;
    detailError("");
    try {
      await api(path, { payload });
      void search();
      if (openSkill === item && dialog.open) await showInfo(item);
    } catch (error) {
      if (openSkill === item) detailError(error.message);
    } finally {
      saving = false;
      install.disabled = !openSkill;
      reviewSubmit.disabled = false;
    }
  }

  input.addEventListener("input", () => {
    clearTimeout(timer);
    ++request;
    searchAbort?.abort();
    timer = setTimeout(() => void search(), 300);
  });
  byId("market-sort").addEventListener("change", renderResults);
  for (const button of tabLabels) {
    button.addEventListener("click", () => {
      currentTab = button.dataset.marketTab;
      input.value = "";
      for (const peer of tabLabels) peer.setAttribute("aria-pressed", String(peer === button));
      void search();
    });
  }
  install.addEventListener("click", () => {
    if (openSkill) void change("/library", { skill_id: openSkill.id, installed: !openSkill.installed });
  });
  reviewForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = byId("skill-review-text").value.trim();
    if (openSkill && text) void change("/review", { skill_id: openSkill.id, stars: Number(byId("skill-stars").value), text });
  });
  byId("skill-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    detailAbort?.abort();
    openSkill = null;
  });
  window.BridgeMarketplace = { open(getJob) { job = getJob; void search(); } };
})();
