"use strict";
/* ALICE telefon uygulaması: Sohbet, Projeler, Bilgisayar, Bildirim, Daha fazla.
   Masaüstüyle aynı sunucu ve API'ler; anahtar sayfaya gömülü (alice phone). Android uygulaması bu sayfayı açar ve
   window.AliceNative ile telefonun konuşma tanımasını, titreşimi ve bildirimleri verir; yoksa tarayıcıda da çalışır. */

const TOKEN = document.querySelector('meta[name="alice-token"]').content;
const NATIVE = window.AliceNative || null;
/* Sayfa hataları bilgisayara yazılır (~/.alice/phone-errors.log): telefonda görünmeyen hataları bulmak için. */
function reportError(text) {
  try {
    fetch("/api/client_error", { method: "POST", headers: { "X-Alice-Token": TOKEN, "Content-Type": "application/json" },
      body: JSON.stringify({ text: String(text).slice(0, 1500) + " | " + navigator.userAgent.slice(0, 120) }) }).catch(() => {});
  } catch (e) { /* yok */ }
}
window.addEventListener("error", (e) => reportError("hata: " + e.message + " @" + e.filename + ":" + e.lineno + ":" + e.colno));
window.addEventListener("unhandledrejection", (e) => reportError("söz: " + ((e.reason && (e.reason.stack || e.reason.message)) || e.reason)));
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const S = {
  sid: (() => { try { return localStorage.getItem("alice.sid") || ""; } catch (e) { return ""; } })(),
  tab: "chat", status: {}, sessions: [], busy: false, pending: new Map(), online: true,
  remote: null, projects: [], images: [], cur: null, steps: null, notifTab: "pending", lastSeenEvent: 0,
  taskAsks: [],
};

/* ================================================================== yardımcılar */
async function api(path, body) {
  const opts = { headers: { "X-Alice-Token": TOKEN } };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  try {
    const r = await fetch(path, opts);
    setOnline(true);
    return await r.json();
  } catch (e) {
    setOnline(false);
    return { ok: false, error: "bilgisayara ulaşılamadı" };
  }
}
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
const P = {
  chat: '<path d="M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-4 3v-3a2 2 0 0 1-2-2z"/>',
  mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>',
  plus: '<path d="M12 5v14M5 12h14"/>', up: '<path d="M12 19V5M6 11l6-6 6 6"/>', hist: '<path d="M4 6h16M4 12h10M4 18h7"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/>', x: '<path d="M6 6l12 12M18 6L6 18"/>', check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  chev: '<path d="M9 6l6 6-6 6"/>', back: '<path d="M15 5l-7 7 7 7"/>', stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  phone: '<path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2"/>',
  bolt: '<path d="M13 3L5 13h6l-1 8 8-10h-6z"/>', shield: '<path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/>',
  brain: '<path d="M9 4a3 3 0 0 0-3 3 3 3 0 0 0-2 5 3 3 0 0 0 2 5 3 3 0 0 0 6 1V5a2 2 0 0 0-3-1zM15 4a3 3 0 0 1 3 3 3 3 0 0 1 2 5 3 3 0 0 1-2 5 3 3 0 0 1-6 1"/>',
  type: '<rect x="3" y="6" width="18" height="12" rx="2"/><path d="M7 10h.01M11 10h.01M15 10h.01M8 14h8"/>',
  expand: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>', play: '<path d="M8 5v14l11-7z"/>', pause: '<path d="M7 5h3v14H7zM14 5h3v14h-3z"/>',
  skipb: '<path d="M19 6L9 12l10 6zM6 6v12"/>', skipf: '<path d="M5 6l10 6-10 6zM18 6v12"/>',
  moon: '<path d="M20 14A8 8 0 1 1 10 4a6 6 0 0 0 10 10z"/>', note: '<path d="M6 4h9l4 4v12H6z"/><path d="M14 4v5h5M9 13h7M9 17h5"/>',
  heart: '<path d="M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10z"/>',
  dl: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M17 6l3 3"/>', wifi: '<path d="M2 9a15 15 0 0 1 20 0M5 12.5a10 10 0 0 1 14 0M8.5 16a5 5 0 0 1 7 0"/><circle cx="12" cy="19" r="1"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>',
  img: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="M21 16l-5-5-9 9"/>',
  trash: '<path d="M5 7h14M10 7V5h4v2M7 7l1 12h8l1-12"/>', pin: '<path d="M9 4h6l-1 5 3 3v2H7v-2l3-3zM12 14v6"/>',
};
function svg(name, cls) {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  if (cls) s.setAttribute("class", cls);
  s.innerHTML = P[name] || name;
  return s;
}
function btn(cls, label, onClick, icon) {
  const b = el("button", cls);
  if (icon) b.appendChild(svg(icon));
  if (label != null) b.appendChild(document.createTextNode(label));
  if (onClick) b.addEventListener("click", (e) => { haptic(); onClick(e); });
  return b;
}
function iconBtn(icon, label, onClick, cls) {
  const b = el("button", "ib" + (cls ? " " + cls : ""));
  b.setAttribute("aria-label", label);
  b.title = label;
  b.appendChild(svg(icon));
  if (onClick) b.addEventListener("click", (e) => { haptic(); onClick(e); });
  return b;
}
function haptic(ms) {
  try { if (NATIVE && NATIVE.vibrate) NATIVE.vibrate(ms || 8); else if (navigator.vibrate) navigator.vibrate(ms || 8); } catch (e) { /* yok */ }
}
let toastTimer = null;
function toast(text) {
  const t = $("#toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 2600);
}
function rel(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 45) return "şimdi";
  if (s < 3600) return Math.round(s / 60) + " dk";
  if (s < 86400) return Math.round(s / 3600) + " sa";
  return new Date(ts * 1000).toLocaleDateString("tr-TR", { day: "numeric", month: "short" });
}
const hhmm = (ts) => new Date(ts * 1000).toLocaleTimeString("tr-TR", { hour: "2-digit", minute: "2-digit" });
const md = (t) => DOMPurify.sanitize(marked.parse(t || ""));
const shortModel = (ref) => (ref ? ref.split(":").slice(1).join(":") || ref : "model yok");
const COLORS = ["#2f6bff", "#c2410c", "#15803d", "#7c3aed", "#a16207", "#be123c", "#0e7490", "#4d7c0f"];
const colorOf = (name) => COLORS[[...String(name)].reduce((a, c) => (a * 31 + c.charCodeAt(0)) >>> 0, 7) % COLORS.length];
function setOnline(v) {
  if (S.online === v) return;
  S.online = v;
  clearTimeout(setOnline._t);
  if (v) { $("#offline").hidden = true; return; }
  setOnline._t = setTimeout(() => { if (!S.online) $("#offline").hidden = false; }, 1500);
}

/* ================================================================== alt sayfa (sheet) */
let sheetClose = null;
function openSheet(build) {
  const sh = $("#sheet"), card = $(".sheet-card", sh);
  card.replaceChildren();
  build(card);
  sh.hidden = false;
  return new Promise((resolve) => { sheetClose = resolve; });
}
function closeSheet(v) {
  $("#sheet").hidden = true;
  const f = sheetClose;
  sheetClose = null;
  if (typeof onSheetClosed === "function") onSheetClosed();
  if (f) f(v);
}
let onSheetClosed = null;
$("#sheet").addEventListener("click", (e) => { if (e.target.id === "sheet") closeSheet(null); });
function confirmSheet(title, text, okLabel, danger) {
  return openSheet((c) => {
    c.append(el("h3", "", title));
    if (text) c.appendChild(el("p", "sub", text));
    const row = el("div", "btns");
    row.append(btn("btn", "Vazgeç", () => closeSheet(false)), btn("btn " + (danger ? "d" : "p"), okLabel || "Evet", () => closeSheet(true)));
    c.appendChild(row);
  });
}
function pickSheet(title, sub, options, current) {
  return openSheet((c) => {
    c.append(el("h3", "", title));
    if (sub) c.appendChild(el("p", "sub", sub));
    for (const [value, label, hint, dim] of options) {
      const o = el("button", "opt" + (dim ? " dim" : ""));
      const l = el("span", "l");
      l.append(el("b", "", label));
      if (hint) l.appendChild(el("small", "", hint));
      o.appendChild(l);
      if (value === current) o.appendChild(svg("check", "ck"));
      o.addEventListener("click", () => { haptic(); closeSheet(value); });
      c.appendChild(o);
    }
  });
}

/* ================================================================== sekmeler ve sayfa yığını */
const STACK = { chat: [], proj: [], pc: [], bell: [], more: [] };
function show(tab) {
  if (!Object.prototype.hasOwnProperty.call(RENDER, tab)) tab = "chat"; // unknown or stored tab name: never call arbitrary keys
  if (S.tab === tab && STACK[tab].length) { popAll(tab); return; }
  S.tab = tab;
  $$("#views .view").forEach((v) => { v.hidden = v.dataset.tab !== tab; });
  $$("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  try { localStorage.setItem("alice-tab", tab); } catch (e) { /* özel pencere */ }
  RENDER[tab]();
}
$$("#tabs button").forEach((b) => b.addEventListener("click", () => { haptic(); show(b.dataset.tab); }));
// Bir sekmenin üstüne ayrıntı sayfası açar (proje, ajan, otomasyonlar...). Android geri tuşu ve "‹" kapatır.
function push(tab, title, build, backLabel) {
  const v = $("#v-" + tab);
  const pg = el("div", "page push");
  const hd = el("div", "hd");
  const back = el("button", "back");
  back.append(svg("back"), document.createTextNode(backLabel || "Geri"));
  back.addEventListener("click", () => { haptic(); pop(tab); });
  hd.append(back, el("span", "grow"));
  const h = el("div", "hd");
  h.style.paddingTop = "0";
  const h1 = el("h1", "", title);
  h.appendChild(h1);
  const body = el("div", "scroll");
  pg.append(hd, h, body);
  v.appendChild(pg);
  const entry = { pg, body, hd, refresh: null };
  STACK[tab].push(entry);
  history.pushState({ alice: tab }, "");
  build(body, entry, hd);
  return entry;
}
function pop(tab) {
  const e = STACK[tab].pop();
  if (e) { e.pg.remove(); if (e.onClose) e.onClose(); }
  const top = STACK[tab][STACK[tab].length - 1];
  if (top && top.refresh) top.refresh();
  else if (!top) RENDER[tab]();
}
function popAll(tab) { while (STACK[tab].length) pop(tab); }
window.addEventListener("popstate", () => {
  if (!$("#sheet").hidden) { closeSheet(null); history.pushState({}, ""); return; }
  if (!$("#viewer").hidden) { closeViewer(); return; }
  if (STACK[S.tab].length) pop(S.tab);
});
// Android uygulaması geri tuşunu buraya yollar: true dönerse uygulama kapanmaz.
window.aliceBack = () => {
  if (CALL) {  // görüşmede geri tuşu görüşmeden çıkmasın
    if (!$("#sheet").hidden) closeSheet(null); else toast("Görüşmeyi bitirmek için Kapat'a bas");
    return true;
  }
  if (!$("#sheet").hidden) { closeSheet(null); return true; }
  if (!$("#viewer").hidden) { closeViewer(); return true; }
  if (STACK[S.tab].length) { pop(S.tab); return true; }
  if (S.tab !== "chat") { show("chat"); return true; }
  return false;
};

/* ================================================================== olaylar */
const handlers = {
  status: (ev) => applyStatus(ev),
  user: (ev) => { if (S.chatBuilt) addUser(ev.text, ev.images); },
  busy: (ev) => { S.busy = !!ev.value; updateComposer(); if (!S.busy) { finishSteps(); loadSessions(); } else showTyping(true); },
  stream_start: () => { S.cur = null; },
  delta: (ev) => {
    if (!S.chatBuilt) return;
    showTyping(false);
    closeSteps();
    if (!S.cur) S.cur = { el: addNode(el("div", "a cursor")), text: "" };
    S.cur.text += ev.text;
    S.cur.el.innerHTML = md(S.cur.text);
    stick();
  },
  stream_reset: () => { if (S.cur) { S.cur.el.remove(); S.cur = null; } },
  stream_end: (ev) => {
    if (!S.chatBuilt) return;
    showTyping(false);
    if (!S.cur && ev.text) S.cur = { el: addNode(el("div", "a")), text: ev.text };
    if (S.cur) { S.cur.el.classList.remove("cursor"); S.cur.el.innerHTML = md(ev.text || S.cur.text); }
    S.cur = null;
    if (S.busy) showTyping(true);
    stick();
  },
  tool_start: (ev) => { if (S.chatBuilt) { showTyping(false); stepStart(ev.id, ev.name, ev.summary); } },
  tool_end: (ev) => { if (S.chatBuilt) { stepEnd(ev.id, ev.name, ev.is_error); if (S.busy) showTyping(true); } },
  permission: (ev) => addPending(ev, "permission"),
  permission_done: (ev) => dropPending(ev.id),
  ask_choice: (ev) => addPending(ev, "choice"),
  ask_choice_done: (ev) => dropPending(ev.id),
  info: (ev) => { if (S.chatBuilt && /hata|başarısız|veremedi|durduruldu/i.test(ev.text || "")) addNode(el("div", "note err", ev.text)); },
  project_note: () => loadProjects(),
  team: () => loadProjects(),
  call: (ev) => incomingCall(ev),
  call_state: (ev) => callState(ev),
  call_audio: (ev) => callAudio(ev),
  call_turn_done: (ev) => callTurnDone(ev),
  call_end: (ev) => callEnded(ev),
  call_pcm: (ev) => callPcm(ev),
  call_pcm_clear: (ev) => { if (CALL && ev.id === CALL.id) liveClear(CALL); },
  call_mic_missing: (ev) => { if (CALL && ev.id === CALL.id) liveNativeMic(CALL, "Mikrofon sesi bilgisayara gitmiyor; telefonun konuşma tanımasıyla devam"); },
  call_text: (ev) => callText(ev),
};
const CHAT_EVENTS = new Set(["user", "busy", "stream_start", "delta", "stream_reset", "stream_end", "tool_start", "tool_end"]);
let es = null;
function connect() {
  // Anahtar adreste değil: sayfa açılırken kurulan HttpOnly çerezle doğrulanır (alice/app/phone.py phone_handler).
  es = new EventSource("/api/events");
  es.onopen = () => setOnline(true);
  es.onerror = () => {
    setOnline(false);
    if (es.readyState === EventSource.CLOSED) { es.close(); setTimeout(connect, 2500); }
  };
  es.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch (e) { return; }
    // Başka bir sohbetin (arkadaki sohbet, telefon görüşmesi) akışı açık sohbete yazılmasın.
    if (CHAT_EVENTS.has(ev.type) && ev.sid && S.sid && ev.sid !== S.sid) return;
    const h = handlers[ev.type];
    if (h) h(ev);
  };
}
/* Telefon kendi açık sohbetini tutar (S.sid). Eskiden bilgisayarın öndeki sohbetini izliyordu: telefonda yeni sohbet
   açınca bilgisayar da ona geçiyor, bilgisayarda başka sohbete geçince telefonun mesajı oraya gidiyordu. */
