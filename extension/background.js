// Obelisk Bridge background service worker.
//
// Two independent jobs share the same polling heartbeat below:
//
// 1. Automatic background-tab flow (fallback for a plain pasted link):
//    poll GET /api/extension/next-job -> open a background tab to that
//    video -> wait for capture.js/relay.js to report a PO token for that
//    tab -> POST /api/extension/po-token -> close the tab.
// 2. The "Завантажити" panel injected on the YouTube page itself
//    (relay.js) can't call the server directly (a content script's
//    fetch() is bound by youtube.com's own CORS policy) - it messages
//    this file to create the job (POST /api/extension/download) and from
//    there, THIS file owns polling /api/extension/status/<id> and, once
//    finished, saving the file to the user's device via
//    chrome.downloads. Deliberately not the panel's own job: the panel's
//    JS dies if its tab is closed, but a download started from it should
//    still finish and save even then.
//
// Every tracked download's full state (status/progress/title/error) lives
// in chrome.storage.local under TASKS_KEY, keyed by job id - the single
// source of truth both the in-page panel (relay.js, while it's still
// open) and the toolbar popup (popup.js) render from via
// chrome.storage.onChanged, instead of each polling the server
// themselves. This is also what lets the popup show "current tasks" even
// after the panel that started a download has been closed.
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
const TASKS_KEY = "obeliskTasks";
// Terminal states are kept around (not deleted) so the popup can still
// show "Збережено"/"Помилка" after the fact - only capped in count, not
// time, to keep this simple. Oldest terminal entries are dropped first
// once the cap is hit.
const MAX_TASKS = 20;
const TERMINAL_STATUSES = ["saved", "save_error", "error"];
const ACTIVE_STATUSES = ["queued", "waiting_extension", "downloading", "finished"];

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

// ---- Task tracking (panel-submitted downloads) ----

async function getTasks() {
  const stored = await chrome.storage.local.get([TASKS_KEY]);
  return stored[TASKS_KEY] || {};
}

async function saveTasks(tasks) {
  await chrome.storage.local.set({ [TASKS_KEY]: tasks });
}

async function upsertTask(jobId, patch) {
  const tasks = await getTasks();
  tasks[jobId] = Object.assign(
    { id: jobId, createdAt: Date.now() },
    tasks[jobId] || {},
    patch,
    { updatedAt: Date.now() }
  );
  const ids = Object.keys(tasks);
  if (ids.length > MAX_TASKS) {
    const terminal = ids
      .filter(function (id) {
        return TERMINAL_STATUSES.indexOf(tasks[id].status) !== -1;
      })
      .sort(function (a, b) {
        return (tasks[a].updatedAt || 0) - (tasks[b].updatedAt || 0);
      });
    while (Object.keys(tasks).length > MAX_TASKS && terminal.length) {
      delete tasks[terminal.shift()];
    }
  }
  await saveTasks(tasks);
  return tasks[jobId];
}

// Guards against a real, observed bug: pollTasks() can run concurrently
// (the setInterval tick, the chrome.alarms fallback, and the immediate
// call right after a job is created can all overlap once any one of them
// is mid-await on a slow network request), and two overlapping calls
// could both observe status==="finished" for the same job before either
// had written back its own update - each independently calling
// chrome.downloads.download() and saving the same file to disk twice.
// Claiming a job id here happens synchronously (no await beforehand), so
// whichever invocation's turn runs first always wins the claim before a
// second concurrent invocation's loop reaches the same job - JS's
// single-threaded event loop guarantees that.
const finalizingJobs = new Set();

