// Runs in the page's own JS world (manifest.json: "world": "MAIN"), so it
// has no chrome.* extension APIs at all - it can only see what the page
// itself sees, and can only get data out via window.postMessage to
// relay.js (the privileged, isolated-world half of this content script).
//
// Goal: capture a real, browser-minted YouTube GVS PO token from the
// page's own network traffic as it sets up playback - see downloader.py's
// _wait_for_extension_token / youtube_sabr.py for what happens to it
// server-side. This is the one genuinely unproven part of the whole
// Obelisk Bridge feature (see the project's plan notes): there's no
// existing reference implementation, so the exact request shape below is
// a best-effort guess based on how YouTube's player API is known to work,
// and will likely need adjusting once tested against a real page.
(function () {
  "use strict";

  function reportToken(token, source) {
    if (!token) return;
    window.postMessage({ __obeliskBridge: true, type: "po-token", token, source }, "*");
  }

  // The PO token most commonly shows up as a "pot" query parameter or a
  // "poToken" JSON field on requests to YouTube's player API
  // (youtubei/v1/player), which is a normal JSON POST - unlike the actual
  // media segment requests (videoplayback), which are binary protobuf and
  // much harder to pick apart from a content script.
  function extractFromUrl(url) {
    try {
      const parsed = new URL(url, location.href);
      const pot = parsed.searchParams.get("pot");
      if (pot) return pot;
    } catch (err) {
      // not a valid absolute/relative URL - ignore
    }
    return null;
  }

  function extractFromBody(body) {
    if (!body || typeof body !== "string") return null;
    try {
      const data = JSON.parse(body);
      const pot =
        data.poToken ||
        (data.serviceIntegrityDimensions && data.serviceIntegrityDimensions.poToken) ||
        (data.playbackContext &&
          data.playbackContext.contentPlaybackContext &&
          data.playbackContext.contentPlaybackContext.poToken);
      if (pot) return pot;
    } catch (err) {
      // body wasn't JSON (e.g. the binary videoplayback requests) - ignore
    }
    return null;
  }

  const originalFetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      const url = typeof input === "string" ? input : input && input.url;
      const fromUrl = url ? extractFromUrl(url) : null;
      if (fromUrl) reportToken(fromUrl, "fetch-url");
      const body = init && init.body;
      const fromBody = extractFromBody(body);
      if (fromBody) reportToken(fromBody, "fetch-body");
    } catch (err) {
      // never let capture logic break the page's own playback
    }
    return originalFetch.apply(this, arguments);
  };

  const originalOpen = XMLHttpRequest.prototype.open;
  const originalSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    try {
      const fromUrl = extractFromUrl(url);
      if (fromUrl) reportToken(fromUrl, "xhr-url");
    } catch (err) {
      // ignore
    }
    return originalOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    try {
      const fromBody = extractFromBody(body);
      if (fromBody) reportToken(fromBody, "xhr-body");
    } catch (err) {
      // ignore
    }
    return originalSend.apply(this, arguments);
  };
})();