function setSid(sid) {
  S.sid = sid || "";
  try { localStorage.setItem("alice.sid", S.sid); } catch (e) { /* gizli sekme */ }
}
function applyStatus(st) {
  if (!st) return;
  Object.assign(S.status, st);
  if (!S.sid && st.session) setSid(st.session);  // ilk açılış: bilgisayarda açık olan sohbetle başla
  if (st.busy !== undefined || st.running) {
    S.busy = S.sid === st.session ? !!st.busy : (st.running || []).includes(S.sid);
    updateComposer();
  }
  for (const p of st.pending || []) addPending(p, p.type === "ask_choice" ? "choice" : "permission");
  if (S.tab === "chat" && !STACK.chat.length) drawChatHead();
  if (S.chatBuilt) drawChips();
}

/* ================================================================== SOHBET */
const VERBS = {
  read_file: "Okudu", write_file: "Yazdı", edit_file: "Düzenledi", multi_edit: "Düzenledi", apply_patch: "Yama",
  list_dir: "Klasör", glob: "Dosya aradı", grep: "Aradı", run_command: "Komut", web_fetch: "Sayfa okudu",
  web_search: "Web'de aradı", browser: "Tarayıcı", computer: "Ekran", windows: "Pencere", ui_elements: "Arayüz",
  ui_act: "Arayüz", clipboard: "Pano", system_info: "Sistem", process_list: "Süreçler", todo_write: "Plan",
  memory: "Hafıza", task: "Alt ajan", reflex: "Reflex", git: "Git", http_request: "İstek", file_ops: "Dosya",
};
const verb = (name) => VERBS[name] || name.replace(/^mcp__/, "").replace(/__/g, " · ");
const MODES = { plan: ["Plan", "Sadece okur, plan yazar"], ask: ["Sor", "Her işlemde izin ister"],
  auto: ["Otomatik", "Sadece tehlikeli işlemde sorar"], yolo: ["Tam erişim", "Hiçbir şey sormaz"] };
const THINK = { off: ["düşünmez", "En hızlı"], low: ["az düşünür", "Önerilen"], medium: ["orta düşünür", ""],
  high: ["çok düşünür", "Zor işler için"], xhigh: ["daha da çok", "Çok zor işler"], max: ["en çok", "En zor işler"],
  default: ["model karar verir", ""] };
const THINK_BINARY = { off: ["düşünmez", "Hemen cevap verir"], high: ["düşünür", "Önce düşünür"], default: ["model karar verir", ""] };
// Seçili modelin desteklediği seviyeler (sunucu modele göre verir).
function thinkOpts() {
  const table = S.status.thinking_kind === "binary" ? THINK_BINARY : THINK;
  return (S.status.thinking_levels || Object.keys(THINK)).filter((k) => table[k]).map((k) => [k, ...table[k]]);
}
function thinkText() {
  const table = S.status.thinking_kind === "binary" ? THINK_BINARY : THINK;
  if (S.status.thinking_kind === "none") return "düşünme yok";
  return ((table[S.status.thinking_effective || S.status.thinking] || ["düşünme"])[0]);
}

function buildChat() {
  const v = $("#v-chat");
  v.replaceChildren();
  const hd = el("div", "hd center");
  hd.id = "chatHd";
  const body = el("div", "scroll");
  body.id = "chatScroll";
  const msgs = el("div");
  msgs.id = "msgs";
  body.appendChild(msgs);
  const chips = el("div", "chips");
  chips.id = "chips";
  const comp = el("form", "comp");
  comp.id = "composer";
  comp.autocomplete = "off";
  const plus = el("button", "plus");
  plus.type = "button";
  plus.setAttribute("aria-label", "Görsel ekle");
  plus.appendChild(svg("plus"));
  plus.addEventListener("click", () => { haptic(); $("#imgPick").click(); });
  const box = el("div", "in");
  const att = el("div", "att");
  att.id = "att";
  att.hidden = true;
  const ta = el("textarea");
  ta.id = "q";
  ta.rows = 1;
  ta.placeholder = "ALICE'e yaz…";
  ta.setAttribute("enterkeyhint", "send");
  box.append(att, ta);
  const go = el("button", "round");
  go.type = "button";
  go.id = "go";
  comp.append(plus, box, go);
  v.append(hd, body, chips, comp);
  ta.addEventListener("input", () => { autosize(ta); updateComposer(); });
  ta.addEventListener("focus", () => document.body.classList.add("kb"));
  ta.addEventListener("blur", () => setTimeout(() => document.body.classList.remove("kb"), 120));
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing && matchMedia("(pointer: fine)").matches) { e.preventDefault(); send(); }
  });
  go.addEventListener("click", () => {
    haptic();
    if (S.busy && !ta.value.trim()) { api("/api/stop", { sid: S.sid }); return; }
    if (ta.value.trim() || S.images.length) send();
    else voiceSheet();
  });
  S.chatBuilt = true;
  drawChatHead();
  drawChips();
  updateComposer();
  loadHistory();
}
function autosize(ta) { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 140) + "px"; }
function updateComposer() {
  const go = $("#go"), ta = $("#q");
  if (!go) return;
  const has = !!(ta.value.trim() || S.images.length);
  go.replaceChildren();
  go.classList.toggle("stop", S.busy && !has);
  if (S.busy && !has) { go.appendChild(svg("stop")); go.setAttribute("aria-label", "Durdur"); }
  else if (has) { go.appendChild(svg("up")); go.setAttribute("aria-label", "Gönder"); }
  else { go.appendChild(svg("mic")); go.setAttribute("aria-label", "Sesle yaz"); }
}
function drawChatHead() {
  const hd = $("#chatHd");
  if (!hd) return;
  hd.replaceChildren();
  const s = S.sessions.find((x) => x.id === S.sid);
  const t = el("button", "t");
  t.append(el("b", "", s ? s.title || "(boş)" : "Yeni sohbet"));
  const conn = el("small", S.online ? "" : "off", (S.remote && S.remote.host ? S.remote.host : "Bilgisayar") + (S.online ? " · bağlı" : " · bağlantı yok"));
  t.appendChild(conn);
  if (s) t.addEventListener("click", () => renameSession(s));
  hd.append(iconBtn("hist", "Geçmiş sohbetler", historySheet), t,
    iconBtn("phone", "ALICE'i ara", () => startCall()),
    iconBtn("plus", "Yeni sohbet", newChat));
}
function drawChips() {
  const c = $("#chips");
  if (!c) return;
  c.replaceChildren();
  const m = MODES[S.status.mode] || ["mod", ""];
  const mode = btn("chip" + (S.status.mode === "yolo" ? " warn" : ""), m[0], modeSheet, "shield");
  const model = btn("chip", shortModel(S.status.model), modelSheet, "bolt");
  const think = btn("chip", thinkText(), thinkSheet, "brain");
  c.append(mode, model, think);
}
async function modeSheet() {
  const v = await pickSheet("İzin modu", "ALICE bilgisayarda bir şey yapmadan önce ne zaman sorsun?",
    Object.entries(MODES).map(([k, [l, h]]) => [k, l, h]), S.status.mode);
  if (v) applyStatus(await api("/api/mode", { mode: v }));
}
async function thinkSheet() {
  const note = S.status.thinking_kind === "none" ? "Bu model düşünme ayarı desteklemiyor." : "";
  const v = await pickSheet("Düşünme", note, thinkOpts(), S.status.thinking_effective || S.status.thinking);
  if (v) applyStatus(await api("/api/thinking", { level: v }));
}
async function modelSheet() {
  const all = await api("/api/models/all");
  const opts = [["reset", "Otomatik", "İlk uygun model; limit dolunca sıradaki"]];
  for (const p of (all && all.providers) || []) {
    if (!p.enabled || p.hidden) continue;
    for (const m of p.models || []) if (!m.hidden) opts.push([m.ref, m.model, p.name + " · " + m.status, m.status !== "hazır"]);
  }
  const v = await pickSheet("Model", "", opts, all && all.pinned ? all.pinned : "reset");
  if (!v) return;
  await api("/api/model", { role: "brain", ref: v });
  applyStatus(await api("/api/state"));
  toast(v === "reset" ? "Otomatik seçim" : "Model: " + shortModel(v));
}
async function newChat() {
  const r = await api("/api/new", { detached: true });
  if (!r || r.ok === false || !r.sid) {
    reportError("yeni sohbet: " + JSON.stringify(r).slice(0, 300));
    return toast((r && r.error) || "Yeni sohbet açılamadı");
  }
  setSid(r.sid);
  applyStatus(r);
  $("#msgs").replaceChildren();
  emptyChat();
  loadSessions();
}
function emptyChat() {
  const e = el("div", "empty");
  e.id = "chatEmpty";
  const img = el("img");
  img.src = "/static/alice.png";
  img.alt = "";
  e.append(img, el("div", "", S.status.user ? "Merhaba " + S.status.user + ". Ne yapalım?" : "Ne yapalım?"));
  $("#msgs").appendChild(e);
}
function addNode(n) {
  const e = $("#chatEmpty");
  if (e) e.remove();
  $("#msgs").appendChild(n);
  stick();
  return n;
}
function stick(force) {
  const sc = $("#chatScroll");
  if (!sc) return;
  if (force || sc.scrollHeight - sc.scrollTop - sc.clientHeight < 160) sc.scrollTop = sc.scrollHeight;
}
let typingEl = null;
function showTyping(on) {
  if (on && !typingEl && S.chatBuilt) { typingEl = el("div", "typing"); typingEl.append(el("i"), el("i"), el("i")); addNode(typingEl); }
  if (!on && typingEl) { typingEl.remove(); typingEl = null; }
  if (on && typingEl && typingEl !== $("#msgs").lastElementChild) $("#msgs").appendChild(typingEl);
}
function addUser(text, images) {
  const b = el("div", "u");
  for (const src of images || []) {
    const i = el("img");
    i.src = src.startsWith("data:") ? src : "data:image/png;base64," + src;
    b.appendChild(i);
  }
  if (text) b.appendChild(document.createTextNode(text));
  addNode(b);
  stick(true);
}
// Art arda araçlar tek "adımlar" kartında; başlık özet, dokununca liste açılır.
function makeSteps() {
  const box = el("div", "steps run");
  const h = el("button", "steps-h");
  const ic = el("span", "st-ic");
  ic.appendChild(svg("check"));
  const t = el("b", "", "Çalışıyor");
  const meta = el("span", "", "");
  h.append(ic, t, meta, el("span", "grow"), svg("chev", "chev"));
  h.addEventListener("click", () => box.classList.toggle("open"));
  const b = el("div", "steps-b");
  box.append(h, b);
  box._t = t; box._meta = meta; box._b = b; box._t0 = Date.now(); box._ids = new Map();
  return box;
}
function updateSteps(box) {
  const rows = $$(".step", box._b);
  const run = rows.filter((r) => r.classList.contains("run")).length;
  const err = rows.filter((r) => r.classList.contains("err")).length;
  box.classList.toggle("run", run > 0);
  box.classList.toggle("err", err > 0);
  box._t.textContent = run ? `Çalışıyor · ${rows.length} adım` : `${rows.length} adım`;
  const secs = ((box._t1 || Date.now()) - box._t0) / 1000;
  box._meta.textContent = [err ? `${err} hata` : "", !run && box._t1 && secs >= 0.1 ? `· ${secs.toFixed(1).replace(".", ",")} sn` : ""].filter(Boolean).join(" ");
}
function stepStart(id, name, summary) {
  if (name === "todo_write") return;
  if (!S.steps) S.steps = addNode(makeSteps());
  const r = el("div", "step run");
  r.append(el("b", "", verb(name)), el("span", "", summary || ""));
  S.steps._b.appendChild(r);
  if (id) S.steps._ids.set(id, r);
  updateSteps(S.steps);
  stick();
}
function stepEnd(id, name, isErr) {
  if (!S.steps) return;
  const r = (id && S.steps._ids.get(id)) || $$(".step.run", S.steps._b).pop();
  if (r) { r.classList.remove("run"); if (isErr) r.classList.add("err"); }
  S.steps._t1 = Date.now();
  updateSteps(S.steps);
}
function closeSteps() { if (S.steps) { updateSteps(S.steps); S.steps = null; } }
function finishSteps() { showTyping(false); closeSteps(); }

async function loadHistory() {
  const h = await api("/api/history?sid=" + encodeURIComponent(S.sid || ""));
  const items = Array.isArray(h) ? h : (h.messages || []);
  const msgs = $("#msgs");
  if (!msgs) return;
  msgs.replaceChildren();
  S.steps = null;
  if (!items.length) { emptyChat(); return; }
  for (const it of items.slice(-80)) {
    if (it.type === "user") { closeSteps(); addUser(it.text, it.images); }
    else if (it.type === "assistant") { closeSteps(); if (it.text) addNode(el("div", "a")).innerHTML = md(it.text); }
    else if (it.type === "tool") { stepStart(null, it.name, it.summary); stepEnd(null, it.name, it.is_error); }
    else if (it.type === "info" && /hata|başarısız/i.test(it.text || "")) addNode(el("div", "note err", it.text));
  }
  if (S.steps) { S.steps._t1 = 0; updateSteps(S.steps); S.steps = null; }
  $$(".steps", msgs).forEach((b) => { b._t1 = 0; updateSteps(b); });
  if (S.busy) showTyping(true);
  stick(true);
}
async function send() {
  const ta = $("#q");
  const text = ta.value.trim();
  if (!text && !S.images.length) return;
  if (S.busy && text.startsWith("/")) return toast("Komut yeni iş başlatır; ALICE bitince gönder ya da durdur");
  const images = S.images.slice();
  // Çalışırken gönderilen mesaj işe iletilir (ALICE bir sonraki adımda görür), bitmesi beklenmez.
  const r = await api("/api/send", { text, images, sid: S.sid });
  if (!r.ok) { toast(r.error || "gönderilemedi"); return; }
  ta.value = "";
  autosize(ta);
  S.images = [];
  drawAttachments();
  updateComposer();
  ta.blur();
}
function drawAttachments() {
  const a = $("#att");
  if (!a) return;
  a.replaceChildren();
  a.hidden = !S.images.length;
  S.images.forEach((b64, i) => {
    const w = el("span");
    const img = el("img");
    img.src = "data:image/png;base64," + b64;
    const x = el("button");
    x.appendChild(svg("x"));
    x.addEventListener("click", () => { S.images.splice(i, 1); drawAttachments(); updateComposer(); });
    w.append(img, x);
    a.appendChild(w);
  });
}
$("#imgPick").addEventListener("change", (e) => {
  const f = e.target.files && e.target.files[0];
  e.target.value = "";
  if (!f) return;
  const img = new Image();
  img.onload = () => {
    // Büyük fotoğraflar küçültülür: model için 1600 px yeter, yükleme hızlanır.
    const k = Math.min(1, 1600 / Math.max(img.width, img.height));
    const c = document.createElement("canvas");
    c.width = Math.round(img.width * k);
    c.height = Math.round(img.height * k);
    c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
    S.images.push(c.toDataURL("image/jpeg", 0.85).split(",")[1]);
    URL.revokeObjectURL(img.src);
    drawAttachments();
    updateComposer();
  };
  img.src = URL.createObjectURL(f);
});

