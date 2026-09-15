(function () {
  const tasksView = document.getElementById("tasks-view");
  const accountView = document.getElementById("account-view");
  const accountToggle = document.getElementById("account-toggle");
  const backToTasks = document.getElementById("back-to-tasks");
  const taskListEl = document.getElementById("task-list");
  const tasksEmptyEl = document.getElementById("tasks-empty");
  const clearFinishedBtn = document.getElementById("clear-finished");

  const loggedOutBox = document.getElementById("logged-out");
  const loggedInBox = document.getElementById("logged-in");
  const connectedAs = document.getElementById("connected-as");
  const counterEl = document.getElementById("extension-counter");
  const errorEl = document.getElementById("error");
  const serverUrlInput = document.getElementById("server-url");
  const usernameInput = document.getElementById("username");
  const passwordInput = document.getElementById("password");

  function showError(text) {
    errorEl.textContent = text;
    errorEl.hidden = false;
  }

  function showAccountView() {
    tasksView.hidden = true;
    accountView.hidden = false;
  }
  function showTasksView() {
    accountView.hidden = true;
    tasksView.hidden = false;
  }
  accountToggle.addEventListener("click", showAccountView);
  backToTasks.addEventListener("click", showTasksView);

  // ---- Account ----

  async function loadCounter(serverUrl, token) {
    try {
      const res = await fetch(serverUrl.replace(/\/$/, "") + "/api/extension/my-stats", {
        headers: { Authorization: "Bearer " + token },
      });
      if (!res.ok) return;
      const data = await res.json();
      if (data.count > 0) {
        counterEl.textContent = "Ви допомогли завантажити " + data.count + " відео завдяки розширенню!";
        counterEl.hidden = false;
      } else {
        counterEl.hidden = true;
      }
    } catch (err) {
      counterEl.hidden = true;
    }
  }

  function renderAccount() {
    chrome.storage.local.get(["serverUrl", "token", "username"], function (stored) {
      if (stored.token) {
        loggedOutBox.hidden = true;
        loggedInBox.hidden = false;
        connectedAs.textContent = "Підключено як " + stored.username + " (" + stored.serverUrl + ")";
        loadCounter(stored.serverUrl, stored.token);
      } else {
        loggedOutBox.hidden = false;
        loggedInBox.hidden = true;
        if (stored.serverUrl) serverUrlInput.value = stored.serverUrl;
        // Nothing useful to show in the tasks view without an account yet.
        showAccountView();
      }
    });
  }

  document.getElementById("login-btn").addEventListener("click", async function () {
    errorEl.hidden = true;
    const serverUrl = serverUrlInput.value.trim().replace(/\/$/, "");
    const username = usernameInput.value.trim();
    const password = passwordInput.value;
    if (!serverUrl || !username || !password) {
      showError("Заповніть усі поля");
      return;
    }
    try {
      const res = await fetch(serverUrl + "/api/extension/login", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: "username=" + encodeURIComponent(username) + "&password=" + encodeURIComponent(password),
      });
      const data = await res.json();
      if (!res.ok || !data.token) {
        showError(data.error || "Не вдалося увійти");
        return;
      }
      await chrome.storage.local.set({ serverUrl, token: data.token, username: data.username });
      passwordInput.value = "";
      renderAccount();
      showTasksView();
    } catch (err) {
      showError("Не вдалося з'єднатися з сервером");
    }
  });

  document.getElementById("logout-btn").addEventListener("click", async function () {
    await chrome.storage.local.remove(["token", "username"]);
    renderAccount();
  });

  // ---- Tasks ----
  // Rendered straight from chrome.storage.local's TASKS_KEY, the same
  // state background.js's own polling writes to - this is what lets the
  // popup show current progress (or the outcome) even after the in-page
  // panel that started a download has been closed.

  function statusLabel(task) {
    switch (task.status) {
      case "queued":
        return "У черзі...";
      case "waiting_extension":
        return "Очікуємо токен...";
      case "downloading":
        return "Завантаження" + (task.progress != null ? ": " + Math.round(task.progress) + "%" : "...");
      case "finished":
        return "Зберігаємо на пристрій...";
      case "saved":
        return "Збережено на пристрій";
      case "save_error":
        return "Готово, але не збереглось: " + (task.error || "");
      case "error":
        return "Помилка: " + (task.error || "");
      default:
        return task.status || "";
    }
  }
  function statusClass(task) {
    if (task.status === "saved") return "ok";
    if (task.status === "error" || task.status === "save_error") return "err";
    return "";
  }
  const PROGRESS_STATUSES = ["queued", "waiting_extension", "downloading", "finished"];

  function renderTasks() {
    chrome.storage.local.get(["obeliskTasks"], function (stored) {
      const tasks = stored.obeliskTasks || {};
      const list = Object.keys(tasks)
        .map(function (id) {
          return tasks[id];
        })
        .sort(function (a, b) {
          return (b.updatedAt || 0) - (a.updatedAt || 0);
        });

      taskListEl.innerHTML = "";
      if (!list.length) {
        tasksEmptyEl.hidden = false;
        clearFinishedBtn.hidden = true;
        return;
      }
      tasksEmptyEl.hidden = true;
      clearFinishedBtn.hidden = !list.some(function (t) {
        return ["saved", "error", "save_error"].indexOf(t.status) !== -1;
      });

      list.forEach(function (task) {
        const row = document.createElement("div");
        row.className = "task";

        const title = document.createElement("p");
        title.className = "task-title";
        title.textContent = task.title || task.url || task.id;
        row.appendChild(title);

        const status = document.createElement("p");
        status.className = "task-status " + statusClass(task);
        status.textContent = statusLabel(task);
        row.appendChild(status);

        if (PROGRESS_STATUSES.indexOf(task.status) !== -1) {
          const bar = document.createElement("progress");
          bar.className = "task-progress";
          bar.max = 100;
          bar.value = task.progress || 0;
          row.appendChild(bar);
        }

        taskListEl.appendChild(row);
      });
    });
  }

  clearFinishedBtn.addEventListener("click", async function () {
    const stored = await chrome.storage.local.get(["obeliskTasks"]);
    const tasks = stored.obeliskTasks || {};
    Object.keys(tasks).forEach(function (id) {
      if (["saved", "error", "save_error"].indexOf(tasks[id].status) !== -1) delete tasks[id];
    });
    await chrome.storage.local.set({ obeliskTasks: tasks });
    renderTasks();
  });

  chrome.storage.onChanged.addListener(function (changes, area) {
    if (area !== "local") return;
    if (changes.obeliskTasks) renderTasks();
    if (changes.token || changes.username || changes.serverUrl) renderAccount();
  });

  // Ask background.js to refresh task statuses right away - its own
  // periodic poll may not have run recently if the service worker was
  // asleep, and the popup shouldn't show stale data on open.
  chrome.runtime.sendMessage({ type: "poll-now" }, function () {
    renderTasks();
  });
  renderTasks();
  renderAccount();
})();
