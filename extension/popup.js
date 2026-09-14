(function () {
  const loggedOutBox = document.getElementById("logged-out");
  const loggedInBox = document.getElementById("logged-in");
  const connectedAs = document.getElementById("connected-as");
  const errorEl = document.getElementById("error");
  const serverUrlInput = document.getElementById("server-url");
  const usernameInput = document.getElementById("username");
  const passwordInput = document.getElementById("password");

  function showError(text) {
    errorEl.textContent = text;
    errorEl.hidden = false;
  }

  function render() {
    chrome.storage.local.get(["serverUrl", "token", "username"], function (stored) {
      if (stored.token) {
        loggedOutBox.hidden = true;
        loggedInBox.hidden = false;
        connectedAs.textContent = "Підключено як " + stored.username + " (" + stored.serverUrl + ")";
      } else {
        loggedOutBox.hidden = false;
        loggedInBox.hidden = true;
        if (stored.serverUrl) serverUrlInput.value = stored.serverUrl;
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
      render();
    } catch (err) {
      showError("Не вдалося з'єднатися з сервером");
    }
  });

  document.getElementById("logout-btn").addEventListener("click", async function () {
    await chrome.storage.local.remove(["token", "username"]);
    render();
  });

  render();
})();
