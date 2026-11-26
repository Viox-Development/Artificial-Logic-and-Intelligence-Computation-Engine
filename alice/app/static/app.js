"use strict";

/* ===================================================================== temel */
const TOKEN = document.querySelector('meta[name="alice-token"]').content;
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const app = $("#app");
const scroller = $("#scroller");
const list = $("#messages");
const input = $("#input");

const S = {
  busy: false,
  status: {},
  cur: null,          // akmakta olan asistan cevabı
  working: null,      // "çalışıyor" göstergesi
  tools: new Map(),
  perms: new Map(),
  attachments: [],    // base64 PNG
  queue: [],          // meşgulken yazılan mesajlar
  commands: [],
  popup: null,        // {kind, items, sel, start}
  turnStart: 0,       // işin başladığı an (performance.now)
  sent: [],           // yukarı okla geri çağrılacak gönderilmiş mesajlar
  sentIdx: -1,
  online: true,
  todo: "",           // son plan (todo_write çıktısı)
  turn: null,         // bu turun sayaçları: araç, hata, değişen dosyalar
  changes: [],        // oturumdaki dosya değişiklikleri (/api/changes)
  started: Date.now(),
};

/* Kullanıcı tercihleri (tema, yazı boyutu, araç ayrıntıları, panel) tarayıcıda saklanır. */
const PREFS_KEY = "alice.prefs";
const PREFS = Object.assign({ theme: "system", size: "normal", verbose: false, panel: false, tab: "plan" },
  (() => { try { return JSON.parse(localStorage.getItem(PREFS_KEY) || "{}"); } catch { return {}; } })());
function savePrefs() { try { localStorage.setItem(PREFS_KEY, JSON.stringify(PREFS)); } catch { /* yok */ } }
function applyPrefs() {
  const root = document.documentElement;
  if (PREFS.theme === "system") delete root.dataset.theme; else root.dataset.theme = PREFS.theme;
  root.dataset.size = PREFS.size;
  app.classList.toggle("verbose", !!PREFS.verbose);
}
applyPrefs();