// Geçmiş sohbetler: sabitlenenler üstte; dokun aç, uzun bas adlandır/sabitle/sil.
async function loadSessions() {
  const r = await api("/api/sessions");
  S.sessions = Array.isArray(r) ? r : [];
  drawChatHead();
}
function historySheet() {
  openSheet((c) => {
    c.append(el("h3", "", "Sohbetler"));
    const nw = el("button", "opt new");
    nw.append(svg("plus", "ic"), el("span", "l", "Yeni sohbet"));
    nw.addEventListener("click", () => { closeSheet(); newChat(); });
    c.appendChild(nw);
    const rows = [...S.sessions.filter((s) => s.pinned), ...S.sessions.filter((s) => !s.pinned)];
    if (!rows.length) c.appendChild(el("p", "sub", "Henüz sohbet yok."));
    for (const s of rows) {
      const o = el("button", "opt");
      const l = el("span", "l");
      l.append(el("b", "", (s.pinned ? "• " : "") + (s.title || "(boş)")), el("small", "", rel(s.updated) + " · " + (s.cwd || "").split(/[\\/]/).pop()));
      o.appendChild(l);
      if (s.id === S.sid) o.appendChild(svg("check", "ck"));
      o.addEventListener("click", async () => {
        closeSheet();
        const r = await api("/api/resume", { id: s.id, detached: true });
        if (!r.ok) return toast(r.error || "açılamadı");
        setSid(r.sid);
        applyStatus(r);
        loadHistory();
      });
      let t = null;
      o.addEventListener("touchstart", () => { t = setTimeout(() => { haptic(20); closeSheet(); sessionMenu(s); }, 550); }, { passive: true });
      o.addEventListener("touchend", () => clearTimeout(t));
      o.addEventListener("touchmove", () => clearTimeout(t), { passive: true });
      o.addEventListener("contextmenu", (e) => { e.preventDefault(); closeSheet(); sessionMenu(s); });
      c.appendChild(o);
    }
    c.appendChild(el("p", "sub", "Uzun bas: yeniden adlandır, sabitle ya da sil."));
  });
}
async function sessionMenu(s) {
  const v = await pickSheet(s.title || "(boş)", "", [["rename", "Yeniden adlandır"], ["pin", s.pinned ? "Sabitlemeyi kaldır" : "Sabitle"], ["del", "Sil"]]);
  if (v === "rename") renameSession(s);
  if (v === "pin") { await api("/api/session/pin", { id: s.id, pinned: !s.pinned }); loadSessions(); }
  if (v === "del" && await confirmSheet("Sohbet silinsin mi?", `"${s.title || "(boş)"}" geçmişten kalıcı olarak silinecek.`, "Sil", true)) {
    const r = await api("/api/session/delete", { id: s.id });
    if (r.ok === false) return toast(r.error || "silinemedi");
    if (s.id === S.sid) newChat(); else loadSessions();
  }
}
function renameSession(s) {
  let inp;
  openSheet((c) => {
    c.append(el("h3", "", "Sohbetin adı"));
    inp = el("input", "field");
    inp.value = s.title || "";
    inp.placeholder = s.auto_title || "Sohbet adı";
    const row = el("div", "btns");
    const save = async () => {
      closeSheet();
      await api("/api/session/rename", { id: s.id, title: inp.value.trim() });
      loadSessions();
    };
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") save(); });
    row.append(btn("btn", "Vazgeç", () => closeSheet()), btn("btn p", "Kaydet", save));
    c.append(inp, row);
    setTimeout(() => inp.focus(), 50);
  });
}

/* ================================================================== SESLE YAZ */
/* Üç yol: Android uygulamasında telefonun konuşma tanıma (canlı yazı), güvenli bağlantıda tarayıcı mikrofonu,
   ikisi de yoksa telefonun ses kaydedicisi (dosya) — kayıt bilgisayarda yazıya çevrilir (/api/transcribe). */
