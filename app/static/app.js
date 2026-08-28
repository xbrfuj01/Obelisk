const form = document.getElementById("download-form");
const statusBox = document.getElementById("status-box");
const modeRadios = document.querySelectorAll('input[name="mode"]');
const qualityWrap = document.getElementById("quality-wrap");
const containerWrap = document.getElementById("container-wrap");
const containerSelect = document.getElementById("container-select");
const urlInput = document.getElementById("url-input");
const urlStatus = document.getElementById("url-status");
const qualitySelect = document.getElementById("quality-select");
const qualityHint = document.getElementById("quality-hint");
const premiereCompatWrap = document.getElementById("premiere-compat-wrap");
const subtitlesWrap = document.getElementById("subtitles-wrap");
const subtitleSelect = document.getElementById("subtitle-select");
const subtitleHint = document.getElementById("subtitle-hint");

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

function renderQualityOptions() {
  if (!lastQualities.length) return; // keep the server-rendered fallback list until a probe succeeds
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
    const bytes = mode === "video_only" ? q.video_bytes : (q.video_bytes && q.audio_bytes ? q.video_bytes + q.audio_bytes : q.video_bytes);
    const size = formatSize(bytes);
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
    opt.textContent = s.label + (s.auto ? " (авто)" : "");
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

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(form);
  statusBox.innerHTML = '<div class="card status-card"><p>Додаємо у чергу...</p></div>';
  try {
    const res = await fetch("/api/download", { method: "POST", body: fd });
    const data = await res.json();
    if (data.error) {
      statusBox.innerHTML = `<div class="card status-card"><p class="error">${data.error}</p></div>`;
      return;
    }
    pollStatus(data.id);
  } catch (err) {
    statusBox.innerHTML = `<div class="card status-card"><p class="error">Помилка з'єднання</p></div>`;
  }
});

function pollStatus(id) {
  const interval = setInterval(async () => {
    try {
      const res = await fetch(`/api/status/${id}`);
      const job = await res.json();
      if (job.status === "finished") {
        statusBox.innerHTML = `<div class="card status-card"><p class="success">✓ Готово: ${job.title || ""}</p><a class="btn-primary btn-download" href="/api/file/${id}">Завантажити файл</a></div>`;
        clearInterval(interval);
        refreshRecent();
      } else if (job.status === "error") {
        statusBox.innerHTML = `<div class="card status-card"><p class="error">Помилка завантаження: ${job.error || "невідома помилка"}</p></div>`;
        clearInterval(interval);
        refreshRecent();
      } else {
        const progress = job.progress || 0;
        statusBox.innerHTML = `<div class="card status-card">
          <p>Статус: ${job.status} (${progress}%)</p>
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

function renderRow(r) {
  const btn =
    r.status === "finished"
      ? `<a class="dl-download-btn" href="/api/file/${r.id}">завантажити</a>`
      : `<span class="dl-download-btn disabled">завантажити</span>`;
  return `
    <div class="dl-row" data-id="${r.id}">
      <span class="status-icon status-${r.status}">${statusIcon(r.status)}</span>
      ${btn}
      <button type="button" class="dl-title">${r.title}</button>
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