async function pollTasks() {
  const tasks = await getTasks();
  const idsToCheck = Object.keys(tasks).filter(function (id) {
    return ACTIVE_STATUSES.indexOf(tasks[id].status) !== -1 && !finalizingJobs.has(id);
  });
  if (!idsToCheck.length) return;
  const { serverUrl, token } = await getConfig();
  if (!serverUrl || !token) return;

  for (const jobId of idsToCheck) {
    finalizingJobs.add(jobId);
    let releaseClaim = true;
    try {
      let status;
      try {
        const res = await apiFetch("/api/extension/status/" + encodeURIComponent(jobId));
        if (!res.ok) continue; // transient error - try again next tick
        status = await res.json();
      } catch (err) {
        continue;
      }
      if (!status || !status.status) continue;

      if (status.status === "error") {
        await upsertTask(jobId, {
          status: "error",
          error: status.error || "Помилка завантаження",
          title: status.title || tasks[jobId].title,
        });
        continue;
      }
      if (status.status !== "finished") {
        await upsertTask(jobId, {
          status: status.status,
          progress: status.progress,
          etaSeconds: status.eta_seconds,
          title: status.title || tasks[jobId].title,
        });
        continue;
      }

      // Server-side download is done - hand it to chrome.downloads so it
      // lands on the user's device without ever opening the site. Keep
      // this job claimed until the save itself resolves, so no other
      // overlapping poll can re-trigger it in the meantime.
      releaseClaim = false;
      await upsertTask(jobId, { status: "finished", progress: 100, title: status.title || tasks[jobId].title });
      const fileUrl = serverUrl.replace(/\/$/, "") + "/api/extension/file/" + encodeURIComponent(jobId);
      chrome.downloads.download(
        {
          url: fileUrl,
          // Reuses the extension's own bearer auth instead of needing a
          // site session cookie - this is the whole point of the flow: the
          // file lands on disk without ever opening the site.
          headers: [{ name: "Authorization", value: "Bearer " + token }],
        },
        function (downloadId) {
          finalizingJobs.delete(jobId);
          if (chrome.runtime.lastError) {
            upsertTask(jobId, { status: "save_error", error: chrome.runtime.lastError.message });
          } else {
            upsertTask(jobId, { status: "saved", downloadId: downloadId });
          }
        }
      );
    } finally {
      if (releaseClaim) finalizingJobs.delete(jobId);
    }
  }
}

// The "Завантажити" panel injected on the YouTube page itself (relay.js)
// can't call the server directly - a content script's fetch() is subject
// to the *page's* CORS policy (youtube.com has no Access-Control-Allow-
// Origin for obelisk.o4.co.ua, so every such call was silently failing as
// "Не вдалося з'єднатися з сервером"). Only this background context's own
// fetch() is exempt from CORS, via manifest.json's host_permissions - so
// both server calls the panel needs go through messages here instead.

chrome.runtime.onMessage.addListener(function (message, sender, sendResponse) {
  if (!message || message.type !== "fetch-formats") return;
  (async function () {
    try {
      const res = await apiFetch("/api/extension/formats?url=" + encodeURIComponent(message.url));
      const data = await res.json();
      sendResponse({ ok: res.ok, data: data });
    } catch (err) {
      sendResponse({ ok: false });
    }
  })();
  return true;
});

// Creates the Download job directly (POST /api/extension/download) and,
// on success, starts tracking it in TASKS_KEY - from here on this file,
// not the panel, owns getting it to the user's device, so the job still
// finishes and saves even if that YouTube tab is closed, and its progress
// stays visible in the toolbar popup regardless of whether the panel that
// started it is still open.
chrome.runtime.onMessage.addListener(function (message, sender, sendResponse) {
  if (!message || message.type !== "start-download") return;
  (async function () {
    try {
      const res = await apiFetch("/api/extension/download", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams(message.fields).toString(),
      });
      const data = await res.json();
      if (res.ok && data.id) {
        await upsertTask(data.id, { status: "queued", progress: 0, url: message.fields.url });
        pollTasks();
      }
      sendResponse({ ok: res.ok && !data.error, data: data });
    } catch (err) {
      sendResponse({ ok: false });
    }
  })();
  return true;
});

// Lets the toolbar popup force an immediate status refresh on open,
// instead of showing whatever's left over from the last periodic tick -
// the service worker may have been asleep for a while before the popup
// woke it back up.
chrome.runtime.onMessage.addListener(function (message, sender, sendResponse) {
  if (!message || message.type !== "poll-now") return;
  pollTasks().then(function () {
    sendResponse({ ok: true });
  });
  return true;
});

function tick() {
  pollOnce();
  pollTasks();
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
