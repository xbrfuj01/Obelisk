const form = document.getElementById("download-form");
const statusBox = document.getElementById("status-box");
const modeRadios = document.querySelectorAll('input[name="mode"]');
const qualityWrap = document.getElementById("quality-wrap");
const containerWrap = document.getElementById("container-wrap");
const containerSelect = document.getElementById("container-select");
const urlInput = document.getElementById("url-input");
const urlStatus = document.getElementById("url-status");
const urlClearBtn = document.getElementById("url-clear-btn");
const qualitySelect = document.getElementById("quality-select");
const qualityHint = document.getElementById("quality-hint");
const premiereCompatWrap = document.getElementById("premiere-compat-wrap");
const subtitlesWrap = document.getElementById("subtitles-wrap");
const subtitleSelect = document.getElementById("subtitle-select");
const subtitleHint = document.getElementById("subtitle-hint");
const clipStartInput = document.getElementById("clip-start-input");
const clipEndInput = document.getElementById("clip-end-input");
const clipHint = document.getElementById("clip-hint");
const advancedToggle = document.getElementById("advanced-toggle");
const advancedWrap = document.getElementById("advanced-wrap");

const CLIP_START_DEFAULT = "00:00:00";
const CLIP_END_DEFAULT = "99:99:99";
const CLIP_HINT_DEFAULT = clipHint ? clipHint.textContent : "";
const CLIP_HINT_ERROR = 'Некоректний таймкод — хвилини та секунди мають бути від 0 до 59 (напр. 00:14:48)';

// Only the leftmost (hours) part is unbounded — minutes/seconds must be < 60,
// mirroring the server-side check in parse_timecode() so bad input like
// "00:61:67" gets flagged immediately instead of only failing on submit.
function isClipFieldValid(value) {
  const parts = value.split(":");
  if (parts.length > 3) return false;
  const nums = parts.map(Number);
  if (parts.some((p) => p.trim() === "") || nums.some((n) => Number.isNaN(n) || n < 0)) return false;
  return nums.slice(1).every((n) => n < 60);
}

function validateClipField(input, defaultValue) {
  const v = input.value.trim();
  const ok = v === "" || v === defaultValue || isClipFieldValid(v);
  input.classList.toggle("field-invalid", !ok);
  return ok;
}

function updateClipValidity() {
  const startOk = validateClipField(clipStartInput, CLIP_START_DEFAULT);
  const endOk = validateClipField(clipEndInput, CLIP_END_DEFAULT);
  const allOk = startOk && endOk;
  if (clipHint) {
    clipHint.textContent = allOk ? CLIP_HINT_DEFAULT : CLIP_HINT_ERROR;
    clipHint.classList.toggle("hint-error", !allOk);
  }
  return allOk;
}

const CLIP_PASTE_INPUT_TYPES = new Set(["insertFromPaste", "insertFromPasteAsQuotation", "insertFromDrop"]);

// While the user types digits by hand, auto-insert a colon after every pair
// (0014 -> 00:14 -> 00:14:4...) so they never have to hit ":" themselves.
// Pasted/dropped text is left as-is — it's usually already formatted, and
// reformatting it would just fight the user.
function autoFormatClipInput(e) {
  const input = e.target;
  if (CLIP_PASTE_INPUT_TYPES.has(e.inputType)) {
    updateClipValidity();
    return;
  }
  const digits = input.value.replace(/\D/g, "").slice(0, 6);
  const groups = [];
  for (let i = 0; i < digits.length; i += 2) groups.push(digits.slice(i, i + 2));
  input.value = groups.join(":");
  updateClipValidity();
}

if (clipStartInput && clipEndInput) {
  clipStartInput.addEventListener("input", autoFormatClipInput);
  clipEndInput.addEventListener("input", autoFormatClipInput);
}

if (advancedToggle) {
  advancedToggle.addEventListener("change", () => {
    advancedWrap.hidden = !advancedToggle.checked;
  });
}

