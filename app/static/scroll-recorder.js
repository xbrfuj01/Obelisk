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
    speed: fd.get("speed"),
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
