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
    const title = el("span", "cb-lang", lang || "kod");
    if (nLines > 1) title.appendChild(el("small", "", `${nLines} satır`));
    head.appendChild(title);
    const cp = el("button", "act");
    cp.dataset.copy = "code";
    cp.appendChild(svg(IC.copy));
    cp.appendChild(document.createTextNode("Kopyala"));
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
    btn.replaceChildren(svg(IC.check), document.createTextNode("Kopyalandı"));
    setTimeout(() => { btn.innerHTML = old; }, 1400);
  } else toast("Kopyalandı");
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
  S.tools.clear(); S.perms.clear(); S.cur = null;
  setTodo("");
  setEmpty(true);
  greet();
}
function greet() {
  const h = new Date().getHours();
  const part = h < 5 ? "İyi geceler" : h < 12 ? "Günaydın" : h < 18 ? "İyi günler" : h < 23 ? "İyi akşamlar" : "İyi geceler";
  const name = S.status.user ? `, ${S.status.user}` : "";
  $("#greeting").textContent = part + name;
}

/* ================================================================== mesajlar */
function addUser(text, mi, images) {
  const wrap = el("div", "turn-user");
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
  const cp = el("button", "act"); cp.title = "Kopyala"; cp.appendChild(svg(IC.copy));
  cp.addEventListener("click", () => copyText(text));
  acts.appendChild(cp);
  if (mi !== undefined && mi !== null) {
    const ed = el("button", "act"); ed.title = "Mesajı düzenle ve buradan yeniden gönder (çift tıklama da olur)";
    ed.append(svg(IC.edit), document.createTextNode("Düzenle"));
    ed.addEventListener("click", () => editInline(wrap, text, mi));
    b.addEventListener("dblclick", () => editInline(wrap, text, mi));
    const rw = el("button", "act"); rw.title = "Bu mesajdan öncesine geri sar (dosyalar dahil)";
    rw.append(svg(IC.rewind), document.createTextNode("Geri sar"));
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
function fmtSecs(s) { return s < 60 ? `${s} sn` : `${Math.floor(s / 60)} dk ${s % 60} sn`; }

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
  const cp = el("button", "act"); cp.append(svg(IC.copy), document.createTextNode("Kopyala"));
  cp.addEventListener("click", () => copyText(text, cp));
  acts.appendChild(cp);
  if (source) acts.appendChild(el("span", "source", source));
  root.appendChild(acts);
}

function onThinking(t) {
  const c = ensureAi();
  if (!c.thought) {
    const d = el("details", "thought");
    const sum = el("summary");
    sum.append(svg(IC.chev), el("span", "shimmer", "Düşünüyor"));
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
  c.thought.sum.lastChild.textContent = `${secs} sn düşündü`;
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
  read_file: ["Okundu", IC.file], write_file: ["Yazıldı", IC.pen], edit_file: ["Düzenlendi", IC.pen],
  multi_edit: ["Düzenlendi", IC.pen],
  list_dir: ["Klasör", IC.folder], glob: ["Dosya arandı", IC.search], grep: ["Arandı", IC.search],
  run_command: ["Komut", IC.term], process_output: ["Süreç çıktısı", IC.term], process_stop: ["Süreç durduruldu", IC.term],
  web_fetch: ["Sayfa okundu", IC.globe], web_search: ["Web'de arandı", IC.globe], browser: ["Tarayıcı", IC.globe],
  computer: ["Ekran", IC.screen], windows: ["Pencere", IC.screen], ui_elements: ["Arayüz tarandı", IC.cursor],
  ui_act: ["Arayüz", IC.cursor], clipboard: ["Pano", IC.copy], system_info: ["Sistem bilgisi", IC.screen],
  process_list: ["Süreçler", IC.list], process_kill: ["Süreç sonlandırıldı", IC.x], todo_write: ["Plan", IC.list],
  memory: ["Hafıza", IC.brain], task: ["Alt ajan", IC.spark], reflex: ["Reflex", IC.bolt],
};
Object.assign(VERBS, {
  assign: ["Ajan kuruldu", IC.spark], plan: ["Plan sunuldu", IC.list], review: ["İnceleme kararı", IC.check],
  message: ["Mesaj", IC.chat], team_send: ["Mesaj", IC.chat], team_wait: ["Cevap beklendi", IC.chat],
  diff: ["Farka bakıldı", IC.diff], council: ["Kurula soruldu", IC.shield], apply_patch: ["Yama", IC.pen],
  file_ops: ["Dosya işlemi", IC.folder], git: ["Git", IC.branch], http_request: ["İstek", IC.globe],
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
  file: (n) => `${n} dosya okundu`, dir: (n) => `${n} klasör listelendi`, search: (n) => `${n} arama yapıldı`,
  web: (n) => `${n} web araması`, page: (n) => `${n} sayfa okundu`, sys: (n) => `${n} sistem sorgusu`,
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
  if (GROUP_KIND[name]) {
    const last = lastContent();
    let g = null;
    if (last && last.classList.contains("tool-group")) g = last;
    else if (last && last.classList.contains("tool") && GROUP_KIND[last._name]) {
      g = makeGroup();
      list.insertBefore(g, last);
      g._body.appendChild(last);
    }
    if (g) {
      g._body.appendChild(t);
      updateGroup(g);
      toBottom();
      return t;
    }
  }
  return add(t);
}
// 1.8k, 840k, 1M gibi kısa token yazımı.
function fmtTok(n) {
  if (n >= 1e6) {
    const m = n / 1048576;
    return String(Math.abs(m - Math.round(m)) < 0.01 ? Math.round(m) : (n / 1e6).toFixed(1)).replace(".", ",") + "M";
  }
  if (n >= 1e4) return `${Math.round(n / 1000)}k`;
  return `${(n / 1000).toFixed(1).replace(".", ",")}k`;
}
function fmtDur(s) { return s < 10 ? `${s.toFixed(1).replace(".", ",")} sn` : fmtSecs(Math.round(s)); }

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
  head.append(svg(IC.list), el("span", "", "Görevler"), el("span", "todo-n", `${done}/${items.length}`));
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

// Komut çıktısı terminal görünümünde: komut, çıkış kodu, çıktı.
function renderTerm(cmd, text, isErr) {
  const m = (text || "").match(/^çıkış kodu (-?\d+)\n?/);
  const code = m ? +m[1] : null;
  const out = m ? text.slice(m[0].length) : text || "";
  const box = el("div", "term");
  const head = el("div", "term-head");
  const c = el("span", "term-cmd");
  c.append(el("span", "pr", "$"), el("span", "", cmd));
  head.appendChild(c);
  if (code !== null) head.appendChild(el("span", "exit " + (code === 0 ? "ok" : "bad"), `çıkış ${code}`));
  else if (isErr) head.appendChild(el("span", "exit bad", "hata"));
  const pre = el("pre", "term-out", out.trim() || "(çıktı yok)");
  box.append(head, pre);
  return { box, code };
}

function peekText(t, ev) {
  let txt = ev.text || "";
  if (t._name === "run_command") txt = txt.replace(/^çıkış kodu -?\d+\n?/, "");
  const lines = txt.split("\n").map((l) => l.trimEnd()).filter((l) => l.trim());
  if (!lines.length) return "";
  const shown = lines.slice(0, 2).map((l) => (l.length > 140 ? l.slice(0, 137) + "…" : l));
  if (lines.length > 2) shown.push(`… +${lines.length - 2} satır`);
  return shown.join("\n");
}

function finishTool(t, ev, live) {
  clearInterval(t._timer);
  t.classList.remove("running");
  t._v.classList.remove("shimmer");
  t.classList.add(ev.is_error ? "err" : "ok");
  const group = t.parentElement && t.parentElement.closest(".tool-group");
  if (group) setTimeout(() => updateGroup(group), 0);
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
    t.replaceWith(renderTodo(ev.text || ""));
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
  } else if (t._name === "run_command" && !/^arka planda/.test(ev.text || "")) {
    const { box, code } = renderTerm(t._summary, ev.text, ev.is_error);
    body.appendChild(box);
    if (code !== null) t._meta.appendChild(el("span", "exit " + (code === 0 ? "ok" : "bad"), `çıkış ${code}`));
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
  const head = ev.risk === "danger" ? "Bu işlem geri alınamayabilir"
    : ev.risk === "send" ? "Bu mesaj başkasına gidecek" : "ALICE izin istiyor";
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
  acts.append(mk("İzin ver", "y", "primary", "1"), mk("Bu oturumda hep izin ver", "a", "", "2"));
  if (lasting) acts.append(mk("Bu projede hep izin ver", "p", "", "3"));
  acts.append(mk("Reddet", "n", "danger", lasting ? "4" : "3"));
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
  const txt = { y: "İzin verildi", a: "Bu oturumda bu araca hep izin verildi",
    p: "Bu projede kalıcı izin verildi (.alice/permissions.toml)", n: "Reddedildi" }[ev.answer] || ev.answer;
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
  qt.append(el("b", "", ev.question), el("small", "", ev.multi ? "Birden fazla seçebilirsin" : "ALICE soruyor"));
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
  other.placeholder = "Başka bir cevap yaz, Enter";
  other.addEventListener("keydown", (e) => { if (e.key === "Enter" && other.value.trim()) send(other.value.trim()); });
  acts.append(other);
  if (ev.multi) {
    const ok = el("button", "btn primary", "Gönder");
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
  card._acts.replaceWith(el("div", "perm-done " + (ev.answer ? "yes" : "no"), ev.answer ? "Cevap: " + ev.answer : "Vazgeçildi"));
  card.classList.add("answered");
}
function pendingPerm() {
  for (const c of S.perms.values()) if (!c.classList.contains("answered")) return c;
  return null;
}

/* ==================================================================== durum */
const MODES = {
  plan: ["Plan", "Sadece okur ve araştırır, değişiklik yapmadan plan yazar", IC.map],
  ask: ["Sor", "Okuma dışındaki her işlemde izin ister", IC.hand],
  auto: ["Otomatik", "Sadece tehlikeli işlemlerde izin ister", IC.shield],
  yolo: ["Tam erişim", "Hiçbir şey sormaz", IC.unlock],
};
function shortModel(ref) {
  if (!ref) return "model yok";
  const m = ref.split(":").slice(1).join(":");
  return (m.split("/").pop() || ref).replace(/:free$/, "");
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
          k.classList.contains("tool-group") ||
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
  $("#sendBtn").title = S.busy ? "Durdur (Esc)" : "Gönder (Enter)";
  if (S.busy && !S.cur && !S.working) showWorking("Çalışıyor");
  if (!S.busy) { hideWorking(); markFinals(); flushQueue(); }
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
  if (t.tools) parts.push(`${t.tools} araç`);
  if (t.errs) parts.push(`${t.errs} hata`);
  row.appendChild(el("span", "", parts.join(" · ")));
  if (t.files.size) {
    let a = 0, d = 0;
    for (const f of t.files.values()) { a += f.a; d += f.d; }
    const ch = el("button", "ts-files");
    ch.append(svg(IC.diff), document.createTextNode(`${t.files.size} dosya `), el("span", "a", `+${a}`),
      document.createTextNode(" "), el("span", "d", `−${d}`));
    ch.title = Array.from(t.files.keys()).join("\n");
    ch.addEventListener("click", () => openPanel("changes"));
    row.appendChild(ch);
  }
  add(row);
}
/* ================================================================ güncelleme */
// Sunucu açılışta ve saatte bir GitHub'daki dala bakar; yeni commit varsa bu çubuk çıkar.
function renderUpdate(u) {
  const bar = $("#update");
  S.update = u && (u.available || u.stale) ? u : null;
  if (!S.update) { bar.hidden = true; return; }
  const n = u.count || 0;
  $("#upText").textContent = !u.available
    ? "Yeni sürüm indirildi · yeniden başlatınca açılır"
    : (n ? `Yeni sürüm hazır · ${n} değişiklik` : "Yeni sürüm hazır") + (u.blocked ? ` — ${u.blocked}` : "");
  $("#upText").title = (u.commits || []).join("\n") || `${u.local} → ${u.remote}`;
  const btn = $("#upApply");
  btn.disabled = false;
  btn.hidden = false;
  btn.textContent = !u.available ? "Yeniden başlat" : u.blocked ? "Yine de güncelle" : "Güncelle ve yeniden başlat";
  $("#upLater").hidden = false;
  bar.hidden = false;
  const v = $("#ver");
  if (v && !v.querySelector(".up-dot")) v.appendChild(el("i", "up-dot"));
}
async function applyUpdate() {
  const btn = $("#upApply");
  const forced = !!(S.update && S.update.blocked);
  btn.disabled = true;
  btn.textContent = "Güncelleniyor…";
  const r = await api("/api/update/apply", { force: forced });
  if (!r || !r.ok) {
    btn.disabled = false;
    btn.textContent = forced ? "Yine de güncelle" : "Güncelle ve yeniden başlat";
    return toast((r && (r.error || r.note)) || "Güncellenemedi");
  }
  btn.hidden = true;
  $("#upLater").hidden = true;
  $("#upText").textContent = r.note + " — ALICE yeniden başlatılıyor, birazdan yeni pencere açılacak.";
}
async function checkUpdate() {
  toast("Güncelleme denetleniyor…");
  const u = await api("/api/update/check", {});
  if (!u) return toast("Denetlenemedi");
  if (!u.available && !u.stale) return toast(u.error || "ALICE güncel");
  renderUpdate(u);
}

function applyStatus(s) {
  Object.assign(S.status, s);
  const st = S.status;
  if ("team" in s) renderTeam(s.team);
  if (st.cwd !== undefined) { $("#cwd").textContent = st.cwd; $("#cwdBtn").title = st.cwd; }
  if (st.version) $("#ver").textContent = "ALICE " + st.version;
  if ("update" in s) renderUpdate(s.update);
  if (st.user) { $("#userName").textContent = st.user; $("#avatar").textContent = st.user[0].toUpperCase(); }
  if (st.mode) {
    const [label, , icon] = MODES[st.mode] || [st.mode, "", IC.shield];
    $("#modeLabel").textContent = label;
    $("#modeIcon").innerHTML = icon;
    $("#modeBtn").className = "c-btn pill m-" + st.mode;
  }
  $("#modelLabel").textContent = shortModel(st.model);
  if (st.thinking && THINK[st.thinking]) {
    $("#thinkLabel").textContent = THINK[st.thinking][0];
    $("#thinkIcon").innerHTML = st.thinking === "off" ? IC.bolt : IC.brain;
    $("#thinkBtn").title = "Düşünme: " + THINK[st.thinking][1];
  }
  $("#modelBtn").title = st.model || "";
  $("#stModel").textContent = st.model || "model yok";
  if (st.ctx_limit) {
    const win = st.ctx_window || st.ctx_limit;
    const pct = Math.min(100, (100 * st.ctx_tokens) / win);
    const m = $("#stCtx .meter");
    m.firstElementChild.style.width = pct + "%";
    m.classList.toggle("hot", pct > 75);
    $("#stCtx span").textContent = `${fmtTok(st.ctx_tokens)} / ${fmtTok(win)}`;
    $("#stCtx").title = `Bağlam: ${st.ctx_tokens.toLocaleString("tr-TR")} / ${win.toLocaleString("tr-TR")} token. ` +
      `${fmtTok(st.ctx_limit)} token'da eski konuşma otomatik özetlenir.`;
  }
  if (st.today_requests !== undefined) $("#stReq").textContent = `bugün ${st.today_requests} istek`;
  if (st.decider) $("#stDec").textContent = `karar: ${st.decider}`;
  if (st.branch !== undefined) {
    $("#branch").textContent = st.branch;
    $("#branchChip").hidden = !st.branch;
    $("#stBranch").textContent = st.branch ? "⎇ " + st.branch : "";
    $$(".st-br").forEach((n) => { n.hidden = !st.branch; });
  }
  if (PREFS.panel && PREFS.tab === "context") renderPanel();
  // Sadece gelen durumda busy varsa uygula: kısmi güncelleme (ör. model değişimi) eski busy değeriyle işi bitmiş saymasın.
  if (s.busy !== undefined && s.busy !== S.busy) setBusy(s.busy);
  for (const p of st.pending || []) (p.type === "ask_choice" ? onChoice : onPermission)(p);
  $$(".sess").forEach((b) => b.classList.toggle("active", b.dataset.id === st.session));
  if (app.classList.contains("empty")) greet();
}
async function refreshStatus() { const s = await api("/api/state"); if (s && s.version) applyStatus(s); }
$("#upApply").addEventListener("click", applyUpdate);
$("#upLater").addEventListener("click", () => { $("#update").hidden = true; });

/* ============================================================ geçmiş/oturum */
function renderHistory(items) {
  clearChat();
  for (const it of items || []) {
    if (it.type === "user") { addUser(it.text, it.mi); remember(it.text); }
    else if (it.type === "assistant") addAiStatic(it.text);
    else if (it.type === "info") addNote(it.text);
    else if (it.type === "tool") finishTool(toolRow(null, it.name, it.summary, false), { is_error: it.is_error, text: it.text });
  }
  if (!items || !items.length) setEmpty(true);
  markFinals();
  toBottom(true);
}
async function loadHistory() { renderHistory(await api("/api/history")); }

// Kenar çubuğu: aramayla süzülen, tarihe göre gruplanmış sohbetler.
async function loadSessions() {
  const rows = await api("/api/sessions");
  S.sessions = Array.isArray(rows) ? rows : [];
  drawSessions();
}
function relTime(ts) {
  if (!ts) return "";
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "şimdi";
  if (s < 3600) return `${Math.floor(s / 60)} dk`;
  if (s < 86400) return `${Math.floor(s / 3600)} sa`;
  if (s < 7 * 86400) return `${Math.floor(s / 86400)} g`;
  return new Date(ts * 1000).toLocaleDateString("tr-TR", { day: "numeric", month: "short" });
}
function dayGroup(ts) {
  if (!ts) return "Daha eski";
  const d = new Date(ts * 1000), now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
  if (ts >= start) return "Bugün";
  if (ts >= start - 86400) return "Dün";
  if (ts >= start - 6 * 86400) return "Bu hafta";
  if (d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth()) return "Bu ay";
  return "Daha eski";
}
function drawSessions() {
  const nav = $("#sessions");
  nav.replaceChildren();
  const q = ($("#sessSearch").value || "").trim().toLocaleLowerCase("tr");
  const rows = (S.sessions || []).filter((s) => !q || (s.title + " " + s.cwd).toLocaleLowerCase("tr").includes(q));
  if (!rows.length) {
    nav.appendChild(el("div", "side-empty", q ? "Eşleşen sohbet yok" : "Henüz sohbet yok"));
    return;
  }
  let group = null;
  for (const s of rows) {
    const g = dayGroup(s.updated);
    if (g !== group) { group = g; nav.appendChild(el("div", "side-label", g)); }
    const b = el("button", "sess");
    b.dataset.id = s.id;
    b.title = `${s.title}\n${s.cwd}`;
    b.appendChild(el("span", "st", s.title || "(boş)"));
    b.appendChild(el("small", "when", relTime(s.updated)));
    const del = el("span", "del");
    del.title = "Sil";
    del.appendChild(svg(IC.trash));
    del.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!(await confirmDialog("Sohbet silinsin mi?", "Bu sohbet geçmişten kalıcı olarak silinecek.", "Sil"))) return;
      await api("/api/session/delete", { id: s.id });
      if (s.id === S.status.session) newChat();
      loadSessions();
    });
    b.appendChild(del);
    b.addEventListener("click", () => resume(s.id));
    nav.appendChild(b);
  }
  $$(".sess").forEach((b) => b.classList.toggle("active", b.dataset.id === S.status.session));
}
$("#sessSearch").addEventListener("input", drawSessions);
$("#sessSearch").addEventListener("keydown", (e) => { if (e.key === "Escape") { e.target.value = ""; drawSessions(); e.target.blur(); } });
async function resume(id) {
  // Proje ekranı açıkken sohbete tıklamak önce projeyi kapatır; aynı sohbetse sadece ona dönülür.
  if (S.proj) { closeProject(); drawSessions(); if (id === S.status.session) { closeSideOnMobile(); return; } }
  if (S.busy) return toast("Önce mevcut işi durdur");
  const r = await api("/api/resume", { id });
  if (!r.ok) return toast(r.error || "Oturum yüklenemedi");
  renderHistory(r.history);
  applyStatus(r);
  refreshChanges();
  closeSideOnMobile();
}
async function newChat() {
  if (S.proj) closeProject();
  if (S.busy) return toast("Önce mevcut işi durdur");
  const r = await api("/api/new", {});
  if (r.ok) { clearChat(); applyStatus(r); loadSessions(); refreshChanges(); input.focus(); }
}
async function rewindTo(mi, edit) {
  if (S.busy) return toast("Önce mevcut işi durdur");
  const ok = await confirmDialog(edit ? "Mesajı düzenle" : "Buraya geri sar",
    "Konuşma bu mesajdan öncesine döner. ALICE'in bu mesajdan sonra dosya araçlarıyla yaptığı değişiklikler geri alınır " +
    "(komutlarla yapılan değişiklikler geri alınamaz).", edit ? "Düzenle" : "Geri sar");
  if (!ok) return;
  const r = await api("/api/rewind", { mi });
  if (!r.ok) return toast(r.error || "Geri sarılamadı");
  renderHistory(r.history);
  applyStatus(r);
  refreshChanges();
  toast(r.restored && r.restored.length ? `Geri sarıldı, ${r.restored.length} dosya eski haline getirildi` : "Geri sarıldı");
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
  row.appendChild(el("small", "", "Sonrası silinir, ALICE'in dosya değişiklikleri geri alınır"));
  const cancel = el("button", "btn", "Vazgeç");
  const send = el("button", "btn primary", "Gönder");
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
    if (S.busy) { toast("ALICE henüz durmadı, biraz sonra tekrar dene"); return false; }
  }
  const r = await api("/api/rewind", { mi });
  if (!r.ok) { toast(r.error || "Düzenlenemedi"); return false; }
  renderHistory(r.history);
  applyStatus(r);
  refreshChanges();
  if (r.restored && r.restored.length) toast(`${r.restored.length} dosya eski haline getirildi`);
  await submit(text);
  return true;
}