async function api(path, body) {
  const opts = { headers: { "X-Alice-Token": TOKEN } };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  try {
    const r = await fetch(path, opts);
    return await r.json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}
function svg(paths, cls) {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  if (cls) s.setAttribute("class", cls);
  s.innerHTML = paths;
  return s;
}
const IC = {
  back: '<path d="M15 6l-6 6 6 6"/>',
  fwd: '<path d="M9 6l6 6-6 6"/>',
  reload: '<path d="M20 12a8 8 0 1 1-2.34-5.66M20 4v4h-4"/>',
  ext: '<path d="M14 5h5v5M19 5l-8 8M18 14v4a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h4"/>',
  home: '<path d="M4 11l8-7 8 7v8a1 1 0 0 1-1 1h-4v-6h-6v6H5a1 1 0 0 1-1-1z"/>',
  disk: '<rect x="3" y="6" width="18" height="12" rx="2"/><path d="M7 14h.01M11 14h6"/>',
  clock: '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/>',
  up: '<path d="M12 19V5M6 11l6-6 6 6"/>',
  more: '<circle cx="5" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="19" cy="12" r="1.2"/>',
  pin: '<path d="M9 4h6l-1 5 3 3v2H7v-2l3-3zM12 14v6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h8"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  rewind: '<path d="M4 12a8 8 0 1 0 2.3-5.6"/><path d="M4 4v4.5h4.5"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/>',
  trash: '<path d="M5 7h14M10 7V5h4v2M7 7l1 12h8l1-12"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  chev: '<path d="M9 6l6 6-6 6"/>',
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
  pen: '<path d="M4 20h4L19 9l-4-4L4 16z"/>',
  term: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M13 15h4"/>',
  search: '<circle cx="11" cy="11" r="6"/><path d="M20 20l-4.3-4.3"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
  screen: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
  cursor: '<path d="M5 3l14 7-6 2-2 6z"/>',
  folder: '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2h7.5A2.5 2.5 0 0 1 21 9.5v7a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 16.5z"/>',
  list: '<path d="M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01"/>',
  bolt: '<path d="M13 3L5 13h6l-1 8 8-10h-6z"/>',
  plug: '<path d="M9 3v5M15 3v5M7 8h10v3a5 5 0 0 1-10 0zM12 16v5"/>',
  brain: '<path d="M9 4a3 3 0 0 0-3 3 3 3 0 0 0-2 5 3 3 0 0 0 2 5 3 3 0 0 0 6 1V5a2 2 0 0 0-3-1zM15 4a3 3 0 0 1 3 3 3 3 0 0 1 2 5 3 3 0 0 1-2 5 3 3 0 0 1-6 1"/>',
  spark: '<path d="M12 2v6M12 16v6M2 12h6M16 12h6M4.9 4.9l4.2 4.2M14.9 14.9l4.2 4.2M4.9 19.1l4.2-4.2M14.9 9.1l4.2-4.2"/>',
  shield: '<path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/>',
  map: '<path d="M9 4L3 6v14l6-2 6 2 6-2V4l-6 2z"/><path d="M9 4v14M15 6v14"/>',
  hand: '<path d="M8 13V5a1.5 1.5 0 0 1 3 0v6M11 11V4a1.5 1.5 0 0 1 3 0v7M14 11V5.5a1.5 1.5 0 0 1 3 0V14a7 7 0 0 1-7 7 6 6 0 0 1-5.5-3.5L3 14a1.5 1.5 0 0 1 2.6-1.4L8 15"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M17 6l3 3M14 9l2 2"/>',
  down: '<path d="M7 14V4M7 14l4 6c1.5 0 2.5-1 2.3-2.5L13 14h5a2 2 0 0 0 2-2.3l-1-6A2 2 0 0 0 17 4H7"/>',
  sound: '<path d="M4 9v6h4l5 4V5L8 9z"/><path d="M16 9a4 4 0 0 1 0 6M18.5 6.5a8 8 0 0 1 0 11"/>',
  unlock: '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 7.5-2"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  panel: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M15 4v16"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z"/>',
  chat: '<path d="M4 5h16v11H8l-4 4z"/>',
  diff: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 14h6M12 11v6"/>',
  gauge: '<path d="M4 18a8 8 0 1 1 16 0"/><path d="M12 18l4-6"/>',
  keyboard: '<rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/>',
  expand: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
  update: '<path d="M12 4v10M8 10l4 4 4-4"/><path d="M5 18h14"/>',
  plug: '<path d="M9 3v6M15 3v6"/><path d="M6 9h12v3a6 6 0 0 1-12 0z"/><path d="M12 18v3"/>',
  branch: '<circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="8" r="2"/><path d="M6 7v10M18 10c0 4-6 3-11.2 7.3"/>',
};

marked.setOptions({ gfm: true, breaks: false });
function renderMd(text) {
  const html = DOMPurify.sanitize(marked.parse(text || ""), {
    FORBID_TAGS: ["style", "form", "input", "button", "iframe", "object", "embed", "img"],
    FORBID_ATTR: ["style"],
  });
  const tpl = document.createElement("template");
  tpl.innerHTML = html;
  // Kod bloklarını başlık + kopyala butonlu kutuya sar.
  for (const pre of tpl.content.querySelectorAll("pre")) {
    const code = pre.querySelector("code");
    const lang = ((code && code.className.match(/language-([\w+#-]+)/)) || [])[1] || "";
    const box = el("div", "codeblock");
    const head = el("div", "cb-head");
    const src = code ? code.textContent : pre.textContent;
    if (code && AliceHL.langOf(lang)) code.innerHTML = AliceHL.highlight(src, lang);
    const nLines = src.replace(/\n$/, "").split("\n").length;
    const title = el("span", "cb-lang", lang || _("code"));
    if (nLines > 1) title.appendChild(el("small", "", _("{n} lines", { n: nLines })));
    head.appendChild(title);
    const cp = el("button", "act");
    cp.dataset.copy = "code";
    cp.appendChild(svg(IC.copy));
    cp.appendChild(document.createTextNode(_("Copy")));
    head.appendChild(cp);
    pre.replaceWith(box);
    box.append(head, pre);
  }
  return tpl.content;
}
function setMd(node, text) {
  node.replaceChildren(renderMd(text));
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.hidden = true; }, 2200);
}
async function copyText(text, btn) {
  try { await navigator.clipboard.writeText(text); }
  catch {
    const ta = el("textarea"); ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand("copy"); ta.remove();
  }
  if (btn) {
    const old = btn.innerHTML;
    btn.replaceChildren(svg(IC.check), document.createTextNode(_("Copied")));
    setTimeout(() => { btn.innerHTML = old; }, 1400);
  } else toast(_("Copied"));
}

/* ================================================================== kaydırma */
function nearBottom() { return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 140; }
function toBottom(force) { if (force || nearBottom()) scroller.scrollTop = scroller.scrollHeight; updateJump(); }
function add(node, force) {
  const stick = force || nearBottom();
  setEmpty(false);
  list.appendChild(node);
  if (stick) scroller.scrollTop = scroller.scrollHeight;
  return node;
}
function setEmpty(v) { app.classList.toggle("empty", v); }
function clearChat() {
  hideWorking();
  list.replaceChildren();
  // Süren araç satırlarının sayaçları da durur: sohbet değişince/yeniden bağlanınca satır atılıyor ama aralığı sonsuza
  // dek kopuk düğümü güncelliyordu (tool_end yeni bir satıra gider).
  for (const t of S.tools.values()) clearInterval(t._timer);
  S.tools.clear(); S.perms.clear(); S.cur = null;
  setTodo("");
  const ts = document.getElementById("turnSuggest");
  if (ts) ts.replaceChildren();
  setEmpty(true);
  greet();
}
function greet() {
  const h = new Date().getHours();
  const part = h < 5 ? _("Good night") : h < 12 ? _("Good morning") : h < 18 ? _("Good afternoon") : h < 23 ? _("Good evening") : _("Good night");
  const name = S.status.user ? `, ${S.status.user}` : "";
  $("#greeting").textContent = part + name;
  if (typeof drawOnboarding === "function" && !greet._ob) { greet._ob = true; drawOnboarding().finally(() => { greet._ob = false; }); }
}

/* ================================================================== mesajlar */
function addUser(text, mi, images) {
  const wrap = el("div", "turn-user");
  wrap.dataset.say = text;  // cevaba 👎 verilince hangi isteğin cevabı olduğu bilinsin
  const b = el("div", "bubble");
  if (images && images.length) {
    const th = el("div", "thumbs");
    for (const src of images) {
      const img = el("img");
      img.src = src.startsWith("data:") ? src : "data:image/png;base64," + src;
      img.addEventListener("click", () => showImage(img.src));
      th.appendChild(img);
    }
    b.appendChild(th);
  }
  b.appendChild(document.createTextNode(text));
  const acts = el("div", "msg-actions");
  const cp = el("button", "act"); cp.title = _("Copy"); cp.appendChild(svg(IC.copy));
  cp.addEventListener("click", () => copyText(text));
  acts.appendChild(cp);
  if (mi !== undefined && mi !== null) {
    const ed = el("button", "act"); ed.title = _("Edit the message and resend from here (double-click works too)");
    ed.append(svg(IC.edit), document.createTextNode(_("Edit")));
    ed.addEventListener("click", () => editInline(wrap, text, mi));
    b.addEventListener("dblclick", () => editInline(wrap, text, mi));
    const rw = el("button", "act"); rw.title = _("Rewind to before this message (files included)");
    rw.append(svg(IC.rewind), document.createTextNode(_("Rewind")));
    rw.addEventListener("click", () => rewindTo(mi, false));
    acts.append(ed, rw);
  }
  wrap.append(b, acts);
  return add(wrap, true);
}

// Göstergede geçen süre de yazar: uzun bir adımda arayüz donmuş gibi görünmesin.
function showWorking(label) {
  if (S.working) { S.working.querySelector(".lbl").textContent = label; return; }
  const w = el("div", "working");
  w.appendChild(svg(IC.spark, "spark"));
  w.appendChild(el("span", "lbl shimmer", label));
  const secs = el("span", "secs");
  w.appendChild(secs);
  const t0 = S.turnStart || performance.now();
  w._timer = setInterval(() => {
    const s = Math.round((performance.now() - t0) / 1000);
    secs.textContent = s >= 3 ? fmtSecs(s) : "";
  }, 1000);
  S.working = add(w);
}
function hideWorking() { if (S.working) { clearInterval(S.working._timer); S.working.remove(); S.working = null; } }
function fmtSecs(s) { return s < 60 ? _("{secs} s", { secs: s }) : _("{m} min {s} s", { m: Math.floor(s / 60), s: s % 60 }); }

function ensureAi() {
  if (S.cur) return S.cur;
  hideWorking();
  const root = el("div", "turn-ai");
  const prose = el("div", "prose");
  root.appendChild(prose);
  add(root);
  S.cur = { root, prose, text: "", thought: null, tStart: 0, raf: false };
  return S.cur;
}
function aiActions(root, text, source) {
  const acts = el("div", "msg-actions");
  const cp = el("button", "act"); cp.append(svg(IC.copy), document.createTextNode(_("Copy")));
  cp.addEventListener("click", () => copyText(text, cp));
  acts.appendChild(cp);
  const bad = el("button", "act"); bad.title = _("I didn't like this answer: it goes into the test set and is tried in later versions");
  bad.append(svg(IC.down), document.createTextNode(_("Didn't like it")));
  bad.addEventListener("click", () => {
    if (acts.querySelector(".fb-note")) return;
    let say = "";
    for (let n = root.previousElementSibling; n; n = n.previousElementSibling) {
      if (n.classList.contains("turn-user")) { say = n.dataset.say || ""; break; }
    }
    const note = el("input", "fb-note");
    note.type = "text";
    note.placeholder = _("What didn't you like? (optional) Enter");
    const send = () => api("/api/feedback", { say, reply: text, note: note.value.trim() }).then((r) => {
      note.remove();
      bad.classList.add("on");
      toast(r && r.ok ? _("Saved, we'll work on it") : _("could not save"));
    });
    note.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); if (e.key === "Escape") note.remove(); });
    acts.appendChild(note);
    note.focus();
  });
  acts.appendChild(bad);
  if (source) acts.appendChild(el("span", "source", source));
  root.appendChild(acts);
}

function onThinking(t) {
  const c = ensureAi();
  if (!c.thought) {
    const d = el("details", "thought");
    const sum = el("summary");
    sum.append(svg(IC.chev), el("span", "shimmer", _("Thinking")));
    const body = el("div", "tbody");
    d.append(sum, body);
    c.root.insertBefore(d, c.prose);
    c.thought = { d, sum, body, text: "" };
    c.tStart = performance.now();
  }
  c.thought.text += t;
  c.thought.body.textContent = c.thought.text;
  toBottom();
}
function closeThought(c) {
  if (!c || !c.thought || c.thought.closed) return;
  c.thought.closed = true;
  const secs = Math.max(1, Math.round((performance.now() - c.tStart) / 1000));
  c.thought.sum.lastChild.className = "";
  c.thought.sum.lastChild.textContent = _("thought for {secs} s", { secs });
}

function onDelta(t) {
  const c = ensureAi();
  closeThought(c);
  c.text += t;
  if (!c.raf) {
    c.raf = true;
    requestAnimationFrame(() => {
      c.raf = false;
      if (S.cur !== c) return;
      const stick = nearBottom();
      setMd(c.prose, c.text);
      c.prose.classList.add("cursor");
      if (stick) scroller.scrollTop = scroller.scrollHeight;
      else updateJump();
    });
  }
}
function onStreamEnd(text, source) {
  const c = S.cur;
  if (!text) {
    if (c) { closeThought(c); if (!c.text && !c.thought) c.root.remove(); }
    S.cur = null;
    return;
  }
  const cc = c || ensureAi();
  closeThought(cc);
  cc.text = text;
  setMd(cc.prose, text);
  cc.prose.classList.remove("cursor");
  aiActions(cc.root, text, source);
  S.cur = null;
  toBottom();
}
function addAiStatic(text) {
  const root = el("div", "turn-ai");
  const prose = el("div", "prose");
  setMd(prose, text);
  root.appendChild(prose);
  aiActions(root, text, "");
  add(root);
}
// Aynı not art arda gelirse (model atlandı, limit...) satır çoğalmasın, sayaç artsın.
/* /btw yan sorusu: iş sürerken sorulur, konuşmaya yazılmaz. Önce "düşünüyor" kartı, cevap gelince aynı kart dolar. */
function addBtw(ev) {
  let card = [...document.querySelectorAll(".btw-card.pending")].find((c) => c._q === ev.q);
  if (!card) {
    card = el("div", "btw-card pending");
    card._q = ev.q;
    card.append(el("div", "btw-q", _("Side question: {q}", { q: ev.q })), el("div", "btw-a md", _("thinking…")));
    add(card);
  }
  if (!ev.pending) {
    card.classList.remove("pending");
    card.querySelector(".btw-a").replaceChildren(renderMd(ev.a || ""));
  }
  toBottom();
}

function addNote(text, kind) {
  const last = lastContent();
  if (last && last.classList.contains("note") && last._text === text) {
    last._n = (last._n || 1) + 1;
    last.textContent = `${text} (×${last._n})`;
    return last;
  }
  const n = el("div", "note" + (kind ? " " + kind : ""), text);
  n._text = text;
  return add(n);
}

/* =================================================================== araçlar */
const VERBS = {
  read_file: [_("Read"), IC.file], write_file: [_("Written"), IC.pen], edit_file: [_("Edited"), IC.pen],
  multi_edit: [_("Edited"), IC.pen],
  list_dir: [_("Folder"), IC.folder], glob: [_("Files searched"), IC.search], grep: [_("Searched"), IC.search],
  run_command: [_("Command"), IC.term], process_output: [_("Process output"), IC.term], process_stop: [_("Process stopped"), IC.term],
  web_fetch: [_("Page read"), IC.globe], web_search: [_("Searched the web"), IC.globe], browser: [_("Browser"), IC.globe],
  computer: [_("Screen"), IC.screen], windows: [_("Window"), IC.screen], ui_elements: [_("UI scanned"), IC.cursor],
  ui_act: [_("UI"), IC.cursor], clipboard: [_("Clipboard"), IC.copy], system_info: [_("System info"), IC.screen],
  process_list: [_("Processes"), IC.list], process_kill: [_("Process ended"), IC.x], todo_write: [_("Plan"), IC.list],
  memory: [_("Memory"), IC.brain], task: [_("Subagent"), IC.spark], reflex: ["Reflex", IC.bolt],
};
Object.assign(VERBS, {
  assign: [_("Agent set up"), IC.spark], plan: [_("Plan presented"), IC.list], review: [_("Review decision"), IC.check],
  message: [_("Message"), IC.chat], team_send: [_("Message"), IC.chat], team_wait: [_("Waited for an answer"), IC.chat],
  diff: [_("Looked at the diff"), IC.diff], council: [_("Asked the council"), IC.shield], apply_patch: [_("Patch"), IC.pen],
  file_ops: [_("File operation"), IC.folder], git: ["Git", IC.branch], http_request: [_("Request"), IC.globe],
});
function verbFor(name) {
  if (name.startsWith("mcp__")) {
    const [, srv, tool] = name.split("__");
    return [`${srv} · ${tool}`, IC.plug];
  }
  return VERBS[name] || [name, IC.bolt];
}

// Art arda gelen salt okuma çağrıları tek satırda toplanır: "3 dosya okundu, 2 arama yapıldı".
const GROUP_KIND = {
  read_file: "file", list_dir: "dir", glob: "search", grep: "search", web_search: "web", web_fetch: "page",
  system_info: "sys", process_list: "sys", process_output: "sys",
};
const GROUP_TEXT = {
  file: (n) => _("{n} files read", { n }), dir: (n) => _("{n} folders listed", { n }), search: (n) => _("{n} searches", { n }),
  web: (n) => _("{n} web searches", { n }), page: (n) => _("{n} pages read", { n }), sys: (n) => _("{n} system queries", { n }),
};
function lastContent() {
  let n = list.lastElementChild;
  if (n && n === S.working) n = n.previousElementSibling;
  return n;
}
function makeGroup() {
  const g = el("div", "tool-group");
  const row = el("div", "tool-row");
  row.appendChild(svg(IC.list, "ti"));
  const v = el("span", "verb");
  const meta = el("span", "meta");
  row.append(v, el("span", "arg"), meta, svg(IC.chev, "tchev"));
  const body = el("div", "group-body");
  g.append(row, body);
  row.addEventListener("click", () => g.classList.toggle("open"));
  if (PREFS.verbose) g.classList.add("open");
  g._v = v; g._meta = meta; g._body = body;
  return g;
}
function updateGroup(g) {
  if (!g) return;
  const members = Array.from(g._body.children);
  const counts = {};
  let running = 0, errors = 0;
  for (const m of members) {
    const k = GROUP_KIND[m._name] || "sys";
    counts[k] = (counts[k] || 0) + 1;
    if (m.classList.contains("running")) running++;
    if (m.classList.contains("err")) errors++;
  }
  const order = ["file", "dir", "search", "web", "page", "sys"];
  const text = order.filter((k) => counts[k]).map((k) => GROUP_TEXT[k](counts[k])).join(", ");
  g._v.textContent = text.charAt(0).toUpperCase() + text.slice(1);
  g._v.classList.toggle("shimmer", running > 0);
  g._meta.replaceChildren();
  if (errors) g._meta.appendChild(el("span", "d", `${errors} hata`));
}

// Adımlar kartı: başlıkta özet ("4 adım · 2 dosya · 1,2 sn"), tıklayınca açılıp kapanır. İş bitince 3+ adımlı ve hatasız
// kartlar kendiliğinden kapanır; ham çıktılar ancak kart açılınca görünür.
const FILE_TOOLS = new Set(["write_file", "edit_file", "multi_edit", "apply_patch", "notebook_edit"]);
function makeSteps() {
  const box = el("div", "steps");
  const head = el("button", "steps-head");
  const ic = el("span", "sh-ic");
  ic.append(svg(IC.check, "ok"), el("i", "spin"));
  const t = el("b", "sh-t", _("Working"));
  const meta = el("span", "sh-meta");
  head.append(ic, t, meta, el("span", "grow"), svg(IC.chev, "tchev"));
  head.addEventListener("click", () => box.classList.toggle("collapsed"));
  const body = el("div", "steps-body");
  box.append(head, body);
  box._t = t; box._meta = meta; box._body = body; box._t0 = performance.now();
  return box;
}
function updateSteps(box) {
  if (!box || !box._body) return;
  const tools = Array.from(box._body.querySelectorAll(".tool"));
  const running = tools.filter((x) => x.classList.contains("running")).length;
  const errs = tools.filter((x) => x.classList.contains("err")).length;
  const files = new Set(tools.filter((x) => FILE_TOOLS.has(x._name)).map((x) => x._summary.split(" ")[0]));
  box.classList.toggle("running", running > 0);
  box.classList.toggle("has-err", errs > 0);
  box._t.textContent = running ? _("Working · {n} steps", { n: tools.length }) : _("{n} steps", { n: tools.length });
  const parts = [];
  if (files.size) parts.push(_("{n} files", { n: files.size }));
  if (errs) parts.push(_("{n} errors", { n: errs }));
  if (!running && box._t1 && box._t1 > box._t0) parts.push(fmtDur((box._t1 - box._t0) / 1000));
  box._meta.textContent = parts.join(" · ");
}
function collapseSteps() {
  for (const b of $$(".steps")) {
    if (b._done) continue;
    b._done = true;
    const n = b._body.querySelectorAll(".tool").length;
    if (n >= 3 && !b.classList.contains("has-err") && !PREFS.verbose) b.classList.add("collapsed");
  }
}

// Araç satırı (listeye eklemeden): sohbet ve proje konuşmaları aynı parçayı kullanır.
function makeTool(name, summary, running) {
  const [verb, icon] = verbFor(name);
  const t = el("div", "tool" + (running ? " running" : ""));
  const row = el("div", "tool-row");
  // Durum yuvası: çalışırken dönen halka, bitince araç simgesi ve yeşil/kırmızı nokta.
  const st = el("span", "tstat");
  st.append(svg(icon, "ti"), el("i", "spin"));
  row.appendChild(st);
  const v = el("span", "verb" + (running ? " shimmer" : ""), verb);
  row.append(v, el("span", "arg", summary || ""));
  const meta = el("span", "meta");
  row.append(meta, svg(IC.chev, "tchev"));
  const peek = el("div", "tool-peek");
  const body = el("div", "tool-body");
  t.append(row, peek, body);
  row.addEventListener("click", () => t.classList.toggle("open"));
  t._v = v; t._meta = meta; t._body = body; t._peek = peek; t._name = name; t._summary = summary || "";
  t._t0 = running ? performance.now() : 0;
  return t;
}
function toolRow(id, name, summary, running) {
  hideWorking();
  if (S.cur) { closeThought(S.cur); if (!S.cur.text && !S.cur.thought) S.cur.root.remove(); S.cur = null; }
  const t = makeTool(name, summary, running);
  if (running && S.turn) S.turn.tools++;
  if (running) {
    // Uzun süren araçta geçen süre canlı yazsın (Claude Code'daki gibi).
    t._timer = setInterval(() => {
      const s = (performance.now() - t._t0) / 1000;
      if (s >= 2) t._meta.textContent = fmtDur(s);
    }, 500);
  }
  if (id) S.tools.set(id, t);
  // Art arda gelen araçlar tek bir "adımlar" kartında toplanır; araya cevap, plan ya da izin kartı girince yeni kart açılır.
  let box = lastContent();
  if (!box || !box.classList.contains("steps")) box = add(makeSteps());
  const card = box;
  box = card._body;
  setTimeout(() => updateSteps(card), 0);
  if (GROUP_KIND[name]) {
    const last = box.lastElementChild;
    let g = null;
    if (last && last.classList.contains("tool-group")) g = last;
    else if (last && last.classList.contains("tool") && GROUP_KIND[last._name]) {
      g = makeGroup();
      box.insertBefore(g, last);
      g._body.appendChild(last);
    }
    if (g) {
      g._body.appendChild(t);
      updateGroup(g);
      toBottom();
      return t;
    }
  }
  const stick = nearBottom();
  box.appendChild(t);
  toBottom(stick);
  return t;
}
// 1.8k, 840k, 1M gibi kısa token yazımı.
function fmtTok(n) {
  if (n >= 1e6) {
    const m = n / 1048576;
    return decimal(Math.abs(m - Math.round(m)) < 0.01 ? String(Math.round(m)) : (n / 1e6).toFixed(1)) + "M";
  }
  if (n >= 1e4) return `${Math.round(n / 1000)}k`;
  return `${decimal((n / 1000).toFixed(1))}k`;
}
// "8.4" in English, "8,4" in Turkish.
function decimal(text) { return ALICE_LANG === "tr" ? text.replace(".", ",") : text; }
function fmtDur(s) { return s < 10 ? _("{secs} s", { secs: decimal(s.toFixed(1)) }) : fmtSecs(Math.round(s)); }

// Satır numaralı, renklendirilmiş unified diff (Codex/Claude Code'daki gibi).
function renderDiff(diff, title) {
  const box = el("div", "diff");
  const head = el("div", "diff-head");
  const lines = diff.split("\n");
  const file = title || (lines.find((l) => l.startsWith("+++ b/")) || "").slice(6);
  let a = 0, d = 0, on = 0, nn = 0;
  const body = el("div", "diff-body");
  const hl = (code) => AliceHL.highlight(code, file);
  for (const line of lines) {
    if (line.startsWith("+++") || line.startsWith("---")) continue;
    const row = el("div", "dl");
    const lo = el("span", "ln"), ln = el("span", "ln"), sg = el("span", "dsg"), code = el("span", "code");
    if (line.startsWith("@@")) {
      const m = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)/);
      if (m) { on = +m[1]; nn = +m[2]; }
      row.className = "dl hunk";
      code.textContent = line;
    } else if (line.startsWith("+")) {
      row.className = "dl add"; a++;
      ln.textContent = nn++; sg.textContent = "+"; code.innerHTML = hl(line.slice(1));
    } else if (line.startsWith("-")) {
      row.className = "dl del"; d++;
      lo.textContent = on++; sg.textContent = "−"; code.innerHTML = hl(line.slice(1));
    } else if (line.startsWith("\\")) {
      row.className = "dl hunk"; code.textContent = line;
    } else {
      lo.textContent = on++; ln.textContent = nn++; code.innerHTML = hl(line.slice(1)) || " ";
    }
    row.append(lo, ln, sg, code);
    body.appendChild(row);
  }
  head.append(svg(IC.file), el("b", "", file), el("span", "a", `+${a}`), el("span", "d", `−${d}`));
  box.append(head, body);
  return { box, a, d, file };
}

