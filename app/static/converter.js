const form = document.getElementById("convert-form");
const statusBox = document.getElementById("status-box");
const fileInput = document.getElementById("file-input");
const fileChooseBtn = document.getElementById("file-choose-btn");
const fileNameEl = document.getElementById("file-name");
const advancedToggle = document.getElementById("advanced-toggle");
const advancedWrap = document.getElementById("advanced-wrap");

if (advancedToggle) {
  advancedToggle.addEventListener("change", () => {
    advancedWrap.hidden = !advancedToggle.checked;
  });
}

fileChooseBtn.addEventListener("click", () => fileInput.click());

// Arriving via "Конвертувати" on an already-downloaded file: skip the
// picker entirely and pre-fill the file field with that download instead
// of a real browser File — the server just copies its own file, no upload.
const _params = new URLSearchParams(window.location.search);
let sourceDownloadId = _params.get("from_download");
const sourceFilename = _params.get("filename");
if (sourceDownloadId) {
  fileInput.required = false;
  fileNameEl.textContent = sourceFilename ? `${sourceFilename} (із завантажень)` : "Файл із завантажень";
}

fileInput.addEventListener("change", () => {
  sourceDownloadId = null; // picking a file manually overrides the download source
  fileNameEl.textContent = fileInput.files[0] ? fileInput.files[0].name : "Файл не обрано";
});

function formatSize(bytes) {
  if (!bytes) return null;
  const mb = bytes / 1048576;
  if (mb >= 1024) return (mb / 1024).toFixed(1) + " ГБ";
  return Math.round(mb) + " МБ";
}

const STATUS_LABELS = {
  queued: "У черзі",
  converting: "Конвертація",
};

const DOWNLOAD_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>';

function triggerAutoDownload(id) {
  const a = document.createElement("a");
  a.href = `/api/convert/file/${id}`;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

async function submitFromDownload(downloadId) {
  const fd = new FormData(form);
  statusBox.innerHTML = `<div class="card status-card">
    <p>Готуємо файл із завантажень...</p>
    <div class="progress"><div class="progress-bar indeterminate"></div></div>
  </div>`;
  try {
    const res = await fetch(`/api/convert/from-download/${downloadId}`, { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok || data.error) {
      statusBox.innerHTML = `<div class="card status-card"><p class="error">${data.error || "Помилка"}</p></div>`;
      return;
    }
    pollConvertStatus(data.id, data.duration_seconds, data.input_summary);
  } catch (err) {
    statusBox.innerHTML = `<div class="card status-card"><p class="error">Помилка з'єднання</p></div>`;
  }
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  if (sourceDownloadId) {
    submitFromDownload(sourceDownloadId);
    return;
  }
  if (!fileInput.files[0]) return;

  const fd = new FormData(form);
  statusBox.innerHTML = `<div class="card status-card">
    <p id="upload-label">Завантаження файлу на сервер... (0%)</p>
    <div class="progress"><div class="progress-bar" id="upload-bar" style="width:0%"></div></div>
  </div>`;

  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/convert");
  xhr.upload.addEventListener("progress", (ev) => {
    if (!ev.lengthComputable) return;
    const pct = Math.round((ev.loaded / ev.total) * 100);
    const bar = document.getElementById("upload-bar");
    const label = document.getElementById("upload-label");
    if (bar) bar.style.width = pct + "%";
    if (label) label.textContent = `Завантаження файлу на сервер... (${pct}%)`;
  });
  xhr.onload = () => {
    let data = null;
    try {
      data = JSON.parse(xhr.responseText);
    } catch (err) {
      data = null;
    }
    if (xhr.status >= 400 || !data || data.error) {
      statusBox.innerHTML = `<div class="card status-card"><p class="error">${(data && data.error) || "Помилка завантаження файлу"}</p></div>`;
      return;
    }
    pollConvertStatus(data.id, data.duration_seconds, data.input_summary);
  };
  xhr.onerror = () => {
    statusBox.innerHTML = `<div class="card status-card"><p class="error">Помилка з'єднання</p></div>`;
  };
  xhr.send(fd);
});

function pollConvertStatus(id, durationSeconds, inputSummary) {
  const isIndeterminate = !durationSeconds;
  const summaryLine = inputSummary ? `<p class="hint">${escapeHtml(inputSummary)}</p>` : "";
  const interval = setInterval(async () => {
    try {
      const res = await fetch(`/api/convert/status/${id}`);
      const job = await res.json();
      if (job.status === "finished") {
        const size = formatSize(job.filesize);
        triggerAutoDownload(id);
        statusBox.innerHTML = `<div class="card status-card">
          <p class="success">✓ Готово${size ? ` (${size})` : ""}</p>
          ${summaryLine}
          <a class="btn-download" href="/api/convert/file/${id}">${DOWNLOAD_ICON} Завантажити ще раз</a>
        </div>`;
        clearInterval(interval);
        refreshRecent();
      } else if (job.status === "error") {
        statusBox.innerHTML = `<div class="card status-card"><p class="error">Помилка конвертації: ${escapeHtml(job.error || "невідома помилка")}</p></div>`;
        clearInterval(interval);
        refreshRecent();
      } else if (isIndeterminate) {
        statusBox.innerHTML = `<div class="card status-card">
          <p>Статус: ${STATUS_LABELS[job.status] || job.status}...</p>
          ${summaryLine}
          <div class="progress"><div class="progress-bar indeterminate"></div></div>
        </div>`;
      } else {
        const progress = job.progress || 0;
        statusBox.innerHTML = `<div class="card status-card">
          <p>Статус: ${STATUS_LABELS[job.status] || job.status} (${progress}%)</p>
          ${summaryLine}
          <div class="progress"><div class="progress-bar" style="width:${progress}%"></div></div>
        </div>`;
      }
    } catch (err) {
      clearInterval(interval);
    }
  }, 1200);
}

function statusIcon(status) {
  if (status === "finished") return "✓";
  if (status === "error") return "✕";
  if (status === "converting") return '<span class="spinner"></span>';
  if (status === "queued") return "⏳";
  return "–";
}

function renderRow(r) {
  const btn =
    r.status === "finished"
      ? `<a class="dl-download-btn" href="/api/convert/file/${r.id}" title="Завантажити">${DOWNLOAD_ICON}</a>`
      : `<span class="dl-download-btn disabled" title="Ще не готово">${DOWNLOAD_ICON}</span>`;
  const size = formatSize(r.filesize);
  const title = size ? `${r.title} (${size})` : r.title;
  return `
    <div class="dl-row" data-id="${r.id}">
      <span class="status-icon status-${r.status}">${statusIcon(r.status)}</span>
      <button type="button" class="dl-title">${escapeHtml(title)}</button>
      ${btn}
    </div>`;
}

async function refreshRecent() {
  try {
    const res = await fetch("/api/convert/recent");
    const rows = await res.json();
    const list = document.getElementById("recent-list");
    if (!list) return;
    if (!rows.length) {
      list.innerHTML = `<p class="empty-hint">Ще немає конвертацій</p>`;
      return;
    }
    list.innerHTML = rows.map(renderRow).join("");
  } catch (err) {
    // ignore
  }
}
setInterval(refreshRecent, 5000);

document.addEventListener("click", (e) => {
  const titleBtn = e.target.closest(".dl-title");
  if (titleBtn) titleBtn.classList.toggle("expanded");
});