let voice = null;
function voiceSheet() {
  if (!NATIVE && !(navigator.mediaDevices && window.isSecureContext && window.MediaRecorder)) { $("#audioPick").click(); return; }
  voice = { text: "", partial: "", t0: Date.now(), timer: null, rec: null, chunks: [], stream: null };
  let wave, heard, info, root;
  openSheet((c) => {
    root = el("div", "voice");
    const top = el("div", "row");
    info = el("span", "", "Dinliyorum · 0:00");
    top.append(info, el("span", "", "Susunca gönderir"));
    wave = el("div", "wave");
    for (let i = 0; i < 18; i++) wave.appendChild(el("i"));
    heard = el("div", "heard", "");
    const bottom = el("div", "row");
    const cancel = iconBtn("x", "Vazgeç", () => { stopVoice(false); closeSheet(); });
    const mic = el("div", "bigmic");
    mic.appendChild(svg("mic"));
    const ok = iconBtn("up", "Gönder", () => stopVoice(true));
    bottom.append(cancel, mic, ok);
    root.append(el("div", "grow"), top, wave, heard, el("div", "grow"), bottom);
    root.firstChild.style.display = "none";
    c.appendChild(root);
  });
  onSheetClosed = () => { if (voice) stopVoice(false); onSheetClosed = null; };
  voice.render = () => {
    const s = Math.round((Date.now() - voice.t0) / 1000);
    info.textContent = (voice.done ? "Yazıya çevriliyor" : "Dinliyorum") + ` · ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    heard.replaceChildren(document.createTextNode(voice.text ? "“" + voice.text : (voice.partial ? "“" : "")));
    if (voice.partial) heard.appendChild(el("span", "", (voice.text ? " " : "") + voice.partial));
    if (voice.text || voice.partial) heard.appendChild(document.createTextNode("”"));
  };
  voice.level = (lv) => {
    const bars = $$("i", wave);
    bars.forEach((b, i) => { b.style.height = Math.max(6, Math.min(66, lv * 70 * (0.5 + Math.abs(Math.sin(Date.now() / 160 + i))))) + "px"; });
  };
  voice.timer = setInterval(() => voice && voice.render(), 250);
  if (NATIVE && NATIVE.listen) { NATIVE.listen("tr-TR"); return; }
  navigator.mediaDevices.getUserMedia({ audio: true }).then((stream) => {
    voice.stream = stream;
    const rec = new MediaRecorder(stream);
    voice.rec = rec;
    rec.ondataavailable = (e) => voice && voice.chunks.push(e.data);
    rec.start(250);
    try {
      const ctx = new AudioContext();
      const an = ctx.createAnalyser();
      ctx.createMediaStreamSource(stream).connect(an);
      const buf = new Uint8Array(an.fftSize);
      const tick = () => {
        if (!voice || !voice.rec) return ctx.close();
        an.getByteTimeDomainData(buf);
        let m = 0;
        for (const x of buf) m = Math.max(m, Math.abs(x - 128));
        voice.level(m / 128);
        requestAnimationFrame(tick);
      };
      tick();
    } catch (e) { /* görselleştirme yok */ }
  }).catch(() => { stopVoice(false); closeSheet(); toast("Mikrofon izni verilmedi"); });
}
// Android uygulaması konuşma tanıma olaylarını buraya yollar.
window.aliceVoice = (ev) => {
  if (CALL && CALL.listening) return callVoice(ev);
  if (!voice) return;
  if (ev.type === "partial") voice.partial = ev.text || "";
  else if (ev.type === "final") { voice.text = ((voice.text ? voice.text + " " : "") + (ev.text || "")).trim(); voice.partial = ""; }
  else if (ev.type === "level") voice.level(Math.max(0, Math.min(1, ev.level || 0)));
  else if (ev.type === "error") { toast(ev.text || "Ses anlaşılamadı"); if (!voice.text) { stopVoice(false); closeSheet(); return; } }
  else if (ev.type === "end") { if (voice.text) { stopVoice(true); return; } }
  voice.render();
};
async function stopVoice(sendIt) {
  const v = voice;
  if (!v) return;
  voice = null;
  clearInterval(v.timer);
  if (NATIVE && NATIVE.listen) {
    try { if (sendIt) NATIVE.stopListening(); else NATIVE.cancelListening(); } catch (e) { /* yok */ }
    const text = (v.text + " " + v.partial).trim();
    closeSheet();
    if (sendIt && text) putAndSend(text);
    return;
  }
  if (v.rec) {
    const done = new Promise((res) => { v.rec.onstop = res; });
    try { v.rec.stop(); } catch (e) { /* zaten durdu */ }
    await done;
    v.stream.getTracks().forEach((t) => t.stop());
  }
  closeSheet();
  if (!sendIt || !v.chunks.length) return;
  transcribeAndSend(new Blob(v.chunks, { type: v.rec.mimeType || "audio/webm" }));
}
$("#audioPick").addEventListener("change", (e) => {
  const f = e.target.files && e.target.files[0];
  e.target.value = "";
  if (f) transcribeAndSend(f);
});
async function transcribeAndSend(blob) {
  toast("Yazıya çevriliyor…");
  const b64 = await new Promise((res) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1] || ""); r.readAsDataURL(blob); });
  const r = await api("/api/transcribe", { audio: b64, mime: blob.type || "audio/webm" });
  if (!r.ok || !r.text) return toast(r.error || "Ses anlaşılamadı");
  putAndSend(r.text);
}
function putAndSend(text) {
  const ta = $("#q");
  if (!ta) return;
  ta.value = (ta.value ? ta.value + " " : "") + text;
  autosize(ta);
  updateComposer();
  send();
}

/* ================================================================== PROJELER */
const PST = { planning: "planlanıyor", running: "çalışıyor", review: "incelemede", needs_you: "seni bekliyor", done: "bitti", stopped: "durdu", failed: "olmadı" };
const MST = { running: "çalışıyor", planning: "planlıyor", review: "inceleme bekliyor", asking: "soruyor", waiting: "bekliyor", done: "onaylandı",
  failed: "olmadı", stopped: "durdu", idle: "beklemede" };
async function loadProjects() {
  const rows = await api("/api/projects");
  S.projects = Array.isArray(rows) ? rows : [];
  const waiting = S.projects.filter((p) => p.questions || (p.members || []).some((m) => m.status === "review")).length;
  const b = $("#projBadge");
  b.hidden = !waiting;
  b.textContent = waiting;
  if (S.tab === "proj" && !STACK.proj.length) drawProjects();
  const top = STACK.proj[STACK.proj.length - 1];
  if (S.tab === "proj" && top && top.refresh) top.refresh();
  updateBellBadge();
}
function renderProjects() {
  const v = $("#v-proj");
  if (STACK.proj.length) return;
  v.replaceChildren();
  const hd = el("div", "hd");
  hd.appendChild(el("h1", "", "Projeler"));
  const body = el("div", "scroll");
  body.id = "projList";
  const fab = btn("fab", "Yeni proje", newProject, "plus");
  v.append(hd, body, fab);
  drawProjects();
  loadProjects();
}
async function drawProjects() {
  const body = $("#projList");
  if (!body) return;
  body.replaceChildren();
  if (!S.projects.length) {
    body.appendChild(el("div", "empty", "Henüz proje yok. Büyük bir işi ALICE'in yönettiği bir ajan takımına ver: işi böler, biten işi inceler, sonunda sana özet yazar."));
    return;
  }
  // Soru soran projelerin sorusu kartta görünsün diye ayrıntısı çekilir.
  const asking = S.projects.filter((p) => p.questions).slice(0, 4);
  const details = Object.fromEntries(await Promise.all(asking.map(async (p) => [p.id, await api("/api/project?id=" + encodeURIComponent(p.id))])));
  body.replaceChildren();
  for (const p of S.projects.slice(0, 40)) {
    const card = el("div", "proj");
    card.setAttribute("role", "button");
    const h = el("div", "h");
    const st = p.questions ? "needs_you" : p.status;
    h.append(el("b", "", p.title || "(adsız)"), el("span", "st st-" + st, PST[st] || st));
    card.appendChild(h);
    const d = details[p.id];
    for (const q of ((d && d.questions) || []).filter((x) => !x.answer).slice(0, 1)) card.appendChild(askBox(p.id, q));
    const mem = p.members || [];
    const done = mem.filter((m) => m.status === "done").length;
    if (mem.length && p.status !== "done") { const bar = el("div", "bar"); const i = el("i"); i.style.width = Math.round(100 * done / mem.length) + "%"; bar.appendChild(i); card.appendChild(bar); }
    const meta = el("div", "meta");
    const avs = el("span", "avs");
    for (const m of mem.slice(0, 5)) { const a = el("i", "", m.name[0].toUpperCase()); a.style.background = colorOf(m.name); avs.appendChild(a); }
    meta.append(avs, el("span", "", mem.length ? `${done}/${mem.length} iş onaylandı` : "ajan yok"), el("span", "", "· " + rel(p.updated || p.created)));
    card.appendChild(meta);
    card.addEventListener("click", (e) => { if (!e.target.closest(".ask")) { haptic(); openProject(p); } });
    body.appendChild(card);
  }
}
function askBox(pid, q) {
  const box = el("div", "ask");
  const p = el("div");
  p.append(el("b", "", (q.member || "Alice") + " soruyor: "), document.createTextNode(q.text));
  const row = el("div", "ans");
  const inp = el("input");
  inp.placeholder = "Cevabın…";
  const go = el("button", "", "Gönder");
  const sendIt = async () => {
    const t = inp.value.trim();
    if (!t) return;
    const r = await api("/api/project/action", { id: pid, action: "answer", name: q.id, text: t });
    if (!r.ok) return toast(r.error || "olmadı");
    toast("Cevap gönderildi");
    loadProjects();
  };
  go.addEventListener("click", sendIt);
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") sendIt(); });
  row.append(inp, go);
  box.append(p, row);
  return box;
}
function newProject() {
  let ta;
  openSheet((c) => {
    c.append(el("h3", "", "Yeni proje"), el("p", "sub", "Ne yapılacak? Bitti sayılma ölçütünü de yaz. ALICE işi ajanlara böler ve takip eder."));
    ta = el("textarea", "field");
    ta.placeholder = "ör. Kayıt sayfası yap: e-posta ile kayıt, doğrulama maili. Testler geçmeli.";
    const row = el("div", "btns");
    const go = btn("btn acc", "Başlat", async () => {
      const goal = ta.value.trim();
      if (!goal) return ta.focus();
      go.disabled = true;
      const r = await api("/api/project/create", { goal, review: true });
      closeSheet();
      if (!r.ok) return toast(r.error || "başlatılamadı");
      toast("Proje başladı");
      await loadProjects();
      const p = S.projects.find((x) => x.id === r.id);
      if (p) openProject(p);
    });
    row.append(btn("btn", "Vazgeç", () => closeSheet()), go);
    c.append(ta, row);
    setTimeout(() => ta.focus(), 60);
  });
}
function openProject(p) {
  push("proj", p.title || "Proje", (body, entry) => {
    const draw = async () => {
      const d = await api("/api/project?id=" + encodeURIComponent(p.id));
      if (!d || !d.id) { body.replaceChildren(el("div", "empty", (d && d.error) || "Proje açılamadı")); return; }
      body.replaceChildren();
      const head = el("div", "meta");
      head.style.marginTop = "0";
      const [done, total] = d.progress || [0, 0];
      head.append(el("span", "st st-" + d.status, PST[d.status] || d.status), el("span", "", total ? `${done}/${total} iş onaylandı` : ""));
      body.appendChild(head);
      if (d.goal && !d.goal.startsWith((d.title || "").replace(/[….]+$/, ""))) body.appendChild(el("p", "goal", d.goal));
      for (const q of (d.questions || []).filter((x) => !x.answer)) body.appendChild(askBox(p.id, q));
      body.appendChild(el("div", "lbl", "Ekip"));
      const lead = el("button", "mem");
      const la = el("span", "av"); la.style.background = colorOf("Alice"); la.textContent = "A";
      const ll = el("span", "l"); ll.append(el("b", "", "Alice"), el("small", "", "Planlıyor, yönetiyor, inceliyor"));
      lead.append(la, ll);
      lead.addEventListener("click", () => openMember(p, { name: "alice", status: d.lead && d.lead.status, task: "Yönetici" }));
      body.appendChild(lead);
      for (const m of d.members || []) {
        const r = el("button", "mem");
        const a = el("span", "av", m.name[0].toUpperCase());
        a.style.background = colorOf(m.name);
        a.appendChild(el("i", "dot s-" + m.status));
        const l = el("span", "l");
        l.append(el("b", "", m.name), el("small", "", m.last || m.task || ""));
        r.append(a, l, el("span", "s s-" + m.status, MST[m.status] || m.status));
        r.addEventListener("click", () => { haptic(); openMember(p, m); });
        body.appendChild(r);
      }
      const acts = el("div", "btns");
      if (d.active) acts.appendChild(btn("btn d", "Durdur", async () => { if (await confirmSheet("Proje durdurulsun mu?", "Çalışan ajanlar durur; sonra devam ettirebilirsin.", "Durdur", true)) projAct(p.id, "stop", ""); }));
      else if (d.status !== "done") acts.appendChild(btn("btn", "Devam ettir", () => projAct(p.id, "resume", "")));
      body.appendChild(acts);
    };
    entry.refresh = draw;
    draw();
    const t = setInterval(() => { if (STACK.proj[STACK.proj.length - 1] === entry && !document.hidden) draw(); }, 6000);
    entry.onClose = () => clearInterval(t);
  }, "Projeler");
}
async function projAct(id, action, name, text) {
  const r = await api("/api/project/action", { id, action, name, text: text || "" });
  if (!r.ok) { toast(r.error || "olmadı"); return false; }
  if (r.note && !["send", "answer"].includes(action)) toast(r.note);
  const top = STACK.proj[STACK.proj.length - 1];
  if (top && top.refresh) top.refresh();
  loadProjects();
  return true;
}
function openMember(p, m) {
  push("proj", m.name === "alice" ? "Alice" : m.name, (body, entry) => {
    entry.pg.appendChild((() => {
      const say = el("div", "say");
      const inp = el("input");
      inp.placeholder = m.name === "alice" ? "Alice'e yaz…" : m.name + "'e yaz: kaldığı yerden devam eder…";
      const go = btn("round", null, async () => {
        const t = inp.value.trim();
        if (!t) return;
        inp.value = "";
        await projAct(p.id, "send", m.name, t);
      }, "up");
      inp.addEventListener("keydown", (e) => { if (e.key === "Enter") go.click(); });
      say.append(inp, go);
      return say;
    })());
    const draw = async () => {
      const d = await api(`/api/project?id=${encodeURIComponent(p.id)}&member=${encodeURIComponent(m.name)}`);
      if (!d || !d.id) return;
      body.replaceChildren();
      const mem = d.member || (d.members || []).find((x) => x.name === m.name) || m;
      if (mem.task) body.appendChild(el("p", "goal", mem.task));
      if (mem.status === "review") {
        const rv = el("div", "review");
        rv.appendChild(el("p", "", "İş bitti, inceleme bekliyor. Alice de inceliyor; istersen kararı sen ver."));
        const fb = el("textarea");
        fb.placeholder = "Düzeltme istiyorsan ne eksik?";
        const row = el("div", "btns");
        row.append(btn("btn", "Düzeltme iste", async () => { const t = fb.value.trim(); if (!t) { fb.focus(); return toast("Önce ne eksik yaz"); } projAct(p.id, "revise", m.name, t); }),
          btn("btn acc", "Onayla", () => projAct(p.id, "approve", m.name)));
        rv.append(fb, row);
        body.appendChild(rv);
      }
      const hist = (mem.history || d.history || []).slice(-40);
      if (!hist.length) body.appendChild(el("div", "empty", "Henüz mesaj yok"));
      for (const it of hist) {
        const f = el("div", "feed-item");
        if (it.type === "tool") { f.append(el("div", "who", verb(it.name)), el("div", "tx mono", (it.summary || "").slice(0, 200))); f.querySelector(".tx").style.fontSize = "12.5px"; }
        else {
          f.appendChild(el("div", "who", it.type === "user" ? (it.from || "görev") : (m.name === "alice" ? "Alice" : m.name)));
          const tx = el("div", "tx");
          tx.innerHTML = md(it.text || "");
          f.appendChild(tx);
        }
        body.appendChild(f);
      }
      body.scrollTop = body.scrollHeight;
    };
    entry.refresh = draw;
    draw();
    const t = setInterval(() => { if (STACK.proj[STACK.proj.length - 1] === entry && !document.hidden) draw(); }, 5000);
    entry.onClose = () => clearInterval(t);
  }, p.title && p.title.length < 18 ? p.title : "Proje");
}

/* ================================================================== BİLGİSAYAR */
let shotUrl = null, shotTimer = null, viewerTimer = null, apps = null;
function renderPc() {
  const v = $("#v-pc");
  if (STACK.pc.length) return;
  v.replaceChildren();
  const hd = el("div", "hd");
  const h1 = el("h1", "", (S.remote && S.remote.host) || "Bilgisayar");
  h1.id = "pcName";
  const mode = btn("pill-btn", "Otomatik", pcModeSheet, "bolt");
  mode.id = "pcMode";
  hd.append(h1, mode);
  const body = el("div", "scroll");
  body.id = "pcBody";
  const hero = el("button", "hero");
  hero.id = "hero";
  hero.setAttribute("aria-label", "Ekranı tam boy aç");
  const img = el("img");
  img.alt = "";
  img.hidden = true;
  const none = el("div", "none", "Ekran yükleniyor…");
  const ov = el("div", "ov");
  const g = el("div", "grow");
  g.append(el("b", "", ""), el("small", "stale", "bağlanıyor"));
  const ex = el("span", "ex");
  ex.appendChild(svg("expand"));
  ov.append(g, ex);
  hero.append(img, none, ov);
  hero.addEventListener("click", () => { haptic(); openViewer(); });
  const tele = el("div", "tele");
  tele.id = "tele";
  for (const [k, label] of [["cpu", "CPU"], ["ram", "RAM"], ["gpu", "GPU"], ["gpu_temp", "Sıcaklık"]]) {
    const c = el("div");
    c.dataset.k = k;
    c.append(el("span", "", label), document.createElementNS("http://www.w3.org/2000/svg", "svg"), el("b", "", "–"));
    c.children[1].setAttribute("class", "sp");
    c.children[1].setAttribute("viewBox", "0 0 54 20");
    tele.appendChild(c);
  }
  const np = el("div", "nowp");
  np.id = "nowp";
  np.hidden = true;
  const wins = el("div");
  wins.id = "wins";
  const cmd = el("div", "cmd");
  const inp = el("input");
  inp.id = "cmdIn";
  inp.placeholder = "Bilgisayarda yaz, aç ya da kopyala";
  inp.setAttribute("enterkeyhint", "go");
  cmd.append(svg("type"), inp);
  const cacts = el("div", "cmd-acts");
  cacts.id = "cmdActs";
  inp.addEventListener("input", drawCmdActs);
  inp.addEventListener("focus", () => { document.body.classList.add("kb"); if (!apps) api("/api/remote/apps").then((r) => { apps = r.apps || []; drawCmdActs(); }); });
  inp.addEventListener("blur", () => setTimeout(() => document.body.classList.remove("kb"), 150));
  // Enter her zaman "Bilgisayarda yaz" (eskiden ilk düğmeye basıyordu: kelime bir uygulama adına benzeyince yazmak yerine
  // "Aç: …" çalışıyordu).
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); const f = $("#cmdType"); if (f) f.click(); } });
  const banner = el("div", "banner");
  banner.id = "shutBanner";
  banner.hidden = true;
  banner.append(el("span", "", "Bilgisayar 60 saniye içinde kapanacak."), btn("", "İptal et", async () => { if (await remote("cancel_shutdown")) banner.hidden = true; }));
  const power = el("div", "power");
  power.append(
    btn("", "Kilitle", () => remote("lock")),
    btn("", "Uyku", async () => { if (await confirmSheet("Uyku moduna geçsin mi?", "Bilgisayar uyur; ALICE de uyur.", "Uyut")) remote("sleep"); }),
    btn("", "HUD", () => remote("hud")),
    btn("d", "Kapat", async () => {
      if (await confirmSheet("Bilgisayar kapansın mı?", "60 saniye sonra kapanır; bu sürede iptal edebilirsin.", "Kapat", true) && await remote("shutdown")) banner.hidden = false;
    }),
  );
  body.append(hero, tele, np, wins, cmd, cacts, banner, power);
  v.append(hd, body);
  refreshPc();
  grabShot();
  clearInterval(shotTimer);
  shotTimer = setInterval(() => { if (S.tab === "pc" && !document.hidden && $("#viewer").hidden) grabShot(); }, 4000);
}
async function remote(action, extra) {
  const r = await api("/api/remote/action", Object.assign({ action }, extra || {}));
  if (!r.ok) { toast(r.error || "olmadı"); return false; }
  if (r.text && r.text !== "tamam") toast(r.text);
  return true;
}
async function pcModeSheet() {
  const cur = (S.remote && S.remote.forced) || "auto";
  const v = await pickSheet("Mod", "Otomatik: oyun, uçuş ya da kod açıkken kendisi değiştirir.",
    [["auto", "Otomatik"], ["normal", "Normal"], ["dev", "Geliştirme"], ["flight", "Uçuş", "Sadece uçuşla ilgili konuşur"], ["quiet", "Sessiz", "Bildirimler sessize alınır"]], cur);
  if (!v) return;
  const r = await api("/api/system/action", { action: "mode", mode: v });
  if (r.ok === false) toast(r.error || "mod değişmedi");
  setTimeout(refreshPc, 500);
}
function spark(svgEl, vals, cls) {
  svgEl.setAttribute("class", "sp" + (cls ? " " + cls : ""));
  svgEl.replaceChildren();
  if (!vals || vals.length < 2) return;
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const span = Math.max(hi - lo, 8);
  const pts = vals.map((v, i) => `${(i * 54 / (vals.length - 1)).toFixed(1)},${(18 - ((v - lo) / span) * 16).toFixed(1)}`).join(" ");
  const pl = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
  pl.setAttribute("points", pts);
  svgEl.appendChild(pl);
}
async function refreshPc() {
  const s = await api("/api/remote/status");
  if (s.ok === false) return;
  S.remote = s;
  const name = $("#pcName");
  if (name) name.textContent = s.host || "Bilgisayar";
  const MN = { auto: "Otomatik", normal: "Normal", dev: "Geliştirme", flight: "Uçuş", quiet: "Sessiz" };
  const pm = $("#pcMode");
  if (pm) { pm.replaceChildren(svg("bolt"), document.createTextNode(MN[s.forced || "auto"] || "Otomatik")); }
  for (const c of $$("#tele > div")) {
    const k = c.dataset.k, v = s[k];
    const temp = k === "gpu_temp";
    const lvl = v == null ? "" : temp ? (v >= 90 ? "c" : v >= 80 ? "w" : "") : (v >= 95 ? "c" : v >= 85 ? "w" : "");
    const b = c.children[2];
    b.className = lvl;
    b.replaceChildren(document.createTextNode(v == null ? "–" : Math.round(v)));
    if (v != null) b.appendChild(el("small", "", temp ? "°" : "%"));
    spark(c.children[1], (s.series || {})[k], lvl);
  }
  const np = $("#nowp");
  if (np) {
    const pl = s.playing;
    np.hidden = !pl;
    if (pl) {
      np.replaceChildren();
      np.classList.toggle("paused", pl.status !== "playing");
      const eq = el("span", "eq");
      eq.append(el("i"), el("i"), el("i"));
      const l = el("span", "l");
      l.append(el("b", "", pl.title || pl.artist || "Çalıyor"), el("small", "", pl.title ? pl.artist || pl.app || "" : pl.app || ""));
      const c = el("span", "c");
      const pp = btn("pp", null, () => remote("play_pause").then(() => setTimeout(refreshPc, 600)), pl.status === "playing" ? "pause" : "play");
      c.append(iconBtn("skipb", "Önceki", () => remote("previous")), pp, iconBtn("skipf", "Sonraki", () => remote("next").then(() => setTimeout(refreshPc, 900))));
      np.append(eq, l, c);
    }
  }
  const hero = $("#hero");
  if (hero) $("b", hero).textContent = s.app || "";
  if (S.tab === "more" && !STACK.more.length) drawMoreTop();
  drawChatHead();
  loadWindows();
}
async function loadWindows() {
  const w = $("#wins");
  if (!w) return;
  const r = await api("/api/remote/windows");
  const rows = (r && r.windows) || [];
  w.replaceChildren();
  if (!rows.length) return;
  w.appendChild(el("div", "lbl", "")).append(el("span", "", "Açık pencereler"), el("span", "", "dokun: öne getir"));
  for (const x of rows.slice(0, 6)) {
    const b = el("button", "win");
    const ap = el("span", "ap", (x.app || "?")[0].toUpperCase());
    ap.style.background = colorOf(x.app);
    const l = el("span", "l");
    l.append(el("b", "", x.app), el("small", "", x.title === x.app ? (x.minimized ? "simge durumunda" : "") : x.title));
    b.append(ap, l);
    if (x.active) b.appendChild(el("span", "fg", "önde"));
    b.addEventListener("click", async () => { haptic(); if (await remote("focus", { title: x.title })) setTimeout(() => { loadWindows(); grabShot(); }, 600); });
    w.appendChild(b);
  }
}
function drawCmdActs() {
  const box = $("#cmdActs"), t = ($("#cmdIn") || {}).value || "";
  if (!box) return;
  box.replaceChildren();
  const text = t.trim();
  if (!text) return;
  const low = text.toLocaleLowerCase("tr");
  const looksUrl = /\.[a-z]{2,}(\/|$)/i.test(text) || /^https?:/i.test(text);
  const hit = looksUrl ? text : (apps || []).find((a) => a.toLocaleLowerCase("tr").startsWith(low)) || (apps || []).find((a) => a.toLocaleLowerCase("tr").includes(low));
  const clear = () => { $("#cmdIn").value = ""; drawCmdActs(); };
  if (hit) box.appendChild(btn("p", "Aç: " + hit, async () => { if (await remote("open", { target: hit })) clear(); }));
  const typeBtn = btn(hit ? "" : "p", "Bilgisayarda yaz", async () => { if (await remote("type", { text })) clear(); });
  typeBtn.id = "cmdType";
  box.appendChild(typeBtn);
  box.appendChild(btn("", "Yaz + Enter", async () => { if (await remote("type", { text, enter: true })) clear(); }));
  box.appendChild(btn("", "Panoya kopyala", async () => { if (await remote("clipboard", { text })) clear(); }));
}
async function grabShot(target) {
  try {
    const r = await fetch("/api/remote/screen", { headers: { "X-Alice-Token": TOKEN } });
    if (!r.ok) throw new Error(String(r.status));
    const url = URL.createObjectURL(await r.blob());
    const imgs = [$("#hero img"), target].filter(Boolean);
    for (const i of imgs) { i.src = url; i.hidden = false; }
    if (shotUrl) URL.revokeObjectURL(shotUrl);
    shotUrl = url;
    const none = $("#hero .none");
    if (none) none.hidden = true;
    const sm = $("#hero small");
    if (sm) { sm.className = ""; sm.textContent = "canlı · " + new Date().toLocaleTimeString("tr-TR", { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }
    return true;
  } catch (e) {
    const none = $("#hero .none");
    if (none) { none.hidden = false; none.textContent = "Ekran alınamadı. Bilgisayar kilitli ya da uyuyor olabilir."; }
    const hi = $("#hero img");
    if (hi && !hi.getAttribute("src")) hi.hidden = true;
    const sm = $("#hero small");
    if (sm) { sm.className = "stale"; sm.textContent = "ekran yok"; }
    return false;
  }
}
function openViewer() {
  const v = $("#viewer");
  v.hidden = false;
  history.pushState({ viewer: 1 }, "");
  const img = $("img", v);
  const tick = async () => { if (!v.hidden && !document.hidden) { if (await grabShot(img)) $("#viewerInfo").textContent = "Canlı · " + new Date().toLocaleTimeString("tr-TR") + " · dokun: tıkla, basılı tut: sağ tık, iki parmak: kaydır"; } };
  tick();
  clearInterval(viewerTimer);
  viewerTimer = setInterval(tick, 1000);
}
function closeViewer() { $("#viewer").hidden = true; clearInterval(viewerTimer); }
/* Canlı ekranda dokunarak kontrol: dokun = tıkla, basılı tut = sağ tık, iki parmakla kaydır = tekerlek. Eskiden ekran
   sadece izlenebiliyordu, dokunmak hiçbir şey yapmıyordu. Görüntü kutuya sığdırıldığı (object-fit: contain) için
   dokunulan nokta kenar boşlukları çıkarılıp görüntünün 0-1 kesrine çevrilir. */
function viewerPoint(img, cx, cy) {
  const r = img.getBoundingClientRect(), nw = img.naturalWidth, nh = img.naturalHeight;
  if (!nw || !nh) return null;
  const k = Math.min(r.width / nw, r.height / nh), w = nw * k, h = nh * k;
  const x = (cx - r.left - (r.width - w) / 2) / w, y = (cy - r.top - (r.height - h) / 2) / h;
  return x >= 0 && x <= 1 && y >= 0 && y <= 1 ? { x, y } : null;
}
(function viewerTouch() {
  const img = $("#viewer img");
  let t0 = 0, sx = 0, sy = 0, moved = false, two = false, lastY = 0, acc = 0, timer = null, longDone = false;
  const refresh = () => setTimeout(() => grabShot(img), 350);
  img.addEventListener("touchstart", (e) => {
    two = e.touches.length >= 2;
    const t = e.touches[0];
    t0 = Date.now(); sx = t.clientX; sy = t.clientY; moved = false; longDone = false; acc = 0;
    lastY = two ? (e.touches[0].clientY + e.touches[1].clientY) / 2 : t.clientY;
    clearTimeout(timer);
    if (!two) timer = setTimeout(async () => {  // basılı tut: sağ tık
      if (moved) return;
      const p = viewerPoint(img, sx, sy);
      if (!p) return;
      longDone = true; haptic(30);
      if (await remote("click", { x: p.x, y: p.y, button: "right" })) refresh();
    }, 550);
  }, { passive: true });
  img.addEventListener("touchmove", (e) => {
    const t = e.touches[0];
    if (Math.hypot(t.clientX - sx, t.clientY - sy) > 12) { moved = true; clearTimeout(timer); }
    if (e.touches.length >= 2) {
      two = true;
      const y = (e.touches[0].clientY + e.touches[1].clientY) / 2;
      acc += lastY - y; lastY = y;
      const steps = Math.trunc(acc / 40);
      if (steps) {
        acc -= steps * 40;
        const p = viewerPoint(img, (e.touches[0].clientX + e.touches[1].clientX) / 2, y) || { x: 0.5, y: 0.5 };
        remote("scroll", { x: p.x, y: p.y, dy: steps }).then(refresh);
      }
    }
  }, { passive: true });
  img.addEventListener("touchend", async (e) => {
    clearTimeout(timer);
    if (two || moved || longDone || e.touches.length) return;
    if (Date.now() - t0 > 550) return;
    const p = viewerPoint(img, sx, sy);
    if (!p) return;
    haptic(10);
    if (await remote("click", { x: p.x, y: p.y })) refresh();
  });
})();
$("#viewerClose").addEventListener("click", () => { closeViewer(); });

/* ================================================================== BİLDİRİM */
// Onay bekleyenler: ALICE'in izin istediği işlemler, seçenekli soruları ve projelerin soruları. Sekme rozeti toplam.
const PERM_TEXT = {
  run_command: "Şu komutu çalıştırmak istiyorum:", write_file: "Bir dosyaya yazmak istiyorum:", edit_file: "Bir dosyayı düzenlemek istiyorum:",
  multi_edit: "Bir dosyayı düzenlemek istiyorum:", file_ops: "Dosyalarla şunu yapmak istiyorum:", apply_patch: "Dosyalara yama uygulamak istiyorum:",
  process_kill: "Bir programı kapatmak istiyorum:", computer: "Ekranda şunu yapmak istiyorum:", ui_act: "Bir pencerede şunu yapmak istiyorum:",
  browser: "Tarayıcıda şunu yapmak istiyorum:", http_request: "Şu isteği göndermek istiyorum:",
};
function addPending(ev, kind) {
  if (!ev || !ev.id || S.pending.has(ev.id)) return;
  S.pending.set(ev.id, Object.assign({ kind, at: Date.now() / 1000 }, ev));
  updateBellBadge();
  if (CALL && ev.sid && ev.sid === CALL.sid) { haptic(60); drawCall(); }
  if (S.tab === "bell") renderBell();
  if (document.hidden && NATIVE && NATIVE.notify) {
    try { NATIVE.notify("ALICE onay istiyor", kind === "choice" ? ev.question : (PERM_TEXT[ev.name] || "İzin istiyor") + " " + (ev.summary || ""), "perm-" + ev.id); } catch (e) { /* yok */ }
  }
}
function dropPending(id) {
  if (!S.pending.delete(id)) return;
  updateBellBadge();
  if (CALL) drawCall();
  if (S.tab === "bell") renderBell();
}
/* Görev onayları (alice/core/tasks.py): arka plandaki bir görev karar istediğinde. Bilgisayarda da aynı kutu var;
   hangisinden cevaplanırsa diğerinden düşer. Uygulama kapalıyken bildirimi Android servisi uyarı olayından atar. */
async function loadTaskAsks() {
  const r = await api("/api/tasks/approvals");
  if (!r || !Array.isArray(r.approvals)) return;
  const known = new Set(S.taskAsks.map((a) => a.id));
  S.taskAsks = r.approvals;
  const fresh = S.taskAsks.filter((a) => !known.has(a.id));
  if (fresh.length && known.size && document.hidden && NATIVE && NATIVE.notify) {
    try { NATIVE.notify("Onay bekliyor · " + (fresh[0].task || "Görev"), fresh[0].title, "task-" + fresh[0].id); } catch (e) { /* yok */ }
  }
  updateBellBadge();
  if (S.tab === "bell" && S.notifTab === "pending" && !STACK.bell.length && (fresh.length || known.size !== S.taskAsks.length)) renderBell();
}
function drawTaskAsks(body) {
  for (const a of S.taskAsks) {
    const box = el("div", "ask2 task");
    box.append(who("Görev · " + (a.task || ""), a.ts), el("p", "", a.title));
    if (a.body) box.appendChild(el("code", "", a.body));
    if (a.action) {
      const det = el("details", "ta-act");
      det.append(el("summary", "", "Onaylarsan yapılacak"), el("code", "", a.action));
      box.appendChild(det);
    }
    const answer = async (decision, reply) => {
      box.classList.add("done");
      const r = await api("/api/tasks/action", { action: "answer", id: a.id, decision, reply: reply || "" });
      if (!r || r.ok === false) { box.classList.remove("done"); return toast((r && r.error) || "olmadı"); }
      haptic(15);
      toast(decision === "approve" ? "Onaylandı, ALICE yapıyor" : decision === "reject" ? "Reddedildi" : "Cevap gitti");
      S.taskAsks = S.taskAsks.filter((x) => x.id !== a.id);
      updateBellBadge();
      renderBell();
    };
    const acts = el("div", "acts");
    acts.append(btn("go", "Onayla", () => answer("approve")), btn("no", "Reddet", () => answer("reject")));
    const ans = el("div", "ans");
    const inp = el("input");
    inp.placeholder = "Cevap ya da düzeltme yaz";
    const go = btn("", "Gönder", () => { const t = inp.value.trim(); if (t) answer("reply", t); });
    ans.append(inp, go);
    box.append(acts, ans);
    body.appendChild(box);
  }
}
function updateBellBadge() {
  const n = S.pending.size + S.taskAsks.length + S.projects.filter((p) => p.questions).length;
  const b = $("#bellBadge");
  b.hidden = !n;
  b.textContent = n;
  try { if (NATIVE && NATIVE.setBadge) NATIVE.setBadge(n); } catch (e) { /* yok */ }
}
function renderBell() {
  const v = $("#v-bell");
  if (STACK.bell.length) return;
  v.replaceChildren();
  const hd = el("div", "hd");
  hd.appendChild(el("h1", "", "Bildirim"));
  const seg = el("div", "seg2");
  const n = S.pending.size + S.taskAsks.length + S.projects.filter((p) => p.questions).length;
  for (const [k, label] of [["pending", "Bekleyen"], ["all", "Hepsi"], ["warn", "Uyarılar"]]) {
    const b = btn(S.notifTab === k ? "on" : "", label, () => { S.notifTab = k; renderBell(); });
    if (k === "pending" && n) b.appendChild(el("i", "", String(n)));
    seg.appendChild(b);
  }
  const body = el("div", "scroll");
  v.append(hd, seg, body);
  if (S.notifTab === "pending") drawPending(body);
  else drawEvents(body, S.notifTab === "warn");
}
function who(text, ts) {
  const w = el("div", "who");
  const img = el("img");
  img.src = "/static/alice.png";
  img.alt = "";
  w.append(img, document.createTextNode(text));
  if (ts) w.appendChild(el("span", "t", rel(ts)));
  return w;
}
async function drawPending(body) {
  drawTaskAsks(body);
  const items = [...S.pending.values()].sort((a, b) => b.at - a.at);
  for (const p of items) {
    const box = el("div", "ask2");
    if (p.kind === "choice") {
      box.append(who("ALICE soruyor", p.at), el("p", "", p.question));
      const opts = el("div", "opts");
      for (const o of p.options || []) {
        const label = String(o).split(" — ")[0];
        opts.appendChild(btn("", label, async () => { box.classList.add("done"); await api("/api/choice", { id: p.id, answer: o }); dropPending(p.id); }));
      }
      const ans = el("div", "ans");
      const inp = el("input");
      inp.placeholder = "Başka bir cevap yaz";
      const go = btn("", "Gönder", async () => { const t = inp.value.trim(); if (!t) return; await api("/api/choice", { id: p.id, answer: t }); dropPending(p.id); });
      ans.append(inp, go);
      box.append(opts, ans);
    } else {
      const send = p.risk === "send";
      box.appendChild(who("ALICE · " + (send ? "dışarıya gönderecek" : verb(p.name)), p.at));
      const txt = el("p");
      txt.textContent = send ? "Şunu göndereceğim:" : (PERM_TEXT[p.name] || "Bunu yapmak istiyorum:");
      box.append(txt, el("code", "", p.summary || p.name));
      const acts = el("div", "acts");
      const answer = async (a) => { box.classList.add("done"); const r = await api("/api/permission", { id: p.id, answer: a }); if (r.ok === false) toast(r.error || "olmadı"); dropPending(p.id); };
      acts.append(btn("go", send ? "Gönder" : "İzin ver", () => answer("y")), btn("no", "Reddet", () => answer("n")));
      if (!send && p.risk !== "danger") acts.appendChild(btn("more", "Bu oturumda hep ›", () => answer("a")));
      box.appendChild(acts);
    }
    body.appendChild(box);
  }
  const asking = S.projects.filter((x) => x.questions);
  for (const p of asking) {
    const d = await api("/api/project?id=" + encodeURIComponent(p.id));
    for (const q of ((d && d.questions) || []).filter((x) => !x.answer)) {
      const box = el("div", "ask2");
      box.append(who("Proje · " + (p.title || ""), p.updated), el("p", "", (q.member ? q.member + " soruyor: " : "") + q.text));
      const ans = el("div", "ans");
      const inp = el("input");
      inp.placeholder = "Cevabın…";
      const go = btn("", "Gönder", async () => {
        const t = inp.value.trim();
        if (!t) return;
        const r = await api("/api/project/action", { id: p.id, action: "answer", name: q.id, text: t });
        if (!r.ok) return toast(r.error || "olmadı");
        box.classList.add("done");
        loadProjects();
      });
      ans.append(inp, go);
      box.appendChild(ans);
      body.appendChild(box);
    }
  }
  if (!body.children.length) body.appendChild(el("div", "empty", "Bekleyen bir şey yok. ALICE izin ya da cevap beklediğinde burada görünür" + (NATIVE ? " ve telefonuna bildirim gelir." : ".")));
}
async function drawEvents(body, warnOnly) {
  const s = await api("/api/remote/status");
  if (s.ok === false) return;
  S.remote = s;
  const evs = (s.events || []).filter((e) => !warnOnly || e.severity === "warn" || e.severity === "critical");
  body.replaceChildren();
  if (!evs.length) { body.appendChild(el("div", "empty", warnOnly ? "Uyarı yok." : "Henüz bir olay yok. Çekirdek çalışırken bilgisayardaki önemli şeyler burada birikir.")); return; }
  let day = null, tl = null;
  for (const e of evs) {
    const d = new Date(e.ts * 1000);
    const today = new Date();
    const label = d.toDateString() === today.toDateString() ? "Bugün" : d.toDateString() === new Date(today - 86400000).toDateString() ? "Dün"
      : d.toLocaleDateString("tr-TR", { day: "numeric", month: "long" });
    if (label !== day) { day = label; body.appendChild(el("div", "day", label)); tl = el("div", "tl"); body.appendChild(tl); }
    const it = el("div", "tli " + (e.severity || ""));
    it.append(el("time", "", hhmm(e.ts)), el("b", "", e.title));
    if (e.body && e.body !== e.title) it.appendChild(el("small", "", e.body));
    tl.appendChild(it);
  }
}

/* ================================================================== DAHA FAZLA */
let rtt = null;
function renderMore() {
  const v = $("#v-more");
  if (STACK.more.length) return;
  v.replaceChildren();
  const hd = el("div", "hd");
  hd.appendChild(el("h1", "", "Daha fazla"));
  const body = el("div", "scroll");
  body.id = "moreBody";
  v.append(hd, body);
  drawMoreTop();
  const t0 = performance.now();
  Promise.all([api("/api/remote/status"), api("/api/state"), api("/api/system"), api("/api/automations"), api("/api/assistant"), api("/api/phone/app")]).then(([r, st, sys, au, as, app]) => {
    rtt = Math.round(performance.now() - t0);
    if (r.ok !== false) S.remote = r;
    if (st && st.session) applyStatus(st);
    S.sys = sys; S.auto = au; S.assist = as; S.app = app;
    drawMoreTop();
  });
}
function drawMoreTop() {
  const body = $("#moreBody");
  if (!body) return;
  body.replaceChildren();
  const r = S.remote || {};
  const me = el("div", "me");
  const img = el("img");
  img.src = "/static/alice.png";
  img.alt = "";
  const l = el("div");
  l.append(el("b", "", "ALICE"), el("small", S.online ? "" : "off", [(r.host || "Bilgisayar"), S.online ? (NATIVE && NATIVE.network ? NATIVE.network() : "bağlı") : "bağlantı yok", rtt != null ? rtt + " ms" : ""].filter(Boolean).join(" · ")));
  me.append(img, l);
  body.appendChild(me);
  const today = el("div", "today");
  const running = S.projects.filter((p) => p.active).length;
  for (const [n, label] of [[S.status.today_requests ?? "–", "bugün istek"], [running, "proje çalışıyor"], [(r.today && r.today.warnings) ?? 0, "uyarı"]]) {
    const d = el("div");
    d.append(el("b", "", String(n)), el("small", "", label));
    today.appendChild(d);
  }
  body.appendChild(today);
  // Hızlı anahtarlar
  const quiet = (r.forced || "") === "quiet";
  body.appendChild(toggleRow("moon", "Rahatsız etme", quiet, async (on) => {
    const res = await api("/api/system/action", { action: "mode", mode: on ? "quiet" : "auto" });
    if (res.ok === false) return toast(res.error || "olmadı");
    toast(on ? "Bildirimler sessizde" : "Rahatsız etme kapalı");
    refreshPc();
  }));
  const voiceOn = !!(S.sys && S.sys.voice && S.sys.voice.enabled);
  body.appendChild(toggleRow("mic", "“Hey Alice”", voiceOn, async (on) => {
    const res = await api("/api/system/action", { action: "voice", on });
    if (res.ok === false) return toast(res.error || "olmadı");
    toast(on ? "Sesli asistan açılıyor" : "Sesli asistan kapandı");
    if (S.sys && S.sys.voice) S.sys.voice.enabled = on;
  }, !S.sys));
  body.appendChild(el("div", "gh", "ALICE"));
  const au = S.auto || {};
  const rules = (au.rules || []).filter((x) => x.enabled).length, tasks = (au.tasks || []).length;
  body.appendChild(navRow("bolt", "Otomasyonlar", au.rules ? `${rules} kural · ${tasks} görev` : "", openAutomations));
  body.appendChild(navRow("note", "Notlar", "", openNotes));
  const checks = (S.assist && S.assist.checks) || [];
  const bad = checks.filter((c) => c.status === "fail" || c.status === "warn").length;
  body.appendChild(navRow("heart", "Sağlık", checks.length ? (bad ? bad + " uyarı" : "sorun yok") : "", openHealth, bad ? "w" : ""));
  body.appendChild(navRow("hist", "Sohbet geçmişi", String(S.sessions.length || ""), () => { show("chat"); historySheet(); }));
  body.appendChild(el("div", "gh", "Ayarlar"));
  body.appendChild(navRow("key", "Model", shortModel(S.status.model), modelSheet));
  body.appendChild(navRow("gear", "Görünüm", { system: "sistem", light: "açık", dark: "koyu" }[theme()] || "sistem", themeSheet));
  if (NATIVE && NATIVE.reconfigure) body.appendChild(navRow("wifi", "Bağlantı", location.host, () => NATIVE.reconfigure()));
  else body.appendChild(navRow("wifi", "Bağlantı", location.host, () => toast("Adres: " + location.host)));
  appRow(body);
  body.appendChild(el("div", "foot2", "ALICE " + (S.status.version || "") + (NATIVE && NATIVE.version ? " · uygulama " + NATIVE.version() : "")));
}
/* Android uygulaması: uygulamanın içindeyken sürüm ve güncelleme, telefonun tarayıcısındayken kurulum. */
function appRow(body) {
  const app = S.app || {};
  if (NATIVE && NATIVE.versionCode) {
    const mine = NATIVE.versionCode();
    const newer = app.available && app.version_code > mine;
    body.appendChild(navRow("dl", "Uygulama", newer ? "güncelleme var: " + app.version_name : NATIVE.version() + " · güncel", () => {
      if (!newer) return toast("Uygulama güncel. Yeni sürüm çıkınca kendini günceller.");
      NATIVE.checkUpdate();
      toast("Güncelleme indiriliyor…");
    }, newer ? "w" : ""));
  } else if (/Android/i.test(navigator.userAgent) && app.available) {
    body.appendChild(navRow("dl", "Android uygulamasını kur", app.version_name, () => {
      location.href = "/api/phone/app.apk";  // çerezle doğrulanır: anahtar indirme geçmişine düşmez
      toast("İnince aç ve kur. Sonra uygulamada QR'yi yeniden okut.");
    }));
  }
}
function toggleRow(icon, label, on, change, disabled) {
  const r = el("div", "pl");
  const t = el("button", "tg" + (on ? " on" : ""));
  t.setAttribute("role", "switch");
  t.setAttribute("aria-checked", on ? "true" : "false");
  t.setAttribute("aria-label", label);
  t.disabled = !!disabled;
  t.addEventListener("click", () => { haptic(); const v = !t.classList.contains("on"); t.classList.toggle("on", v); change(v); });
  r.append(svg(icon), el("b", "", label), t);
  return r;
}
function navRow(icon, label, value, onClick, cls) {
  const r = el("button", "pl");
  r.append(svg(icon), el("b", "", label));
  if (value) r.appendChild(el("span", cls || "", value));
  r.appendChild(svg("chev", "chev"));
  r.addEventListener("click", () => { haptic(); onClick(); });
  return r;
}
function theme() { try { return localStorage.getItem("alice-theme") || "system"; } catch (e) { return "system"; } }
function applyTheme() {
  const t = theme();
  if (t === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.dataset.theme = t;
  try { if (NATIVE && NATIVE.setTheme) NATIVE.setTheme(t); } catch (e) { /* yok */ }
}
async function themeSheet() {
  const v = await pickSheet("Görünüm", "", [["system", "Sistem", "Telefonun ayarını izler"], ["light", "Açık"], ["dark", "Koyu"]], theme());
  if (!v) return;
  try { localStorage.setItem("alice-theme", v); } catch (e) { /* yok */ }
  applyTheme();
  drawMoreTop();
}
function openAutomations() {
  push("more", "Otomasyonlar", (body, entry) => {
    const call = (action, extra) => api("/api/automations/action", Object.assign({ action }, extra || {}));
    const draw = async () => {
      const d = await api("/api/automations");
      body.replaceChildren();
      if (!d.core_running) {
        const w = el("div", "ask");
        w.append(el("b", "", "Çekirdek çalışmıyor. "), document.createTextNode("Kurallar ve görevler çekirdek açıkken çalışır."));
        const go = btn("", "Çekirdeği başlat", async () => { await api("/api/system/action", { action: "start" }); toast("Başlatılıyor"); setTimeout(draw, 3000); });
        go.style.cssText = "margin-top:8px;padding:8px 14px;border-radius:10px;background:var(--text);color:var(--bg);font-weight:600";
        w.appendChild(el("div")).appendChild(go);
        body.appendChild(w);
      }
      const add = el("div", "cmd");
      const inp = el("input");
      inp.placeholder = "Ne olunca haber vereyim? ya da: her sabah 9'da…";
      add.append(svg("bolt"), inp);
      inp.addEventListener("keydown", async (e) => {
        if (e.key !== "Enter" || !inp.value.trim()) return;
        const text = inp.value.trim();
        const isTask = /\b(her|saat|sabah|akşam|pazartesi|salı|çarşamba|perşembe|cuma|cumartesi|pazar|\d{1,2}[:.]\d{2})\b/i.test(text);
        const r = await call(isTask ? "task_parse" : "rule_parse", { text });
        if (!r || !r.ok) return toast((r && r.error) || "anlayamadım");
        const v = r.rule || r.task || {};
        const lines = isTask ? [["Ne zaman", v.when], ["Ne yapılacak", v.prompt]] : [["Koşul", v.condition], ["Mesaj", v.message]];
        const ok = await openSheet((c) => {
          c.append(el("h3", "", "Anladığım"));
          for (const [k, val] of lines) if (val) { const o = el("div", "opt"); const l = el("span", "l"); l.append(el("small", "", k), el("b", "", String(val))); o.appendChild(l); c.appendChild(o); }
          const row = el("div", "btns");
          row.append(btn("btn", "Vazgeç", () => closeSheet(false)), btn("btn acc", "Kaydet", () => closeSheet(true)));
          c.appendChild(row);
        });
        if (!ok) return;
        const s = await call(isTask ? "task_add" : "rule_add", isTask ? { task: r.task } : { rule: r.rule });
        if (!s || !s.ok) return toast((s && s.error) || "kaydedilemedi");
        inp.value = "";
        toast("Kaydedildi");
        draw();
      });
      body.appendChild(add);
      body.appendChild(el("div", "lbl", `Kurallar · ${(d.rules || []).length}`));
      for (const r of d.rules || []) {
        const row = el("div", "rule" + (r.enabled ? "" : " off"));
        const l = el("div", "l");
        l.append(el("b", "", r.text), el("small", "", r.condition));
        const t = el("button", "tg" + (r.enabled ? " on" : ""));
        t.setAttribute("aria-label", r.enabled ? "Kapat" : "Aç");
        t.addEventListener("click", async () => { haptic(); await call("rule_enable", { id: r.id, on: !r.enabled }); draw(); });
        row.append(l, t);
        row.addEventListener("contextmenu", async (e) => { e.preventDefault(); if (await confirmSheet("Kural silinsin mi?", r.text, "Sil", true)) { await call("rule_rm", { id: r.id }); draw(); } });
        body.appendChild(row);
      }
      body.appendChild(el("div", "lbl", `Zamanlanmış görevler · ${(d.tasks || []).length}`));
      if (!(d.tasks || []).length) body.appendChild(el("p", "goal", "Yok. Yukarıya “her sabah 9'da Discord'u özetle” gibi yaz."));
      for (const t of d.tasks || []) {
        const row = el("div", "rule" + (t.enabled ? "" : " off"));
        const l = el("div", "l");
        l.append(el("b", "", t.text), el("small", "", t.when + (t.enabled && t.next ? " · sonraki " + t.next : "")));
        const tg = el("button", "tg" + (t.enabled ? " on" : ""));
        tg.addEventListener("click", async () => { haptic(); await call("task_enable", { id: t.id, on: !t.enabled }); draw(); });
        row.append(l, tg);
        body.appendChild(row);
      }
      body.appendChild(el("p", "foot2", "Silmek için kurala uzun bas."));
    };
    entry.refresh = draw;
    draw();
  }, "Daha fazla");
}
function openNotes() {
  push("more", "Notlar", (body, entry) => {
    const draw = async () => {
      const d = await api("/api/notes");
      body.replaceChildren();
      const add = el("div", "cmd");
      const inp = el("input");
      inp.placeholder = "Yeni not";
      add.append(svg("note"), inp);
      inp.addEventListener("keydown", async (e) => {
        if (e.key !== "Enter" || !inp.value.trim()) return;
        const r = await api("/api/notes/action", { action: "add", text: inp.value.trim() });
        if (!r || r.ok === false) return toast((r && r.error) || "kaydedilemedi");
        inp.value = "";
        draw();
      });
      body.appendChild(add);
      if (d.brief) { body.appendChild(el("div", "lbl", "Sabah özeti")); const b = el("div", "a"); b.innerHTML = md(typeof d.brief === "string" ? d.brief : d.brief.text || ""); body.appendChild(b); }
      body.appendChild(el("div", "lbl", `Notlar · ${d.total || 0}`));
      if (!(d.notes || []).length) body.appendChild(el("p", "goal", "Henüz not yok. Sesle “not al: …” de ya da buraya yaz."));
      for (const n of d.notes || []) {
        const r = el("div", "notep");
        r.append(el("time", "", rel(n.ts)), el("div", "", n.text));
        r.addEventListener("contextmenu", async (e) => { e.preventDefault(); if (await confirmSheet("Not silinsin mi?", n.text.slice(0, 120), "Sil", true)) { await api("/api/notes/action", { action: "remove", ts: n.ts }); draw(); } });
        body.appendChild(r);
      }
    };
    entry.refresh = draw;
    draw();
  }, "Daha fazla");
}
function openHealth() {
  push("more", "Sağlık", async (body) => {
    const d = await api("/api/assistant");
    const checks = (d && d.checks) || [];
    const order = { fail: 0, warn: 1, ok: 2, off: 3 };
    const mark = { ok: "✓", warn: "!", fail: "✗", off: "–" };
    for (const c of checks.slice().sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9))) {
      const r = el("div", "chk " + c.status);
      const l = el("div", "l");
      l.append(el("b", "", c.name), el("small", "", c.detail));
      if (c.fix && c.status !== "ok") l.appendChild(el("div", "fix", c.fix));
      r.append(el("i", "", mark[c.status] || "?"), l);
      body.appendChild(r);
    }
    if (!checks.length) body.appendChild(el("div", "empty", "Durum alınamadı."));
  }, "Daha fazla");
}

/* ================================================================== ARAMA */
/* Telefonla ALICE arasında sesli görüşme. Telefon konuşma tanımayla dinler (Android uygulaması), söylenen bilgisayara
   gider; ALICE'in cevabı bilgisayarda cümle cümle sese çevrilip burada çalınır (ses yoksa telefon kendi sesiyle okur).
   ALICE konuşurken ortadaki yuvarlağa dokunmak onu susturur ve dinlemeye geçer. */
let CALL = null;
const CALL_END_TEXT = { missed: "Cevapsız arama", declined: "Arama reddedildi", hangup: "Görüşme bitti", silence: "Sessiz kaldın, görüşme bitti" };
function callUI() {
  let root = $("#call");
  if (!root) {
    root = el("div", "call");
    root.id = "call";
    document.body.appendChild(root);
  }
  return root;
}
function fmtDur(s) { return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`; }
function drawCall() {
  const c = CALL;
  const root = callUI();
  if (!c) { root.hidden = true; root.replaceChildren(); document.body.classList.remove("in-call"); return; }
  root.hidden = false;
  document.body.classList.add("in-call");
  root.replaceChildren();
  root.className = "call " + c.state + (c.outbound ? " outbound" : "") + (c.mode ? " m-" + c.mode : "");
  const top = el("div", "c-top");
  const title = c.state === "ringing" ? (c.outbound ? "ALICE arıyor" : "Aranıyor…") : "ALICE";
  const sub = c.state === "ringing" ? (c.reason || "Sesli arama") : fmtDur((Date.now() - c.t0) / 1000);
  top.append(el("b", "", title), el("small", "c-time", sub));
  const orb = el("button", "c-orb");
  orb.setAttribute("aria-label", c.mode === "speaking" ? "Sustur ve konuş" : "Konuş");
  const img = el("img");
  img.src = "/static/alice.png";
  img.alt = "";
  orb.append(el("i", "r1"), el("i", "r2"), img);
  orb.addEventListener("click", () => { if (c.state === "active") { haptic(14); bargeIn(); } });
  const perm = [...S.pending.values()].find((p) => p.sid && p.sid === c.sid);
  const mode = el("div", "c-mode", c.state === "ringing" ? "" : perm ? "Onayını bekliyor" : { listening: c.muted ? "Mikrofon kapalı" : "Dinliyorum", thinking: "Düşünüyor…",
    speaking: "Konuşuyor · dokun, araya gir", idle: "" }[c.mode || "idle"] || "");
  const said = el("div", "c-said", c.caption || "");
  const heard = el("div", "c-heard", c.heard ? "“" + c.heard + "”" : "");
  const mid = el("div", "c-mid");
  mid.append(orb, mode, said, heard);
  if (perm) mid.appendChild(callPermission(perm));
  const bar = el("div", "c-bar");
  if (c.state === "ringing" && c.outbound) {
    bar.append(callBtn("x", "Reddet", "red", () => { api("/api/call/decline", { id: c.id }); endCall("declined"); }),
      callBtn("phone", "Aç", "green", () => answerCall()));
  } else {
    bar.append(callBtn("mic", c.muted ? "Sesi aç" : "Sessiz", c.muted ? "on" : "", () => toggleMute()),
      callBtn("phone", "Kapat", "red hang", () => hangup()),
      callBtn("type", "Yaz", "", () => typeInCall()));
  }
  root.append(top, mid, bar);
}
function callBtn(icon, label, cls, fn) {
  const w = el("div", "c-btn " + (cls || ""));
  const b = el("button");
  b.setAttribute("aria-label", label);
  b.appendChild(svg(icon));
  b.addEventListener("click", () => { haptic(12); fn(); });
  w.append(b, el("small", "", label));
  return w;
}
function callPermission(p) {
  const box = el("div", "c-perm");
  box.append(el("small", "", PERM_TEXT[p.name] || "İzin istiyor"), el("code", "", p.summary || p.question || ""));
  const row = el("div", "btns");
  row.append(btn("btn", "Reddet", () => api("/api/permission", { id: p.id, answer: "n" }).then(() => dropPending(p.id))),
    btn("btn p", "İzin ver", () => api("/api/permission", { id: p.id, answer: "y" }).then(() => dropPending(p.id))));
  box.appendChild(row);
  return box;
}
function newCall(id, opts) {
  CALL = Object.assign({ id, state: "active", outbound: false, reason: "", queue: [], turn: 0, turnDone: 0, endAfter: false,
    mode: "thinking", caption: "", heard: "", text: "", listening: false, muted: false, silence: 0, t0: Date.now(), sid: "",
    audio: new Audio(), timer: null }, opts || {});
  CALL.audio.preload = "auto";
  CALL.timer = setInterval(() => { const t = $("#call .c-time"); if (t && CALL && CALL.state === "active") t.textContent = fmtDur((Date.now() - CALL.t0) / 1000); }, 1000);
  try { if (NATIVE && NATIVE.keepScreen) NATIVE.keepScreen(true); } catch (e) { /* yok */ }
  drawCall();
}
async function startCall() {
  if (CALL) return drawCall();
  const canLive = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  if (!canLive && (!NATIVE || !NATIVE.listen)) return toast("Sesli arama için mikrofon izni gerekli");
  newCall("", { state: "ringing", outbound: false });
  liveAudio(CALL);  // ses çalıcı dokunuşla açılsın (tarayıcı sonradan açılan sesi engelliyor)
  const r = await api("/api/call/start", {});
  if (!r || !r.ok) { endCall(); return toast((r && r.error) || "Aranamadı"); }
  CALL.id = r.id;
  CALL.state = "active";
  CALL.outbound = !!r.outbound;
  CALL.t0 = Date.now();
  CALL.sid = r.sid || "";
  CALL.live = !!r.live;
  if (CALL.live) { CALL.mode = "thinking"; startLiveMic(CALL); }
  drawCall();
  // Selam, "arama başladı" cevabından önce gelmiş olabilir: kimlik belli olunca bekleyen olayları işle.
  const early = CALL.early || [];
  CALL.early = [];
  for (const ev of early) if (ev.id === r.id) (handlers[ev.type] || (() => {}))(ev);
}
function incomingCall(ev) {
  if (CALL && CALL.id === ev.id) return;
  if (CALL) return;
  newCall(ev.id, { state: "ringing", outbound: true, reason: ev.reason || "", caption: ev.text || "" });
  CALL.mode = "idle";
  // Uygulama arkadaysa zili bildirim çalar; burada da çalarsa iki kez çalardı.
  if (!document.hidden) { try { if (NATIVE && NATIVE.ring) NATIVE.ring(true); else haptic(400); } catch (e) { /* yok */ } }
  drawCall();
}
async function answerCall() {
  const c = CALL;
  if (!c) return;
  try { if (NATIVE && NATIVE.ring) NATIVE.ring(false); } catch (e) { /* yok */ }
  liveAudio(c);
  const r = await api("/api/call/answer", { id: c.id });
  if (!r || r.ok === false) { endCall(); return toast((r && r.error) || "Görüşme açılamadı"); }
  c.state = "active";
  c.t0 = Date.now();
  if (r.live) { c.live = true; c.mode = "thinking"; startLiveMic(c); return drawCall(); }
  if (!NATIVE || !NATIVE.listen) toast("Dinlemek için Android uygulaması gerekli; ALICE'i yine de duyarsın");
  // İlk cümle bu cevaptan önce gelip çalmaya başlamış (hatta bitmiş) olabilir.
  if (c.mode === "speaking") return drawCall();
  if (c.queue.length) return playNext();
  if (c.played) return startCallListening();
  c.mode = "thinking";
  drawCall();
}
function callState(ev) {
  if (earlyEvent(ev)) return;
  if (!CALL || ev.id !== CALL.id) {
    if (!CALL && ev.state === "active" && ev.outbound === false) return;  // başka cihazdan açılan görüşme
    return;
  }
  if (ev.state === "ended") return endCall(ev.end_reason);
  if (ev.state === "active" && CALL.state !== "active") { CALL.state = "active"; CALL.t0 = Date.now(); }
  if (ev.sid) CALL.sid = ev.sid;
  if (CALL.live && ev.live === false && CALL.state === "active") { stopLiveMic(CALL); CALL.live = false; }
  if (ev.lines && ev.lines.length && !CALL.caption) CALL.caption = ev.lines.filter((l) => l.who === "alice").map((l) => l.text).pop() || "";
  drawCall();
}
function earlyEvent(ev) {
  if (!CALL || CALL.id) return false;
  (CALL.early = CALL.early || []).push(ev);
  return true;
}
function callAudio(ev) {
  if (earlyEvent(ev)) return;
  const c = CALL;
  if (!c || ev.id !== c.id || ev.turn < c.turn) return;
  c.queue.push(ev);
  if (c.listening) stopCallListening();
  if (c.mode !== "speaking") playNext();
}
function callTurnDone(ev) {
  if (earlyEvent(ev)) return;
  const c = CALL;
  if (!c || ev.id !== c.id || ev.turn < c.turn) return;
  c.turnDone = ev.turn;
  c.endAfter = !!ev.end;
  if (c.mode !== "speaking" && !c.queue.length) afterSpeech();
}
function playNext() {
  const c = CALL;
  if (!c) return;
  const seg = c.queue.shift();
  if (!seg) {
    if (c.turnDone >= c.turn) afterSpeech();
    else { c.mode = "thinking"; drawCall(); }
    return;
  }
  c.mode = "speaking";
  c.played = true;
  c.caption = seg.turn === c.capTurn ? (c.caption + " " + seg.text).trim() : seg.text;
  c.capTurn = seg.turn;
  c.heard = "";
  drawCall();
  const next = () => { if (CALL === c && c.mode === "speaking") playNext(); };
  if (seg.audio) {
    c.audio.onended = next;
    c.audio.onerror = () => speakText(seg.text, next);
    c.audio.src = `/api/call/audio?id=${encodeURIComponent(c.id)}&n=${seg.n}`;  // çerezle doğrulanır
    c.audio.play().catch(() => speakText(seg.text, next));
  } else speakText(seg.text, next);
}
// Bilgisayar sesi üretemediyse telefonun kendi sesi; o da yoksa yazı ekranda kalır, okuma süresi kadar beklenir.
function speakText(text, done) {
  if (NATIVE && NATIVE.speak) {
    window.aliceSpoke = () => { window.aliceSpoke = null; done(); };
    try { NATIVE.speak(text); return; } catch (e) { /* yok */ }
  }
  setTimeout(done, Math.min(8000, 600 + text.length * 55));
}
function afterSpeech() {
  const c = CALL;
  if (!c) return;
  if (c.state !== "active") { c.mode = "idle"; return drawCall(); }
  if (c.endAfter) { setTimeout(() => hangup(), 400); return; }
  startCallListening();
}
function startCallListening() {
  const c = CALL;
  if (!c) return;
  c.mode = "listening";
  c.text = "";
  c.heard = "";
  drawCall();
  if (c.muted || !NATIVE || !NATIVE.listen) return;
  c.listening = true;
  try { NATIVE.listen("tr-TR"); } catch (e) { c.listening = false; }
}
function stopCallListening() {
  const c = CALL;
  if (!c || !c.listening) return;
  c.listening = false;
  try { NATIVE.cancelListening(); } catch (e) { /* yok */ }
}
function callVoice(ev) {
  const c = CALL;
  if (!c) return;
  if (ev.type === "partial") { c.heard = ((c.text ? c.text + " " : "") + (ev.text || "")).trim(); return drawCallHeard(); }
  if (ev.type === "final") { c.text = ((c.text ? c.text + " " : "") + (ev.text || "")).trim(); c.heard = c.text; return drawCallHeard(); }
  if (ev.type === "level") return levelOrb(ev.level || 0);
  if (ev.type === "error" || ev.type === "end") {
    c.listening = false;
    const text = (c.text || c.heard || "").trim();
    if (text) { c.silence = 0; return sayInCall(text); }
    if (c.muted || c.mode !== "listening") return;
    c.silence += 1;
    if (c.silence >= 4) { toast(CALL_END_TEXT.silence); return hangup(); }
    setTimeout(() => { if (CALL === c && c.mode === "listening" && !c.listening) startCallListening(); }, 250);
  }
}
function drawCallHeard() {
  const h = $("#call .c-heard");
  if (h && CALL) h.textContent = CALL.heard ? "“" + CALL.heard + "”" : "";
}
function levelOrb(lv) {
  const o = $("#call .c-orb");
  if (o) o.style.setProperty("--lv", String(Math.max(0, Math.min(1, lv))));
}
async function sayInCall(text) {
  const c = CALL;
  if (!c) return;
  if (c.live) {
    c.heard = text;
    c.mode = "thinking";
    drawCall();
    const r = await api("/api/call/say", { id: c.id, text });
    if (CALL === c && (!r || !r.ok)) { toast((r && r.error) || "Gönderilemedi"); liveMode(c); }
    return;
  }
  c.mode = "thinking";
  c.heard = text;
  c.queue = [];
  c.turn += 1;  // eski turun gecikmiş cümleleri okunmasın
  drawCall();
  const r = await api("/api/call/say", { id: c.id, text });
  if (CALL !== c) return;
  if (!r || !r.ok) { toast((r && r.error) || "Gönderilemedi"); return startCallListening(); }
  c.turn = r.turn;
}
function bargeIn() {
  const c = CALL;
  if (!c) return;
  if (c.live) {
    liveClear(c);
    if (c.id) api("/api/call/interrupt", { id: c.id });
    c.muted = false;
    return liveMode(c);
  }
  c.queue = [];
  try { c.audio.pause(); } catch (e) { /* yok */ }
  try { if (NATIVE && NATIVE.stopSpeaking) NATIVE.stopSpeaking(); } catch (e) { /* yok */ }
  c.turn += 1;
  if (c.muted) { c.muted = false; }
  startCallListening();
}
function toggleMute() {
  const c = CALL;
  if (!c) return;
  c.muted = !c.muted;
  if (c.live) return drawCall();  // mikrofon akışı sessizdeyken parça göndermez
  if (c.muted) stopCallListening();
  if (!c.muted && c.mode === "listening") startCallListening();
  else drawCall();
}
function typeInCall() {
  let inp;
  openSheet((box) => {
    box.append(el("h3", "", "ALICE'e yaz"));
    inp = el("input", "field");
    inp.placeholder = "Söylemek istediğin…";
    const go = () => { const t = inp.value.trim(); closeSheet(); if (t) { stopCallListening(); sayInCall(t); } };
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
    const row = el("div", "btns");
    row.append(btn("btn", "Vazgeç", () => closeSheet()), btn("btn p", "Gönder", go));
    box.append(inp, row);
    setTimeout(() => inp.focus(), 50);
  });
}
function hangup() {
  const c = CALL;
  if (!c) return;
  if (c.id) api("/api/call/hangup", { id: c.id });
  endCall("hangup");
}
function callEnded(ev) {
  if (CALL && ev.id === CALL.id) endCall(ev.reason);
  else if (!CALL && ev.reason === "missed") toast("Cevapsız arama: " + (ev.text || "ALICE"));
}
function endCall(reason) {
  const c = CALL;
  if (!c) return;
  CALL = null;
  clearInterval(c.timer);
  stopLiveMic(c);
  liveClear(c);
  try { if (c.out) c.out.close(); } catch (e) { /* yok */ }
  try { c.audio.pause(); } catch (e) { /* yok */ }
  try { if (NATIVE) { NATIVE.ring && NATIVE.ring(false); NATIVE.stopSpeaking && NATIVE.stopSpeaking(); NATIVE.keepScreen && NATIVE.keepScreen(false); } } catch (e) { /* yok */ }
  if (c.listening) { try { NATIVE.cancelListening(); } catch (e) { /* yok */ } }
  drawCall();
  if (reason && reason !== "hangup") toast(CALL_END_TEXT[reason] || "Görüşme bitti");
  else if (c.state === "active") toast("Görüşme bitti · " + fmtDur((Date.now() - c.t0) / 1000));
  loadSessions();
}
// Android uygulaması bildirimden "Aç" ya da "Reddet" ile açılınca buraya yollar.
window.aliceCallIntent = async (it) => {
  const cur = await api("/api/call");
  if (!cur || !cur.id || (it.id && cur.id !== it.id) || cur.state === "ended") return toast("Arama artık yok");
  if (it.action === "decline") { api("/api/call/decline", { id: cur.id }); if (CALL) endCall("declined"); return; }
  if (cur.state === "ringing") {
    if (!CALL) incomingCall({ id: cur.id, reason: cur.reason, text: (cur.lines || [])[0] ? cur.lines[0].text : "" });
    if (it.action === "answer") answerCall();
  } else if (!CALL) resumeCall();
};
// Sayfa yenilenirse süren görüşmeye dön.
async function resumeCall() {
  const cur = await api("/api/call");
  if (!cur || !cur.id || cur.state === "ended" || CALL) return;
  if (cur.state === "ringing" && cur.outbound) return incomingCall({ id: cur.id, reason: cur.reason, text: "" });
  if (cur.state === "active" && cur.live) {
    newCall(cur.id, { state: "active", outbound: cur.outbound, t0: (cur.answered || cur.started) * 1000, live: true, mode: "listening" });
    CALL.caption = (cur.lines || []).filter((l) => l.who === "alice").map((l) => l.text).pop() || "";
    startLiveMic(CALL);
    return;
  }
  if (cur.state === "active" && NATIVE && NATIVE.listen) {
    newCall(cur.id, { state: "active", outbound: cur.outbound, t0: (cur.answered || cur.started) * 1000 });
    CALL.caption = (cur.lines || []).filter((l) => l.who === "alice").map((l) => l.text).pop() || "";
    startCallListening();
  }
}

/* Canlı arama (bilgisayardaki sesli asistanla aynı yol): mikrofon 16 kHz PCM olarak ~120 ms'lik parçalarla bilgisayara
   akar, ChatGPT Realtime dinler ve konuşur; ALICE'in sesi call_pcm parçalarıyla gelir, WebAudio ile kesintisiz çalınır.
   Yankıyı telefonun yankı engelleyicisi siler, bu yüzden ALICE konuşurken araya girilebilir. */
function liveAudio(c) {
  if (c.out) return c.out;
  const AC = window.AudioContext || window.webkitAudioContext;
  if (!AC) return null;
  try { c.out = new AC({ sampleRate: 24000 }); } catch (e) { c.out = new AC(); }
  c.next = 0;
  c.srcs = [];
  return c.out;
}
function callPcm(ev) {
  if (earlyEvent(ev)) return;
  const c = CALL;
  if (!c || ev.id !== c.id) return;
  const ctx = liveAudio(c);
  if (!ctx) return;
  if (ctx.state === "suspended") ctx.resume();
  const bin = atob(ev.pcm || "");
  const n = bin.length >> 1;
  if (!n) return;
  const buf = ctx.createBuffer(1, n, 24000);
  const ch = buf.getChannelData(0);
  for (let i = 0; i < n; i++) {
    let v = bin.charCodeAt(2 * i) | (bin.charCodeAt(2 * i + 1) << 8);
    if (v >= 32768) v -= 65536;
    ch[i] = v / 32768;
  }
  const src = ctx.createBufferSource();
  src.buffer = buf;
  src.connect(ctx.destination);
  const fresh = !c.next || c.next < ctx.currentTime;
  const t = fresh ? ctx.currentTime + 0.25 : c.next;  // yeni konuşma: ağ titremesine karşı kısa tampon
  src.start(t);
  c.next = t + buf.duration;
  c.srcs.push(src);
  src.onended = () => {
    c.srcs = c.srcs.filter((x) => x !== src);
    if (c.srcs.length || CALL !== c) return;
    clearTimeout(c.quietT);  // cümle arası kısa boşlukta sıra kullanıcıya geçmesin
    c.quietT = setTimeout(() => { if (CALL === c && !(c.srcs && c.srcs.length)) liveMode(c); }, 700);
  };
  c.played = true;
  if (c.mode !== "speaking") { c.mode = "speaking"; drawCall(); }
}
function liveClear(c) {
  for (const s of c.srcs || []) { try { s.onended = null; s.stop(); } catch (e) { /* yok */ } }
  c.srcs = [];
  c.next = 0;
}
function liveMode(c) {
  const m = (c.srcs && c.srcs.length) ? "speaking" : "listening";
  if (c.nativeMic && m === "listening") { if (!c.listening) startCallListening(); return; }
  if (c.mode !== m) { c.mode = m; drawCall(); }
}
/* Canlı mikrofon çalışmıyorsa (WebView ses vermiyor): telefonun konuşma tanıması dinler, yazı canlı oturuma gider,
   ALICE yine canlı sesle cevap verir. */
function liveNativeMic(c, msg) {
  if (!c || c.nativeMic || CALL !== c) return;
  stopLiveMic(c);
  if (!NATIVE || !NATIVE.listen) { toast("Mikrofon sesi alınamıyor"); return; }
  c.nativeMic = true;
  toast(msg);
  if (!(c.srcs && c.srcs.length)) startCallListening();
}
function callText(ev) {
  if (earlyEvent(ev)) return;
  const c = CALL;
  if (!c || ev.id !== c.id) return;
  if (ev.who === "alice") { c.caption = ev.text || ""; c.heard = ""; drawCall(); return; }
  c.heard = ev.text || "";
  if (c.mode !== "speaking") c.mode = "thinking";
  drawCall();
}
function micSender(c) {
  let inflight = 0;
  return (b64) => {
    if (inflight > 6) return;  // ağ tıkandı: eski sesi biriktirmek gecikmeyi büyütür
    inflight++;
    api("/api/call/mic", { id: c.id, pcm: b64 }).then((r) => {
      inflight--;
      if (r && r.ok === false && r.live === false && CALL === c) { stopLiveMic(c); c.live = false; }
    });
  };
}
function startNativeMic(c) {
  if (!NATIVE || !NATIVE.micStart) return false;
  const send = micSender(c);
  window.aliceMic = (ev) => {
    if (CALL !== c || !c.live || !ev || !ev.pcm) return;
    c.micFrames = (c.micFrames || 0) + 1;
    send(c.muted ? ev.pcm.replace(/[^=]/g, "A") : ev.pcm);  // susturulduysa sessizlik
    if (c.mode !== "speaking") levelOrb(c.muted ? 0 : (ev.lv || 0) * 2);
  };
  window.aliceMicPermission = (ok) => { if (CALL === c && ok && !c.mic) startLiveMic(c); };
  let ok = false;
  try { ok = !!NATIVE.micStart(); } catch (e) { ok = false; }
  if (!ok) { window.aliceMic = null; return false; }
  c.mic = { native: true };
  return true;
}
async function startLiveMic(c) {
  if (startNativeMic(c)) {
    setTimeout(() => { if (CALL === c && c.mic && !c.micFrames) liveNativeMic(c, "Mikrofon sesi alınamadı; telefonun konuşma tanımasıyla devam"); }, 4000);
    return;
  }
  if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)) {
    // Eski uygulama + ev ağı (http): canlı mikrofon yok. İzin bekleniyorsa (micStart false) bir şey yapma.
    if (NATIVE && NATIVE.micStart) return;
    return liveNativeMic(c, "Canlı mikrofon için telefon uygulamasını güncelle; şimdilik konuşma tanımasıyla");
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
  } catch (e) {
    if (CALL !== c) return;
    toast("Mikrofon açılamadı; telefonun konuşma tanımasıyla devam");
    c.mode = "listening";
    drawCall();
    return startCallListening();  // söylenen yazıya döner, yine canlı oturuma gider
  }
  if (CALL !== c) { stream.getTracks().forEach((t) => t.stop()); return; }
  const AC = window.AudioContext || window.webkitAudioContext;
  let ctx;
  // Cihazın kendi örnekleme hızı: bazı WebView'larda 16 kHz bağlam mikrofondan sessizlik veriyor. 16 kHz'e burada inilir.
  ctx = new AC();
  if (ctx.state === "suspended") ctx.resume().catch(() => {});
  const src = ctx.createMediaStreamSource(stream);
  const proc = ctx.createScriptProcessor(1024, 1, 1);
  const step = ctx.sampleRate / 16000;
  let pos = 0, pend = new Int16Array(4096), len = 0, inflight = 0, lvT = 0;
  const flush = () => {
    if (!len) return;
    const bytes = new Uint8Array(pend.buffer, 0, len * 2);
    let bin = "";
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    len = 0;
    if (inflight > 6) return;  // ağ tıkandı: eski sesi biriktirmek gecikmeyi büyütür
    inflight++;
    api("/api/call/mic", { id: c.id, pcm: btoa(bin) }).then((r) => {
      inflight--;
      if (r && r.ok === false && r.live === false && CALL === c) { stopLiveMic(c); c.live = false; }
    });
  };
  proc.onaudioprocess = (e) => {
    if (CALL !== c || !c.live) return;
    c.micFrames = (c.micFrames || 0) + 1;
    const inp = e.inputBuffer.getChannelData(0);
    let peak = 0;
    for (; pos < inp.length; pos += step) {
      const v = Math.max(-1, Math.min(1, inp[pos | 0]));
      if (Math.abs(v) > peak) peak = Math.abs(v);
      if (len < pend.length) pend[len++] = c.muted ? 0 : v * 32767;
    }
    pos -= inp.length;
    const now = Date.now();
    if (now - lvT > 80) { lvT = now; if (c.mode !== "speaking") levelOrb(c.muted ? 0 : peak * 2); }
    if (len >= 1920) flush();  // 120 ms
  };
  src.connect(proc);
  proc.connect(ctx.destination);  // ScriptProcessor bağlı değilse çalışmıyor; çıkışı sessiz
  c.mic = { stream, ctx, proc };
  setTimeout(() => { if (CALL === c && c.mic && !c.micFrames) liveNativeMic(c, "Mikrofon sesi alınamadı; telefonun konuşma tanımasıyla devam"); }, 4000);
  if (c.mode === "thinking" && !c.played) drawCall();
}
function stopLiveMic(c) {
  const m = c && c.mic;
  if (!m) return;
  c.mic = null;
  if (m.native) {
    window.aliceMic = null;
    try { NATIVE.micStop(); } catch (e) { /* yok */ }
    return;
  }
  try { m.proc.onaudioprocess = null; m.proc.disconnect(); } catch (e) { /* yok */ }
  try { m.stream.getTracks().forEach((t) => t.stop()); } catch (e) { /* yok */ }
  try { m.ctx.close(); } catch (e) { /* yok */ }
}