const VIDEO_FORMAT_OPTIONS = [
  { value: "mp4", label: "MP4" },
  { value: "webm", label: "WebM" },
  { value: "mkv", label: "MKV" },
];
const AUDIO_FORMAT_OPTIONS = [
  { value: "mp3", label: "MP3" },
  { value: "m4a", label: "M4A" },
  { value: "opus", label: "Opus" },
  { value: "wav", label: "WAV" },
];

function formatSize(bytes) {
  if (!bytes) return null;
  const mb = bytes / 1048576;
  if (mb >= 1024) return (mb / 1024).toFixed(1) + " ГБ";
  return Math.round(mb) + " МБ";
}

function currentMode() {
  const checked = document.querySelector('input[name="mode"]:checked');
  return checked ? checked.value : "video";
}

let lastQualities = [];

function qualityBytes(q, mode) {
  if (mode === "video_only") return q.video_bytes;
  if (q.video_bytes && q.audio_bytes) return q.video_bytes + q.audio_bytes;
  return q.video_bytes;
}

function estimatedBytesForSelection() {
  const mode = currentMode();
  if (mode === "audio") return null;
  const q = lastQualities.find((q) => q.value === qualitySelect.value);
  return q ? qualityBytes(q, mode) : null;
}

function renderQualityOptions() {
  if (!lastQualities.length) return; // stay on just "Найкраща доступна" until a probe succeeds
  const mode = currentMode();
  const current = qualitySelect.value;
  qualitySelect.innerHTML = "";

  const bestOpt = document.createElement("option");
  bestOpt.value = "best";
  bestOpt.textContent = "Найкраща доступна";
  qualitySelect.appendChild(bestOpt);

  lastQualities.forEach((q) => {
    const opt = document.createElement("option");
    opt.value = q.value;
    let label = q.label;
    const size = formatSize(qualityBytes(q, mode));
    if (size) label += ` — ~${size}`;
    opt.textContent = label;
    qualitySelect.appendChild(opt);
  });

  const values = Array.from(qualitySelect.options).map((o) => o.value);
  qualitySelect.value = values.includes(current) ? current : "best";
}

let lastSubtitles = [];

function renderSubtitleOptions() {
  const current = subtitleSelect.value;
  subtitleSelect.innerHTML = "";

  const noneOpt = document.createElement("option");
  noneOpt.value = "";
  noneOpt.textContent = "Без субтитрів";
  subtitleSelect.appendChild(noneOpt);

  lastSubtitles.forEach((s) => {
    const opt = document.createElement("option");
    opt.value = s.code;
    opt.textContent = s.label + (s.auto ? " (авто)" : " (оригінал)");
    subtitleSelect.appendChild(opt);
  });

  const values = Array.from(subtitleSelect.options).map((o) => o.value);
  subtitleSelect.value = values.includes(current) ? current : "";
}

function updateSubtitleAvailability() {
  if (!subtitleSelect) return;
  const embeddable = containerSelect.value === "mp4" || containerSelect.value === "mkv";
  subtitleSelect.disabled = !embeddable;
  if (!embeddable) {
    subtitleSelect.value = "";
    if (subtitleHint) subtitleHint.textContent = "Субтитри вшиваються лише для MP4/MKV — оберіть інший формат файлу.";
  } else if (lastSubtitles.length) {
    if (subtitleHint) subtitleHint.textContent = `Знайдено ${lastSubtitles.length} мов(и) субтитрів для цього відео.`;
  } else if (subtitleHint) {
    subtitleHint.textContent = "Встав посилання, щоб побачити доступні мови. Вшиваються у файл — лише для MP4/MKV.";
  }
}
containerSelect.addEventListener("change", updateSubtitleAvailability);

