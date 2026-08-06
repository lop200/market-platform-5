const SELECTORS = {
  loggedIn: ['[data-testid="account-menu"]','[data-testid="portfolio"]','a[href*="portfolio"]'],
  search: ['input[data-testid="symbol-search"]','input[placeholder*="Symbol" i]','input[placeholder*="رمز"]','input[type="search"]'],
  quantity: ['input[data-testid="order-quantity"]','input[name="quantity"]','input[aria-label*="Quantity" i]','input[aria-label*="الكمية"]'],
  limit: ['input[data-testid="limit-price"]','input[name="limitPrice"]','input[aria-label*="Limit" i]'],
  takeProfit: ['input[data-testid="take-profit"]','input[name="takeProfit"]','input[aria-label*="Take Profit" i]'],
  stopLoss: ['input[data-testid="stop-loss"]','input[name="stopLoss"]','input[aria-label*="Stop Loss" i]'],
  review: ['button[data-testid="review-order"]','button[data-testid="preview-order"]'],
  submit: ['button[data-testid="place-order"]','button[data-testid="submit-order"]'],
  cash: ['[data-testid="cash-balance"]','[data-testid="cash"]'],
  buyingPower: ['[data-testid="buying-power"]'],
  positionRows: ['[data-testid="position-row"]','table tbody tr'],
  orderRows: ['[data-testid="order-row"]']
};
let activeIntent = null;
let lastReportedOrderStatus = null;

const first = names => names.map(selector => document.querySelector(selector)).find(Boolean) || null;
const text = names => first(names)?.textContent?.trim() || "";
const number = value => { const parsed = Number(String(value).replace(/[^0-9.-]/g, "")); return Number.isFinite(parsed) ? parsed : null; };
const setInput = (element, value) => {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  setter ? setter.call(element, String(value)) : element.value = String(value);
  element.dispatchEvent(new Event("input", {bubbles: true}));
  element.dispatchEvent(new Event("change", {bubbles: true}));
};
const visible = element => !!(element && element.getClientRects().length && !element.disabled);
const findButton = patterns => [...document.querySelectorAll("button")].find(button => visible(button) && patterns.some(pattern => pattern.test(button.textContent || "")));

function rowsFor(selectors, kind) {
  const selector = selectors.find(item => document.querySelector(item));
  if (!selector) return [];
  return [...document.querySelectorAll(selector)].slice(0, 100).map(row => {
    const cells = [...row.querySelectorAll("td,[data-field]")].map(cell => cell.textContent.trim());
    const symbol = (row.getAttribute("data-symbol") || cells.find(value => /^[A-Z]{1,6}(?:\d{6}[CP]\d{8})?$/.test(value)) || "").toUpperCase();
    if (!symbol) return null;
    if (kind === "orders") return {symbol, status: row.getAttribute("data-status") || cells.find(value => /filled|partial|open|rejected|مرفوض|منفذ/i.test(value)) || "unknown"};
    const values = cells.map(number).filter(value => value !== null);
    return {instrument_type: symbol.length > 10 ? "option" : "stock", symbol, underlying_symbol: symbol.slice(0, 6).replace(/\d+$/, ""), quantity: Math.max(0, Math.trunc(values[0] || 0)), average_price: values[1] || 0, current_price: values[2] || values[1] || 0};
  }).filter(Boolean);
}

function snapshot() {
  const bodyText = document.body?.innerText || "";
  const loginVisible = !!document.querySelector('input[type="password"],input[autocomplete="current-password"]');
  const accountVisible = SELECTORS.loggedIn.some(selector => document.querySelector(selector)) || /Buying Power|Portfolio|Positions|القوة الشرائية|المحفظة|المراكز/i.test(bodyText);
  const loginStatus = loginVisible ? "logged_out" : accountVisible ? "logged_in" : "unknown";
  return {
    bridge_ready: true,
    login_status: loginStatus,
    logged_in: loginStatus === "logged_in",
    cash: number(text(SELECTORS.cash)),
    buying_power: number(text(SELECTORS.buyingPower)),
    positions: rowsFor(SELECTORS.positionRows, "positions"),
    orders: rowsFor(SELECTORS.orderRows, "orders"),
    captured_at: new Date().toISOString(),
    url: location.origin + location.pathname
  };
}

async function publishSnapshot() {
  const current = snapshot();
  await chrome.runtime.sendMessage({type: "SAHM_SNAPSHOT", snapshot: current});
  if (activeIntent) {
    const order = current.orders.find(item => item.symbol === activeIntent.symbol);
    const raw = String(order?.status || "").toLowerCase();
    const status = /partial|جزئي/.test(raw) ? "partially_filled" : /filled|منفذ/.test(raw) ? "filled" : /reject|مرفوض/.test(raw) ? "rejected" : /open|pending|مفتوح/.test(raw) ? "open" : null;
    if (status && status !== lastReportedOrderStatus) {
      lastReportedOrderStatus = status;
      state(activeIntent, status, `حالة الأمر في سهم: ${order.status}`);
      if (["filled", "rejected"].includes(status)) activeIntent = null;
    }
  }
}

function closeOverlay() { document.getElementById("marsad-confirm-overlay")?.remove(); }
function state(intent, status, message, extra={}) {
  chrome.runtime.sendMessage({type: "EXECUTION_STATE", idempotency_key: intent.idempotency_key, state: {status, message, idempotency_key: intent.idempotency_key, at: new Date().toISOString(), ...extra}});
}

