(() => {
  "use strict";

  const byId = (id) => document.getElementById(id);
  const input = byId("market-query");
  const dialog = byId("skill-dialog");
  const install = byId("skill-install");
  const reviewForm = byId("skill-review-form");
  const reviewSubmit = byId("skill-review-submit");
  const sort = byId("market-sort");
  const more = byId("market-more");
  const globalLink = byId("market-global");
  const rankPaths = { "all-time": "/", trending: "/trending", hot: "/hot" };
  const number = new Intl.NumberFormat("ru-RU");
  const tabLabels = [...document.querySelectorAll("[data-market-tab]")];
  const designTopics = [...document.querySelectorAll("[data-design-topic]")];
  const recipeGroups = [...document.querySelectorAll("[data-recipe-group]")];
  let currentTab = "catalog";
  let currentTopic = "all";
  let currentGroup = "all";
  let opened = false;
  let results = [];
  let page = 1;
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
    const cards = results.map((item) => {
      const card = node("button", "", "market-card");
      card.type = "button";
      const status = currentTab === "recommended" ? (item.recipe_group === "design" ? "Дизайн и UI" : "Инженерия") :
        item.recipe_title ? "Готово для агентов" : "Из Skills.sh";
      const label = item.installed ? "В библиотеке" : status;
      const badge = node("span", item.rank ? "#" + item.rank + " · " + label : label, "market-badge");
      badge.dataset.ready = String(Boolean(item.recipe_title));
      const title = node("strong", item.recipe_title || item.name, "market-card-name");
      const source = node("span", item.source, "market-card-source");
      const stats = node("span", ratings(item), "market-card-stats");
      if (item.installs != null) stats.append(node("span", "↓ " + number.format(item.installs), "market-installs"));
      if (item.rank_stat) stats.append(node("span", item.rank_stat + " · " + item.rank_label, "market-installs"));
      card.append(badge, title, source);
      if (currentTab === "recommended") card.append(node("span", item.description, "market-card-summary"));
      card.append(stats);
      card.addEventListener("click", () => void showInfo(item));
      return card;
    });
    byId("market-results").replaceChildren(...cards);
  }

  async function search(nextPage = 1) {
    clearTimeout(timer);
    const version = ++request;
    searchAbort?.abort();
    searchAbort = new AbortController();
    const status = byId("market-status");
    status.textContent = "Загружаю каталог…";
    more.disabled = true;
    const q = input.value.trim();
    const params = new URLSearchParams({
      q, tab: currentTab, sort: sort.value, page: nextPage, topic: currentTopic, group: currentGroup,
    });
    try {
      const data = await api("?" + params, { signal: searchAbort.signal });
      if (request !== version) return;
      if (nextPage === 1) results = [];
      const seen = new Set(results.map((item) => item.id));
      results.push(...data.items.filter((item) => !seen.has(item.id)));
      page = nextPage;
      renderResults();
      const ranked = currentTab in rankPaths;
      const context = ranked ? " в доступной части рейтинга" :
        currentTab === "catalog" && !q ? " в витрине" : currentTab === "design" && !q ? " по дизайну" : "";
      const count = results.length ? `Показано ${number.format(results.length)} из ${number.format(data.total)}${context}` :
        "Ничего не найдено. Попробуй другой запрос или раздел.";
      const cap = q && data.public && ["catalog", "design"].includes(currentTab) && data.total >= 200
        ? "Уточни запрос или тему для другой выдачи." : "";
      status.textContent = [count, data.warning, cap, data.public ? "" : "офлайн-демо"].filter(Boolean).join(" · ");
      globalLink.hidden = !ranked || !data.public;
      if (!globalLink.hidden) {
        globalLink.href = "https://www.skills.sh" + rankPaths[currentTab];
        globalLink.textContent = data.source_total
          ? number.format(data.source_total) + " в Skills.sh ↗"
          : "Весь рейтинг на Skills.sh ↗";
      }
      more.hidden = !data.has_more;
      more.disabled = false;
    } catch (error) {
      if (request !== version || error.name === "AbortError") return;
      if (nextPage === 1) {
        results = [];
        renderResults();
        more.hidden = true;
      }
      more.disabled = false;
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
    task.href = "/tasks/new?" + params;
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
    more.disabled = true;
    timer = setTimeout(() => void search(), 300);
  });
  sort.addEventListener("change", () => void search());
  more.addEventListener("click", () => void search(page + 1));
  function setPressed(buttons, key, value) {
    for (const button of buttons) button.setAttribute("aria-pressed", String(button.dataset[key] === value));
  }

  function selectTab(tab) {
    currentTab = tab;
    currentTopic = currentGroup = "all";
    input.value = "";
    input.placeholder = currentTab === "catalog" ? "Название или тема: React, Python, дизайн…" :
      currentTab === "design" ? "Figma, дизайн-система, анимация…" : "Фильтр этого раздела";
    sort.value = "default";
    sort.querySelector("[value=installs]").disabled = currentTab in rankPaths;
    byId("design-collection").hidden = currentTab !== "design";
    byId("recommended-collection").hidden = currentTab !== "recommended";
    more.hidden = globalLink.hidden = true;
    setPressed(tabLabels, "marketTab", tab);
    setPressed(designTopics, "designTopic", "all");
    setPressed(recipeGroups, "recipeGroup", "all");
    void search();
  }

  for (const button of tabLabels) button.addEventListener("click", () => selectTab(button.dataset.marketTab));
  for (const button of designTopics) button.addEventListener("click", () => {
    currentTopic = button.dataset.designTopic;
    setPressed(designTopics, "designTopic", currentTopic);
    void search();
  });
  for (const button of recipeGroups) button.addEventListener("click", () => {
    currentGroup = button.dataset.recipeGroup;
    setPressed(recipeGroups, "recipeGroup", currentGroup);
    void search();
  });
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
  window.BridgeMarketplace = { open(getJob) {
    job = getJob;
    if (!opened) {
      opened = true;
      const tab = new URLSearchParams(location.search).get("tab");
      if (tabLabels.some((button) => button.dataset.marketTab === tab)) {
        selectTab(tab);
        return;
      }
    }
    void search();
  } };
})();
