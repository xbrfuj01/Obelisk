// Obelisk Bridge background service worker.
//
// Flow: poll GET /api/extension/next-job -> open a background tab to that
// video -> wait for content-script.js/capture.js's relay to report a PO
// token for that tab -> POST /api/extension/po-token -> close the tab.
//
// Polling every few seconds via setInterval, not chrome.alarms - simpler,
// and MV3 service workers stay alive while there's other activity in the
// browser, which matches how this is meant to be used (the person is
// actively at their computer when they submit a download, per the
// project's own design notes). A more bulletproof version would add
// chrome.alarms as a wake-up safety net, but that's not needed for v1.

const POLL_INTERVAL_MS = 5000;
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

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(pollOnce, POLL_INTERVAL_MS);
  pollOnce();
}

function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
}

chrome.storage.local.get(["token"], function (stored) {
  if (stored.token) startPolling();
});

chrome.storage.onChanged.addListener(function (changes, area) {
  if (area !== "local" || !("token" in changes)) return;
  if (changes.token.newValue) startPolling();
  else stopPolling();
});