function updateVisibility() {
  const mode = currentMode();
  qualityWrap.style.display = mode === "audio" ? "none" : "";
  if (premiereCompatWrap) premiereCompatWrap.style.display = mode === "audio" ? "none" : "";
  if (subtitlesWrap) subtitlesWrap.style.display = mode === "audio" ? "none" : "";
  renderQualityOptions();

  const options = mode === "audio" ? AUDIO_FORMAT_OPTIONS : VIDEO_FORMAT_OPTIONS;
  const current = containerSelect.value;
  containerSelect.innerHTML = "";
  options.forEach((o) => {
    const opt = document.createElement("option");
    opt.value = o.value;
    opt.textContent = o.label;
    containerSelect.appendChild(opt);
  });
  const values = options.map((o) => o.value);
  containerSelect.value = values.includes(current) ? current : options[0].value;

  updateSubtitleAvailability();
}
modeRadios.forEach((r) => r.addEventListener("change", updateVisibility));
updateVisibility();

let lastProbedUrl = "";
let probeTimer = null;

async function probeQualities() {
  const url = urlInput.value.trim();
  if (!url || !url.startsWith("http") || url === lastProbedUrl) return;
  lastProbedUrl = url;

  urlStatus.innerHTML = '<span class="spinner"></span>';
  urlStatus.className = "url-status loading";
  qualityHint.textContent = "Отримуємо список доступних роздільних здатностей...";

  try {
    const res = await fetch(`/api/formats?url=${encodeURIComponent(url)}`);
    const data = await res.json();

    if (data.error || !data.qualities || !data.qualities.length) {
      urlStatus.innerHTML = "";
      qualityHint.textContent = "Не вдалося визначити якості для цього посилання — буде використано найкращу доступну.";
      return;
    }

    lastQualities = data.qualities;
    lastSubtitles = data.subtitles || [];
    renderQualityOptions();
    renderSubtitleOptions();
    updateSubtitleAvailability();

    urlStatus.innerHTML = "✓";
    urlStatus.className = "url-status ok";
    qualityHint.textContent = `Знайдено ${data.qualities.length} варіант(ів) якості для цього відео.`;
  } catch (err) {
    urlStatus.innerHTML = "";
    qualityHint.textContent = "Не вдалося зв'язатись із сервером для перевірки якостей.";
  }
}

urlInput.addEventListener("blur", probeQualities);
urlInput.addEventListener("paste", () => {
  clearTimeout(probeTimer);
  probeTimer = setTimeout(probeQualities, 200);
});
urlInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    probeQualities();
  }
});

function updateUrlClearButton() {
  if (urlClearBtn) urlClearBtn.hidden = !urlInput.value;
}
urlInput.addEventListener("input", updateUrlClearButton);
updateUrlClearButton();

if (urlClearBtn) {
  urlClearBtn.addEventListener("click", () => {
    urlInput.value = "";
    lastProbedUrl = "";
    lastQualities = [];
    lastSubtitles = [];
    qualitySelect.innerHTML = '<option value="best">Найкраща доступна</option>';
    renderSubtitleOptions();
    updateSubtitleAvailability();
    urlStatus.innerHTML = "";
    urlStatus.className = "url-status";
    qualityHint.textContent = "Встав посилання, щоб побачити реальні доступні роздільні здатності (в т.ч. 4K/8K)";
    updateUrlClearButton();
    urlInput.focus();
  });
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!updateClipValidity()) {
    statusBox.innerHTML = `<div class="card status-card"><p class="error">${CLIP_HINT_ERROR}</p></div>`;
    return;
  }
  const estimatedBytes = estimatedBytesForSelection();
  const fd = new FormData(form);
  const clipStart = clipStartInput.value.trim();
  const clipEnd = clipEndInput.value.trim();
  fd.set("clip_start", clipStart === CLIP_START_DEFAULT ? "" : clipStart);
  fd.set("clip_end", clipEnd === CLIP_END_DEFAULT ? "" : clipEnd);
  const isClipped = Boolean(fd.get("clip_start")) || Boolean(fd.get("clip_end"));
  statusBox.innerHTML = '<div class="card status-card"><p>Додаємо у чергу...</p></div>';
  try {
    const res = await fetch("/api/download", { method: "POST", body: fd });
    const data = await res.json();
    if (data.error) {
      statusBox.innerHTML = `<div class="card status-card"><p class="error">${data.error}</p></div>`;
      return;
    }
    pollStatus(data.id, estimatedBytes, isClipped);
  } catch (err) {
    statusBox.innerHTML = `<div class="card status-card"><p class="error">Помилка з'єднання</p></div>`;
  }
});