function parseTodo(text) {
  const out = [];
  for (const line of (text || "").split("\n")) {
    const m = line.match(/^\[( |~|x)\]\s*(.*)$/);
    if (m) out.push({ state: m[1] === "x" ? "done" : m[1] === "~" ? "doing" : "", text: m[2] });
  }
  return out;
}
function renderTodo(text, big) {
  const items = parseTodo(text);
  const done = items.filter((i) => i.state === "done").length;
  const box = el("div", "todo" + (big ? " big" : ""));
  const head = el("div", "todo-title");
  head.append(svg(IC.list), el("span", "", _("Tasks")), el("span", "todo-n", `${done}/${items.length}`));
  const bar = el("i", "todo-bar");
  const fill = el("i");
  fill.style.width = items.length ? (100 * done) / items.length + "%" : "0";
  bar.appendChild(fill);
  box.append(head, bar);
  const ul = el("ul");
  for (const it of items) {
    const li = el("li", it.state);
    const bx = el("span", "box");
    if (it.state === "done") bx.appendChild(svg(IC.check));
    li.append(bx, el("span", "", it.text));
    ul.appendChild(li);
  }
  box.appendChild(ul);
  box.addEventListener("click", () => openPanel("plan"));
  return box;
}

// Command output in a terminal view: command, exit code, output. The tool writes "exit code N"; chats saved
// before that was English say "çıkış kodu N", so both are recognized (same for background starts).
const EXIT_RX = /^(?:exit code|çıkış kodu) (-?\d+)\n?/;
const BACKGROUND_RX = /^(?:started in the background|arka planda)/;
function renderTerm(cmd, text, isErr) {
  const m = (text || "").match(EXIT_RX);
  const code = m ? +m[1] : null;
  const out = m ? text.slice(m[0].length) : text || "";
  const box = el("div", "term");
  const head = el("div", "term-head");
  const c = el("span", "term-cmd");
  c.append(el("span", "pr", "$"), el("span", "", cmd));
  head.appendChild(c);
  if (code !== null) head.appendChild(el("span", "exit " + (code === 0 ? "ok" : "bad"), _("exit {code}", { code })));
  else if (isErr) head.appendChild(el("span", "exit bad", _("error")));
  const pre = el("pre", "term-out", out.trim() || _("(no output)"));
  box.append(head, pre);
  return { box, code };
}

