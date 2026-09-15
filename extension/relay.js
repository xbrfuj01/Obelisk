// Runs in the isolated content-script world (the default one, has
// chrome.* access) alongside capture.js's page-world script. window.
// postMessage is the only channel between the two - capture.js can't call
// chrome.runtime.sendMessage itself since MAIN-world scripts have no
// extension APIs at all.
(function () {
  "use strict";

  // Matched by video id, not the exact location.href string - YouTube
  // routinely grows the url with extra query params (a timestamp after
  // seeking, &si=, playlist context, ...) without firing a real
  // navigation, which made an exact-string comparison here go stale on a
  // real page in testing even though it was still genuinely the same
  // video being watched.
  function videoIdFromUrl(url) {
    try {
      const parsed = new URL(url);
      if (parsed.pathname === "/watch") return parsed.searchParams.get("v");
      if (parsed.pathname.startsWith("/shorts/")) return parsed.pathname.slice("/shorts/".length);
    } catch (err) {
      // ignore
    }
    return null;
  }

  // Last token/quality-list capture.js reported, and which video each was
  // for - reset on every SPA navigation (see yt-navigate-finish below) so
  // data from a previous video is never mistaken for the current one.
  let latestToken = null;
  let latestTokenVideoId = null;
  let latestQualities = null;
  let latestQualitiesVideoId = null;

  window.addEventListener("message", function (event) {
    if (event.source !== window) return;
    const data = event.data;
    if (!data || !data.__obeliskBridge) return;

    if (data.type === "po-token") {
      latestToken = data.token;
      latestTokenVideoId = videoIdFromUrl(location.href);
      console.log("[Obelisk] relay.js cached token for", location.href, "via", data.source);
      chrome.runtime.sendMessage({
        type: "po-token",
        token: data.token,
        url: location.href,
      });
    } else if (data.type === "qualities") {
      // Purely local - only ever read from here when the panel below is
      // opened, never sent to the server on its own.
      latestQualities = data.qualities;
      latestQualitiesVideoId = videoIdFromUrl(location.href);
      console.log("[Obelisk] relay.js cached qualities for", location.href, data.qualities);
    }
  });

  // -------- On-page "Завантажити" button + configuration panel --------
  // Injected next to the YouTube logo, video (watch) and Shorts pages
  // only. Clicking it opens a small panel right there with the same
  // options the site's own download form has (mode/quality/format/
  // subtitles/timecodes/auto-convert) - confirming starts the job
  // directly (POST /api/extension/download, bearer-authed) and hands it
  // off to background.js, which polls it to completion and saves the
  // finished file straight to disk via chrome.downloads. The whole point
  // is never having to open the Obelisk site at all.
  //
  // (Investigated and ruled out: reusing the extension's own toolbar
  // popup via chrome.action.openPopup() from background.js - Chrome only
  // honors that call with an actual user-gesture flag, which does not
  // survive a chrome.runtime.sendMessage hop from a content script.)

  const BTN_ID = "obelisk-bridge-download-btn";
  const PANEL_ID = "obelisk-bridge-panel";

  function isVideoPage() {
    return location.pathname === "/watch" || location.pathname.startsWith("/shorts/");
  }

  function removePanel() {
    const existing = document.getElementById(PANEL_ID);
    if (existing) existing.remove();
  }

  function qualityOptionsHtml(qualities) {
    let html = '<option value="best">Найкраща доступна</option>';
    if (qualities && qualities.length) {
      qualities.forEach(function (q) {
        html += '<option value="' + q.value + '">' + q.label + "</option>";
      });
    }
    return html;
  }

  async function getStoredConfig() {
    const stored = await chrome.storage.local.get(["serverUrl", "token"]);
    return { serverUrl: stored.serverUrl || "", token: stored.token || "" };
  }

  function loadSubtitlesInto(select, url) {
    // Routed through background.js, not fetched here directly - a content
    // script's fetch() is bound by youtube.com's own CORS policy (which
    // has no allowance for obelisk.o4.co.ua), only the background
    // context's fetch is exempt from that, via host_permissions.
    chrome.runtime.sendMessage({ type: "fetch-formats", url: url }, function (response) {
      select.innerHTML = '<option value="">Без субтитрів</option>';
      if (response && response.ok && response.data && Array.isArray(response.data.subtitles)) {
        response.data.subtitles.forEach(function (s) {
          const opt = document.createElement("option");
          opt.value = s.code;
          opt.textContent = s.label + (s.auto ? " (авто)" : "");
          select.appendChild(opt);
        });
      }
    });
  }

  function submitDownload(panel, url, tokenForThisVideo) {
    const confirmBtn = panel.querySelector(".obelisk-confirm-btn");
    const statusEl = panel.querySelector(".obelisk-status");
    confirmBtn.disabled = true;
    statusEl.hidden = false;
    statusEl.textContent = "Надсилаємо...";

    const fields = {
      url: url,
      mode: panel.querySelector(".obelisk-mode").value,
      quality: panel.querySelector(".obelisk-quality").value,
      container: panel.querySelector(".obelisk-container").value,
      subtitle_lang: panel.querySelector(".obelisk-subtitles").value,
      premiere_compat: panel.querySelector(".obelisk-premiere").checked ? "true" : "false",
      clip_start: panel.querySelector(".obelisk-clip-start").value,
      clip_end: panel.querySelector(".obelisk-clip-end").value,
      po_token: tokenForThisVideo || "",
    };

    // background.js both creates the job (same CORS reason as above) and,
    // from here on, owns getting it to the user's device - it'll keep
    // polling and auto-save even if this tab is switched away from or
    // closed outright.
    chrome.runtime.sendMessage({ type: "start-download", fields: fields }, function (response) {
      if (chrome.runtime.lastError || !response || !response.ok) {
        const err = response && response.data && response.data.error;
        statusEl.textContent = err || "Не вдалося з'єднатися з сервером";
        confirmBtn.disabled = false;
        return;
      }
      statusEl.textContent = "Завантаження розпочато на сервері - можна переходити на іншу вкладку.";
      setTimeout(removePanel, 4000);
    });
  }

  async function openPanel() {
    removePanel();
    const url = location.href;
    const videoId = videoIdFromUrl(url);

    const { serverUrl, token } = await getStoredConfig();
    if (!serverUrl || !token) {
      alert("Спершу увійдіть у розширення Obelisk Bridge через його іконку в панелі браузера.");
      return;
    }

    // No artificial wait here for a browser-captured PO token - real
    // extraction from page network traffic stopped working reliably for
    // YouTube's current player (confirmed: never once observed across
    // several real test videos), so the SABR fork's own bgutil-based token
    // generation is what actually delivers full quality in practice. A
    // multi-second wait here was only ever waiting on something that
    // (almost) never arrives, and it was blocking the panel from opening -
    // felt like the button itself was slow to respond. If a token happens
    // to already be cached for this exact video, use it; otherwise submit
    // without one and let the server-side bgutil fallback handle it.
    const tokenForThisVideo = latestTokenVideoId === videoId ? latestToken : null;
    const qualitiesForThisVideo = latestQualitiesVideoId === videoId ? latestQualities : null;

    const panel = document.createElement("div");
    panel.id = PANEL_ID;
    panel.innerHTML =
      '<div class="obelisk-panel-header"><span>Obelisk</span>' +
      '<button type="button" class="obelisk-panel-close" aria-label="Закрити">×</button></div>' +
      '<div class="obelisk-panel-body">' +
      '<label class="obelisk-field"><span>Тип завантаження</span>' +
      '<select class="obelisk-mode">' +
      '<option value="video">Відео + аудіо</option>' +
      '<option value="video_only">Лише відео</option>' +
      '<option value="audio">Лише аудіо</option>' +
      "</select></label>" +
      '<label class="obelisk-field obelisk-quality-field"><span>Якість</span>' +
      '<select class="obelisk-quality">' + qualityOptionsHtml(qualitiesForThisVideo) + "</select></label>" +
      '<label class="obelisk-field"><span>Формат файлу</span>' +
      '<select class="obelisk-container"><option value="mp4">MP4</option><option value="webm">WebM</option><option value="mkv">MKV</option></select></label>' +
      '<label class="obelisk-field"><span>Субтитри</span>' +
      '<select class="obelisk-subtitles"><option value="">Завантаження...</option></select></label>' +
      '<div class="obelisk-clip-row">' +
      '<label class="obelisk-field"><span>Початок</span><input type="text" class="obelisk-clip-start" placeholder="00:00:00"></label>' +
      '<label class="obelisk-field"><span>Кінець</span><input type="text" class="obelisk-clip-end" placeholder="99:99:99"></label>' +
      "</div>" +
      '<label class="obelisk-checkbox-row"><input type="checkbox" class="obelisk-premiere" checked>' +
      "<span>Сумісність з відеоредакторами</span></label>" +
      '<p class="obelisk-status" hidden></p>' +
      '<button type="button" class="obelisk-confirm-btn">Підтвердити завантаження</button>' +
      "</div>";
    document.body.appendChild(panel);

    panel.querySelector(".obelisk-panel-close").addEventListener("click", removePanel);

    const modeSelect = panel.querySelector(".obelisk-mode");
    const qualityField = panel.querySelector(".obelisk-quality-field");
    function updateFieldsForMode() {
      qualityField.hidden = modeSelect.value === "audio";
    }
    modeSelect.addEventListener("change", updateFieldsForMode);
    updateFieldsForMode();

    loadSubtitlesInto(panel.querySelector(".obelisk-subtitles"), url);

    panel.querySelector(".obelisk-confirm-btn").addEventListener("click", function () {
      submitDownload(panel, url, tokenForThisVideo);
    });
  }

  function onButtonClick() {
    if (document.getElementById(PANEL_ID)) {
      removePanel();
      return;
    }
    openPanel();
  }

  function makeButton() {
    const btn = document.createElement("button");
    btn.id = BTN_ID;
    btn.type = "button";
    btn.title = "Завантажити через Obelisk";
    btn.innerHTML =
      '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg><span>Obelisk</span>';
    btn.addEventListener("click", onButtonClick);
    return btn;
  }

  function findLogoAnchor() {
    // #logo is the id YouTube's own masthead logo link has used across
    // redesigns for years (ytd-topbar-logo-renderer#logo).
    return document.querySelector("#logo");
  }

  function ensureButton() {
    const existing = document.getElementById(BTN_ID);
    if (!isVideoPage()) {
      if (existing) existing.remove();
      return;
    }
    if (existing) return;
    const logo = findLogoAnchor();
    if (!logo || !logo.parentNode) return;
    logo.parentNode.insertBefore(makeButton(), logo.nextSibling);
  }

  function injectStyle() {
    if (document.getElementById("obelisk-bridge-style")) return;
    const style = document.createElement("style");
    style.id = "obelisk-bridge-style";
    style.textContent =
      "#" + BTN_ID + "{display:inline-flex;align-items:center;gap:6px;margin-left:12px;padding:0 12px;" +
      "height:36px;border-radius:18px;border:none;cursor:pointer;background:#3e6ae1;color:#fff;" +
      "font:500 13px/1 Roboto,Arial,sans-serif;flex:none;white-space:nowrap;}" +
      "#" + BTN_ID + ":hover{background:#345bc4;}" +
      "#" + BTN_ID + ":disabled{opacity:.65;cursor:default;}" +
      "#" + PANEL_ID + "{position:fixed;top:64px;right:16px;width:340px;z-index:2147483647;" +
      "background:#15181e;color:#e6e8ec;border:1px solid #333844;border-radius:12px;" +
      "box-shadow:0 12px 30px rgba(0,0,0,.4);font:13px/1.4 Roboto,Arial,sans-serif;overflow:hidden;}" +
      "#" + PANEL_ID + " .obelisk-panel-header{display:flex;align-items:center;justify-content:space-between;" +
      "padding:10px 14px;font-weight:600;border-bottom:1px solid #262b38;}" +
      "#" + PANEL_ID + " .obelisk-panel-close{background:none;border:none;color:#9aa2b1;font-size:18px;" +
      "line-height:1;cursor:pointer;padding:0 2px;}" +
      "#" + PANEL_ID + " .obelisk-panel-body{padding:12px 14px;display:flex;flex-direction:column;gap:10px;" +
      "box-sizing:border-box;}" +
      "#" + PANEL_ID + " .obelisk-field{display:flex;flex-direction:column;gap:4px;font-size:12px;color:#9aa2b1;" +
      "min-width:0;}" +
      "#" + PANEL_ID + " select,#" + PANEL_ID + " input[type=text]{background:#0e1017;color:#e6e8ec;" +
      "border:1px solid #333844;border-radius:6px;padding:6px 8px;font-size:13px;width:100%;" +
      "box-sizing:border-box;}" +
      "#" + PANEL_ID + " .obelisk-clip-row{display:flex;gap:8px;min-width:0;}" +
      "#" + PANEL_ID + " .obelisk-clip-row .obelisk-field{flex:1 1 0;min-width:0;}" +
      "#" + PANEL_ID + " .obelisk-checkbox-row{display:flex;align-items:center;gap:8px;font-size:12px;color:#e6e8ec;}" +
      "#" + PANEL_ID + " .obelisk-status{margin:0;font-size:12px;color:#9aa2b1;}" +
      "#" + PANEL_ID + " .obelisk-confirm-btn{background:#3e6ae1;color:#fff;border:none;border-radius:8px;" +
      "padding:9px;font-size:13px;font-weight:500;cursor:pointer;}" +
      "#" + PANEL_ID + " .obelisk-confirm-btn:hover{background:#345bc4;}" +
      "#" + PANEL_ID + " .obelisk-confirm-btn:disabled{opacity:.6;cursor:default;}";
    document.documentElement.appendChild(style);
  }

  function init() {
    injectStyle();
    ensureButton();
  }

  if (document.body) init();
  else document.addEventListener("DOMContentLoaded", init);

  // YouTube is a SPA - moving between videos (or away from one) fires
  // this instead of a real page load, and doesn't necessarily leave the
  // masthead DOM (and our button) intact either. It also fires in
  // practice for things that AREN'T a real video change (an ad finishing
  // and playback resuming, seen while testing) - resetting the caches
  // unconditionally on every firing wiped out a token/qualities that were
  // still perfectly valid for the video actually still on screen, so this
  // only clears them when the video id genuinely changed.
  document.addEventListener("yt-navigate-finish", function () {
    const videoId = videoIdFromUrl(location.href);
    console.log("[Obelisk] yt-navigate-finish, videoId now", videoId);
    if (videoId !== latestTokenVideoId) {
      latestToken = null;
      latestTokenVideoId = null;
    }
    if (videoId !== latestQualitiesVideoId) {
      latestQualities = null;
      latestQualitiesVideoId = null;
    }
    removePanel();
    ensureButton();
  });

  // Belt-and-braces for the masthead not existing yet this early
  // (document_start) or getting re-rendered independently of the
  // yt-navigate-finish event above.
  new MutationObserver(function () {
    ensureButton();
  }).observe(document.documentElement, { childList: true, subtree: true });
})();
