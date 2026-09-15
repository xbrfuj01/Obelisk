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
      chrome.runtime.sendMessage({
        type: "po-token",
        token: data.token,
        url: location.href,
      });
    } else if (data.type === "qualities") {
      // Purely local - unlike the token, the server never needs this on
      // its own; it only matters bundled into a download-request below.
      latestQualities = data.qualities;
      latestQualitiesVideoId = videoIdFromUrl(location.href);
      console.log("[Obelisk] relay.js cached qualities for", location.href, data.qualities);
    }
  });

  // -------- On-page "Завантажити" button --------
  // Injected next to the YouTube logo, video (watch) and Shorts pages
  // only. Clicking it hands the current video's URL plus whatever PO
  // token has already been captured above (waiting a short grace period
  // for one if needed) to background.js, which opens a new Obelisk tab
  // prefilled and ready to go. Far more reliable than the automatic
  // background-tab flow (see downloader.py's _run_job): the token here
  // comes from a real, foreground, actively-watched tab, not a hidden one
  // YouTube may never even start playing in.

  const BTN_ID = "obelisk-bridge-download-btn";
  const TOKEN_GRACE_MS = 4000;

  function isVideoPage() {
    return location.pathname === "/watch" || location.pathname.startsWith("/shorts/");
  }

  function setButtonState(btn, state) {
    btn.dataset.state = state;
    btn.disabled = state === "loading";
    btn.querySelector("span").textContent =
      state === "loading" ? "..." : state === "done" ? "Готово" : state === "error" ? "Помилка" : "Obelisk";
  }

  async function onButtonClick() {
    const btn = document.getElementById(BTN_ID);
    if (!btn) return;
    setButtonState(btn, "loading");
    const url = location.href;
    const videoId = videoIdFromUrl(url);

    const deadline = Date.now() + TOKEN_GRACE_MS;
    while ((!latestToken || latestTokenVideoId !== videoId) && Date.now() < deadline) {
      await new Promise(function (resolve) {
        setTimeout(resolve, 250);
      });
    }

    const qualitiesToSend = latestQualitiesVideoId === videoId ? latestQualities : null;
    console.log(
      "[Obelisk] sending download-request",
      { url: url, hasToken: !!(latestTokenVideoId === videoId && latestToken), qualities: qualitiesToSend }
    );

    chrome.runtime.sendMessage(
      {
        type: "download-request",
        url: url,
        token: latestTokenVideoId === videoId ? latestToken : null,
        // Whatever the page's own player response already told us about
        // available resolutions - opportunistic, not waited for
        // separately, since Obelisk's own probe is a fine fallback if
        // this hasn't shown up yet.
        qualities: qualitiesToSend,
      },
      function (response) {
        const ok = !chrome.runtime.lastError && response && response.ok;
        setButtonState(btn, ok ? "done" : "error");
        setTimeout(function () {
          if (document.getElementById(BTN_ID) === btn) setButtonState(btn, "idle");
        }, 2000);
      }
    );
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
      "#" + BTN_ID + "[data-state=\"done\"]{background:#1a9469;}" +
      "#" + BTN_ID + "[data-state=\"error\"]{background:#d6394e;}";
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
  // masthead DOM (and our button) intact either.
  document.addEventListener("yt-navigate-finish", function () {
    latestToken = null;
    latestTokenVideoId = null;
    latestQualities = null;
    latestQualitiesVideoId = null;
    ensureButton();
  });

  // Belt-and-braces for the masthead not existing yet this early
  // (document_start) or getting re-rendered independently of the
  // yt-navigate-finish event above.
  new MutationObserver(function () {
    ensureButton();
  }).observe(document.documentElement, { childList: true, subtree: true });
})();
