const form = document.getElementById("scroll-recorder-form");
const statusBox = document.getElementById("status-box");

const STATUS_LABELS = {
  queued: "У черзі",
  recording: "Запис скролу",
  encoding: "Кодування відео",
};

const DOWNLOAD_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>';
const CANCEL_ICON = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

function triggerAutoDownload(id) {
  const a = document.createElement("a");
  a.href = `/api/scroll-recorder/jobs/${id}/file`;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function cancelBtn(id) {
  return `<button type="button" class="status-cancel-corner" data-cancel-id="${id}" title="Скасувати" aria-label="Скасувати">${CANCEL_ICON}</button>`;
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(form);
  const body = {
    url: fd.get("url").trim(),
    aspect_ratio: fd.get("aspect_ratio"),
    duration_seconds: Number(fd.get("duration_seconds")),
    framerate: Number(fd.get("framerate")),
    block_ads: fd.get("block_ads") === "on",
    use_proxy: fd.get("use_proxy") === "on",
  };

  statusBox.innerHTML = `<div class="card status-card">
    <p>Надсилаємо запит...</p>
    <div class="progress"><div class="progress-bar indeterminate"></div></div>
  </div>`;

  try {
    const res = await fetch("/api/scroll-recorder/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok || data.error) {
      statusBox.innerHTML = `<div class="card status-card"><p class="error">${escapeHtml(data.error || data.detail || "Помилка")}</p></div>`;
      return;
    }
    pollStatus(data.job_id);
  } catch (err) {
    statusBox.innerHTML = `<div class="card status-card"><p class="error">Помилка з'єднання</p></div>`;
  }
});

function pollStatus(id) {
  const interval = setInterval(async () => {
    try {
      const res = await fetch(`/api/scroll-recorder/jobs/${id}`);
      const job = await res.json();
      if (!res.ok || job.error) {
        statusBox.innerHTML = `<div class="card status-card"><p class="error">${escapeHtml(job.error || "невідома помилка")}</p></div>`;
        clearInterval(interval);
        return;
      }
      if (job.status === "finished") {
        triggerAutoDownload(id);
        statusBox.innerHTML = `<div class="card status-card">
          <p class="success">✓ Готово</p>
          <a class="btn-download" href="/api/scroll-recorder/jobs/${id}/file">${DOWNLOAD_ICON} Завантажити ще раз</a>
        </div>`;
        clearInterval(interval);
      } else if (job.status === "error") {
        statusBox.innerHTML = `<div class="card status-card"><p class="error">Помилка запису: ${escapeHtml(job.error || "невідома помилка")}</p></div>`;
        clearInterval(interval);
      } else if (job.status === "cancelled") {
        statusBox.innerHTML = `<div class="card status-card"><p>Запис скасовано.</p></div>`;
        clearInterval(interval);
      } else {
        const progress = job.progress || 0;
        const indeterminate = job.status === "queued" || job.status === "encoding";
        statusBox.innerHTML = `<div class="card status-card">
          ${cancelBtn(id)}
          <p>Статус: ${STATUS_LABELS[job.status] || job.status}${indeterminate ? "..." : ` (${Math.round(progress)}%)`}</p>
          <div class="progress"><div class="progress-bar${indeterminate ? " indeterminate" : ""}" style="width:${indeterminate ? "" : progress + "%"}"></div></div>
        </div>`;
      }
    } catch (err) {
      clearInterval(interval);
    }
  }, 1200);
}

document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".status-cancel-corner");
  if (!btn) return;
  btn.disabled = true;
  try {
    await fetch(`/api/scroll-recorder/jobs/${btn.dataset.cancelId}`, { method: "DELETE" });
  } catch (err) {
    // ignore — the next poll tick will just show whatever state actually stuck
  }
});

// --- Preview / element-removal picker ---

const previewBtn = document.getElementById("sr-preview-btn");
const pickerModal = document.getElementById("picker-modal");
const pickerScreenshot = document.getElementById("picker-screenshot");
const pickerLoading = document.getElementById("picker-loading");
const pickerUndoBtn = document.getElementById("picker-undo");
const pickerRecordBtn = document.getElementById("picker-record");
const pickerCloseBtn = document.getElementById("picker-close");

let previewSessionId = null;

