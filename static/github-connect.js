"use strict";

for (const panel of document.querySelectorAll("[data-github-connect]")) {
  const message = panel.querySelector("[data-github-message]");
  const signIn = panel.querySelector("[data-github-sign-in]");
  const install = panel.querySelector("[data-github-install]");
  const manage = panel.querySelector("[data-github-manage]");
  const reload = panel.querySelector("[data-github-reload]");
  const disconnect = panel.querySelector("[data-github-disconnect]");
  const select = panel.querySelector("[data-github-repos]");
  const params = new URLSearchParams(location.search);
  const event = params.get("github");
  const notices = {
    cancelled: "Подключение отменено в GitHub.",
    failed: "Не удалось войти через GitHub. Попробуй ещё раз.",
    setup: "Войди через GitHub, чтобы увидеть выбранные репозитории.",
  };
  if (event) {
    params.delete("github");
    history.replaceState(null, "", location.pathname + (params.size ? "?" + params : "") + location.hash);
  }

  async function api(path, method = "GET") {
    const response = await fetch("/api/github/" + path, { method, headers: { "X-Bridge-Request": "1" } });
    if (!response.ok) {
      const data = await response.json();
      throw new Error(typeof data.detail === "string" ? data.detail : "GitHub недоступен. Попробуй позже.");
    }
    return response.json();
  }

  async function loadRepos(force = false) {
    if (!select) return;
    reload.disabled = true;
    try {
      const { repos, partial } = await api("repos" + (force ? "?reload=true" : ""));
      select.replaceChildren(new Option("Выбрать репозиторий…", ""),
        ...repos.map((repo) => new Option(repo, repo)));
      select.hidden = repos.length === 0;
      message.textContent = repos.length === 0
        ? "Аккаунт подключён. Выбери репозитории на GitHub."
        : message.dataset.account + " · " + (partial ? "Показана часть репозиториев." : "Доступно: " + repos.length);
    } finally {
      reload.disabled = false;
    }
  }

  function showError(error) {
    message.textContent = error.message;
  }

  signIn.addEventListener("click", async () => {
    signIn.disabled = true;
    try {
      const { url } = await api("start?target=" + encodeURIComponent(panel.dataset.githubConnect), "POST");
      location.assign(url);
    } catch (error) {
      showError(error);
      signIn.disabled = false;
    }
  });
  select?.addEventListener("change", () => {
    if (!select.value) return;
    const field = document.getElementById(panel.dataset.githubInput);
    field.value = select.value;
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.focus();
  });
  reload?.addEventListener("click", () => void loadRepos(true).catch(showError));
  disconnect?.addEventListener("click", async () => {
    disconnect.disabled = true;
    try {
      await api("disconnect", "POST");
      location.reload();
    } catch (error) {
      showError(error);
      disconnect.disabled = false;
    }
  });
  api("status").then(({ enabled, login, install: url }) => {
    if (!enabled) {
      message.textContent = "Вход через GitHub не настроен на сервере.";
      return;
    }
    signIn.hidden = Boolean(login);
    message.textContent = login ? "GitHub · @" + login : (notices[event] || "Подключи GitHub, чтобы дать доступ к коду.");
    if (!login) return;
    message.dataset.account = message.textContent;
    if (install) {
      install.href = url;
      install.hidden = false;
    }
    for (const action of [manage, reload, disconnect]) {
      if (action) action.hidden = false;
    }
    return loadRepos(event === "setup");
  }).catch(showError);
}
