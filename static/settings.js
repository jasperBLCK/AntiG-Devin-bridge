"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const settingsForm = byId("settings-form");
  const key = byId("api-key");
  const labels = { missing: "Нет ключа", environment: "Из окружения", runtime: "Введён в панели" };
  let current = null;

  function provider() {
    return settingsForm.querySelector("input[name=provider]:checked").value;
  }

  function draw(data) {
    current = data;
    for (const [name, status] of Object.entries(data.keys)) {
      byId("key-" + name).textContent = labels[status];
      byId("key-" + name).dataset.status = status === "missing" ? "failed" : "done";
    }
    refresh();
  }

  function refresh() {
    if (!current) return;
    byId("save-settings").disabled = !current.can_manage;
    byId("reset-key").disabled = !current.can_manage || current.keys[provider()] !== "runtime";
    key.disabled = !current.can_manage;
    byId("settings-note").textContent = current.can_manage ? "" :
      "Менять ключи можно только в защищённом кабинете.";
  }

  async function send(payload) {
    const error = byId("settings-error");
    error.hidden = true;
    try {
      const response = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Bridge-Request": "1" },
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Проверь поля настроек.");
      draw(data);
      byId("settings-note").textContent = "Сохранено.";
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
    } finally {
      key.value = "";
    }
  }

  settingsForm.addEventListener("change", refresh);
  settingsForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const value = key.value.trim();
    void send(value ? { provider: provider(), key: value } : { provider: provider() });
  });
  byId("reset-key").addEventListener("click", () => void send({ provider: provider(), reset: true }));

  fetch("/api/settings").then((response) => response.json()).then((data) => {
    settingsForm.querySelector("input[value=" + data.provider + "]").checked = true;
    draw(data);
  }).catch(() => {
    byId("settings-error").textContent = "Настройки недоступны.";
    byId("settings-error").hidden = false;
  });
})();
