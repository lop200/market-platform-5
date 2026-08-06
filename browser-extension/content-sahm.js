const SELECTORS = {
  loggedIn: ['[data-testid="account-menu"]','[data-testid="portfolio"]','a[href*="portfolio"]'],
  search: ['input[data-testid="symbol-search"]','input[placeholder*="Symbol" i]','input[placeholder*="Ticker" i]','input[placeholder*="Search" i]','input[placeholder*="رمز"]','input[placeholder*="بحث"]','input[type="search"]'],
  quantity: ['input[data-testid="order-quantity"]','input[name="quantity"]','input[name*="qty" i]','input[aria-label*="Quantity" i]','input[placeholder*="Quantity" i]','input[aria-label*="الكمية"]'],
  limit: ['input[data-testid="limit-price"]','input[name="limitPrice"]','input[name*="price" i]','input[aria-label*="Limit" i]','input[placeholder*="Price" i]'],
  takeProfit: ['input[data-testid="take-profit"]','input[name="takeProfit"]','input[aria-label*="Take Profit" i]'],
  stopLoss: ['input[data-testid="stop-loss"]','input[name="stopLoss"]','input[aria-label*="Stop Loss" i]'],
  review: ['button[data-testid="review-order"]','button[data-testid="preview-order"]'],
  submit: ['button[data-testid="place-order"]','button[data-testid="submit-order"]'],
  cash: ['[data-testid="cash-balance"]','[data-testid="cash"]','[data-testid*="cash" i]'],
  buyingPower: ['[data-testid="buying-power"]','[data-testid*="buying" i]','[data-testid*="power" i]'],
  positionRows: ['[data-testid="position-row"]','[data-testid*="position" i] tbody tr','table tbody tr','[role="row"]'],
  orderRows: ['[data-testid="order-row"]']
};
let activeIntent = null;
let lastReportedOrderStatus = null;

let rootsCache={at:0,value:[document]};
const roots = () => { if(Date.now()-rootsCache.at<500)return rootsCache.value;const found=[document];for(let i=0;i<found.length;i++){for(const el of found[i].querySelectorAll?.("*")||[]){if(el.shadowRoot)found.push(el.shadowRoot);if(el.tagName==="IFRAME"){try{if(el.contentDocument)found.push(el.contentDocument)}catch{}}}}rootsCache={at:Date.now(),value:found};return found; };
const all = selector => roots().flatMap(root => {try{return [...root.querySelectorAll(selector)]}catch{return []}});
const first = names => names.flatMap(all).find(Boolean) || null;
const text = names => first(names)?.textContent?.trim() || "";
const number = value => { const cleaned=String(value??"").replace(/[^0-9.-]/g, "");if(!cleaned||cleaned==="-"||cleaned===".")return null;const parsed=Number(cleaned);return Number.isFinite(parsed)?parsed:null; };
const setInput = (element, value) => {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  setter ? setter.call(element, String(value)) : element.value = String(value);
  element.dispatchEvent(new Event("input", {bubbles: true}));
  element.dispatchEvent(new Event("change", {bubbles: true}));
};
const visible = element => !!(element && element.getClientRects().length && !element.disabled);
const findButton = patterns => all("button,[role=button]").find(button => visible(button) && patterns.some(pattern => pattern.test(button.textContent || "")));
const semanticInput = patterns => all('input:not([type="password"]):not([autocomplete="current-password"])').find(input => {const context=[input.name,input.id,input.placeholder,input.getAttribute("aria-label"),input.closest("label,fieldset,section,div")?.innerText?.slice(0,160)].filter(Boolean).join(" ");return visible(input)&&patterns.some(pattern=>pattern.test(context))})||null;
function labeledNumber(patterns){for(const el of all("[data-testid],dt,dd,span,p,div")){const own=(el.childElementCount?"":el.textContent||"").trim();if(!own||own.length>80||!patterns.some(pattern=>pattern.test(own)))continue;for(const candidate of [el.nextElementSibling,el.parentElement,el.parentElement?.nextElementSibling]){const values=String(candidate?.innerText||candidate?.textContent||"").match(/-?[\d,]+(?:\.\d+)?/g)||[];for(const value of values){const parsed=number(value);if(parsed!==null)return parsed}}}return null}

