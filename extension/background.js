// Obelisk Bridge background service worker.
//
// Flow: poll GET /api/extension/next-job -> open a background tab to that
// video -> wait for content-script.js/capture.js's relay to report a PO
// token for that tab -> POST /api/extension/po-token -> close the tab.
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
    headers: { ...(options.headers || {}), Authorization: "Bearer " + token },
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

// The "Завантажити" button relay.js injects into the YouTube page itself
// (next to the logo, on watch/Shorts pages) sends this instead of going
// through the poll/hidden-tab dance above - the token it carries (if any)
// was already captured from a real, actively-watched foreground tab, so
// there's nothing to wait for here beyond the one request below.
chrome.runtime.onMessage.addListener(function (message, sender, sendResponse) {
  if (!message || message.type !== "download-request") return;
  (async function () {
    try {
      const { serverUrl } = await getConfig();
      if (!serverUrl) throw new Error("not configured");
      const res = await apiFetch("/api/extension/prefill", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: "url=" + encodeURIComponent(message.url) + "&po_token=" + encodeURIComponent(message.token || ""),
      });
      if (!res.ok) throw new Error("prefill failed");
      const data = await res.json();
      if (!data.id) throw new Error("no prefill id");
      await chrome.tabs.create({
        url: serverUrl.replace(/\/$/, "") + "/downloader?prefill=" + encodeURIComponent(data.id),
        active: true,
      });
      sendResponse({ ok: true });
    } catch (err) {
      sendResponse({ ok: false });
    }
  })();
  return true; // keep the message channel open for the async work above
});

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(pollOnce, POLL_INTERVAL_MS);
  // periodInMinutes: 1 is the safe, portable minimum (unpacked/dev-mode
  // extensions can go shorter, but this works the same everywhere).
  chrome.alarms.create(ALARM_NAME, { periodInMinutes: 1 });
  pollOnce();
}

function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
  chrome.alarms.clear(ALARM_NAME);
}

chrome.alarms.onAlarm.addListener(function (alarm) {
  if (alarm.name === ALARM_NAME) pollOnce();
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