function peekText(t, ev) {
  let txt = ev.text || "";
  if (t._name === "run_command") txt = txt.replace(EXIT_RX, "");
  const lines = txt.split("\n").map((l) => l.trimEnd()).filter((l) => l.trim());
  if (!lines.length) return "";
  const shown = lines.slice(0, 2).map((l) => (l.length > 140 ? l.slice(0, 137) + "…" : l));
  if (lines.length > 2) shown.push(_("… +{n} lines", { n: lines.length - 2 }));
  return shown.join("\n");
}

function finishTool(t, ev, live) {
  clearInterval(t._timer);
  t.classList.remove("running");
  t._v.classList.remove("shimmer");
  t.classList.add(ev.is_error ? "err" : "ok");
  const group = t.parentElement && t.parentElement.closest(".tool-group");
  if (group) setTimeout(() => updateGroup(group), 0);
  const steps = t.closest(".steps");
  if (steps) { steps._t1 = live ? performance.now() : 0; setTimeout(() => updateSteps(steps), 0); }
  const body = t._body;
  body.replaceChildren();
  t._meta.replaceChildren();
  if (live && S.turn && ev.is_error) S.turn.errs++;
  if (t._name === "todo_write" && !ev.is_error) {
    setTodo(ev.text || "");
    // Aynı turdaki eski plan kartı kalkar, güncel olanı en sonda durur (Codex'teki gibi tek plan).
    for (let n = t.previousElementSibling; n && !n.classList.contains("turn-user"); n = n.previousElementSibling) {
      if (n.classList.contains("todo")) { n.remove(); break; }
    }
    // Plan kartı adımlar kartının içinde değil, hemen altında durur.
    const todo = renderTodo(ev.text || "");
    if (steps) {
      steps.after(todo);
      t.replaceWith();
      if (!steps._body.childElementCount) steps.replaceWith();
      else updateSteps(steps);
    } else t.replaceWith(todo);
    return;
  }
  if (ev.diff) {
    const { box, a, d, file } = renderDiff(ev.diff);
    body.appendChild(box);
    t._meta.append(el("span", "a", `+${a}`), document.createTextNode(" "), el("span", "d", `−${d}`));
    t.classList.add("open");
    if (live && S.turn) {
      const f = S.turn.files.get(file) || { a: 0, d: 0 };
      f.a += a; f.d += d;
      S.turn.files.set(file, f);
      refreshChanges();
    }
  } else if (t._name === "run_command" && !BACKGROUND_RX.test(ev.text || "")) {
    const { box, code } = renderTerm(t._summary, ev.text, ev.is_error);
    body.appendChild(box);
    if (code !== null) t._meta.appendChild(el("span", "exit " + (code === 0 ? "ok" : "bad"), _("exit {code}", { code })));
  } else if (ev.text) {
    body.appendChild(el("pre", "out", ev.text));
  }
  const took = t._t0 ? (performance.now() - t._t0) / 1000 : 0;
  if (took >= 0.05) t._meta.appendChild(el("span", "dur", fmtDur(took)));
  if (PREFS.verbose && body.childNodes.length) t.classList.add("open");
  if (!ev.diff) t._peek.textContent = peekText(t, ev);
  if (ev.images && ev.images.length) {
    const imgs = el("div", "imgs");
    for (const b64 of ev.images) {
      const img = el("img");
      img.src = "data:image/png;base64," + b64;
      img.addEventListener("click", () => showImage(img.src));
      imgs.appendChild(img);
    }
    body.appendChild(imgs);
    t.classList.add("open");
  }
  // Dosya kartları ve özet kutusu adımların içinde değil mesaj düzeyinde: tur bitince adımlar katlanıyor, kart da
  // onlarla birlikte gözden kayboluyordu.
  if (ev.files && ev.files.length) placeAfter(t, fileCards(ev.files));
  if (ev.card && ev.card.kind === "next_step") placeAfter(t, nextStepCard(ev.card, live));
  if (ev.card && ev.card.kind === "usage") {
    const card = el("div", "usage-card");
    card.appendChild(renderUsage(ev.card));
    placeAfter(t, card);
  }
}
function placeAfter(t, node) {
  const step = t.closest(".tool-group") || t.closest(".steps") || t;
  const top = step.closest("#messages > *") || step;
  if (top.parentElement === list) top.after(node); else list.appendChild(node);
  if (S.working) list.appendChild(S.working);
}

/* send_file: kullanıcıya gösterilen dosyalar. Aç / Klasörde göster sadece bu dosyalar için çalışır (/api/reveal). */
function fmtSize(n) {
  return n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;
}
const FILE_KIND = [
  [/\.(png|jpe?g|gif|webp|svg|bmp)$/i, _("image"), "#22c55e"], [/\.pdf$/i, "PDF", "#ef4444"],
  [/\.(md|markdown|txt|log)$/i, _("text"), "#a3a3a3"], [/\.(csv|tsv|xlsx?)$/i, _("table"), "#10b981"],
  [/\.(html?|css)$/i, "web", "#f97316"], [/\.(py|js|ts|json|toml|ya?ml|sh|ps1|sql|xml|ini|cfg)$/i, _("code"), "#60a5fa"],
  [/\.(zip|7z|rar|tar|gz)$/i, _("archive"), "#eab308"], [/\.(exe|msi|apk)$/i, "program", "#a78bfa"],
  [/\.(mp3|wav|ogg|flac|mp4|mkv|webm)$/i, _("media"), "#ec4899"]];
const PREVIEWABLE = /\.(png|jpe?g|gif|webp|svg|bmp|pdf|md|markdown|txt|log|csv|tsv|json|py|js|ts|html?|css|toml|ya?ml|xml|sh|ps1|sql|ini|cfg)$/i;
function fileKind(name) {
  for (const [rx, label, color] of FILE_KIND) if (rx.test(name)) return [label, color];
  return [_("file"), "var(--faint)"];
}
function fileCards(files) {
  const box = el("div", "file-cards");
  for (const f of files) {
    const [kind, color] = fileKind(f.name);
    const card = el("div", "file-card");
    const ext = el("span", "fc-ext", (f.name.split(".").pop() || "").slice(0, 4).toUpperCase());
    ext.style.setProperty("--fc", color);
    const meta = el("span", "fc-meta");
    const dir = (f.path || "").replace(/[\\/][^\\/]*$/, "");
    meta.append(el("b", "", f.name), el("small", "", `${kind} · ${fmtSize(f.size || 0)}` + (dir ? ` · ${dir}` : "")));
    const acts = el("span", "fc-acts");
    if (PREVIEWABLE.test(f.name)) {
      const pv = el("button", "btn", _("Preview"));
      pv.addEventListener("click", () => previewFile(f));
      acts.appendChild(pv);
    }
    const open = el("button", "btn ghost", _("Open"));
    open.addEventListener("click", () => api("/api/reveal", { path: f.path }).then((r) => r && !r.ok && toast(r.error || _("could not open"))));
    const folder = iconAct(IC.folder, _("Show in folder"), () => api("/api/reveal", { path: f.path, folder: true }));
    acts.append(open, folder);
    card.title = f.path;
    card.append(ext, meta, acts);
    if (PREVIEWABLE.test(f.name)) meta.addEventListener("click", () => previewFile(f));
    box.appendChild(card);
  }
  return box;
}
// Dosyayı uygulamadan çıkmadan göster: markdown işlenir, kod renklenir, CSV tablo olur, HTML yalıtılmış çerçevede.
async function previewFile(f) {
  const r = await api("/api/file_preview?path=" + encodeURIComponent(f.path));
  if (!r || !r.ok) return toast((r && r.error) || _("could not preview"));
  showDialog((d) => {
    d.classList.add("preview");
    const head = el("div", "pv-head");
    const open = el("button", "btn ghost", _("Open"));
    open.addEventListener("click", () => api("/api/reveal", { path: f.path }));
    head.append(el("b", "", r.name), open);
    const body = el("div", "pv-body");
    if (r.kind === "image") {
      const img = el("img");
      img.src = `data:${r.mime};base64,${r.b64}`;
      body.appendChild(img);
    } else if (r.kind === "pdf") {
      const bytes = Uint8Array.from(atob(r.b64), (c) => c.charCodeAt(0));
      const fr = el("iframe");
      const url = URL.createObjectURL(new Blob([bytes], { type: "application/pdf" }));
      fr.src = url;
      onDialogClose = () => URL.revokeObjectURL(url);  // her önizleme PDF'i (15 MB'a kadar) bellekte bırakıyordu
      body.appendChild(fr);
    } else if (r.kind === "markdown") {
      const md = el("div", "md");
      md.replaceChildren(renderMd(r.text));
      body.appendChild(md);
    } else if (r.kind === "html") {
      const fr = el("iframe");
      fr.setAttribute("sandbox", "");  // betik çalışmaz, ALICE'e erişemez
      fr.srcdoc = r.text;
      body.appendChild(fr);
    } else if (r.kind === "csv") {
      const sep = /\.tsv$/i.test(r.name) ? "\t" : r.text.split("\n")[0].split(";").length > r.text.split("\n")[0].split(",").length ? ";" : ",";
      const table = el("table", "pv-table");
      r.text.split(/\r?\n/).filter(Boolean).slice(0, 500).forEach((line, i) => {
        const tr = el("tr");
        for (const cell of line.split(sep)) tr.appendChild(el(i ? "td" : "th", "", cell.replace(/^"|"$/g, "")));
        table.appendChild(tr);
      });
      body.appendChild(table);
    } else {
      const pre = el("pre", "pv-code");
      const code = el("code", "language-" + (r.lang || "text"));
      code.textContent = r.text;
      pre.appendChild(code);
      body.appendChild(pre);
      if (r.kind === "code" && window.AliceHL && AliceHL.langOf(r.lang)) code.innerHTML = AliceHL.highlight(r.text, r.lang);
    }
    if (r.truncated) body.appendChild(el("small", "pv-note", _("The file is large: only the start is shown.")));
    d.append(head, body);
    dialogClose(d);
  });
}

