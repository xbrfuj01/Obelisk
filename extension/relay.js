// Runs in the isolated content-script world (the default one, has
// chrome.* access) alongside capture.js's page-world script. window.
// postMessage is the only channel between the two - capture.js can't call
// chrome.runtime.sendMessage itself since MAIN-world scripts have no
// extension APIs at all.
(function () {
  "use strict";

  window.addEventListener("message", function (event) {
    if (event.source !== window) return;
    const data = event.data;
    if (!data || !data.__obeliskBridge || data.type !== "po-token") return;
    chrome.runtime.sendMessage({
      type: "po-token",
      token: data.token,
      url: location.href,
    });
  });
})();
