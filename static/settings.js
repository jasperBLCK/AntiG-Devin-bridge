"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const settingsForm = byId("settings-form");
  const key = byId("api-key");
  const devinForm = byId("devin-settings-form");
  const devinKey = byId("devin-key");
  const devinOrg = byId("devin-org");
  const labels = { missing: "Нет ключа", environment: "Из окружения", runtime: "Введён в панели" };
  let current = null;

  function provider() {
    return settingsForm.querySelector("input[name=provider]:checked").value;
  }

  function draw(data) {
    current = data;
    for (const [name, status] of Object.entries({ ...data.keys, devin: data.devin })) {
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
    byId("save-devin").disabled = !current.can_manage;
    byId("reset-devin").disabled = !current.can_manage || current.devin !== "runtime";
    devinKey.disabled = devinOrg.disabled = !current.can_manage;
    byId("devin-settings-note").textContent = byId("settings-note").textContent;
  }

  async function send(payload, isDevin = false) {
    const prefix = isDevin ? "devin-settings-" : "settings-";
    const error = byId(prefix + "error");
    error.hidden = true;
    try {
      const response = await fetch(isDevin ? "/api/settings/devin" : "/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Bridge-Request": "1" },
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Проверь поля настроек.");
      draw(data);
      document.dispatchEvent(new Event("bridge-settings-updated"));
      byId(prefix + "note").textContent = payload.reset ? "Сброшено." :
        isDevin ? "Сохранено. Доступ к Devin проверится при запуске задачи." : "Сохранено.";
      if (isDevin && payload.reset) devinOrg.value = "";
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
    } finally {
      (isDevin ? devinKey : key).value = "";
    }
  }

  settingsForm.addEventListener("change", refresh);
  settingsForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const value = key.value.trim();
    void send(value ? { provider: provider(), key: value } : { provider: provider() });
  });
  byId("reset-key").addEventListener("click", () => void send({ provider: provider(), reset: true }));
  devinForm.addEventListener("submit", (event) => {
    event.preventDefault();
    void send({ key: devinKey.value.trim(), org: devinOrg.value.trim() }, true);
  });
  byId("reset-devin").addEventListener("click", () => void send({ reset: true }, true));

  fetch("/api/settings").then((response) => response.json()).then((data) => {
    settingsForm.querySelector("input[value=" + data.provider + "]").checked = true;
    draw(data);
  }).catch(() => {
    for (const id of ["settings-error", "devin-settings-error"]) {
      byId(id).textContent = "Настройки недоступны.";
      byId(id).hidden = false;
    }
  });
})();