/* Tur sonu kutusu (next_step aracı): kısa özet, varsa tek soru ve düğmeler. Düğmeye basınca metni cevap olarak
   gider. Yalnız en sondaki kutu tıklanabilir; yeni mesaj yazılınca eskiler sönükleşir. */
function nextStepCard(card, live) {
  $$(".next-step").forEach((n) => n.classList.add("stale"));
  const box = el("div", "next-step" + (live ? "" : " stale"));
  box.appendChild(el("p", "ns-recap", card.recap || ""));
  if (card.question) {
    const q = el("div", "ns-q");
    q.appendChild(el("b", "", card.question));
    const row = el("div", "ns-acts");
    (card.options || []).forEach((label, i) => {
      const b = el("button", i ? "btn" : "btn accent", label);
      b.addEventListener("click", () => {
        if (box.classList.contains("stale")) return;
        box.classList.add("stale", "answered");
        b.classList.add("chosen");
        submit(label);
      });
      row.appendChild(b);
    });
    q.appendChild(row);
    box.appendChild(q);
  }
  return box;
}

/* ===================================================================== izin */
function onPermission(ev) {
  if (S.perms.has(ev.id)) return;
  hideWorking();
  const card = el("div", "perm" + (ev.risk === "danger" ? " danger" : ev.risk === "send" ? " send" : ""));
  const q = el("div", "perm-q");
  const [verb, icon] = verbFor(ev.name);
  const ic = el("span", "perm-ic");
  ic.appendChild(svg(ev.risk === "danger" ? IC.shield : icon));
  const qt = el("div", "perm-qt");
  const head = ev.risk === "danger" ? _("This may not be undoable")
    : ev.risk === "send" ? _("This message will go to someone else") : _("ALICE asks for permission");
  qt.append(el("b", "", head),
    el("small", "", `${verb} · ${ev.name}`));
  q.append(ic, qt);
  const acts = el("div", "perm-actions");
  // 1-4 tuşları da cevaplar (Claude Code'daki gibi).
  const mk = (label, ans, cls, key) => {
    const b = el("button", "btn " + (cls || ""));
    b.append(document.createTextNode(label), el("kbd", "", key));
    b.addEventListener("click", () => api("/api/permission", { id: ev.id, answer: ans }));
    return b;
  };
  // Tehlikeli ve dışarı giden işlerde kalıcı izin yok (sunucu da kabul etmez).
  const lasting = ev.risk !== "danger" && ev.risk !== "send";
  acts.append(mk(_("Allow"), "y", "primary", "1"), mk(_("Always allow in this session"), "a", "", "2"));
  if (lasting) acts.append(mk(_("Always allow in this project"), "p", "", "3"));
  acts.append(mk(_("Deny"), "n", "danger", lasting ? "4" : "3"));
  const pre = el("pre", ev.name === "run_command" ? "cmd" : "", ev.summary);
  card.append(q, pre, acts);
  card._id = ev.id;
  card._acts = acts;
  S.perms.set(ev.id, card);
  add(card, true);
}
function onPermissionDone(ev) {
  const card = S.perms.get(ev.id);
  if (!card) return;
  const txt = { y: _("Allowed"), a: _("This tool is always allowed in this session"),
    p: _("Allowed permanently in this project (.alice/permissions.toml)"), n: _("Denied") }[ev.answer] || ev.answer;
  card._acts.replaceWith(el("div", "perm-done " + (ev.answer === "n" ? "no" : "yes"), txt));
  card.classList.add("answered");
}
/* ===================================================================== seçenekli soru */
function onChoice(ev) {
  if (S.perms.has(ev.id)) return;
  hideWorking();
  const card = el("div", "perm choice");
  const q = el("div", "perm-q");
  const qt = el("div", "perm-qt");
  qt.append(el("b", "", ev.question), el("small", "", ev.multi ? _("You can choose more than one") : _("ALICE asks")));
  q.append(qt);
  const acts = el("div", "perm-actions choice-actions");
  const send = (ans) => api("/api/choice", { id: ev.id, answer: ans });
  const picked = new Set();
  (ev.options || []).forEach((o, i) => {
    const [label, desc] = String(o).split(" — ");
    const b = el("button", "btn" + (i === 0 ? " primary" : ""));
    b.append(document.createTextNode(label), el("kbd", "", String(i + 1)));
    if (desc) b.title = desc;
    b.addEventListener("click", () => {
      if (!ev.multi) return send(o);
      picked.has(o) ? picked.delete(o) : picked.add(o);
      b.classList.toggle("primary", picked.has(o));
    });
    acts.append(b);
  });
  const other = el("input", "choice-other");
  other.placeholder = _("Write another answer, Enter");
  other.addEventListener("keydown", (e) => { if (e.key === "Enter" && other.value.trim()) send(other.value.trim()); });
  acts.append(other);
  if (ev.multi) {
    const ok = el("button", "btn primary", _("Send"));
    ok.addEventListener("click", () => send([...picked].join(", ") || other.value.trim()));
    acts.append(ok);
  }
  card.append(q, acts);
  card._id = ev.id;
  card._acts = acts;
  card._choice = true;
  S.perms.set(ev.id, card);
  add(card, true);
}
function onChoiceDone(ev) {
  const card = S.perms.get(ev.id);
  if (!card) return;
  card._acts.replaceWith(el("div", "perm-done " + (ev.answer ? "yes" : "no"), ev.answer ? _("Answer: {answer}", { answer: ev.answer }) : _("Cancelled")));
  card.classList.add("answered");
}
function pendingPerm() {
  for (const c of S.perms.values()) if (!c.classList.contains("answered")) return c;
  return null;
}

/* ==================================================================== durum */
const MODES = {
  plan: [_("Plan"), _("Only reads and researches, writes a plan without changing anything"), IC.map],
  ask: [_("Ask"), _("Asks before anything other than reading"), IC.hand],
  auto: [_("Automatic"), _("Asks only for dangerous actions"), IC.shield],
  yolo: [_("Full access"), _("Never asks"), IC.unlock],
};
function shortModel(ref) {
  if (!ref) return _("no model");
  const m = ref.split(":").slice(1).join(":");
  return (m.split("/").pop() || ref).replace(/:free$/, "");
}
// Menüde ve düğmede görünen ad: claude-opus-5-5 -> Opus 5.5, gpt-6.1-terra -> GPT-6.1 Terra, kimi-k3.1-pro -> Kimi K3.1 Pro
function prettyModel(ref) {
  const id = shortModel(ref && ref.includes(":") ? ref : "x:" + (ref || ""));
  if (!ref || !/^[a-z]/.test(id)) return id;
  const parts = [];
  for (const w of id.split("-")) {
    const last = parts[parts.length - 1];
    if (/^\d{1,2}$/.test(w) && last && /^\d{1,2}$/.test(last)) parts[parts.length - 1] = last + "." + w;
    else parts.push(w);
  }
  if (parts[0] === "claude" && parts.length > 1) parts.shift();
  const up = { gpt: "GPT", glm: "GLM", oss: "OSS", vl: "VL" };
  const named = parts.map((w) => up[w] || w[0].toUpperCase() + w.slice(1));
  return named[0] === "GPT" && named.length > 1 ? "GPT-" + named.slice(1).join(" ") : named.join(" ");
}
// Kopyala satırı sadece bir turun son cevabında görünsün (Claude'daki gibi).
function markFinals() {
  const kids = Array.from(list.children);
  kids.forEach((n, i) => {
    if (!n.classList.contains("turn-ai")) return;
    let final = true;
    for (let j = i + 1; j < kids.length; j++) {
      const k = kids[j];
      if (k.classList.contains("turn-user")) break;
      if (k.classList.contains("turn-ai") || k.classList.contains("tool") || k.classList.contains("todo") ||
          k.classList.contains("tool-group") || k.classList.contains("steps") ||
          k.classList.contains("perm")) { final = false; break; }
    }
    n.classList.toggle("final", final && !(S.busy && i === kids.length - 1 - (S.working ? 1 : 0)));
  });
}

