const EXTENSION_VERSION = chrome.runtime.getManifest().version;
function markReady() {
  if (!document.documentElement) { setTimeout(markReady, 0); return; }
  document.documentElement.setAttribute("data-marsad-extension", EXTENSION_VERSION);
}
markReady();

function emit(name, detail) {
  document.dispatchEvent(new CustomEvent(name, {detail}));
}

document.addEventListener("marsad:ping", async () => {
  const state = await chrome.runtime.sendMessage({type: "MARSAD_PING"});
  emit("marsad:bridge-status", state || {connected: false});
});

document.addEventListener("marsad:request-snapshot", async () => {
  const response = await chrome.runtime.sendMessage({type: "REQUEST_SNAPSHOT"});
  if (response?.snapshot) emit("marsad:sahm-snapshot", response.snapshot);
});

document.addEventListener("marsad:connect-sahm", async () => {
  const response = await chrome.runtime.sendMessage({type: "CONNECT_SAHM"});
  emit("marsad:bridge-status", response || {connected: false});
  if (response?.snapshot) emit("marsad:sahm-snapshot", response.snapshot);
});

window.addEventListener("message", async event => {
  if (event.source !== window || event.data?.source !== "marsad-page") return;
  if (event.data.type === "CONNECT_SAHM") {
    const response = await chrome.runtime.sendMessage({type: "CONNECT_SAHM"});
    window.postMessage({source: "marsad-extension", type: "BRIDGE_STATUS", detail: response || {connected: false}}, "*");
    if (response?.snapshot) window.postMessage({source: "marsad-extension", type: "SAHM_SNAPSHOT", detail: response.snapshot}, "*");
  }
});

document.addEventListener("marsad:trade-intent", async event => {
  const response = await chrome.runtime.sendMessage({type: "TRADE_INTENT", intent: event.detail});
  if (!response?.ok) emit("marsad:execution-state", {status: "error", message: response?.error || "تعذر إرسال الأمر إلى سهم"});
  else emit("marsad:execution-state", {status: "prepared", message: "تم تجهيز المعاينة داخل سهم؛ راجعها ثم أكد يدويًا"});
});

chrome.runtime.onMessage.addListener(message => {
  if (message.type === "SAHM_SNAPSHOT") emit("marsad:sahm-snapshot", message.snapshot);
  if (message.type === "EXECUTION_STATE") emit("marsad:execution-state", message.state);
});

addEventListener("DOMContentLoaded", () => {markReady();document.dispatchEvent(new CustomEvent("marsad:ping"))});