/* ================================================================== açılış */
const RENDER = {
  chat: () => { if (!S.chatBuilt) buildChat(); else { drawChatHead(); drawChips(); } },
  proj: renderProjects,
  pc: () => { if (!$("#pcBody")) renderPc(); else { refreshPc(); grabShot(); } },
  bell: renderBell,
  more: renderMore,
};
document.addEventListener("visibilitychange", () => {
  if (document.hidden) return;
  if (CALL && CALL.state === "ringing" && CALL.outbound) { try { NATIVE && NATIVE.ring && NATIVE.ring(true); } catch (e) { /* yok */ } }
  if (S.tab === "pc") { refreshPc(); grabShot(); }
  api("/api/state").then(applyStatus);
  loadProjects();
  loadTaskAsks();
});
setInterval(() => { if (!document.hidden && S.tab === "pc") refreshPc(); }, 5000);
setInterval(() => { if (!document.hidden) loadProjects(); }, 20000);
setInterval(loadTaskAsks, 20000);  // also while hidden: the in-app notification needs it (cheap: one small request)
loadTaskAsks();
applyTheme();
connect();
(async () => {
  const st = await api("/api/state");
  if (st && st.session !== undefined) applyStatus(st);
  await Promise.all([loadSessions(), loadProjects(), api("/api/remote/status").then((r) => { if (r.ok !== false) S.remote = r; })]);
  let saved = "chat";
  try { saved = localStorage.getItem("alice-tab") || "chat"; } catch (e) { /* özel pencere */ }
  // Bildirimden açıldıysa (#bell gibi) o sekme.
  const hash = location.hash.replace("#", "");
  show(RENDER[hash] ? hash : RENDER[saved] ? saved : "chat");
  drawChatHead();
  resumeCall();
})();