function setBusy(v) {
  if (v && !S.busy) {
    S.turnStart = performance.now();
    S.turn = { tools: 0, errs: 0, files: new Map() };
  }
  const ended = S.busy && !v;
  S.busy = !!v;
  app.classList.toggle("busy", S.busy);
  document.title = S.busy ? "● ALICE" : "ALICE";
  $("#sendBtn").title = S.busy ? _("Stop (Esc)") : _("Send (Enter)");
  if (S.busy && !S.cur && !S.working) showWorking(_("Working"));
  if (!S.busy) { hideWorking(); markFinals(); flushQueue(); setTimeout(collapseSteps, 0); }
  if (ended) { turnSummary(); refreshChanges(); }
  setTodo(S.todo);
  updateSend();
}
// İş bitince tek satırlık özet: süre, araç sayısı, değişen dosyalar.
function turnSummary() {
  const t = S.turn;
  S.turn = null;
  if (!t) return;
  const secs = (performance.now() - S.turnStart) / 1000;
  if (!t.tools && secs < 4) return;
  const row = el("div", "turn-sum" + (t.errs ? " has-err" : ""));
  row.appendChild(svg(t.errs ? IC.x : IC.check));
  const parts = [fmtDur(secs)];
  if (t.tools) parts.push(_("{n} tools", { n: t.tools }));
  if (t.errs) parts.push(_("{n} errors", { n: t.errs }));
  row.appendChild(el("span", "", parts.join(" · ")));
  if (t.files.size) {
    let a = 0, d = 0;
    for (const f of t.files.values()) { a += f.a; d += f.d; }
    const ch = el("button", "ts-files");
    ch.append(svg(IC.diff), document.createTextNode(_("{n} files", { n: t.files.size }) + " "), el("span", "a", `+${a}`),
      document.createTextNode(" "), el("span", "d", `−${d}`));
    ch.title = Array.from(t.files.keys()).join("\n");
    ch.addEventListener("click", () => openPanel("changes"));
    row.appendChild(ch);
  }
  add(row);
}
/* ================================================================ güncelleme */
// Sunucu açılışta ve saatte bir GitHub'daki dala bakar; yeni commit varsa bu çubuk çıkar.
// "Sonra" denen sürüm hatırlanır: durum her yenilendiğinde çubuk geri geliyordu. Yeni bir commit gelince yine çıkar.
const updateKey = (u) => (u.available ? "r:" + u.remote : "s:" + u.local);
function updateDismissed(u) {
  try { return localStorage.getItem("alice.updateLater") === updateKey(u); } catch (e) { return false; }
}
function renderUpdate(u, manual) {
  const bar = $("#update");
  S.update = u && (u.available || u.stale) ? u : null;
  if (!S.update) { bar.hidden = true; return; }
  // The bar does not pop up by itself when the repo is on another branch (development), when the code only moved on
  // disk through a manual `git pull` (while developing that is every merge, and the bar kept nagging), or when the
  // user said "Sonra"; the dot next to the version stays and "Güncellemeleri denetle" still shows it. An update that
  // ALICE pulled itself is not hand_pulled, so its _("Restart") bar still appears for everyone.
  const handPulled = !u.available && u.stale && u.hand_pulled;
  const quiet = !manual && bar.hidden && ((u.available && u.off_branch) || handPulled || updateDismissed(u));
  const v0 = $("#ver");
  if (v0 && !v0.querySelector(".up-dot")) v0.appendChild(el("i", "up-dot"));
  if (quiet) return;
  const n = u.count || 0;
  $("#upText").textContent = !u.available
    ? _("New version downloaded · it opens after a restart")
    : (n ? _("A new version is ready · {n} changes", { n }) : _("A new version is ready")) + (u.blocked ? ` — ${u.blocked}` : "");
  $("#upText").title = (u.commits || []).join("\n") || `${u.local} → ${u.remote}`;
  const btn = $("#upApply");
  btn.disabled = false;
  btn.hidden = false;
  btn.textContent = !u.available ? _("Restart") : u.blocked ? _("Update anyway") : _("Update and restart");
  $("#upLater").hidden = false;
  bar.hidden = false;
}
async function applyUpdate() {
  const btn = $("#upApply");
  const forced = !!(S.update && S.update.blocked);
  btn.disabled = true;
  btn.textContent = _("Updating…");
  const r = await api("/api/update/apply", { force: forced });
  if (!r || !r.ok) {
    btn.disabled = false;
    btn.textContent = forced ? _("Update anyway") : _("Update and restart");
    return toast((r && (r.error || r.note)) || _("Could not update"));
  }
  btn.hidden = true;
  $("#upLater").hidden = true;
  $("#upText").textContent = r.note + _(" — ALICE is restarting, a new window opens shortly.");
}
async function checkUpdate() {
  toast(_("Checking for updates…"));
  const u = await api("/api/update/check", {});
  if (!u) return toast(_("Could not check"));
  if (!u.available && !u.stale) return toast(u.error || _("ALICE is up to date"));
  renderUpdate(u, true);
}

function applyStatus(s) {
  Object.assign(S.status, s);
  const st = S.status;
  // Oturum süresi sohbetin ilk açılışından sayılır (sayfa yenilenince ya da uygulama yeniden açılınca sıfırlanmasın).
  if (s.started) S.started = s.started * 1000;
  if ("team" in s) renderTeam(s.team);
  if (st.cwd !== undefined) {
    // Üst çubukta sadece klasörün adı; tam yol üzerine gelince.
    $("#cwd").textContent = st.cwd.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || st.cwd;
    $("#cwdBtn").title = st.cwd + "\n" + _("Click to change the folder");
  }
  if (st.version) $("#ver").textContent = "ALICE " + st.version;
  if ("update" in s) renderUpdate(s.update);
  if (st.user) { $("#userName").textContent = st.user; $("#avatar").textContent = st.user[0].toUpperCase(); }
  if (st.mode) {
    const [label, , icon] = MODES[st.mode] || [st.mode, "", IC.shield];
    $("#modeLabel").textContent = label;
    $("#modeIcon").innerHTML = icon;
    $("#modeBtn").className = "c-btn pill m-" + st.mode;
  }
  $("#modelLabel").textContent = st.model ? prettyModel(st.model) : shortModel(st.model);
  if (st.thinking) {
    // Seçili modelin gerçekten uyguladığı seviye (model desteklemiyorsa en yakını); destek yoksa düğme sönük.
    const merged = { ...S.status, ...st };
    const lab = thinkLabel(merged);
    if (lab) {
      const ultra = !!merged.ultracode;
      $("#thinkLabel").textContent = (merged.thinking_kind === "none" ? (ultra ? "" : _("no thinking setting")) : lab[0])
        + (ultra ? (merged.thinking_kind === "none" ? "" : " · ") + "Ultracode" : "");
      $("#thinkIcon").innerHTML = (merged.thinking_effective || st.thinking) === "off" ? IC.bolt : IC.brain;
      $("#thinkBtn").title = merged.thinking_kind === "none" ? _("This model has no thinking setting")
        : _("Thinking: {level}", { level: lab[1] });
      $("#thinkBtn").classList.toggle("dim", merged.thinking_kind === "none" && !ultra);
      $("#thinkBtn").classList.toggle("ultra", ultra);
    }
  }
  // Sabitlenen model cevap veremeyip yedeğe geçildiyse düğme seçileni, çubuk gerçekten cevap vereni gösterir.
  const served = st.served_model || st.model;
  const fellBack = st.brain_pinned && st.served_model && st.served_model !== st.model;
  $("#modelBtn").title = fellBack ? _("Chosen: {model} · last answer from: {served} (the chosen model could not answer)", { model: st.model, served: st.served_model }) : st.model || "";
  $("#stModel").textContent = served || _("no model");
  $("#stModel").classList.toggle("warn", !!fellBack);
  if (st.stats) renderStats(st.stats, st);
  if (st.ctx_limit) {
    const win = st.ctx_window || st.ctx_limit;
    const pct = Math.min(100, (100 * st.ctx_tokens) / win);
    const m = $("#stCtx .meter");
    m.firstElementChild.style.width = pct + "%";
    m.classList.toggle("hot", pct > 75);
    $("#stCtx span").textContent = `${fmtTok(st.ctx_tokens)} / ${fmtTok(win)}`;
  }
  if (st.today_requests !== undefined) $("#stReq").textContent = _("{n} requests today", { n: st.today_requests });
  if (st.decider) $("#stDec").textContent = _("decider: {name}", { name: st.decider });
  if (st.branch !== undefined) {
    $("#branch").textContent = st.branch;
    $("#branchChip").hidden = !st.branch;
    $("#stBranch").textContent = st.branch ? "⎇ " + st.branch : "";
    $$(".st-br").forEach((n) => { n.hidden = !st.branch; });
  }
  if (PREFS.panel && PREFS.tab === "context") renderPanel();
  // Sadece gelen durumda busy varsa uygula: kısmi güncelleme (ör. model değişimi) eski busy değeriyle işi bitmiş saymasın.
  if (s.busy !== undefined && s.busy !== S.busy) setBusy(s.busy);
  // Arkadaki sohbetin sorusu öndekine çizilmez (olayı da çizilmiyor; cevabı gelince kart açık kalıyordu).
  for (const p of st.pending || []) {
    if (!p.sid || !st.session || p.sid === st.session) (p.type === "ask_choice" ? onChoice : onPermission)(p);
  }
  markSessions();
  onWorkflowSession();
  drawChatTitle();
  if (app.classList.contains("empty")) greet();
}
async function refreshStatus() { const s = await api("/api/state"); if (s && s.version) applyStatus(s); }
$("#upApply").addEventListener("click", applyUpdate);
$("#upLater").addEventListener("click", () => {
  $("#update").hidden = true;
  try { if (S.update) localStorage.setItem("alice.updateLater", updateKey(S.update)); } catch (e) { /* depolama kapalı */ }
});

