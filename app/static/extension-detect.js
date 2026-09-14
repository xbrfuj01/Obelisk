(function () {
  // Fixed extension ID - see extension/manifest.json's "key" field (a
  // pinned RSA public key, not the random per-install ID Chrome would
  // otherwise assign an unpacked/side-loaded extension). Without a stable
  // ID there'd be nothing for this page to reliably ping.
  var EXTENSION_ID = "mcnlglghkaalpdcddboiidnamfgblnki";
  var banner = document.getElementById("extension-banner");
  if (!banner) return;

  // chrome.runtime only exists on pages at all in Chromium-based browsers
  // - on anything else (Firefox, Safari) there's no extension to detect,
  // so just leave the banner hidden rather than claim it's missing.
  if (typeof chrome === "undefined" || !chrome.runtime || !chrome.runtime.sendMessage) return;

  try {
    chrome.runtime.sendMessage(EXTENSION_ID, { type: "ping" }, function (response) {
      // chrome.runtime.lastError is how Chrome reports "nothing answered"
      // (extension not installed) - must be read to avoid an "Unchecked
      // runtime.lastError" console warning even when we don't act on it.
      var notInstalled = chrome.runtime.lastError || !response || !response.ok;
      banner.hidden = !notInstalled;
    });
  } catch (err) {
    // Some browsers throw synchronously for an unknown extension ID
    // instead of going through the lastError callback path.
    banner.hidden = false;
  }
})();
