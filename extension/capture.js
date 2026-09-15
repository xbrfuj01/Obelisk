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

  // The page's own /youtubei/v1/player response (or, for the first video
  // on a freshly loaded page, window.ytInitialPlayerResponse) already
  // lists every resolution the video actually has, height/width/
  // contentLength included - even for formats SABR strips the url from.
  // Reading it here sidesteps the server-side quality-probe's whole
  // SABR problem (see downloader.py's probe_qualities) entirely: this
  // isn't asking the format to be *downloadable*, just reading metadata
  // the page already received regardless.
  const COMMON_LABELS = { 4320: "8K", 2160: "4K", 1440: "2K", 1080: "Full HD", 720: "HD" };

  function extractQualities(playerResponse) {
    const streamingData = playerResponse && playerResponse.streamingData;
    if (!streamingData) return null;
    const allFormats = [].concat(streamingData.formats || [], streamingData.adaptiveFormats || []);
    const byHeight = {};
    let bestAudioBytes = null;
    for (const f of allFormats) {
      const height = f.height;
      const bytes = f.contentLength ? parseInt(f.contentLength, 10) : null;
      const isAudioOnly = !height && typeof f.mimeType === "string" && f.mimeType.indexOf("audio/") === 0;
      if (height) {
        const prev = byHeight[height];
        if (!prev || (bytes && (!prev.bytes || bytes > prev.bytes))) {
          byHeight[height] = { width: f.width, bytes: bytes };
        }
      } else if (isAudioOnly && bytes && (!bestAudioBytes || bytes > bestAudioBytes)) {
        bestAudioBytes = bytes;
      }
    }
    const heights = Object.keys(byHeight)
      .map(Number)
      .sort(function (a, b) {
        return b - a;
      });
    if (!heights.length) return null;
    return heights.map(function (h) {
      const entry = byHeight[h];
      let label = entry.width ? entry.width + "×" + h : h + "p";
      if (COMMON_LABELS[h]) label += " (" + COMMON_LABELS[h] + ")";
      return { value: String(h), label: label, video_bytes: entry.bytes, audio_bytes: bestAudioBytes };
    });
  }

  function reportQualities(qualities) {
    if (!qualities || !qualities.length) return;
    window.postMessage({ __obeliskBridge: true, type: "qualities", qualities: qualities }, "*");
  }

  function isPlayerRequestUrl(url) {
    return typeof url === "string" && url.indexOf("/youtubei/v1/player") !== -1;
  }

  // The tab background.js opens for this is created with active:false, so
  // document.visibilityState is "hidden" for its whole life (that's a
  // property of the tab itself, unrelated to the earlier chrome.alarms
  // fix for the *window* not having OS focus) - and YouTube's player, like
  // most video sites, defers actually starting playback while hidden to
  // save bandwidth. The fetch/XHR hooks below are purely passive, so if
  // playback never starts, the player-API request carrying a PO token
  // never fires either and this whole capture is a no-op. Nudge it
  // directly instead of waiting for autoplay that a hidden tab won't get:
  // muted playback isn't blocked by Chrome's autoplay policy, and once
  // yt-dlp/downloader.py has the token it never touches the video's own
  // bytes anyway, so it doesn't matter that this "watches" nothing.
  function forcePlayback() {
    try {
      const video = document.querySelector("video");
      if (!video) return;
      video.muted = true;
      const p = video.play();
      if (p && typeof p.catch === "function") p.catch(function () {});
    } catch (err) {
      // never let this break the page - worst case the passive hooks
      // below still catch a token if the page starts playback on its own
    }
  }
  let playbackNudgeAttempts = 0;
  const playbackNudgeTimer = setInterval(function () {
    playbackNudgeAttempts += 1;
    forcePlayback();
    if (playbackNudgeAttempts > 40) clearInterval(playbackNudgeTimer);
  }, 250);

  const originalFetch = window.fetch;
  window.fetch = function (input, init) {
    const url = typeof input === "string" ? input : input && input.url;
    try {
      const fromUrl = url ? extractFromUrl(url) : null;
      if (fromUrl) reportToken(fromUrl, "fetch-url");
      const body = init && init.body;
      const fromBody = extractFromBody(body);
      if (fromBody) reportToken(fromBody, "fetch-body");
    } catch (err) {
      // never let capture logic break the page's own playback
    }
    const result = originalFetch.apply(this, arguments);
    if (isPlayerRequestUrl(url)) {
      result
        .then(function (res) {
          return res.clone().json();
        })
        .then(function (json) {
          reportQualities(extractQualities(json));
        })
        .catch(function () {});
    }
    return result;
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
    this.__obeliskIsPlayerRequest = isPlayerRequestUrl(url);
    return originalOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    try {
      const fromBody = extractFromBody(body);
      if (fromBody) reportToken(fromBody, "xhr-body");
    } catch (err) {
      // ignore
    }
    if (this.__obeliskIsPlayerRequest) {
      this.addEventListener("load", function () {
        try {
          reportQualities(extractQualities(JSON.parse(this.responseText)));
        } catch (err) {
          // not JSON, or shape we don't recognize - ignore
        }
      });
    }
    return originalSend.apply(this, arguments);
  };

  // Covers the very first video on a freshly loaded page: its player
  // response is embedded straight into the HTML (window.ytInitialPlayerResponse)
  // rather than fetched via the hooks above, which only see *subsequent*
  // videos navigated to within the same SPA session.
  let initialCheckAttempts = 0;
  const initialCheckTimer = setInterval(function () {
    initialCheckAttempts += 1;
    if (window.ytInitialPlayerResponse) {
      reportQualities(extractQualities(window.ytInitialPlayerResponse));
      clearInterval(initialCheckTimer);
    } else if (initialCheckAttempts > 40) {
      clearInterval(initialCheckTimer);
    }
  }, 250);
})();