/* ============================================================ geçmiş/oturum */
function renderHistory(items) {
  clearChat();
  for (const it of items || []) {
    if (it.type === "user") { addUser(it.text, it.mi); remember(it.text); }
    else if (it.type === "assistant") addAiStatic(it.text);
    else if (it.type === "info") addNote(it.text);
    else if (it.type === "tool") finishTool(toolRow(null, it.name, it.summary, false), { is_error: it.is_error, text: it.text,
      files: it.files, card: it.card });
  }
  if (!items || !items.length) setEmpty(true);
  markFinals();
  setTimeout(collapseSteps, 0);  // özetler (setTimeout ile) yazıldıktan sonra
  toBottom(true);
}
async function loadHistory() {
  loadWorkflows();  // at the start and after a reconnect: events may have been missed
  renderHistory(await api("/api/history"));
}

// Kenar çubuğu: aramayla süzülen, tarihe göre gruplanmış sohbetler.
async function loadSessions() {
  const rows = await api("/api/sessions");
  S.sessions = Array.isArray(rows) ? rows : [];
  drawSessions();
}
function relTime(ts) {
  if (!ts) return "";
  const s = Date.now() / 1000 - ts;
  if (s < 60) return _("now");
  if (s < 3600) return _("{n} min", { n: Math.floor(s / 60) });
  if (s < 86400) return _("{n} h", { n: Math.floor(s / 3600) });
  if (s < 7 * 86400) return _("{n} d", { n: Math.floor(s / 86400) });
  return new Date(ts * 1000).toLocaleDateString(ALICE_LOCALE, { day: "numeric", month: "short" });
}
function dayGroup(ts) {
  if (!ts) return _("Older");
  const d = new Date(ts * 1000), now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
  if (ts >= start) return _("Today");
  if (ts >= start - 86400) return _("Yesterday");
  if (ts >= start - 6 * 86400) return _("This week");
  if (d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth()) return _("This month");
  return _("Older");
}
function drawSessions() {
  const nav = $("#sessions");
  nav.replaceChildren();
  const q = ($("#sessSearch").value || "").trim().toLocaleLowerCase(ALICE_LOCALE);
  const rows = (S.sessions || []).filter((s) => !q || (s.title + " " + s.cwd).toLocaleLowerCase(ALICE_LOCALE).includes(q));
  if (!rows.length) {
    nav.appendChild(el("div", "side-empty", q ? _("No matching chats") : _("No chats yet")));
    drawChatTitle();
    return;
  }
  // Sabitlenenler en üstte, gerisi güne göre.
  const ordered = [...rows.filter((s) => s.pinned), ...rows.filter((s) => !s.pinned)];
  let group = null;
  for (const s of ordered) {
    const g = s.pinned ? _("Pinned") : dayGroup(s.updated);
    if (g !== group) { group = g; nav.appendChild(el("div", "side-label", g)); }
    const b = el("div", "sess" + (s.pinned ? " pinned" : ""));
    b.dataset.id = s.id;
    b.tabIndex = 0;
    b.title = `${s.title}\n${s.cwd}`;
    b.appendChild(el("span", "st", s.title || _("(empty)")));
    b.appendChild(el("small", "when", relTime(s.updated)));
    const more = el("button", "more");
    more.title = _("More");
    more.setAttribute("aria-label", _("Chat actions"));
    more.appendChild(svg(IC.more));
    more.addEventListener("click", (e) => { e.stopPropagation(); sessionMenu(more, s); });
    b.appendChild(more);
    b.addEventListener("click", () => resume(s.id));
    b.addEventListener("keydown", (e) => { if (e.key === "Enter") resume(s.id); if (e.key === "F2") renameSession(s); });
    b.addEventListener("dblclick", (e) => { e.preventDefault(); renameSession(s); });
    nav.appendChild(b);
  }
  markSessions();
  drawChatTitle();
}
function sessionMenu(anchor, s) {
  openMenu(anchor, (m) => {
    menuItem(m, IC.edit, _("Rename"), _("F2 or double-click"), () => renameSession(s));
    menuItem(m, IC.pin, s.pinned ? _("Unpin") : _("Pin"), s.pinned ? "" : _("Stays at the top of the list"), async () => {
      await api("/api/session/pin", { id: s.id, pinned: !s.pinned });
      loadSessions();
    });
    menuItem(m, IC.trash, _("Delete"), _("Permanently from history"), async () => {
      if (!(await confirmDialog(_("Delete the chat?"), _("\"{title}\" will be deleted from history permanently.", { title: s.title || _("(empty)") }), _("Delete")))) return;
      const r = await api("/api/session/delete", { id: s.id });
      if (r && r.ok === false) return toast(r.error || _("Could not delete"));
      if (s.id === S.status.session) newChat();
      loadSessions();
    });
  }, "right", true);
}
// Yerinde yeniden adlandırma: satırdaki ya da üst çubuktaki başlık yazı kutusuna döner. Enter kaydeder, Esc vazgeçer;
// boş bırakmak otomatik başlığa döndürür.
function renameSession(s, host) {
  const span = host || document.querySelector(`.sess[data-id="${CSS.escape(s.id)}"] .st`);
  if (!span) return;
  const inp = el("input", "rename-in");
  inp.type = "text";
  inp.value = s.title || "";
  inp.placeholder = s.auto_title || _("Chat name");
  const done = async (save) => {
    if (inp._done) return;
    inp._done = true;
    const value = inp.value.trim();
    inp.replaceWith(span);
    if (save && value !== (s.title || "")) {
      span.textContent = value || s.auto_title || s.title;
      await api("/api/session/rename", { id: s.id, title: value });
      await loadSessions();
    }
  };
  inp.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") done(true);
    if (e.key === "Escape") done(false);
  });
  inp.addEventListener("click", (e) => e.stopPropagation());
  inp.addEventListener("blur", () => done(true));
  span.replaceWith(inp);
  inp.focus(); inp.select();
}
// Üst çubuk: açık sohbetin adı (tıklayınca yeniden adlandırılır).
function drawChatTitle() {
  const t = $("#chatTitle");
  if (!t) return;
  const s = (S.sessions || []).find((x) => x.id === S.status.session);
  t.textContent = s ? (s.title || _("(empty)")) : _("New chat");
  t.classList.toggle("new", !s);
  t.title = s ? _("Click to rename") : "";
  t.disabled = !s;
}
// Öndeki sohbet ve arkada hâlâ çalışanlar (bir sohbet çalışırken başka sohbete geçilebilir).
function markSessions() {
  const running = new Set(S.status.running || []);
  $$(".sess").forEach((b) => {
    b.classList.toggle("active", b.dataset.id === S.status.session);
    b.classList.toggle("running", running.has(b.dataset.id));
    if (running.has(b.dataset.id)) b.title = b.title.replace(/^(⏳ [^\n]*\n)?/, "⏳ " + _("running in the background") + "\n");
  });
}
// Sohbet değişirken: öndekinin bekleyen mesajları yenisine gitmesin, turunun özeti yenisine yazılmasın.
// Meşgul durumu sessizce bırakılır; yeni sohbetin durumu applyStatus ile gelir.
function leaveChat() {
  S.queue = [];
  if (typeof drawQueue === "function") drawQueue();
  S.busy = false;
  setBusy(false);
}
$("#sessSearch").addEventListener("input", drawSessions);
$("#sessSearch").addEventListener("keydown", (e) => { if (e.key === "Escape") { e.target.value = ""; drawSessions(); e.target.blur(); } });
async function resume(id) {
  if (app.dataset.sec !== "chat") markSection("chat");
  // Proje ekranı açıkken sohbete tıklamak önce projeyi kapatır; aynı sohbetse sadece ona dönülür.
  if (S.proj) { closeProject(); drawSessions(); if (id === S.status.session) { closeSideOnMobile(); return; } }
  if (id === S.status.session) { closeSideOnMobile(); return; }
  // Çalışan sohbet durmaz: arkada sürer, kenar çubuğunda işaretlenir.
  const r = await api("/api/resume", { id });
  if (!r.ok) return toast(r.error || _("Could not load the session"));
  leaveChat();
  renderHistory(r.history);
  applyStatus(r);
  refreshChanges();
  closeSideOnMobile();
}
async function newChat() {
  if (S.proj) closeProject();
  markSection("chat");
  const r = await api("/api/new", {});
  if (!r.ok) return toast(r.error || _("Could not open a new chat"));
  leaveChat();
  clearChat(); applyStatus(r); loadSessions(); refreshChanges(); input.focus();
}
async function rewindTo(mi, edit) {
  if (S.busy) return toast(_("Stop the current job first"));
  const ok = await confirmDialog(edit ? _("Edit the message") : _("Rewind to here"),
    _("The conversation goes back to before this message. Changes ALICE made with the file tools after it are undone (changes made with commands can't be undone)."),
    edit ? _("Edit") : _("Rewind"));
  if (!ok) return;
  const r = await api("/api/rewind", { mi });
  if (!r.ok) return toast(r.error || _("Could not rewind"));
  renderHistory(r.history);
  applyStatus(r);
  refreshChanges();
  toast(r.restored && r.restored.length ? _("Rewound, {n} files restored", { n: r.restored.length }) : _("Rewound"));
  if (edit) { input.value = r.text; autosize(); input.focus(); }
}