function rowsFor(selectors, kind) {
  const selector = selectors.find(item => all(item).length);
  if (!selector) return [];
  return all(selector).slice(0, 100).map(row => {
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
  const accountVisible = SELECTORS.loggedIn.some(selector => all(selector).length) || /Buying Power|Portfolio|Positions|القوة الشرائية|المحفظة|المراكز/i.test(bodyText);
  const loginStatus = loginVisible ? "logged_out" : accountVisible ? "logged_in" : "unknown";
  const cash = number(text(SELECTORS.cash)) ?? labeledNumber([/^cash$/i,/cash balance/i,/النقد/i,/الرصيد النقدي/i]);
  const buyingPower = number(text(SELECTORS.buyingPower)) ?? labeledNumber([/buying power/i,/available to trade/i,/purchasing power/i,/القوة الشرائية/i,/متاح للتداول/i]);
  const positions = rowsFor(SELECTORS.positionRows, "positions");
  const searchField = first(SELECTORS.search) || semanticInput([/symbol|ticker|search/i,/رمز|بحث/i]);
  const quantityField = first(SELECTORS.quantity) || semanticInput([/quantity|qty/i,/الكمية/i]);
  const limitField = first(SELECTORS.limit) || semanticInput([/limit|price/i,/السعر|محدد/i]);
  return {
    bridge_ready: true,
    login_status: loginStatus,
    logged_in: loginStatus === "logged_in",
    cash,
    buying_power: buyingPower,
    positions,
    orders: rowsFor(SELECTORS.orderRows, "orders"),
    captured_at: new Date().toISOString(),
    url: location.origin + location.pathname,
    page_hint: cash===null&&buyingPower===null?"افتح Portfolio أو Account داخل سهم لقراءة الرصيد.":quantityField===null?"افتح Trade ثم اختر سهمًا لتجهيز نموذج الأمر.":"تم التعرف على صفحة سهم.",
    diagnostics: {cash: cash!==null, buying_power: buyingPower!==null, positions: positions.length>0||/positions|المراكز/i.test(bodyText), search: !!searchField, quantity: !!quantityField, limit: !!limitField, order_form: !!(quantityField&&limitField)}
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
  const search = first(SELECTORS.search) || semanticInput([/symbol|ticker|search/i,/رمز|بحث/i]);
  if (!search) throw new Error("لم تتعرف الإضافة على خانة البحث في نسخة سهم الحالية");
  setInput(search, intent.symbol);
  await new Promise(resolve => setTimeout(resolve, 900));
  const exact = all("button,a,[role=option]").find(el => visible(el) && (el.textContent || "").trim().toUpperCase().includes(intent.symbol));
  if (exact) exact.click();
  await new Promise(resolve => setTimeout(resolve, 900));
  const quantity = first(SELECTORS.quantity)||semanticInput([/quantity|qty/i,/الكمية/i]), limit = first(SELECTORS.limit)||semanticInput([/limit|price/i,/السعر|محدد/i]), takeProfit = first(SELECTORS.takeProfit)||semanticInput([/take profit|target/i,/جني الأرباح|الهدف/i]), stopLoss = first(SELECTORS.stopLoss)||semanticInput([/stop loss|stop price/i,/وقف الخسارة/i]);
  if (![quantity,limit].every(visible)) throw new Error("تعذر التحقق من حقلي الكمية وLimit؛ لم يُرسل أمر");
  setInput(quantity, intent.quantity); setInput(limit, intent.limit_price);
  if (visible(takeProfit)&&visible(stopLoss)) { setInput(takeProfit, intent.take_profit); setInput(stopLoss, intent.stop_loss); }
  return {quantity,limit,takeProfit,stopLoss};
}

function verifyFilledOrder(intent) {
  const quantity = first(SELECTORS.quantity)||semanticInput([/quantity|qty/i,/الكمية/i]);
  const limit = first(SELECTORS.limit)||semanticInput([/limit|price/i,/السعر|محدد/i]);
  if (![quantity,limit].every(visible)) throw new Error("تعذر إعادة فحص الكمية وLimit؛ لم يُرسل الأمر");
  if (Math.trunc(number(quantity.value) || 0) !== Math.trunc(Number(intent.quantity))) throw new Error("كمية سهم لا تطابق أمر مرصاد؛ توقف الإرسال");
  if (Math.abs((number(limit.value) || 0) - Number(intent.limit_price)) > .011) throw new Error("سعر Limit لا يطابق أمر مرصاد؛ توقف الإرسال");
  if (!(document.body?.innerText || "").toUpperCase().includes(String(intent.symbol).toUpperCase())) throw new Error("رمز السهم غير ظاهر في صفحة الأمر؛ توقف الإرسال");
}

function reviewOverlay(intent) {
  closeOverlay();
  const overlay = document.createElement("section");
  overlay.id = "marsad-confirm-overlay";
  overlay.innerHTML = `<div class="marsad-review"><button class="marsad-close" aria-label="إغلاق">×</button><h2>تأكيد أمر سهم الحقيقي</h2><p class="marsad-warning">راجع القيم. بعد كلمة المرور ستضغط «إرسال الأمر الآن» وينفذ الأمر في سهم.</p><dl><dt>الأداة</dt><dd dir="ltr">${intent.symbol}</dd><dt>الكمية</dt><dd>${intent.quantity}</dd><dt>Buy Limit</dt><dd>${intent.limit_price}</dd><dt>Take Profit</dt><dd>${intent.take_profit}</dd><dt>Stop Loss</dt><dd>${intent.stop_loss}</dd><dt>صالح حتى</dt><dd>${new Date(intent.entry_valid_until).toLocaleString("ar-SA")}</dd></dl><button class="marsad-confirm">راجعت الأمر — فتح كلمة المرور</button><p class="marsad-note">لن تقرأ أو تحفظ الإضافة كلمة المرور أو OTP أو cookies. لا يوجد تنفيذ بلا ضغطك النهائي.</p></div>`;
  document.body.appendChild(overlay);
  overlay.querySelector(".marsad-close").onclick=()=>{state(intent,"cancelled","ألغى المستخدم المعاينة");closeOverlay()};
  const confirm=overlay.querySelector(".marsad-confirm"),warning=overlay.querySelector(".marsad-warning"),reviewBox=overlay.querySelector(".marsad-review");let stage="review";
  confirm.onclick=async()=>{confirm.disabled=true;try{
    if(Date.parse(intent.entry_valid_until)<=Date.now())throw new Error("انتهى وقت الدخول؛ أعد التحليل");
    if(stage==="review"){
      verifyFilledOrder(intent);
      const review=first(SELECTORS.review)||findButton([/review/i,/preview/i,/معاينة/,/مراجعة/,/unlock trade/i,/فتح التداول/i]);
      if(!review)throw new Error("لم يظهر زر Review أو Unlock Trade؛ لم يُرسل الأمر");
      review.click();await new Promise(resolve=>setTimeout(resolve,700));
      stage="submit";overlay.style.cssText="place-items:end start;background:transparent;pointer-events:none";reviewBox.style.cssText="width:min(390px,calc(100vw - 28px));pointer-events:auto";warning.textContent="أدخل كلمة مرور التداول في سهم، ثم اضغط الزر الأخضر هنا.";confirm.textContent="إرسال الأمر الآن";confirm.disabled=false;
      state(intent,"awaiting_user_confirmation","أدخل كلمة مرور التداول ثم اضغط إرسال الأمر الآن",{filled_quantity:0});return;
    }
    const finalSubmit=first(SELECTORS.submit)||findButton([/^place order$/i,/^submit order$/i,/^إرسال الأمر$/,/^تأكيد الشراء$/,/^تنفيذ الأمر$/]);
    if(!finalSubmit)throw new Error("أكمل كلمة المرور وUnlock Trade حتى يظهر زر إرسال الأمر");
    finalSubmit.click();state(intent,"submitted","تم ضغط إرسال الأمر في سهم؛ تتم مراقبة حالته",{filled_quantity:0});closeOverlay();setTimeout(publishSnapshot,1500);
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

publishSnapshot();setInterval(publishSnapshot,10000);document.addEventListener("visibilitychange",()=>{if(!document.hidden)publishSnapshot()});