const STATUS_LABELS = {
  queued: "У черзі",
  downloading: "Підготовка",
};

const DOWNLOAD_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>';

// Kicks off the browser's own download for the finished file without
// navigating away from the page, so the user gets it on their device
// automatically instead of having to click the button themselves.
function triggerAutoDownload(id) {
  const a = document.createElement("a");
  a.href = `/api/file/${id}`;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function pollStatus(id, estimatedBytes, isClipped) {
  const estimatedSize = formatSize(estimatedBytes);
  const interval = setInterval(async () => {
    try {
      const res = await fetch(`/api/status/${id}`);
      const job = await res.json();
      if (job.status === "finished") {
        const finalSize = formatSize(job.filesize) || estimatedSize;
        triggerAutoDownload(id);
        statusBox.innerHTML = `<div class="card status-card"><p class="success">✓ Готово: ${job.title || ""}${finalSize ? ` (${finalSize})` : ""}</p><a class="btn-download" href="/api/file/${id}">${DOWNLOAD_ICON} Завантажити ще раз</a></div>`;
        clearInterval(interval);
        refreshRecent();
      } else if (job.status === "error") {
        statusBox.innerHTML = `<div class="card status-card"><p class="error">Помилка завантаження: ${job.error || "невідома помилка"}</p></div>`;
        clearInterval(interval);
        refreshRecent();
      } else if (isClipped) {
        // yt-dlp never reports incremental progress while cutting a clip —
        // only a single event once it's fully done — so a real percentage
        // would just sit frozen. Show an indeterminate bar instead.
        statusBox.innerHTML = `<div class="card status-card">
          <p>Статус: Підготовка фрагмента... (це може тривати довше за звичайне завантаження)</p>
          <div class="progress"><div class="progress-bar indeterminate"></div></div>
        </div>`;
      } else {
        const progress = job.progress || 0;
        const label = STATUS_LABELS[job.status] || job.status;
        statusBox.innerHTML = `<div class="card status-card">
          <p>Статус: ${label} (${progress}%)${estimatedSize ? ` — орієнтовно ~${estimatedSize}` : ""}</p>
          <div class="progress"><div class="progress-bar" style="width:${progress}%"></div></div>
        </div>`;
      }
    } catch (err) {
      clearInterval(interval);
    }
  }, 1500);
}

function statusIcon(status) {
  if (status === "finished") return "✓";
  if (status === "error") return "✕";
  if (status === "downloading") return '<span class="spinner"></span>';
  if (status === "queued") return "⏳";
  return "–";
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

function renderRow(r) {
  const btn =
    r.status === "finished"
      ? `<a class="dl-download-btn" href="/api/file/${r.id}" title="Завантажити">${DOWNLOAD_ICON}</a>`
      : `<span class="dl-download-btn disabled" title="Ще не готово">${DOWNLOAD_ICON}</span>`;
  const size = formatSize(r.filesize);
  const title = size ? `${r.title} (${size})` : r.title;
  const sourceLink = r.url
    ? `<a class="dl-source-btn" href="${escapeHtml(r.url)}" target="_blank" rel="noopener noreferrer" title="Відкрити оригінал">🔍</a>`
    : "";
  return `
    <div class="dl-row" data-id="${r.id}">
      <span class="status-icon status-${r.status}">${statusIcon(r.status)}</span>
      <button type="button" class="dl-title">${escapeHtml(title)}</button>
      ${sourceLink}
      ${btn}
    </div>`;
}

async function refreshRecent() {
  try {
    const res = await fetch("/api/recent");
    const rows = await res.json();
    const list = document.getElementById("recent-list");
    if (!list) return;
    if (!rows.length) {
      list.innerHTML = `<p class="empty-hint">Ще немає завантажень</p>`;
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
