(function () {
  var wrap = document.getElementById("processes-wrap");
  var btn = document.getElementById("processes-btn");
  var badge = document.getElementById("processes-badge");
  var panel = document.getElementById("processes-panel");
  var list = document.getElementById("processes-list");
  if (!wrap || !btn || !panel || !list) return;

  var ACTIVE_STATUSES = { queued: true, downloading: true, converting: true };
  var POLL_OPEN_MS = 2000;
  var POLL_CLOSED_MS = 8000;
  var pollTimer = null;
  var everSucceeded = false;

  var DOWNLOAD_ICON = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>';
  var CANCEL_ICON = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';

  function escapeHtml(str) {
    var div = document.createElement("div");
    div.textContent = str == null ? "" : String(str);
    return div.innerHTML;
  }

  function formatSize(bytes) {
    if (!bytes) return "";
    var mb = bytes / 1048576;
    if (mb >= 1024) return (mb / 1024).toFixed(1) + " ГБ";
    if (mb < 1) return mb.toFixed(1) + " МБ";
    return Math.round(mb) + " МБ";
  }

  function formatEta(seconds) {
    if (seconds == null || seconds < 0) return "";
    seconds = Math.round(seconds);
    if (seconds < 60) return "≈" + seconds + " с";
    var totalMinutes = Math.floor(seconds / 60);
    var s = seconds % 60;
    if (totalMinutes < 60) return "≈" + totalMinutes + " хв" + (s ? " " + s + " с" : "");
    var h = Math.floor(totalMinutes / 60);
    var m = totalMinutes % 60;
    return "≈" + h + " год" + (m ? " " + m + " хв" : "");
  }

  function statusSymbol(status) {
    if (status === "finished") return "✓";
    if (status === "error") return "✕";
    if (status === "cancelled") return "⊘";
    if (status === "downloading" || status === "converting") return '<span class="spinner"></span>';
    if (status === "queued") return "⏳";
    return "–";
  }

  function renderRow(item) {
    var isActive = !!ACTIVE_STATUSES[item.status];
    var fileUrl = item.kind === "download" ? "/api/file/" + item.id : "/api/convert/file/" + item.id;
    var metaBits = [];
    if (item.status === "queued") metaBits.push("У черзі");
    else if (isActive && item.eta_seconds != null) metaBits.push(formatEta(item.eta_seconds) + " до завершення");
    if (item.status === "finished" && item.filesize) metaBits.push(formatSize(item.filesize));
    if (item.status === "error") metaBits.push("Помилка");
    if (item.status === "cancelled") metaBits.push("Скасовано");

    var action = "";
    if (item.status === "finished") {
      action = '<a href="' + fileUrl + '" class="processes-row-link" title="Завантажити файл" aria-label="Завантажити файл">' + DOWNLOAD_ICON + '</a>';
    } else if (isActive) {
      action = '<button type="button" class="processes-row-link cancel" data-cancel-kind="' + item.kind + '" data-cancel-id="' + item.id + '" title="Скасувати" aria-label="Скасувати">' + CANCEL_ICON + '</button>';
    }
    var progressBar = isActive
      ? '<div class="progress"><div class="progress-bar' + (item.progress ? '' : ' indeterminate') + '" style="width:' + (item.progress || 0) + '%"></div></div>'
      : "";

    return '<div class="processes-row">' +
      '<div class="processes-row-top">' +
        '<span class="status-icon status-' + item.status + '">' + statusSymbol(item.status) + '</span>' +
        '<span class="processes-row-title" title="' + escapeHtml(item.title) + '">' + escapeHtml(item.title) + '</span>' +
        action +
      '</div>' +
      (metaBits.length ? '<span class="processes-row-meta">' + escapeHtml(metaBits.join(" · ")) + '</span>' : "") +
      progressBar +
      '</div>';
  }

  function render(items) {
    if (!items.length) {
      list.innerHTML = '<p class="empty-hint">Немає процесів</p>';
    } else {
      list.innerHTML = items.map(renderRow).join("");
    }
    var activeCount = items.filter(function (it) { return ACTIVE_STATUSES[it.status]; }).length;
    if (activeCount > 0) {
      badge.textContent = activeCount > 99 ? "99+" : String(activeCount);
      badge.hidden = false;
    } else {
      badge.hidden = true;
    }
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(refresh, panel.hidden ? POLL_CLOSED_MS : POLL_OPEN_MS);
  }

  async function refresh() {
    try {
      const res = await fetch("/api/processes");
      if (res.status === 401 || res.status === 403) {
        wrap.hidden = true;
        return;
      }
      if (!res.ok) {
        schedulePoll();
        return;
      }
      const items = await res.json();
      if (!everSucceeded) {
        everSucceeded = true;
        wrap.hidden = false;
      }
      render(items);
    } catch (err) {
      // ignore — keep showing the last known state, try again next tick
    } finally {
      schedulePoll();
    }
  }

  function closePanel() {
    panel.hidden = true;
  }

  btn.addEventListener("click", function () {
    var willOpen = panel.hidden;
    panel.hidden = !willOpen;
    if (willOpen) {
      refresh();
    }
  });

  document.addEventListener("click", function (e) {
    if (!panel.hidden && !wrap.contains(e.target)) closePanel();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !panel.hidden) closePanel();
  });

  list.addEventListener("click", async function (e) {
    var cancelBtn = e.target.closest(".processes-row-link.cancel");
    if (!cancelBtn) return;
    e.stopPropagation();
    cancelBtn.disabled = true;
    var url = cancelBtn.dataset.cancelKind === "download"
      ? "/api/cancel/" + cancelBtn.dataset.cancelId
      : "/api/convert/cancel/" + cancelBtn.dataset.cancelId;
    try {
      await fetch(url, { method: "POST" });
    } catch (err) {
      // ignore — refresh() below will just show whatever state actually stuck
    }
    refresh();
  });

  refresh();
})();
