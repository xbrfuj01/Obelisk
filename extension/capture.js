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
    console.log("[Obelisk] captured PO token via", source, "(" + token.length + " chars)");
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
      // Player requests carry their own videoId at the top level - reject
      // a token meant for some other video (YouTube quietly requests
      // player data for videos other than the one on screen too, e.g. an
      // autoplay-next prefetch) rather than silently misattributing it to
      // the current page. Bodies without a videoId (e.g. subtitle
      // requests) skip this check entirely.
      const expectedVideoId = currentVideoId();
      if (data.videoId && expectedVideoId && data.videoId !== expectedVideoId) return null;
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

  // watch?v=<id> or shorts/<id> - matched against playerResponse.videoDetails.videoId
  // below, since YouTube quietly fires /youtubei/v1/player requests for
  // videos other than the one on screen too (autoplay-next prefetch, the
  // "up next" panel, etc.) - without this check, real-world logs showed
  // a *different* video's format list (16 heights vs. the actual video's
  // 6) getting reported as if it were the current one's.
  function currentVideoId() {
    if (location.pathname === "/watch") return new URLSearchParams(location.search).get("v");
    if (location.pathname.startsWith("/shorts/")) return location.pathname.slice("/shorts/".length);
    return null;
  }

  function extractQualities(rawResponse) {
    // Some /youtubei/v1/player responses (seen in practice alongside ad
    // placement data) wrap the actual payload one level deeper as
    // {adPlacements, playerAds, playerResponse: {...}, playerConfig} -
    // window.ytInitialPlayerResponse and most fetch responses are the
    // inner shape directly, so unwrap only when the outer object doesn't
    // already look like one itself.
    const playerResponse = (!rawResponse || rawResponse.streamingData) ? rawResponse : rawResponse.playerResponse;
    const streamingData = playerResponse && playerResponse.streamingData;
    if (!streamingData) {
      console.log("[Obelisk] extractQualities: no streamingData in player response", rawResponse);
      return null;
    }
    const responseVideoId = playerResponse.videoDetails && playerResponse.videoDetails.videoId;
    const expectedVideoId = currentVideoId();
    if (responseVideoId && expectedVideoId && responseVideoId !== expectedVideoId) {
      console.log("[Obelisk] extractQualities: ignoring response for", responseVideoId, "- current video is", expectedVideoId);
      return null;
    }
    const allFormats = [].concat(streamingData.formats || [], streamingData.adaptiveFormats || []);
    const heightsSeen = new Set();
    for (const f of allFormats) {
      if (f.height) heightsSeen.add(f.height);
    }
    const heights = Array.from(heightsSeen).sort(function (a, b) {
      return b - a;
    });
    if (!heights.length) {
      console.log("[Obelisk] extractQualities: streamingData had formats but none with a height", streamingData);
      return null;
    }
    // contentLength on adaptive formats turned out unreliable for
    // SABR-restricted videos in practice (real logs showed several
    // different heights reporting byte-for-byte identical sizes) - rather
    // than show a number that might just be wrong, this only reports
    // which resolutions exist, same as the label-only fallback
    // probe_qualities itself uses when it can't size a format either.
    return heights.map(function (h) {
      const withThisHeight = allFormats.filter(function (f) {
        return f.height === h;
      });
      const width = withThisHeight.length ? withThisHeight[0].width : null;
      let label = width ? width + "×" + h : h + "p";
      if (COMMON_LABELS[h]) label += " (" + COMMON_LABELS[h] + ")";
      return { value: String(h), label: label, video_bytes: null, audio_bytes: null };
    });
  }

  function reportQualities(qualities) {
    if (!qualities || !qualities.length) return;
    console.log("[Obelisk] captured qualities from player response:", qualities);
    window.postMessage({ __obeliskBridge: true, type: "qualities", qualities: qualities }, "*");
  }

  function isPlayerRequestUrl(url) {
    return typeof url === "string" && url.indexOf("/youtubei/v1/player") !== -1;
  }

  // Runs fn once the main thread actually has spare time instead of
  // immediately - used to keep our own (non-time-critical) parsing of a
  // player response out of the way of YouTube's own, much more urgent,
  // parsing of the very same data right as playback is starting.
  function runWhenIdle(fn) {
    if (typeof requestIdleCallback === "function") {
      requestIdleCallback(fn, { timeout: 2000 });
    } else {
      setTimeout(fn, 0);
    }
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
  // Only ever meant for the hidden background tab background.js opens for
  // the automatic flow - running this unconditionally on every youtube.com
  // page load meant it was ALSO forcibly muting and repeatedly calling
  // .play() on the video the user is actually, normally watching (real
  // symptoms reported: playback couldn't be paused/stopped, and audio
  // stayed muted until toggling the player's mute button twice). Gate it
  // on document.hidden at injection time - a tab background.js creates
  // with active:false is hidden for its whole life, but a tab the user
  // opened themselves is visible immediately. Also bail out the moment the
  // tab becomes visible, in case a still-loading auto-flow tab gets
  // manually clicked into before it finishes.
  if (document.hidden) {
    let playbackNudgeAttempts = 0;
    const playbackNudgeTimer = setInterval(function () {
      if (!document.hidden) {
        clearInterval(playbackNudgeTimer);
        return;
      }
      playbackNudgeAttempts += 1;
      forcePlayback();
      if (playbackNudgeAttempts > 40) clearInterval(playbackNudgeTimer);
    }, 250);
  }

  // Scanning EVERY fetch/XHR on the page (ads, analytics, thumbnails, ...)
  // for a token that only ever shows up on player-endpoint requests was
  // pure wasted work on the vast majority of a YouTube page's own
  // traffic, adding JS overhead to every single one of them during page
  // load - narrowed to just the requests it could actually be on.
  const originalFetch = window.fetch;
  window.fetch = function (input, init) {
    const url = typeof input === "string" ? input : input && input.url;
    const isPlayerReq = isPlayerRequestUrl(url);
    if (isPlayerReq) {
      try {
        const fromUrl = url ? extractFromUrl(url) : null;
        if (fromUrl) reportToken(fromUrl, "fetch-url");
        const body = init && init.body;
        const fromBody = extractFromBody(body);
        if (fromBody) reportToken(fromBody, "fetch-body");
      } catch (err) {
        // never let capture logic break the page's own playback
      }
    }
    const result = originalFetch.apply(this, arguments);
    if (isPlayerReq) {
      console.log("[Obelisk] intercepted fetch to player endpoint:", url);
      // .clone() has to happen synchronously right here, before YouTube's
      // own code gets a chance to consume the original response body (a
      // Response's body can only be read once - cloning after that throws).
      // But actually *parsing* our clone is deferred to an idle callback:
      // running it as a plain microtask straight off this promise meant it
      // executed immediately after the real response arrived - exactly
      // when YouTube's own player is busy parsing this very same
      // (sometimes multi-MB) response to actually start playback.
      // Competing for main-thread time right at that moment was a
      // suspected contributor to the video itself taking noticeably
      // longer to start inside the player (reported: page loads fine, but
      // playback start is slow) even after page-load overhead elsewhere
      // was already cut. This data is only for the panel's quality
      // dropdown - nothing time-critical about reading it a beat later
      // once the browser actually has spare time.
      result
        .then(function (res) {
          const cloned = res.clone();
          runWhenIdle(function () {
            cloned
              .json()
              .then(function (json) {
                reportQualities(extractQualities(json));
              })
              .catch(function (err) {
                console.log("[Obelisk] failed to read player fetch response as JSON:", err);
              });
          });
        })
        .catch(function (err) {
          console.log("[Obelisk] failed to read player fetch response:", err);
        });
    }
    return result;
  };

  const originalOpen = XMLHttpRequest.prototype.open;
  const originalSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__obeliskIsPlayerRequest = isPlayerRequestUrl(url);
    if (this.__obeliskIsPlayerRequest) {
      try {
        const fromUrl = extractFromUrl(url);
        if (fromUrl) reportToken(fromUrl, "xhr-url");
      } catch (err) {
        // ignore
      }
    }
    return originalOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    if (this.__obeliskIsPlayerRequest) {
      try {
        const fromBody = extractFromBody(body);
        if (fromBody) reportToken(fromBody, "xhr-body");
      } catch (err) {
        // ignore
      }
      console.log("[Obelisk] intercepted XHR to player endpoint");
      const xhr = this;
      this.addEventListener("load", function () {
        // Same reasoning as the fetch path above: defer the actual parse
        // off the 'load' event itself, since this.responseText is already
        // fully buffered by XHR (no clone-before-consumed concern here).
        runWhenIdle(function () {
          try {
            reportQualities(extractQualities(JSON.parse(xhr.responseText)));
          } catch (err) {
            console.log("[Obelisk] failed to parse player XHR response as JSON:", err);
          }
        });
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
      console.log("[Obelisk] found window.ytInitialPlayerResponse");
      const initialResponse = window.ytInitialPlayerResponse;
      runWhenIdle(function () {
        reportQualities(extractQualities(initialResponse));
      });
      clearInterval(initialCheckTimer);
    } else if (initialCheckAttempts > 40) {
      console.log("[Obelisk] window.ytInitialPlayerResponse never appeared after 10s");
      clearInterval(initialCheckTimer);
    }
  }, 250);
})();