function setPickerLoading(loading) {
  pickerLoading.hidden = !loading;
  pickerScreenshot.style.pointerEvents = loading ? "none" : "auto";
}

function showPickerScreenshot(base64) {
  pickerScreenshot.src = `data:image/png;base64,${base64}`;
}

async function closePreviewSession() {
  if (!previewSessionId) return;
  const id = previewSessionId;
  previewSessionId = null;
  try {
    await fetch(`/api/scroll-recorder/preview/${id}`, { method: "DELETE" });
  } catch (err) {
    // ignore — an idle-cleanup sweep on the sidecar will eventually close it anyway
  }
}

previewBtn.addEventListener("click", async () => {
  const url = document.getElementById("sr-url").value.trim();
  if (!url) return;
  const aspectRatio = document.getElementById("sr-aspect-ratio").value;
  const blockAds = document.getElementById("sr-block-ads").checked;
  const useProxy = document.getElementById("sr-use-proxy").checked;

  pickerModal.hidden = false;
  pickerScreenshot.removeAttribute("src");
  setPickerLoading(true);

  try {
    const res = await fetch("/api/scroll-recorder/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, aspect_ratio: aspectRatio, block_ads: blockAds, use_proxy: useProxy }),
    });
    const data = await res.json();
    if (!res.ok || data.error) {
      pickerModal.hidden = true;
      statusBox.innerHTML = `<div class="card status-card"><p class="error">${escapeHtml(data.error || data.detail || "Не вдалося відкрити сторінку")}</p></div>`;
      return;
    }
    previewSessionId = data.session_id;
    showPickerScreenshot(data.screenshot);
  } catch (err) {
    pickerModal.hidden = true;
    statusBox.innerHTML = `<div class="card status-card"><p class="error">Помилка з'єднання</p></div>`;
  } finally {
    setPickerLoading(false);
  }
});

pickerScreenshot.addEventListener("click", async (e) => {
  if (!previewSessionId) return;
  const rect = pickerScreenshot.getBoundingClientRect();
  const scaleX = pickerScreenshot.naturalWidth / rect.width;
  const scaleY = pickerScreenshot.naturalHeight / rect.height;
  const x = (e.clientX - rect.left) * scaleX;
  const y = (e.clientY - rect.top) * scaleY;

  setPickerLoading(true);
  try {
    const res = await fetch(`/api/scroll-recorder/preview/${previewSessionId}/remove`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ x, y }),
    });
    const data = await res.json();
    if (res.ok && data.screenshot) showPickerScreenshot(data.screenshot);
  } catch (err) {
    // ignore — the screenshot just won't update this click, user can retry
  } finally {
    setPickerLoading(false);
  }
});

pickerUndoBtn.addEventListener("click", async () => {
  if (!previewSessionId) return;
  setPickerLoading(true);
  try {
    const res = await fetch(`/api/scroll-recorder/preview/${previewSessionId}/undo`, {
      method: "POST",
    });
    const data = await res.json();
    if (res.ok && data.screenshot) showPickerScreenshot(data.screenshot);
  } catch (err) {
    // ignore
  } finally {
    setPickerLoading(false);
  }
});

pickerRecordBtn.addEventListener("click", async () => {
  if (!previewSessionId) return;
  const durationSeconds = Number(document.getElementById("sr-duration").value);
  const framerate = Number(document.getElementById("sr-framerate").value);
  const id = previewSessionId;
  previewSessionId = null; // the sidecar session is consumed by /record either way
  pickerModal.hidden = true;

  statusBox.innerHTML = `<div class="card status-card">
    <p>Надсилаємо запит...</p>
    <div class="progress"><div class="progress-bar indeterminate"></div></div>
  </div>`;

  try {
    const res = await fetch(`/api/scroll-recorder/preview/${id}/record`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ duration_seconds: durationSeconds, framerate }),
    });
    const data = await res.json();
    if (!res.ok || data.error) {
      statusBox.innerHTML = `<div class="card status-card"><p class="error">${escapeHtml(data.error || data.detail || "Помилка")}</p></div>`;
      return;
    }
    pollStatus(data.job_id);
  } catch (err) {
    statusBox.innerHTML = `<div class="card status-card"><p class="error">Помилка з'єднання</p></div>`;
  }
});

pickerCloseBtn.addEventListener("click", () => {
  pickerModal.hidden = true;
  closePreviewSession();
});