async function findAndFill(intent) {
  const search = first(SELECTORS.search);
  if (!search) throw new Error("لم تتعرف الإضافة على خانة البحث في نسخة سهم الحالية");
  setInput(search, intent.symbol);
  await new Promise(resolve => setTimeout(resolve, 900));
  const exact = [...document.querySelectorAll("button,a,[role=option]")].find(el => visible(el) && (el.textContent || "").trim().toUpperCase().includes(intent.symbol));
  if (exact) exact.click();
  await new Promise(resolve => setTimeout(resolve, 900));
  const quantity = first(SELECTORS.quantity), limit = first(SELECTORS.limit), takeProfit = first(SELECTORS.takeProfit), stopLoss = first(SELECTORS.stopLoss);
  if (![quantity,limit].every(visible)) throw new Error("تعذر التحقق من حقلي الكمية وLimit؛ لم يُرسل أمر");
  if (![takeProfit,stopLoss].every(visible)) throw new Error("لم تتوفر حقول حماية OCO الأصلية؛ رُفض إرسال أمر غير محمي");
  setInput(quantity, intent.quantity); setInput(limit, intent.limit_price); setInput(takeProfit, intent.take_profit); setInput(stopLoss, intent.stop_loss);
  return {quantity,limit,takeProfit,stopLoss};
}

function reviewOverlay(intent) {
  closeOverlay();
  const overlay = document.createElement("section");
  overlay.id = "marsad-confirm-overlay";
  overlay.innerHTML = `<div class="marsad-review"><button class="marsad-close" aria-label="إغلاق">×</button><h2>معاينة مرصاد الأخيرة</h2><p class="marsad-warning">أمر حقيقي في سهم. تحقق من الرمز والعقد والأسعار قبل التأكيد.</p><dl><dt>الأداة</dt><dd dir="ltr">${intent.symbol}</dd><dt>الكمية</dt><dd>${intent.quantity}</dd><dt>Buy Limit</dt><dd>${intent.limit_price}</dd><dt>Take Profit</dt><dd>${intent.take_profit}</dd><dt>Stop Loss</dt><dd>${intent.stop_loss}</dd><dt>صالح حتى</dt><dd>${new Date(intent.entry_valid_until).toLocaleString("ar-SA")}</dd></dl><label class="marsad-check"><input type="checkbox"> راجعت الرمز والكمية والأسعار وأوافق على إرسال الأمر الحقيقي</label><button class="marsad-confirm" disabled>تأكيد التنفيذ</button><p class="marsad-note">لن تُحفظ كلمة المرور أو OTP أو cookies. لا يوجد Full Auto.</p></div>`;
  document.body.appendChild(overlay);
  overlay.querySelector(".marsad-close").onclick=()=>{state(intent,"cancelled","ألغى المستخدم المعاينة");closeOverlay()};
  const check=overlay.querySelector("input"),confirm=overlay.querySelector(".marsad-confirm");check.onchange=()=>confirm.disabled=!check.checked;
  confirm.onclick=async()=>{confirm.disabled=true;try{
    if(Date.parse(intent.entry_valid_until)<=Date.now())throw new Error("انتهى وقت الدخول؛ أعد التحليل");
    const review=first(SELECTORS.review)||findButton([/review/i,/preview/i,/معاينة/,/مراجعة/]);if(review){review.click();await new Promise(resolve=>setTimeout(resolve,700));}
    const submit=first(SELECTORS.submit)||findButton([/place order/i,/submit order/i,/إرسال الأمر/,/تأكيد الشراء/,/^شراء$/]);
    if(!submit)throw new Error("لم تتعرف الإضافة على زر الإرسال النهائي؛ لم يُرسل أمر");
    submit.click();state(intent,"submitted","أُرسل الأمر إلى سهم وينتظر تحقق الحالة",{filled_quantity:0});closeOverlay();setTimeout(publishSnapshot,1500);
  }catch(error){state(intent,"error",error.message);confirm.disabled=false;}}
}

async function prepare(intent) {
  if (snapshot().login_status === "logged_out") throw new Error("المستخدم غير مسجل الدخول في سهم");
  if (Date.parse(intent.entry_valid_until) <= Date.now()) throw new Error("انتهى وقت الدخول؛ أعد التحليل");
  const stored = await chrome.storage.local.get("confirmMode");
  if (stored.confirmMode !== true) throw new Error("فعّل Confirm Mode من نافذة الإضافة أولًا");
  await findAndFill(intent);
  activeIntent = intent;
  lastReportedOrderStatus = null;
  reviewOverlay(intent); state(intent,"awaiting_user_confirmation","الأمر معبأ وينتظر تأكيد المستخدم");
}

chrome.runtime.onMessage.addListener((message, _sender, reply) => {
  if (message.type === "PING_SAHM") { reply({ok: true, snapshot: snapshot()}); return; }
  if (message.type === "CAPTURE_SNAPSHOT") { publishSnapshot().then(()=>reply({ok:true})); return true; }
  if (message.type === "PREPARE_TRADE") { prepare(message.intent).then(()=>reply({ok:true})).catch(error=>reply({ok:false,error:error.message})); return true; }
});

publishSnapshot();setInterval(()=>{if(!document.hidden)publishSnapshot()},10000);
