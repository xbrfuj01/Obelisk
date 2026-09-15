// Obelisk Bridge background service worker.
//
// Two independent jobs share the same polling heartbeat below:
//
// 1. Automatic background-tab flow (fallback for a plain pasted link):
//    poll GET /api/extension/next-job -> open a background tab to that
//    video -> wait for capture.js/relay.js to report a PO token for that
//    tab -> POST /api/extension/po-token -> close the tab.
// 2. The "Завантажити" panel injected on the YouTube page itself
//    (relay.js) starts a job directly via POST /api/extension/download
//    and just tells this file the job id (see the "download-started"
//    listener below) - from there, THIS file owns polling
//    /api/extension/status/<id> and, once finished, saving the file to
//    the user's device via chrome.downloads. Deliberately not the
//    panel's own job: the panel's JS dies if its tab is closed, but a
//    download started from it should still finish and save even then.
//
// Polls two ways: a plain setInterval for fast response whenever the
// worker happens to already be alive, PLUS chrome.alarms as a guaranteed
// floor - MV3 can terminate an idle service worker at any time (nothing
// to do with whether the browser *window* has OS focus - a background
// tab keeps running either way), which silently clears setInterval
// timers. chrome.alarms is redelivered even after the worker was killed
// and gets restarted for the alarm event, so polling can't go silent for
// longer than the alarm period even through a suspension.
const POLL_INTERVAL_MS = 5000;
const ALARM_NAME = "obelisk-bridge-poll";
const TOKEN_WAIT_TIMEOUT_MS = 30000;
// Persisted (not just in-memory) so a job started right before the
// service worker gets killed and restarted isn't silently forgotten.
const PENDING_DOWNLOADS_KEY = "pendingDownloads";

let pollTimer = null;
// job_id -> { tabId, resolve } while a background tab is open and we're
// waiting on capture.js to report back for it.
const pendingJobs = new Map();

async function getConfig() {
  const stored = await chrome.storage.local.get(["serverUrl", "token"]);
  return { serverUrl: stored.serverUrl || "", token: stored.token || "" };
}

async function apiFetch(path, options = {}) {
  const { serverUrl, token } = await getConfig();
  if (!serverUrl || !token) throw new Error("not configured");
  const res = await fetch(serverUrl.replace(/\/$/, "") + path, {
    ...options,
    headers: {
      ...(options.headers || {}),
      Authorization: "Bearer " + token,
      // Lets the admin peers list show which build a connection is
      // running - chrome.storage.local (and so this login) can survive an
      // unpacked-extension reload/update, so this is the only way to spot
      // a row that's quietly still on an old version.
      "X-Extension-Version": chrome.runtime.getManifest().version,
    },
  });
  return res;
}

async function pollOnce() {
  const { serverUrl, token } = await getConfig();
  if (!serverUrl || !token) return;

  let job;
  try {
    const res = await apiFetch("/api/extension/next-job");
    if (!res.ok) return;
    job = await res.json();
  } catch (err) {
    return; // server unreachable this tick - just try again next tick
  }
  if (!job || !job.job_id || pendingJobs.has(job.job_id)) return;

  handleJob(job.job_id, job.url).catch(function () {
    // errors are already logged inside handleJob - nothing more to do
  });
}

function handleJob(jobId, url) {
  return new Promise(function (resolve) {
    chrome.tabs.create({ url, active: false }, function (tab) {
      const entry = { tabId: tab.id, resolve };
      pendingJobs.set(jobId, entry);

      const timeout = setTimeout(function () {
        cleanupJob(jobId);
        resolve();
      }, TOKEN_WAIT_TIMEOUT_MS);

      entry.timeout = timeout;
    });
  });
}

function cleanupJob(jobId) {
  const entry = pendingJobs.get(jobId);
  if (!entry) return;
  clearTimeout(entry.timeout);
  pendingJobs.delete(jobId);
  chrome.tabs.remove(entry.tabId).catch(function () {});
}

