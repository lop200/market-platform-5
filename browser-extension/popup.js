const $=id=>document.getElementById(id);
async function refresh(){const s=await chrome.runtime.sendMessage({type:"GET_POPUP_STATE"});$("marsad").textContent=s.marsad_open?"مفتوح":"غير مفتوح";$("sahm").textContent=s.sahm_open?"مفتوح":"غير مفتوح";$("login").textContent=s.snapshot?.logged_in?"مسجل":"غير مسجل";$("confirmMode").checked=s.confirm_mode===true}
$("confirmMode").onchange=()=>chrome.storage.local.set({confirmMode:$("confirmMode").checked});$("openSahm").onclick=()=>chrome.tabs.create({url:"https://app.sahmcapital.com/"});refresh();
