const MARSAD = "https://market-platform-5.onrender.com/*";
const SAHM = "https://app.sahmcapital.com/*";

async function tabsFor(pattern) {
  return chrome.tabs.query({ url: pattern });
}

async function send(tabId, message) {
  try { return await chrome.tabs.sendMessage(tabId, message); }
  catch { return null; }
}

async function routeToMarsad(message) {
  const tabs = await tabsFor(MARSAD);
  await Promise.all(tabs.map(tab => send(tab.id, message)));
}

async function sahmTab() {
  const tabs = await tabsFor(SAHM);
  if (tabs.length) return tabs[0];
  return chrome.tabs.create({ url: "https://app.sahmcapital.com/", active: true });
}

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  (async () => {
    if (message.type === "MARSAD_PING") {
      const tabs = await tabsFor(SAHM);
      reply({ connected: tabs.length > 0, label: tabs.length ? "الإضافة متصلة" : "افتح منصة سهم" });
      return;
    }
    if (message.type === "SAHM_SNAPSHOT") {
      const snapshot = {...message.snapshot, tab_id: sender.tab?.id, received_at: new Date().toISOString()};
      await chrome.storage.local.set({ latestSahmSnapshot: snapshot });
      await routeToMarsad({ type: "SAHM_SNAPSHOT", snapshot });
      reply({ ok: true });
      return;
    }
    if (message.type === "REQUEST_SNAPSHOT") {
      const tabs = await tabsFor(SAHM);
      if (tabs[0]) await send(tabs[0].id, { type: "CAPTURE_SNAPSHOT" });
      const {latestSahmSnapshot} = await chrome.storage.local.get("latestSahmSnapshot");
      reply({ snapshot: latestSahmSnapshot || null });
      return;
    }
    if (message.type === "TRADE_INTENT") {
      const intent = message.intent || {};
      if (!intent.idempotency_key || !intent.entry_valid_until) throw new Error("TradeIntent غير مكتمل");
      if (Date.parse(intent.entry_valid_until) <= Date.now()) throw new Error("انتهى وقت الدخول؛ أعد التحليل");
      const key = `intent:${intent.idempotency_key}`;
      const existing = await chrome.storage.local.get(key);
      if (existing[key]) throw new Error("تم استقبال هذا الأمر سابقًا؛ مُنع التكرار");
      await chrome.storage.local.set({[key]: {state: "received", received_at: new Date().toISOString()}});
      const tab = await sahmTab();
      await chrome.tabs.update(tab.id, { active: true });
      let accepted = null;
      for (let attempt = 0; attempt < 20 && !accepted; attempt++) {
        accepted = await send(tab.id, { type: "PREPARE_TRADE", intent });
        if (!accepted) await new Promise(resolve => setTimeout(resolve, 500));
      }
      if (!accepted?.ok) throw new Error(accepted?.error || "تعذر تجهيز الأمر داخل سهم");
      reply({ ok: true });
      return;
    }
    if (message.type === "EXECUTION_STATE") {
      if (message.idempotency_key) {
        await chrome.storage.local.set({[`intent:${message.idempotency_key}`]: message.state});
      }
      await routeToMarsad({ type: "EXECUTION_STATE", state: message.state });
      reply({ ok: true });
      return;
    }
    if (message.type === "GET_POPUP_STATE") {
      const [sahm, marsad] = await Promise.all([tabsFor(SAHM), tabsFor(MARSAD)]);
      const stored = await chrome.storage.local.get(["latestSahmSnapshot", "confirmMode"]);
      reply({ sahm_open: !!sahm.length, marsad_open: !!marsad.length, snapshot: stored.latestSahmSnapshot || null, confirm_mode: stored.confirmMode === true });
    }
  })().catch(error => reply({ ok: false, error: error.message }));
  return true;
});

chrome.tabs.onRemoved.addListener(async tabId => {
  const stored = await chrome.storage.local.get("latestSahmSnapshot");
  if (stored.latestSahmSnapshot?.tab_id === tabId) {
    await chrome.storage.local.remove("latestSahmSnapshot");
    await routeToMarsad({type: "SAHM_SNAPSHOT", snapshot: {logged_in: false, positions: [], disconnected: true}});
  }
});