// Mesajı yerinde düzenle: balon yazı kutusuna döner; Gönder konuşmayı o mesajın öncesine sarar ve yeni metni yollar.
function editInline(wrap, text, mi) {
  if (wrap.querySelector(".edit-box")) return;
  const box = el("div", "edit-box");
  const ta = el("textarea");
  ta.value = text;
  ta.rows = 1;
  const fit = () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 320) + "px"; };
  const row = el("div", "edit-row");
  row.appendChild(el("small", "", _("Everything after it is deleted and ALICE's file changes are undone")));
  const cancel = el("button", "btn", _("Cancel"));
  const send = el("button", "btn primary", _("Send"));
  row.append(cancel, send);
  box.append(ta, row);
  wrap.classList.add("editing");
  wrap.appendChild(box);
  const close = () => { box.remove(); wrap.classList.remove("editing"); };
  const go = async () => {
    const t = ta.value.trim();
    if (!t) return;
    send.disabled = cancel.disabled = true;
    if (!(await resendFrom(mi, t))) { send.disabled = cancel.disabled = false; return; }
  };
  cancel.addEventListener("click", close);
  send.addEventListener("click", go);
  ta.addEventListener("input", fit);
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); close(); }
    else if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); go(); }
  });
  fit();
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
}
// ALICE çalışıyorsa önce durdurur (bitmesini en fazla ~10 sn bekler), sonra geri sarıp yeni metni gönderir.
async function resendFrom(mi, text) {
  if (S.busy) {
    stop();
    for (let i = 0; i < 50 && S.busy; i++) { await new Promise((r) => setTimeout(r, 200)); await refreshStatus(); }
    if (S.busy) { toast(_("ALICE hasn't stopped yet, try again in a moment")); return false; }
  }
  const r = await api("/api/rewind", { mi });
  if (!r.ok) { toast(r.error || _("Could not edit")); return false; }
  renderHistory(r.history);
  applyStatus(r);
  refreshChanges();
  if (r.restored && r.restored.length) toast(`${r.restored.length} dosya eski haline getirildi`);
  await submit(text);
  return true;
}

/* ============================================================ durum çubuğu panelleri */
// Oturum istatistiği, token kullanımı ve bağlam dağılımı: çubukta kısa özet, üzerine gelince ayrıntı paneli.
function fmtDurShort(s) {
  s = Math.round(s || 0);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60), r = s % 60;
  return m < 60 ? `${m}m${String(r).padStart(2, "0")}s` : `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}m`;
}
function popRows(pop, title, total, rows, extra) {
  pop.replaceChildren();
  const head = el("div", "sp-head");
  head.append(el("b", "", title), el("span", "", total));
  pop.appendChild(head);
  if (extra) pop.appendChild(extra);
  for (const [label, value, dot] of rows) {
    const r = el("div", "sp-row");
    const l = el("span", "sp-l");
    if (dot) { const d = el("i", "sp-dot"); d.style.background = dot; l.appendChild(d); }
    l.appendChild(document.createTextNode(label));
    r.append(l, el("span", "sp-v", value));
    pop.appendChild(r);
  }
}
function renderStats(s, st) {
  const nf = (n) => Number(n || 0).toLocaleString(ALICE_LOCALE);
  const stats = $("#stStats"), tok = $("#stTok"), ctx = $("#stCtx");
  stats.hidden = !s.turns;
  if (s.turns) {
    stats.querySelector("span").textContent = _("{turns} turns {steps} steps", { turns: s.turns, steps: s.steps }) + (s.tps ? ` · ${s.tps} tok/s` : "");
    popRows(stats.querySelector(".st-pop"), _("Session stats"), _("{turns} turns {steps} steps", { turns: s.turns, steps: s.steps }), [
      [_("Model time"), fmtDurShort(s.llm_s)], [_("Tool time"), fmtDurShort(s.tool_s)],
      [_("Avg. time to first token (TTFT)"), s.ttft ? `${s.ttft.toFixed(1)}s` : "—"],
      [_("Tokens per second (TPS)"), s.tps ? `${s.tps} tok/s` : "—"]]);
  }
  const total = (s.input || 0) + (s.output || 0);
  tok.hidden = !total;
  if (total) {
    const hit = s.input ? Math.round(100 * (s.cached || 0) / s.input) : 0;
    tok.querySelector("span").textContent = `${fmtTok(total)} tok` + (s.cached ? _(" · cache {pct}%", { pct: hit }) : "");
    const more = el("button", "act sp-more", _("Breakdown: where the tokens went →"));
    more.addEventListener("click", (e) => { e.stopPropagation(); showTokenUsage(); });
    popRows(tok.querySelector(".st-pop"), _("Token usage"), `${nf(total)} tok`, [
      [_("From cache (cache hit)"), s.cached ? `%${hit}` : "—"], [_("Uncached input"), nf((s.input || 0) - (s.cached || 0))],
      [_("Input from cache"), nf(s.cached)], [_("Written to cache"), s.written ? nf(s.written) : "—"],
      [_("Output"), nf(s.output)]]);
    tok.querySelector(".st-pop").appendChild(more);
  }
  const win = st.ctx_window || st.ctx_limit;
  if (win && s.ctx) {
    const pct = Math.min(100, Math.round(100 * (st.ctx_tokens || s.ctx.total) / win));
    const bar = el("div", "sp-bar");
    for (const [v, c] of [[s.ctx.system, "var(--faint)"], [s.ctx.tools, "#a78bfa"], [s.ctx.messages, "var(--accent)"]]) {
      const i = el("i");
      i.style.width = Math.max(0, 100 * v / win) + "%";
      i.style.background = c;
      bar.appendChild(i);
    }
    const note = el("small", "sp-note", _("At {n} tokens the older conversation is summarized automatically.", { n: fmtTok(st.ctx_limit) }));
    const box = el("div");
    box.append(bar, note);
    popRows(ctx.querySelector(".st-pop"), _("Context {pct}% full", { pct }), `~${fmtTok(st.ctx_tokens || s.ctx.total)} / ${fmtTok(win)}`, [
      [_("System prompt"), "~" + fmtTok(s.ctx.system), "var(--faint)"], [_("Tool definitions"), "~" + fmtTok(s.ctx.tools), "#a78bfa"],
      [_("Messages"), "~" + fmtTok(s.ctx.messages), "var(--accent)"]], box);
  }
}

/* Kullanım dökümü: bu oturumda tokenlar hangi modele, bağlam hangi araç çıktılarına gitti. Durum çubuğundaki token
   balonundan ya da usage aracının kartından açılır. */
function usageBars(rows, label, value, sub, color) {
  const box = el("div", "us-list");
  const max = Math.max(1, ...rows.map(value));
  for (const r of rows) {
    const row = el("div", "us-row");
    const head = el("div", "us-head");
    head.append(el("span", "us-name", label(r)), el("span", "us-v", fmtTok(value(r))));
    const bar = el("div", "us-bar");
    const fill = el("i");
    fill.style.width = (100 * value(r) / max) + "%";
    fill.style.background = color;
    bar.appendChild(fill);
    row.append(head, bar, el("small", "", sub(r)));
    box.appendChild(row);
  }
  return box;
}
function renderUsage(u) {
  const box = el("div", "usage");
  const nf = (n) => Number(n || 0).toLocaleString(ALICE_LOCALE);
  const hit = u.input ? Math.round(100 * (u.cached || 0) / u.input) : 0;
  box.appendChild(el("div", "us-sum", _("{turns} turns · {steps} model calls · input {input} (cache {pct}%) · output {output}",
    { turns: u.turns, steps: u.steps, input: fmtTok(u.input), pct: hit, output: fmtTok(u.output) })));
  box.appendChild(el("div", "sec", _("Models (input + output)")));
  if (u.models && u.models.length) {
    box.appendChild(usageBars(u.models, (m) => m.model, (m) => m.input + m.output,
      (m) => _("{calls} calls · input {input} (from cache {cached}{written}) · output {output} · {time}", {
        calls: m.calls, input: nf(m.input), cached: nf(m.cached), output: nf(m.output), time: fmtDurShort(m.seconds),
        written: m.written ? _(", written {n}", { n: nf(m.written) }) : "" }),
      "var(--accent)"));
  } else box.appendChild(el("div", "note", _("No model calls yet.")));
  box.appendChild(el("div", "sec", _("Tool output (added to the context)")));
  if (u.tools && u.tools.length) {
    box.appendChild(usageBars(u.tools.slice(0, 12), (t) => t.tool, (t) => t.tokens,
      (t) => _("{n} times", { n: t.calls }) + (t.errors ? " · " + _("{n} errors", { n: t.errors }) : ""), "#a78bfa"));
  } else box.appendChild(el("div", "note", _("No tools used.")));
  const c = u.ctx || {};
  box.appendChild(el("div", "sec", _("Context now ~{n}", { n: fmtTok(c.total || 0) })));
  box.appendChild(usageBars([{ n: _("System prompt"), v: c.system || 0 }, { n: _("Tool definitions"), v: c.tools || 0 },
    { n: _("Messages"), v: c.messages || 0 }], (r) => r.n, (r) => r.v, () => "", "var(--faint)"));
  const tip = [];
  if (u.input && hit < 30 && u.steps > 4) tip.push(_("Low cache rate: if the model switches often, the context is paid for again on every switch."));
  const big = (u.tools || [])[0];
  if (big && c.total && big.tokens > c.total * 0.3) tip.push(_("{tool} output fills most of the context; narrower queries or /compact help.", { tool: big.tool }));
  if (u.steps && c.tools && c.total && c.tools > c.total * 0.35) tip.push(_("Tool definitions are over a third of the context: turning off unneeded connections frees room."));
  for (const t of tip) box.appendChild(el("div", "us-tip", t));
  return box;
}
async function showTokenUsage() {
  const u = await api("/api/usage");
  if (!u || u.ok === false) return toast((u && u.error) || _("could not get the stats"));
  showDialog((d) => {
    d.classList.add("usage-dialog");
    d.append(el("h3", "", _("Usage breakdown")), renderUsage(u));
    const row = el("div", "row");
    const ok = el("button", "btn primary", _("Close"));
    ok.addEventListener("click", closeDialog);
    row.appendChild(ok);
    d.appendChild(row);
  });
}
