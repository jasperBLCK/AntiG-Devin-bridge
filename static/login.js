"use strict";

(() => {
  const connect = document.getElementById("connect");
  const enterDemo = document.getElementById("enter-demo");
  const status = document.getElementById("status");
  const params = new URLSearchParams(location.search);
  const target = encodeURIComponent(params.get("target") || "overview");
  const notice = {
    cancelled: "Вход отменён. Можно попробовать снова.",
    failed: "Не удалось войти через GitHub. Попробуй ещё раз.",
    denied: "Этот аккаунт GitHub не имеет доступа к кабинету.",
  }[params.get("github")];

  async function api(path, method = "GET") {
    const response = await fetch("/api/" + path, { method, headers: { "X-Bridge-Request": "1" } });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Вход недоступен.");
    return data;
  }

  function start(button, path, next) {
    button.addEventListener("click", async () => {
      button.disabled = true;
      status.textContent = "";
      try {
        const data = await api(path + "?target=" + target, "POST");
        next(data);
      } catch (error) {
        status.textContent = error.message;
        button.disabled = false;
      }
    });
  }

  start(connect, "github/start", ({ url }) => location.assign(url));
  start(enterDemo, "demo/enter", ({ path }) => location.replace(path));
  api("github/status").then(({ enabled, demo }) => {
    connect.disabled = !enabled;
    document.getElementById("demo").hidden = !demo;
    status.textContent = notice || (enabled ? "" : demo ? "GitHub недоступен в демо." : "GitHub App ещё не настроен.");
  }).catch(() => {
    status.textContent = "Не удалось проверить доступность входа.";
  });
})();