chrome.runtime.onMessage.addListener(function (message, sender) {
  if (!message || message.type !== "po-token" || !sender.tab) return;
  // Match the reporting tab back to whichever job we opened it for.
  for (const [jobId, entry] of pendingJobs.entries()) {
    if (entry.tabId !== sender.tab.id) continue;
    apiFetch("/api/extension/po-token", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: "job_id=" + encodeURIComponent(jobId) + "&po_token=" + encodeURIComponent(message.token),
    }).catch(function () {});
    cleanupJob(jobId);
    entry.resolve();
    break;
  }
});

async function getPendingDownloads() {
  const stored = await chrome.storage.local.get([PENDING_DOWNLOADS_KEY]);
  return stored[PENDING_DOWNLOADS_KEY] || [];
}

async function addPendingDownload(jobId) {
  const pending = await getPendingDownloads();
  if (!pending.includes(jobId)) {
    pending.push(jobId);
    await chrome.storage.local.set({ [PENDING_DOWNLOADS_KEY]: pending });
  }
}

async function removePendingDownload(jobId) {
  const pending = await getPendingDownloads();
  const next = pending.filter(function (id) {
    return id !== jobId;
  });
  await chrome.storage.local.set({ [PENDING_DOWNLOADS_KEY]: next });
}

async function checkPendingDownloads() {
  const pending = await getPendingDownloads();
  if (!pending.length) return;
  const { serverUrl, token } = await getConfig();
  if (!serverUrl || !token) return;

  for (const jobId of pending) {
    let status;
    try {
      const res = await apiFetch("/api/extension/status/" + encodeURIComponent(jobId));
      if (!res.ok) continue; // transient error - try again next tick
      status = await res.json();
    } catch (err) {
      continue;
    }
    if (!status || !status.status || status.status === "error") {
      await removePendingDownload(jobId);
      continue;
    }
    if (status.status !== "finished") continue;

    await removePendingDownload(jobId);
    chrome.downloads
      .download({
        url: serverUrl.replace(/\/$/, "") + "/api/extension/file/" + encodeURIComponent(jobId),
        // Reuses the extension's own bearer auth instead of needing a
        // site session cookie - this is the whole point of the flow:
        // the file lands on disk without ever opening the site.
        headers: [{ name: "Authorization", value: "Bearer " + token }],
      })
      .catch(function () {});
  }
}

// The "Завантажити" panel injected on the YouTube page itself (relay.js)
// creates the Download job directly (POST /api/extension/download) and
// only tells this file the resulting id - from here on this file, not
// the panel, owns getting it to the user's device, so the job still
// finishes and saves even if that YouTube tab is closed.
chrome.runtime.onMessage.addListener(function (message) {
  if (!message || message.type !== "download-started" || !message.jobId) return;
  addPendingDownload(message.jobId).then(checkPendingDownloads);
});

function tick() {
  pollOnce();
  checkPendingDownloads();
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(tick, POLL_INTERVAL_MS);
  // periodInMinutes: 1 is the safe, portable minimum (unpacked/dev-mode
  // extensions can go shorter, but this works the same everywhere).
  chrome.alarms.create(ALARM_NAME, { periodInMinutes: 1 });
  tick();
}

function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
  chrome.alarms.clear(ALARM_NAME);
}

chrome.alarms.onAlarm.addListener(function (alarm) {
  if (alarm.name === ALARM_NAME) tick();
});

chrome.storage.local.get(["token"], function (stored) {
  if (stored.token) startPolling();
});

chrome.storage.onChanged.addListener(function (changes, area) {
  if (area !== "local" || !("token" in changes)) return;
  if (changes.token.newValue) startPolling();
  else stopPolling();
});

// Lets the Obelisk site itself (see app/static/extension-detect.js) check
// whether this extension is installed and logged in, via
// chrome.runtime.sendMessage(EXTENSION_ID, ...) from a normal page script -
// allowed cross-origin only because manifest.json's externally_connectable
// whitelists the site's own origin.
chrome.runtime.onMessageExternal.addListener(function (message, sender, sendResponse) {
  if (!message || message.type !== "ping") return;
  chrome.storage.local.get(["token", "username"], function (stored) {
    sendResponse({ ok: !!stored.token, username: stored.username || null });
  });
  return true; // keep the message channel open for the async sendResponse above
});
