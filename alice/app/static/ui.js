"use strict";

/* ALICE — menüler, pencereler, yan panel, komut paleti, ayarlar ve yazma kutusu.
   Sıradan (modül olmayan) betikler: hepsi aynı kapsamı paylaşır, index.html'deki sırayla yüklenir
   (app.js → projects.js → ui.js → main.js). Yükleme anında çalışan kod sadece önceki dosyaları kullanır. */

/* ==================================================================== menüler */
function openMenu(anchor, build, align) {
  const m = $("#menu");
  m.replaceChildren();
  build(m);
  m.hidden = false;
  const r = anchor.getBoundingClientRect();
  const mh = m.offsetHeight, mw = m.offsetWidth;
  let top = r.top - mh - 6;
  if (top < 8) top = r.bottom + 6;
  let left = align === "right" ? r.right - mw : r.left;
  left = Math.max(8, Math.min(left, window.innerWidth - mw - 8));
  m.style.top = top + "px";
  m.style.left = left + "px";
}
function closeMenu() { $("#menu").hidden = true; }
function menuItem(m, icon, title, sub, onClick, checked) {
  const b = el("button", "mi");
  if (icon) b.appendChild(svg(icon));
  const t = el("span", "mt");
  t.appendChild(el("b", "", title));
  if (sub) t.appendChild(el("small", "", sub));
  b.appendChild(t);
  if (checked) b.appendChild(svg(IC.check, "check"));
  b.addEventListener("click", () => { closeMenu(); onClick(); });
  m.appendChild(b);
}
document.addEventListener("mousedown", (e) => {
  if (!$("#menu").hidden && !e.target.closest("#menu") && !e.target.closest(".c-btn") && !e.target.closest(".user-chip")) closeMenu();
});

function modeMenu() {
  openMenu($("#modeBtn"), (m) => {
    m.appendChild(el("div", "menu-title", "İzin modu"));
    for (const [k, [label, sub, icon]] of Object.entries(MODES)) {
      menuItem(m, icon, label, sub, () => setMode(k), S.status.mode === k);
    }
  });
}
async function setMode(mode) { applyStatus(await api("/api/mode", { mode })); }
function cycleMode() {
  const keys = Object.keys(MODES);
  setMode(keys[(keys.indexOf(S.status.mode) + 1) % keys.length]);
}

const THINK = {
  off: ["düşünmez", "En hızlı: hemen cevap verir. Basit işler için"],
  low: ["az düşünür", "Kısa düşünür, sonra yapar (önerilen)"],
  medium: ["orta düşünür", "Biraz daha dikkatli, biraz daha yavaş"],
  high: ["çok düşünür", "Zor işler için; basit işte gereksiz uzun sürer"],
  default: ["model karar verir", "Modelin kendi ayarı"],
};
async function setThinking(level) {
  applyStatus(await api("/api/thinking", { level }));
  toast("Düşünme: " + THINK[level][0]);
}

function thinkMenu() {
  openMenu($("#thinkBtn"), (m) => {
    m.appendChild(el("div", "menu-title", "Düşünme"));
    for (const [k, [label, sub]] of Object.entries(THINK)) {
      menuItem(m, k === "off" ? IC.bolt : IC.brain, label[0].toUpperCase() + label.slice(1), sub,
        () => setThinking(k), S.status.thinking === k);
    }
  }, "right");
}
async function modelMenu() {
  const all = await api("/api/models/all");
  const provs = (all && all.providers) || [];
  const pinned = all ? all.pinned : (S.status.brain_pinned ? S.status.brain_chain[0] : null);
  openMenu($("#modelBtn"), (m) => {
    m.appendChild(el("div", "menu-title", "Ana model"));
    menuItem(m, IC.spark, "Otomatik", "Zincirdeki ilk uygun model, limit dolunca sıradakine geçer",
      () => pinModel("reset"), !pinned);
    // Sağlayıcıya göre gruplu; gizlenen sağlayıcı/modeller ve anahtarı olmayanlar menüde görünmez.
    for (const p of provs) {
      const rows = p.enabled && !p.hidden ? p.models.filter((x) => !x.hidden) : [];
      if (!rows.length) continue;
      m.appendChild(el("div", "menu-title", p.name + (p.note ? " · limit " + p.note : "")));
      for (const x of rows) {
        menuItem(m, null, x.model, x.status === "hazır" ? "" : x.status, () => pinModel(x.ref), pinned === x.ref);
      }
    }
    m.appendChild(el("div", "menu-sep"));
    menuItem(m, IC.list, "Modelleri yönet", "Sağlayıcı ya da model gizle/göster, durumları gör", () => showSettings("models"));
  }, "right");
}
async function hideModel(name, hidden) {
  const r = await api("/api/models/hide", { name, hidden });
  if (!r.ok) return toast(r.error || "Kaydedilemedi");
  applyStatus(r);
  toast((hidden ? "Gizlendi: " : "Gösteriliyor: ") + name);
}
function modelsPane(pane, all) {
  const provs = (all && all.providers) || [];
  pane.appendChild(el("div", "sec", "Sağlayıcılar ve modeller"));
  pane.appendChild(el("p", "", "Gizlenen sağlayıcı ya da model otomatik zincirde kullanılmaz ve model menüsünde görünmez. " +
    "Sabitlediğin model gizli olsa da kullanılır."));
  const f = el("input"); f.type = "text"; f.placeholder = "Model ara"; f.className = "set-filter";
  const box = el("div");
  const toggle = (hidden) => el("button", "act", hidden ? "Göster" : "Gizle");
  const draw = () => {
    box.replaceChildren();
    const q = f.value.trim().toLowerCase();
    for (const p of provs) {
      const rows = p.models.filter((x) => !q || x.ref.toLowerCase().includes(q));
      if (q && !rows.length) continue;
      const head = el("div", "list-row prov" + (p.hidden || !p.enabled ? " off" : ""));
      const shown = p.models.filter((x) => !x.hidden).length;
      head.append(el("b", "l plain", p.name),
        el("span", "r", !p.enabled ? (p.name === "chatgpt" ? "giriş yapılmamış (Kurulum → ChatGPT ile giriş)" : `${p.key} yok`) : p.hidden ? `gizli · ${p.models.length} model` : `${shown}/${p.models.length} model açık`));
      const hb = toggle(p.hidden);
      hb.addEventListener("click", async () => { await hideModel(p.name, !p.hidden); showSettings("models"); });
      head.appendChild(hb);
      box.appendChild(head);
      if (p.hidden && !q) continue;
      for (const x of rows) {
        const row = el("div", "list-row" + (x.hidden || p.hidden ? " off" : ""));
        row.append(el("span", "l", x.model),
          el("span", "r" + (x.status === "hazır" ? " ok" : ""), x.tested ? x.status : x.status + " · ölçülmedi"));
        const tb = toggle(x.hidden);
        tb.addEventListener("click", async () => { await hideModel(x.ref, !x.hidden); showSettings("models"); });
        const pin = el("button", "act" + (all.pinned === x.ref ? " on" : ""), all.pinned === x.ref ? "Sabit" : "Sabitle");
        pin.addEventListener("click", async () => { await pinModel(all.pinned === x.ref ? "reset" : x.ref); showSettings("models"); });
        row.append(tb, pin);
        box.appendChild(row);
      }
    }
    if (!box.children.length) box.appendChild(el("p", "", q ? "Eşleşen model yok." : "Henüz ölçülmüş model yok."));
  };
  f.addEventListener("input", draw);
  draw();
  pane.append(f, box);
}
async function pinModel(ref) {
  await api("/api/model", { role: "brain", ref });
  await refreshStatus();
  toast(ref === "reset" ? "Otomatik model seçimi" : "Model sabitlendi: " + shortModel(ref));
}

/* =================================================================== dialoglar */
let onDialogClose = null;
function showDialog(build) {
  if (onDialogClose) closeDialog();
  const d = $("#dialog");
  d.className = "dialog";
  d.replaceChildren();
  build(d);
  $("#overlay").hidden = false;
}
// Esc ya da dışarı tıklamak da "Vazgeç" sayılır; onay bekleyen kod askıda kalmasın.
function closeDialog() {
  $("#overlay").hidden = true;
  const f = onDialogClose;
  onDialogClose = null;
  if (f) f();
}
$("#overlay").addEventListener("mousedown", (e) => { if (e.target.id === "overlay") closeDialog(); });
function dialogClose(d, label) {
  const row = el("div", "row");
  const b = el("button", "btn primary", label || "Kapat");
  b.addEventListener("click", closeDialog);
  row.appendChild(b);
  d.appendChild(row);
}
function confirmDialog(title, text, okLabel) {
  return new Promise((resolve) => {
    showDialog((d) => {
      onDialogClose = () => resolve(false);
      d.append(el("h2", "", title), el("p", "", text));
      const row = el("div", "row");
      const no = el("button", "btn", "Vazgeç");
      const yes = el("button", "btn accent", okLabel || "Tamam");
      no.addEventListener("click", closeDialog);
      yes.addEventListener("click", () => { onDialogClose = null; closeDialog(); resolve(true); });
      row.append(no, yes);
      d.appendChild(row);
      setTimeout(() => yes.focus(), 0);
    });
  });
}
function showImage(src) {
  showDialog((d) => { const i = el("img", "full"); i.src = src; d.appendChild(i); dialogClose(d); });
}
function askCwd() {
  showDialog((d) => {
    d.append(el("h2", "", "Çalışma klasörü"), el("p", "", "Komutlar ve göreli dosya yolları bu klasöre göre çalışır. Projenin klasörünü seç."));
    const inp = el("input"); inp.type = "text"; inp.value = S.status.cwd || "";
    const err = el("div", "note err");
    const row = el("div", "row");
    const no = el("button", "btn", "Vazgeç");
    const ok = el("button", "btn accent", "Değiştir");
    const submit = async () => {
      const r = await api("/api/cwd", { path: inp.value });
      if (!r.ok) { err.textContent = r.error; return; }
      applyStatus(r); closeDialog(); toast("Klasör değişti");
    };
    no.addEventListener("click", closeDialog);
    ok.addEventListener("click", submit);
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
    row.append(no, ok);
    d.append(inp, err, row, githubPicker(err));
    setTimeout(() => { inp.focus(); inp.select(); }, 0);
  });
}
// GitHub'daki depolar: tıklanınca ~/GitHub altına klonlanır (varsa güncellenir) ve çalışma klasörü olur.
function githubPicker(err) {
  const box = el("div", "gh-pick");
  const q = el("input"); q.type = "text"; q.placeholder = "GitHub deposu ara…";
  const list = el("div", "gh-list");
  const note = el("div", "note", "Depolar yükleniyor…");
  box.append(el("h3", "", "GitHub'dan aç"), q, note, list);
  let repos = [];
  const draw = () => {
    const f = q.value.trim().toLocaleLowerCase("tr");
    const rows = repos.filter((r) => !f || (r.full_name + " " + r.description).toLocaleLowerCase("tr").includes(f));
    list.replaceChildren(...rows.slice(0, 50).map((r) => {
      const b = el("button", "list-row gh-row");
      const l = el("span", "l", r.full_name);
      if (r.description) l.title = r.description;
      b.append(l, el("span", "r" + (r.local ? " ok" : ""), r.local ? "bilgisayarda" : r.private ? "gizli" : ""));
      b.addEventListener("click", async () => {
        list.querySelectorAll("button").forEach((x) => (x.disabled = true));
        note.textContent = r.local ? `${r.full_name} güncelleniyor…` : `${r.full_name} klonlanıyor…`;
        const res = await api("/api/github/open", { repo: r.full_name });
        if (!res.ok) {
          err.textContent = res.error; note.textContent = "";
          list.querySelectorAll("button").forEach((x) => (x.disabled = false));
          return;
        }
        applyStatus(res); closeDialog(); toast(`${r.full_name} ${res.note}`);
      });
      return b;
    }));
    note.textContent = rows.length ? "" : (repos.length ? "Eşleşen depo yok" : note.textContent);
  };
  q.addEventListener("input", draw);
  api("/api/github/repos").then((r) => {
    if (!r.ok) { note.textContent = r.error; note.classList.add("warn"); return; }
    repos = r.repos || [];
    note.textContent = repos.length ? "" : "Depo yok";
    draw();
  });
  return box;
}
const showModels = () => showSettings("models");
const showTools = () => showSettings("tools");
const showMemory = () => showSettings("memory");
const showUsage = () => showSettings("usage");
const showHelp = () => showSettings("keys");

/* ================================================================ plan çubuğu */
function setTodo(text) {
  S.todo = text || "";
  const items = parseTodo(S.todo);
  const done = items.filter((i) => i.state === "done").length;
  const bar = $("#planbar");
  // Plan çubuğu sadece iş sürerken görünür; bitince plan yan panelde kalır.
  const open = S.busy && items.length && done < items.length;
  bar.hidden = !open;
  if (open) {
    const cur = items.find((i) => i.state === "doing") || items.find((i) => i.state !== "done");
    const next = items.find((i) => i !== cur && i.state === "");
    $("#pbCount").textContent = `${done}/${items.length}`;
    $("#pbText").textContent = cur ? cur.text : "";
    $("#pbNext").textContent = next ? "Sonra: " + next.text : "";
    const arc = $("#pbArc");
    const len = 2 * Math.PI * 15;
    arc.style.strokeDasharray = `${(len * done) / items.length} ${len}`;
  }
  if (PREFS.panel && PREFS.tab === "plan") renderPanel();
}
$("#planbar").addEventListener("click", () => openPanel("plan"));

/* ============================================================ değişiklikler */
let changesSeq = 0;
async function refreshChanges() {
  const seq = ++changesSeq;
  const rows = await api("/api/changes");
  if (seq !== changesSeq || !Array.isArray(rows)) return;
  S.changes = rows;
  drawChangesChip();
  if (PREFS.panel && PREFS.tab === "changes") renderPanel();
}
function changeTotals() {
  let a = 0, d = 0;
  for (const c of S.changes) { a += c.added; d += c.removed; }
  return { a, d, n: S.changes.length };
}
function drawChangesChip() {
  const { a, d, n } = changeTotals();
  $("#changesBtn").hidden = !n;
  $("#chA").textContent = `+${a}`;
  $("#chD").textContent = `−${d}`;
  $("#changesBtn").title = `Bu oturumda ${n} dosya değişti`;
  $("#chCount").textContent = n ? String(n) : "";
}
async function revertFile(c) {
  const ok = await confirmDialog("Değişiklik geri alınsın mı?",
    `${c.path} bu oturumdaki ilk haline döner` + (c.status === "eklendi" ? " (ALICE oluşturduğu için silinir)." : "."), "Geri al");
  if (!ok) return;
  const r = await api("/api/changes/revert", { path: c.abs });
  if (!r.ok) return toast(r.error || "Geri alınamadı");
  S.changes = r.changes || [];
  drawChangesChip();
  renderPanel();
  toast(`${c.path} geri alındı`);
}

/* ================================================================= yan panel */
function openPanel(tab) {
  if (tab) PREFS.tab = tab;
  PREFS.panel = true;
  savePrefs();
  app.classList.add("panel-open");
  renderPanel();
  if (PREFS.tab === "changes") refreshChanges();
}
function closePanel() { PREFS.panel = false; savePrefs(); app.classList.remove("panel-open"); }
function togglePanel() { if (PREFS.panel) closePanel(); else openPanel(); }
$("#panelBtn").addEventListener("click", togglePanel);
$("#panelClose").addEventListener("click", closePanel);
$("#changesBtn").addEventListener("click", () => openPanel("changes"));
$$("#panel .tab").forEach((b) => b.addEventListener("click", () => openPanel(b.dataset.tab)));

function emptyState(icon, title, text) {
  const e = el("div", "p-empty");
  e.append(svg(icon), el("b", "", title), el("p", "", text));
  return e;
}
function renderPanel() {
  if (!PREFS.panel) return;
  $$("#panel .tab").forEach((b) => b.classList.toggle("on", b.dataset.tab === PREFS.tab));
  const body = $("#panelBody");
  const keep = body.scrollTop;
  body.replaceChildren();
  if (PREFS.tab === "plan") {
    if (!parseTodo(S.todo).length) {
      body.appendChild(emptyState(IC.list, "Henüz plan yok",
        "ALICE çok adımlı bir işe başladığında yapılacakları burada listeler ve ilerledikçe işaretler."));
    } else body.appendChild(renderTodo(S.todo, true));
  } else if (PREFS.tab === "changes") {
    if (!S.changes.length) {
      body.appendChild(emptyState(IC.diff, "Değişiklik yok",
        "ALICE bu oturumda dosya araçlarıyla bir dosyayı değiştirdiğinde fark burada görünür. Dosya bazında geri alabilirsin."));
    } else {
      const { a, d, n } = changeTotals();
      const head = el("div", "ch-sum");
      head.append(el("b", "", `${n} dosya değişti`), el("span", "a", `+${a}`), el("span", "d", `−${d}`));
      const fold = el("button", "act", "Tümünü daralt");
      fold.addEventListener("click", () => $$(".ch-file", body).forEach((f) => f.classList.remove("open")));
      head.append(el("span", "grow"), fold);
      body.appendChild(head);
      S.changes.forEach((c, i) => {
        const f = el("div", "ch-file" + (i < 3 ? " open" : ""));
        const row = el("div", "ch-row");
        row.append(svg(IC.chev, "tchev"), el("span", "ch-st " + c.status, c.status[0].toUpperCase()));
        const name = el("span", "ch-name");
        const slash = c.path.lastIndexOf("/");
        if (slash >= 0) name.appendChild(el("small", "", c.path.slice(0, slash + 1)));
        name.appendChild(document.createTextNode(c.path.slice(slash + 1)));
        row.append(name, el("span", "a", `+${c.added}`), el("span", "d", `−${c.removed}`));
        const rv = el("button", "act ch-revert");
        rv.title = "Bu dosyayı geri al";
        rv.append(svg(IC.rewind), document.createTextNode("Geri al"));
        rv.addEventListener("click", (e) => { e.stopPropagation(); revertFile(c); });
        row.appendChild(rv);
        row.addEventListener("click", () => f.classList.toggle("open"));
        const inner = el("div", "ch-body");
        if (c.binary) inner.appendChild(el("div", "note", "İkili dosya, fark gösterilemiyor"));
        else if (c.diff) inner.appendChild(renderDiff(c.diff, c.path).box);
        f.append(row, inner);
        body.appendChild(f);
      });
    }
  } else {
    const st = S.status;
    const win = st.ctx_window || st.ctx_limit || 0;
    const pct = win ? Math.min(100, (100 * st.ctx_tokens) / win) : 0;
    const ctx = el("div", "ctx-card");
    const big = el("div", "ctx-big");
    big.append(el("b", "", `${Math.round(pct)}%`), el("span", "", `${fmtTok(st.ctx_tokens || 0)} / ${fmtTok(win)} token`));
    const meter = el("i", "ctx-meter" + (pct > 75 ? " hot" : ""));
    const fill = el("i");
    fill.style.width = pct + "%";
    meter.appendChild(fill);
    const cp = el("button", "btn", "Şimdi özetle");
    cp.addEventListener("click", () => runCommand("/compact"));
    ctx.append(el("div", "sec", "Bağlam doluluğu"), big, meter,
      el("p", "", `Modelin penceresi ${fmtTok(win)} token. ${fmtTok(st.ctx_limit || 0)} token'a gelince eski konuşma otomatik özetlenir. ` +
        "Sayı, sağlayıcının son istekte saydığı token'dır (araç tanımları dahil)."), cp);
    body.appendChild(ctx);
    const kv = el("dl", "kv");
    const mins = Math.floor((Date.now() - S.started) / 60000);
    const { a, d, n } = changeTotals();
    for (const [k, v] of [["Model", st.model || "yok"], ["Düşünme", (THINK[st.thinking] || [st.thinking || "?"])[0]],
      ["İzin modu", (MODES[st.mode] || [st.mode])[0]], ["Karar", st.decider || "-"], ["Klasör", st.cwd || ""],
      ["Git dalı", st.branch || "-"], ["Oturum", st.session || ""], ["Açık kalma", mins < 1 ? "1 dk'dan az" : `${mins} dk`],
      ["Bugün", `${st.today_requests || 0} istek`], ["Değişiklik", n ? `${n} dosya +${a} −${d}` : "yok"]]) {
      kv.append(el("dt", "", k), el("dd", "", v));
    }
    const sec = el("div", "sec", "Oturum");
    body.append(sec, kv);
  }
  body.scrollTop = keep;
}

/* ============================================================ komut paleti */
function paletteItems() {
  const items = [];
  const act = (group, icon, label, hint, run, key) => items.push({ group, icon, label, hint, run, key });
  act("Eylemler", IC.chat, "Yeni sohbet", "", newChat, "Ctrl+Shift+O");
  act("Eylemler", IC.panel, PREFS.panel ? "Yan paneli kapat" : "Yan paneli aç", "", togglePanel, "Ctrl+J");
  act("Eylemler", IC.list, "Planı göster", "Yapılacaklar listesi", () => openPanel("plan"));
  act("Eylemler", IC.diff, "Değişiklikleri göster", "Bu oturumda değişen dosyalar", () => openPanel("changes"));
  act("Eylemler", IC.gauge, "Bağlam ve oturum bilgisi", "", () => openPanel("context"));
  act("Eylemler", IC.expand, PREFS.verbose ? "Araç ayrıntılarını gizle" : "Araç ayrıntılarını göster", "Tüm araç çıktılarını aç/kapat", toggleVerbose, "Ctrl+O");
  act("Eylemler", IC.folder, "Çalışma klasörünü değiştir", S.status.cwd || "", askCwd);
  act("Eylemler", IC.spark, "Konuşmayı özetle", "/compact", () => runCommand("/compact"));
  act("Eylemler", IC.rewind, "Son mesaja geri sar", "/rewind", () => runCommand("/rewind"));
  act("Eylemler", IC.update, "Güncellemeleri denetle", "GitHub'daki son sürüm", checkUpdate);
  act("Eylemler", IC.gear, "Ayarlar", "", () => showSettings("general"), "Ctrl+,");
  act("Eylemler", IC.keyboard, "Klavye kısayolları", "", () => showSettings("keys"));
  for (const [k, label] of [["system", "Sistem"], ["light", "Açık"], ["dark", "Koyu"]]) {
    act("Tema", k === "dark" ? IC.moon : IC.sun, `Tema: ${label}`, "", () => setPref("theme", k));
  }
  for (const [k, [label, sub, icon]] of Object.entries(MODES)) act("İzin modu", icon, `Mod: ${label}`, sub, () => setMode(k));
  for (const [k, [label, sub]] of Object.entries(THINK)) act("Düşünme", IC.brain, `Düşünme: ${label}`, sub, () => setThinking(k));
  act("Model", IC.spark, "Model: Otomatik", "Zincirdeki ilk uygun model", () => pinModel("reset"));
  for (const ref of S.status.brain_chain || []) act("Model", IC.spark, `Model: ${shortModel(ref)}`, ref, () => pinModel(ref));
  for (const c of S.commands) {
    act("Komutlar", IC.term, "/" + c.name, c.description, () => {
      input.value = "/" + c.name + " "; autosize(); input.focus();
    });
  }
  for (const s of S.sessions || []) act("Sohbetler", IC.chat, s.title || "(boş)", relTime(s.updated), () => resume(s.id));
  return items;
}
// Sırayla geçen harf eşleşmesi; ardışık harfler ve kelime başları daha yüksek puan alır.
function fuzzy(q, text) {
  if (!q) return 1;
  const t = text.toLocaleLowerCase("tr");
  const idx = t.indexOf(q);
  if (idx >= 0) return 100 - idx + (idx === 0 || /\s|\/|:/.test(t[idx - 1]) ? 50 : 0);
  let score = 0, j = 0, run = 0;
  for (let i = 0; i < t.length && j < q.length; i++) {
    if (t[i] === q[j]) { j++; run++; score += run * 2 + (i === 0 || /\s|\/|:/.test(t[i - 1]) ? 5 : 0); }
    else run = 0;
  }
  return j === q.length ? score : 0;
}
const PAL = { items: [], shown: [], sel: 0 };
function openPalette(prefill) {
  closeMenu();
  PAL.items = paletteItems();
  $("#palette").hidden = false;
  const inp = $("#palInput");
  inp.value = prefill || "";
  drawPalette();
  setTimeout(() => inp.focus(), 0);
}
function closePalette() { $("#palette").hidden = true; }
function drawPalette() {
  const q = $("#palInput").value.trim().toLocaleLowerCase("tr");
  const scored = PAL.items.map((it) => ({ it, sc: Math.max(fuzzy(q, it.label), fuzzy(q, it.group + " " + it.label) * 0.6, fuzzy(q, it.hint || "") * 0.4) }))
    .filter((x) => x.sc > 0);
  if (q) scored.sort((x, y) => y.sc - x.sc);
  PAL.shown = scored.slice(0, 60).map((x) => x.it);
  PAL.sel = Math.min(PAL.sel, Math.max(0, PAL.shown.length - 1));
  const box = $("#palList");
  box.replaceChildren();
  if (!PAL.shown.length) { box.appendChild(el("div", "pal-empty", "Sonuç yok")); return; }
  let group = null;
  PAL.shown.forEach((it, i) => {
    if (!q && it.group !== group) { group = it.group; box.appendChild(el("div", "pal-group", group)); }
    const row = el("div", "pal-item" + (i === PAL.sel ? " sel" : ""));
    row.appendChild(svg(it.icon));
    const t = el("span", "pal-t");
    t.appendChild(el("b", "", it.label));
    if (it.hint) t.appendChild(el("small", "", it.hint));
    row.appendChild(t);
    if (q) row.appendChild(el("span", "pal-g", it.group));
    if (it.key) row.appendChild(el("kbd", "", it.key));
    row.addEventListener("mousemove", () => { if (PAL.sel !== i) { PAL.sel = i; markPalSel(); } });
    row.addEventListener("click", () => runPalette(i));
    box.appendChild(row);
  });
  markPalSel();
}
function markPalSel() {
  $$("#palList .pal-item").forEach((r, i) => r.classList.toggle("sel", i === PAL.sel));
  $("#palList .pal-item.sel")?.scrollIntoView({ block: "nearest" });
}
function runPalette(i) {
  const it = PAL.shown[i];
  closePalette();
  if (it) it.run();
}
$("#palInput").addEventListener("input", () => { PAL.sel = 0; drawPalette(); });
$("#palInput").addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") { e.preventDefault(); PAL.sel = Math.min(PAL.shown.length - 1, PAL.sel + 1); markPalSel(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); PAL.sel = Math.max(0, PAL.sel - 1); markPalSel(); }
  else if (e.key === "Enter") { e.preventDefault(); runPalette(PAL.sel); }
  else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closePalette(); }
});
$("#palette").addEventListener("mousedown", (e) => { if (e.target.id === "palette") closePalette(); });
$("#paletteBtn").addEventListener("click", () => openPalette());

/* ================================================================== ayarlar */
function setPref(k, v) {
  PREFS[k] = v;
  savePrefs();
  applyPrefs();
}
function toggleVerbose() {
  setPref("verbose", !PREFS.verbose);
  $$(".tool").forEach((t) => t.classList.toggle("open", PREFS.verbose || !!t.querySelector(".diff")));
  $$(".tool-group").forEach((g) => g.classList.toggle("open", PREFS.verbose));
  toast(PREFS.verbose ? "Araç ayrıntıları açık" : "Araç ayrıntıları kapalı");
}
function segmented(options, value, onPick) {
  const seg = el("div", "seg");
  for (const [k, label] of options) {
    const b = el("button", k === value ? "on" : "", label);
    b.addEventListener("click", () => { onPick(k); $$("button", seg).forEach((x) => x.classList.toggle("on", x === b)); });
    seg.appendChild(b);
  }
  return seg;
}
function setRow(title, sub, control) {
  const r = el("div", "set-row");
  const t = el("div", "set-t");
  t.appendChild(el("b", "", title));
  if (sub) t.appendChild(el("small", "", sub));
  r.append(t, control);
  return r;
}
/* Ayarlar > Asistan: özelliklerin durumu, güvenlik/karşılama ayarları, izin kuralları, Gmail hesapları, skill'ler. */
function assistantPane(pane, data) {
  const d = data || {};
  const v = d.settings || {};
  const set = (section, key, value, label) =>
    api("/api/assistant/set", { section, key, value }).then((r) => toast(r && r.ok ? label + " kaydedildi" : "kaydedilemedi"));
  const onOff = (section, key, label) => segmented([["1", "Açık"], ["0", "Kapalı"]], v[section + "." + key] === false ? "0" : "1",
    (x) => set(section, key, x === "1", label));
  pane.append(el("h2", "", "Asistan"),
    el("p", "", "Yeni özelliklerin durumu ve ayarları. Ayrıntılı test için terminalde: alice doctor"));
  pane.appendChild(el("div", "sec", "Durum"));
  const mark = { ok: "✓", warn: "!", off: "–", fail: "✗" };
  for (const c of d.checks || []) {
    const row = el("div", "list-row");
    const l = el("span", "l");
    l.append(el("b", "", c.name), el("small", "", c.detail + (c.fix && c.status !== "ok" ? "  → " + c.fix : "")));
    row.append(el("span", "chk " + c.status, mark[c.status] || "?"), l);
    pane.appendChild(row);
  }
  pane.appendChild(el("div", "sec", "Ayarlar"));
  pane.append(
    setRow("Komut sandbox'ı", "Otomatik: dış içerik (web, e-posta) okunan turda komutlar proje dışına yazamayan sandbox'ta çalışır",
      segmented([["off", "Kapalı"], ["auto", "Otomatik"], ["on", "Her zaman"]], v["agent.sandbox"] || "auto",
        (x) => set("agent", "sandbox", x, "Sandbox"))),
    setRow("Prompt injection koruması", "Dış içerik okunan turda komut ve mesaj gönderme onaya bağlanır",
      onOff("agent", "taint_guard", "Koruma")),
    setRow("Google araması", "web_search önce Google'da (Gemini) arar, kota dolarsa DuckDuckGo",
      onOff("agent", "search_grounding", "Google araması")),
    setRow("Dönüşte karşılama", "Uzun yokluktan sonra takip, takvim ve e-postalarla kısa sesli brifing",
      onOff("greeting", "enabled", "Karşılama")),
    setRow("Yokluk süresi", "Bu kadar dakika uzakta kalınca dönüş sayılır",
      segmented([["60", "60"], ["90", "90"], ["120", "120"], ["180", "180"]], String(v["greeting.away_minutes"] || 90),
        (x) => set("greeting", "away_minutes", Number(x), "Yokluk süresi"))),
    setRow("Konuşma özetleri", "Biten konuşma özetlenir, takipler çıkarılır",
      onOff("memory", "digest", "Özetler")),
    setRow("E-posta bildirimi", "Yeni önemli e-posta gelince haber ver", onOff("gmail", "notify", "E-posta bildirimi")),
  );
  pane.appendChild(el("div", "sec", "İzin kuralları"));
  const rules = d.rules || [];
  if (!rules.length) pane.appendChild(el("p", "", "Kural yok. Onay kartında \"Bu projede hep izin ver\" deyince buraya eklenir."));
  for (const r of rules) {
    const row = el("div", "list-row");
    const l = el("span", "l");
    l.append(el("b", "", r.rule), el("small", "", (r.kind === "allow" ? "izin ver" : r.kind === "deny" ? "yasak" : "sor") +
      " · " + (r.source === "project" ? "bu proje (.alice/permissions.toml)" : "config.toml (dosyadan düzenle)")));
    row.appendChild(l);
    if (r.source === "project") {
      const b = el("button", "btn", "Sil");
      b.addEventListener("click", () => api("/api/assistant/action", { action: "rule_delete", kind: r.kind, rule: r.rule })
        .then((x) => { if (x && x.ok) row.remove(); }));
      row.appendChild(b);
    }
    pane.appendChild(row);
  }
  pane.appendChild(el("div", "sec", "Gmail hesapları"));
  for (const g of d.gmail || []) {
    const row = el("div", "list-row");
    const l = el("span", "l");
    l.append(el("b", "", g.account), el("small", "", g.email || ""));
    const b = el("button", "btn", "Çıkış");
    b.addEventListener("click", () => api("/api/assistant/action", { action: "gmail_logout", account: g.account })
      .then((x) => { if (x && x.ok) row.remove(); }));
    row.append(l, b);
    pane.appendChild(row);
  }
  if (d.google_ready) {
    const row = el("div", "list-row");
    const inp = el("input", "set-filter");
    inp.placeholder = "Hesap adı (ör. okul) ve Enter: tarayıcıda o hesabı seç";
    inp.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" || !inp.value.trim()) return;
      api("/api/assistant/action", { action: "gmail_connect", account: inp.value.trim() })
        .then((x) => toast(x && x.ok ? "Tarayıcıda izin sayfası açıldı" : (x && x.error) || "başlatılamadı"));
      inp.value = "";
    });
    row.appendChild(inp);
    pane.appendChild(row);
  } else {
    const g = el("div", "list-row warn");
    const gl = el("span", "l");
    gl.append(el("b", "", "Google kurulumu yapılmamış"), el("small", "", "Takvim ve Gmail için adım adım kurulum, sayfaları kendisi açar."));
    const gb = el("button", "btn primary", "Kuruluma git");
    gb.addEventListener("click", () => showSettings("setup"));
    g.append(gl, gb);
    pane.appendChild(g);
  }
  pane.appendChild(el("div", "sec", "Skill'ler ve alt ajanlar"));
  const items = [...(d.skills || []).map((x) => ["skill", x]), ...(d.agents || []).map((x) => ["alt ajan", x])];
  if (!items.length) pane.appendChild(el("p", "", "Yok. ~/.alice/skills/<ad>/SKILL.md ve ~/.alice/agents/<ad>.md ile eklenir (README: Skill örneği)."));
  for (const [kind, x] of items) {
    const row = el("div", "list-row");
    const l = el("span", "l");
    l.append(el("b", "", x.name), el("small", "", kind + " · " + x.description + " · " + x.path));
    row.appendChild(l);
    pane.appendChild(row);
  }
}
/* Ayarlar > Ses: Gemini seslerini dinle ve seç (Live ve normal sesli cevap aynı sesle konuşur). */
let previewAudio = null;
function voicePane(pane, data) {
  const d = data || {};
  pane.append(el("h2", "", "Ses"),
    el("p", "", "ALICE'in konuşma sesi. Dinle'ye basınca o sesle kısa bir örnek çalar (ilk seferde birkaç saniye "
      + "sürer). Seçtiğin ses canlı konuşmada bir sonraki \"Hey Alice\"te geçerli olur."));
  pane.appendChild(el("div", "sec", "Sesimi tanıt"));
  pane.appendChild(enrollCard());
  if (!d.can_preview) pane.appendChild(el("p", "", "Örnek dinlemek için Gemini anahtarı gerekli (Ayarlar → Anahtarlar)."));
  pane.appendChild(el("div", "sec", "Sesler"));
  for (const v of d.voices || []) {
    const on = v.name === d.live;
    const row = el("div", "list-row");
    const l = el("span", "l");
    l.append(el("b", "", v.name + (on ? "  ✓" : "")), el("small", "", v.desc + (on ? " · şu an bu ses" : "")));
    const play = el("button", "btn", "▶ Dinle");
    play.disabled = !d.can_preview;
    play.addEventListener("click", () => {
      play.textContent = "…";
      api("/api/voice/preview", { voice: v.name }).then((r) => {
        play.textContent = "▶ Dinle";
        if (!r || !r.ok) return toast((r && r.error) || "örnek üretilemedi");
        if (previewAudio) previewAudio.pause();
        previewAudio = new Audio("data:audio/wav;base64," + r.audio);
        previewAudio.play().catch((e) => toast("ses çalınamadı: " + (e && e.message || e)));
      });
    });
    const pick = el("button", on ? "btn" : "btn primary", on ? "Seçili" : "Seç");
    pick.disabled = on;
    pick.addEventListener("click", () => api("/api/voice/set", { voice: v.name }).then((r) => {
      toast(r && r.ok ? v.name + " seçildi; bir sonraki konuşmada bu sesle konuşur" : "kaydedilemedi");
      showSettings("voice");
    }));
    row.append(l, play, pick);
    pane.appendChild(row);
  }
}
/* Ayarlar > Ses > Sesimi tanıt: okunacak cümle büyük yazılır, mikrofon seviyesi canlı görünür. Kayıt sunucuda
   (mikrofonun olduğu bilgisayarda) yürür; burası durumu sık sık okur. Kart ekrandan kalkınca okuma durur. */
const ENROLL_BUSY = ["preparing", "installing", "downloading", "recording", "testing"];
function enrollCard() {
  const card = el("div", "enroll");
  let timer = null;
  const act = (action) => api("/api/voice/enroll", { action }).then((r) => {
    if (r && r.ok === false && r.error) toast(r.error);
    poll();
  });
  const btn = (label, action, primary) => {
    const b = el("button", primary ? "btn primary" : "btn", label);
    b.addEventListener("click", () => act(action));
    return b;
  };
  const draw = (s) => {
    card.replaceChildren();
    const busy = ENROLL_BUSY.includes(s.state);
    const prof = s.profile;
    if (!busy) {
      const head = el("div", "enroll-head");
      const l = el("span", "l");
      if (prof) {
        l.append(el("b", "", "Sesin tanıtıldı ✓"),
          el("small", "", `${prof.samples} cümle · eşik ${Number(prof.threshold).toFixed(2)} · tutarlılık ${Number(prof.consistency).toFixed(2)}. `
            + "\"Alice\" deyince sadece senin sesine cevap verir; Discord'daki arkadaşların ya da videodaki sesler uyandırmaz."));
      } else {
        l.append(el("b", "", "Sadece senin sesine cevap versin"),
          el("small", "", "6 kısa cümle okursun (1 dakika). ALICE sesinin izini çıkarır; başkası \"Alice\" deyince uyanmaz. "
            + "Discord'da/oyunda nasıl konuşuyorsan öyle oku."));
      }
      const acts = el("span", "enroll-act");
      if (!s.available) acts.append(btn("Gerekeni kur", "install", true));
      else if (prof) acts.append(btn("Dene", "test"), btn("Yeniden tanıt", "start"), btn("Kapat", "remove"));
      else acts.append(btn("Sesimi tanıt", "start", true));
      head.append(l, acts);
      card.appendChild(head);
      if (!s.available) card.appendChild(el("small", "enroll-note", "Ses tanıma paketi (sherpa-onnx) kurulu değil; \"Gerekeni kur\" bir kez kurar."));
      else if (!s.model) card.appendChild(el("small", "enroll-note", "İlk seferde ses tanıma modeli indirilir (~40 MB)."));
    }
    if (busy) {
      const steps = el("div", "enroll-steps");
      for (let i = 1; i <= s.total; i++) {
        steps.appendChild(el("span", i < s.step || (i === s.step && s.state === "testing") ? "done" : i === s.step && s.state === "recording" ? "on" : ""));
      }
      if (s.state === "recording") {
        card.append(steps, el("div", "enroll-n", `${s.step}/${s.total} · bu cümleyi sesli oku`),
          el("div", "enroll-say", "“" + s.sentence + "”"));
      } else if (s.state === "testing") {
        card.append(steps, el("div", "enroll-say small", "Şimdi kendi aklından bir cümle söyle, deneyelim."));
      }
      if (s.state === "recording" || s.state === "testing") {
        const meter = el("div", "enroll-meter" + (s.hearing ? " hearing" : ""));
        const bar = el("i");
        bar.style.width = Math.round((s.level || 0) * 100) + "%";
        meter.appendChild(bar);
        // Ne olduğu hep yazsın: dinliyor mu, duyuyor mu, kaç saniyedir, alındı mı.
        const status = s.got ? s.got
          : s.hearing ? `● duyuyorum… ${Number(s.took || 0).toFixed(1)} sn. Cümle bitince kendiliğinden alır; bitirdiysen "Okudum"a bas.`
          : (s.message === "oku" ? "dinliyorum, sesli oku" : s.message) + (s.left ? ` (${s.left} sn)` : "");
        card.append(meter, el("div", "enroll-status" + (s.got ? " got" : s.hearing ? " on" : ""), status));
      } else {
        card.appendChild(el("div", "enroll-say small", s.message || "…"));
        if (s.state === "downloading" && s.progress != null) {
          const meter = el("div", "enroll-meter dl");
          const bar = el("i");
          bar.style.width = Math.round(s.progress * 100) + "%";
          meter.appendChild(bar);
          card.append(meter, el("small", "enroll-note", Math.round(s.progress * 100) + "%"));
        }
      }
      const row = el("div", "row");
      row.appendChild(btn("Vazgeç", "cancel"));
      if (s.state === "recording" && !s.got) row.appendChild(btn("Bu cümleyi atla", "skip"));
      if ((s.state === "recording" || s.state === "testing") && !s.got) row.appendChild(btn("Okudum", "done", true));
      card.appendChild(row);
    } else if (s.state === "done" && s.result) {
      const r = s.result;
      const msg = r.score == null ? s.message
        : `Deneme: benzerlik ${Number(r.score).toFixed(2)} (eşik ${Number(r.threshold).toFixed(2)}) — ` + (r.recognized ? "seni tanıdı ✓" : "tanımadı ✗");
      card.appendChild(el("div", "enroll-result " + (r.recognized === false ? "bad" : "good"), msg));
      if (r.recognized === false || r.noisy) {
        card.appendChild(el("small", "enroll-note", "Kayıtlar birbirine pek benzemedi ya da deneme tutmadı: sessiz bir anda, mikrofona aynı uzaklıkta yeniden tanıt."));
      }
    } else if ((s.state === "error" || s.state === "cancelled" || s.message) && s.state !== "done") {
      card.appendChild(el("div", "enroll-result" + (s.state === "error" ? " bad" : ""), s.message));
    }
    return busy;
  };
  let shown = false;  // ilk çizim kart sayfaya eklenmeden yapılır
  function poll() {
    clearTimeout(timer);
    api("/api/voice/enroll").then((s) => {
      if (!s || (shown && !card.isConnected)) return;
      shown = true;
      if (draw(s)) timer = setTimeout(poll, 150);
    });
  }
  poll();
  return card;
}
/* Ayarlar > Çekirdek: arka plandaki ALICE (bildirimler, görevler, "Hey Alice") ve sesli asistan. Komut satırındaki
   alice core status/install/mode ve alice voice on/off/test burada düğme. Durum birkaç saniyede bir tazelenir. */
const MODE_LABELS = { auto: "Otomatik", normal: "Normal", dev: "Geliştirme", flight: "Uçuş", quiet: "Sessiz" };
function systemPane(pane) {
  pane.append(el("h2", "", "Çekirdek"),
    el("p", "", "ALICE'in arka planda çalışan kısmı: \"Hey Alice\" dinleme, bildirimler, zamanlanmış görevler, "
      + "kurallar. Tepsideki ALICE simgesi odur. Bu pencereyi kapatsan da çalışmaya devam eder."));
  const body = el("div");
  pane.appendChild(body);
  const mic = micTestCard();  // tek kart: durum tazelenirken süren test sıfırlanmasın
  let timer = null;
  const act = (action, extra, msg) => api("/api/system/action", Object.assign({ action }, extra || {})).then((r) => {
    if (!r || !r.ok) return toast((r && r.error) || "olmadı");
    if (msg) toast(msg);
    load();
  });
  const btn = (label, action, extra, primary, msg) => {
    const b = el("button", primary ? "btn primary" : "btn", label);
    b.addEventListener("click", () => { b.disabled = true; act(action, extra, msg); });
    return b;
  };
  const draw = (d) => {
    body.replaceChildren();
    const c = d.core || {};
    const v = d.voice || {};
    // Durum
    const st = el("div", "list-row sys-state");
    const l = el("span", "l");
    const state = c.busy ? c.busy + "…" : c.running ? "Çalışıyor" : "Çalışmıyor";
    const voiceTxt = !c.running ? "" : c.voice === "listening" ? " · \"Hey Alice\" dinliyor"
      : c.voice === "no-mic" ? " · mikrofon bulunamadı, sadece bildirimleri okur" : " · ses kapalı";
    l.append(el("b", "", state),
      el("small", "", c.running ? `mod: ${MODE_LABELS[c.mode] || c.mode || "?"}${c.forced ? " (elle sabit)" : ""}${voiceTxt} · pid ${c.pid}`
        : "Bildirimler, görevler ve sesli asistan çalışmıyor."));
    const acts = el("span", "enroll-act");
    if (c.busy) acts.appendChild(el("span", "sys-spin", ""));
    else if (c.running) acts.append(btn("Yeniden başlat", "restart"), btn("Durdur", "stop"));
    else acts.appendChild(btn("Başlat", "start", null, true));
    st.append(el("span", "chk " + (c.running ? "ok" : "off"), c.running ? "●" : "○"), l, acts);
    body.appendChild(st);
    body.appendChild(setRow("Windows açılınca başlasın", "Oturum açınca çekirdek kendiliğinden, pencere açmadan başlar.",
      segmented([[true, "Açık"], [false, "Kapalı"]], !!c.autostart, (on) => act("autostart", { on }, on ? "otomatik başlatma kuruldu" : "otomatik başlatma kaldırıldı"))));
    body.appendChild(setRow("Mod", "Otomatik: oyun/uçuş/kod açıkken kendisi değiştirir. Elle seçersen öyle kalır.",
      segmented((d.modes || []).map((m) => [m, MODE_LABELS[m] || m]), c.forced || "auto", (mode) => act("mode", { mode }, "mod: " + (MODE_LABELS[mode] || mode)))));
    // Ses
    body.appendChild(el("div", "sec", "Sesli asistan"));
    body.appendChild(setRow("\"Hey Alice\" ile konuş",
      "Açınca çekirdek yeniden başlar ve mikrofonu dinlemeye başlar." + (v.ptt_key ? ` Bas-konuş tuşu: ${keyName(v.ptt_key)}.` : ""),
      segmented([[true, "Açık"], [false, "Kapalı"]], !!v.enabled, (on) => act("voice", { on }, on ? "sesli asistan açılıyor" : "sesli asistan kapatıldı"))));
    const ok = (x) => x ? "✓" : "✗";
    body.appendChild(setRow("Mikrofon", (v.mic ? "bulundu" : "bulunamadı") + " · konuşmayı yazıya çeviren: " + ((v.stt || []).join(", ") || "yok (Groq ya da Gemini anahtarı gir)"),
      el("span", "chk " + (v.mic ? "ok" : "fail"), ok(v.mic))));
    body.appendChild(mic);
    // Paketler
    const missing = (d.packages || []).filter((p) => !p.ok);
    body.appendChild(el("div", "sec", "Paketler"));
    for (const p of d.packages || []) {
      const r = el("div", "list-row");
      const pl = el("span", "l");
      pl.append(el("b", "", p.name), el("small", "", p.why));
      r.append(el("span", "chk " + (p.ok ? "ok" : "off"), p.ok ? "✓" : "–"), pl);
      body.appendChild(r);
    }
    if (missing.length) body.appendChild(el("small", "enroll-note", "Eksikler isteğe bağlı. Kurmak için: pip install \"alice-harness[voice]\""));
    // Olaylar
    if ((d.events || []).length) {
      body.appendChild(el("div", "sec", "Son olaylar"));
      for (const e of d.events) {
        const r = el("div", "list-row ev-row");
        const el_ = el("span", "l");
        const t = new Date(e.ts * 1000);
        el_.append(el("b", "", e.title), el("small", "", t.toLocaleString("tr-TR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })
          + " · " + e.kind + (e.body ? " · " + e.body : "")));
        r.append(el("span", "chk " + (e.severity === "warn" || e.severity === "critical" ? "warn" : "off"), "•"), el_);
        body.appendChild(r);
      }
    }
    return c.busy;
  };
  let shown = false;
  function load() {
    clearTimeout(timer);
    api("/api/system").then((d) => {
      if (!d || (shown && !body.isConnected)) return;
      shown = true;
      const busy = draw(d);
      timer = setTimeout(() => { if (body.isConnected) load(); }, busy ? 800 : 5000);
    });
  }
  load();
}
// "<scroll_lock>" -> "Scroll Lock", "<ctrl>+<alt>+a" -> "Ctrl + Alt + A"
function keyName(k) {
  return String(k).split("+").map((p) => p.replace(/[<>]/g, "").split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(" ")).join(" + ");
}
function micTestCard() {
  const card = el("div", "enroll mic-test");
  let timer = null;
  const draw = (s) => {
    card.replaceChildren();
    const head = el("div", "enroll-head");
    const l = el("span", "l");
    l.append(el("b", "", "Mikrofon testi"), el("small", "", "5 saniye konuş; ses seviyesini ve ALICE'in ne duyduğunu gösterir."));
    const b = el("button", "btn", s.state === "listening" ? `dinliyorum… ${Math.ceil(s.left || 0)}` : s.state === "transcribing" ? "yazıya çevriliyor…" : "Test et");
    b.disabled = s.state === "listening" || s.state === "transcribing";
    b.addEventListener("click", () => api("/api/system/action", { action: "mic_test" }).then(poll));
    head.append(l, b);
    card.appendChild(head);
    if (s.state === "listening") {
      const meter = el("div", "enroll-meter hearing");
      const bar = el("i");
      bar.style.width = Math.round((s.level || 0) * 100) + "%";
      meter.appendChild(bar);
      meter.style.marginTop = "12px";
      card.appendChild(meter);
    } else if (s.state === "done" || s.state === "error") {
      if (s.text) card.appendChild(el("div", "enroll-say small", "“" + s.text + "”"));
      const note = s.text ? `duyulan${s.engine ? " (" + s.engine + ")" : ""} · en yüksek seviye ${Number(s.peak).toFixed(3)}` : s.message;
      card.appendChild(el("div", "enroll-result" + (s.text ? " good" : " bad"), note));
    }
    return s.state === "listening" || s.state === "transcribing";
  };
  let shown = false;
  function poll() {
    clearTimeout(timer);
    api("/api/system/mictest").then((s) => {
      if (!s || (shown && !card.isConnected)) return;
      shown = true;
      if (draw(s)) timer = setTimeout(poll, 150);
    });
  }
  poll();
  return card;
}
/* Ayarlar > Otomasyon: kurallar ("GPU 85'i geçerse haber ver"), zamanlanmış görevler ("her sabah 9'da ...") ve
   akışlar (bağlı servisleri izleyip haber verenler). Kural/görev düz cümleyle yazılır; ne anlaşıldığı gösterilir,
   onaylanınca kaydedilir. */
function autoPane(pane) {
  pane.append(el("h2", "", "Otomasyon"),
    el("p", "", "ALICE'in kendiliğinden yaptıkları. Düz cümleyle yaz; ne anladığını gösterir, onaylarsan kaydeder. "
      + "Sesle ya da sohbette \"her sabah 9'da ...\" demek de aynı yere yazar."));
  const body = el("div");
  pane.appendChild(body);
  const call = (action, extra) => api("/api/automations/action", Object.assign({ action }, extra || {}));
  const reload = () => api("/api/automations").then((d) => d && draw(d));
  const after = (msg) => (r) => {
    if (!r || !r.ok) return toast((r && r.error) || "olmadı");
    if (msg) toast(msg);
    reload();
  };
  const small = (label, fn, primary) => {
    const b = el("button", primary ? "btn primary" : "btn", label);
    b.addEventListener("click", fn);
    return b;
  };
  // Cümle yaz -> "Anladığım" önizlemesi -> Kaydet
  const composer = (placeholder, examples, parse, preview, save) => {
    const box = el("div", "auto-new");
    const row = el("div", "key-edit");
    const inp = el("input");
    inp.type = "text";
    inp.placeholder = placeholder;
    const go = el("button", "btn primary", "Ekle");
    const out = el("div");
    const run = () => {
      const text = inp.value.trim();
      if (!text) return;
      go.disabled = true;
      go.textContent = "anlıyorum…";
      parse(text).then((r) => {
        go.disabled = false;
        go.textContent = "Ekle";
        out.replaceChildren();
        if (!r || !r.ok) return toast((r && r.error) || "anlaşılamadı");
        const card = el("div", "auto-preview");
        card.appendChild(el("div", "sec", "Anladığım"));
        for (const [k, v] of preview(r.view)) if (v) {
          const line = el("div", "auto-line");
          line.append(el("span", "", k), el("b", "", v));
          card.appendChild(line);
        }
        const btns = el("div", "row");
        btns.append(small("Vazgeç", () => out.replaceChildren()),
          small("Kaydet", () => save(r).then((x) => { if (x && x.ok) { inp.value = ""; out.replaceChildren(); } after("kaydedildi")(x); }), true));
        card.appendChild(btns);
        out.appendChild(card);
      });
    };
    go.addEventListener("click", run);
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });
    row.append(inp, go);
    box.append(row, el("small", "enroll-note", "Örnek: " + examples), out);
    return box;
  };
  const kept = {};  // tazelenince yazılan cümle ve önizleme kaybolmasın
  function draw(d) {
    body.replaceChildren();
    if (!d.core_running) {
      const w = el("div", "list-row warn");
      const l = el("span", "l");
      l.append(el("b", "", "Çekirdek çalışmıyor"), el("small", "", "Kurallar, görevler ve akışlar çekirdek açıkken çalışır."));
      w.append(l, small("Çekirdeğe git", () => showSettings("system")));
      body.appendChild(w);
    }
    // Kurallar
    body.appendChild(el("div", "sec", `Kurallar (${d.rules.length})`));
    kept.rule = kept.rule || composer("Ne olunca haber vereyim?", "\"GPU 85 dereceyi 2 dakika geçerse haber ver\", \"disk %90 dolunca uyar\"",
      (text) => call("rule_parse", { text }),
      (v) => [["Koşul", v.condition], ["Mesaj", v.message], ["Önem", v.severity], ["Modlar", (v.modes || []).join(", ")],
        ["Önce sorulacak", v.confirm], ["ALICE ne yapar", v.agent]],
      (r) => call("rule_add", { rule: r.rule }));
    body.appendChild(kept.rule);
    for (const r of d.rules) {
      const row = el("div", "list-row" + (r.enabled ? "" : " off"));
      const l = el("span", "l");
      l.append(el("b", "", r.text), el("small", "", r.condition + " · " + r.message + (r.agent ? " · ALICE araştırır" : "") + (r.enabled ? "" : " · kapalı")));
      row.append(l, small(r.enabled ? "Kapat" : "Aç", () => call("rule_enable", { id: r.id, on: !r.enabled }).then(after())),
        small("Sil", () => call("rule_rm", { id: r.id }).then(after("kural silindi"))));
      body.appendChild(row);
    }
    // Görevler
    body.appendChild(el("div", "sec", `Zamanlanmış görevler (${d.tasks.length})`));
    kept.task = kept.task || composer("Ne zaman ne yapayım?", "\"her sabah 9'da Discord'daki mesajları özetle\", \"cuma 18:00'de haftalık rapor\"",
      (text) => call("task_parse", { text }),
      (v) => [["Ne zaman", v.when], ["İlk çalışma", v.next], ["Ne yapılacak", v.prompt], ["Kaçırılırsa", v.catch_up ? "açılışta telafi edilir" : "atlanır"]],
      (r) => call("task_add", { task: r.task }));
    body.appendChild(kept.task);
    for (const t of d.tasks) {
      const row = el("div", "list-row auto-task" + (t.enabled ? "" : " off"));
      const l = el("span", "l");
      l.append(el("b", "", t.text), el("small", "", t.when + (t.enabled ? " · sonraki: " + t.next : " · kapalı")
        + " · son: " + (t.last || "hiç") + (t.pending ? " · bir sonraki turda çalışacak" : "")));
      if (t.result) {
        const det = el("details", "auto-result");
        det.append(el("summary", "", "son sonuç"), el("div", "", t.result));
        l.appendChild(det);
      }
      row.append(l, small("Şimdi çalıştır", () => call("task_run", { id: t.id }).then(after(d.core_running ? "bir sonraki çekirdek turunda çalışacak" : "çekirdek açılınca çalışacak"))),
        small(t.enabled ? "Kapat" : "Aç", () => call("task_enable", { id: t.id, on: !t.enabled }).then(after())),
        small("Sil", () => call("task_rm", { id: t.id }).then(after("görev silindi"))));
      body.appendChild(row);
    }
    // Akışlar
    body.appendChild(el("div", "sec", `Akışlar (${d.feeds.length})`));
    const fh = el("div", "list-row");
    const fl = el("span", "l");
    fl.append(el("b", "", "Bağlı servisleri izle"), el("small", "", "Discord'da adın geçince, arkadaşın oyuna girince haber ver gibi. "
      + "Tanımlar " + d.feeds_path + " dosyasında."));
    fh.append(fl, small(d.feeds_file ? "Dosyayı aç" : "Örnek dosya oluştur", () => call("feed_open").then((r) => {
      if (!r || !r.ok) return toast((r && r.error) || "açılamadı");
      toast(r.created ? "örnek dosya oluşturuldu ve açıldı; kaydedince çekirdek kendisi okur" : "dosya açıldı");
      reload();
    })));
    body.appendChild(fh);
    for (const e of d.feed_errors || []) body.appendChild(el("div", "list-row warn", e));
    for (const f of d.feeds) {
      const row = el("div", "list-row" + (f.enabled ? "" : " off"));
      const l = el("span", "l");
      l.append(el("b", "", f.name + (f.enabled ? "" : " (kapalı)")), el("small", "", f.describe + (f.agent ? " · ALICE: " + f.agent : "")));
      const out = el("div", "auto-verdicts");
      const test = small("Dene", () => {
        test.disabled = true;
        test.textContent = "yokluyor…";
        call("feed_test", { name: f.name }).then((r) => {
          test.disabled = false;
          test.textContent = "Dene";
          out.replaceChildren();
          if (!r || !r.ok) { out.appendChild(el("div", "enroll-result bad", (r && r.error) || "olmadı")); return; }
          out.appendChild(el("small", "", `${r.total} öğe, ${r.passed} tanesi koşulu geçiyor (bildirim gönderilmedi)`));
          for (const v of r.items) {
            const it = el("div", "auto-line");
            it.append(el("span", v.ok ? "ok" : "", v.ok ? (v.severity === "critical" ? "!" : "✓") : "✗"), el("b", "", v.title + (v.body ? " — " + v.body : "")), el("small", "", v.reason));
            out.appendChild(it);
          }
        });
      });
      l.appendChild(out);
      row.append(l, test);
      body.appendChild(row);
    }
  }
  reload();
}
/* Ayarlar > Notlar: ALICE'e "not al" dediklerin, sabah özeti ve haftalık rapor (alice notes / brief / report). */
function scheduleRow(title, sub, info, def, weekdaysOpt, prefix, after) {
  const row = el("div", "list-row sched-row");
  const l = el("span", "l");
  l.append(el("b", "", title), el("small", "", info ? `kurulu: ${info.when}${info.enabled ? "" : " (kapalı)"}` : sub));
  const at = el("input");
  at.type = "time";
  at.value = (info && info.at) || def;
  const ctl = el("span", "enroll-act");
  ctl.appendChild(at);
  let wd = null;
  if (weekdaysOpt) {
    wd = el("label", "sched-wd");
    const c = el("input");
    c.type = "checkbox";
    c.checked = !!(info && info.weekdays);
    wd.append(c, document.createTextNode(" hafta içi"));
    wd.box = c;
    ctl.appendChild(wd);
  }
  const save = el("button", info ? "btn" : "btn primary", info ? "Güncelle" : "Kur");
  save.addEventListener("click", () => api("/api/notes/action", { action: prefix + "_install", at: at.value, weekdays: wd ? wd.box.checked : false })
    .then((r) => { toast(r && r.ok ? title + " kuruldu" : (r && r.error) || "kurulamadı"); after(); }));
  ctl.appendChild(save);
  if (info) {
    const rm = el("button", "btn", "Kaldır");
    rm.addEventListener("click", () => api("/api/notes/action", { action: prefix + "_remove" }).then(() => { toast(title + " kaldırıldı"); after(); }));
    ctl.appendChild(rm);
  }
  row.append(l, ctl);
  return row;
}
function notesPane(pane) {
  pane.append(el("h2", "", "Notlar"),
    el("p", "", "ALICE'e \"not al: ...\" dediklerin burada. Sesle \"dün ne not almıştım\" diye de sorabilirsin."));
  const add = el("div", "key-edit auto-new");
  const inp = el("input");
  inp.type = "text";
  inp.placeholder = "Yeni not ya da aramak için yaz";
  const addBtn = el("button", "btn primary", "Not al");
  add.append(inp, addBtn);
  const head = el("div");
  const list = el("div");
  const tools = el("div");
  pane.append(add, head, list, tools);
  let timer = null;
  const load = () => api("/api/notes?q=" + encodeURIComponent(inp.value.trim())).then((d) => {
    if (!d) return;
    head.replaceChildren(el("div", "sec", inp.value.trim() ? `Eşleşen (${d.notes.length})` : `Notlar (${d.total})`));
    list.replaceChildren();
    if (!d.notes.length) list.appendChild(el("p", "", inp.value.trim() ? "Eşleşen not yok; \"Not al\" ile ekleyebilirsin." : "Henüz not yok."));
    for (const n of d.notes) {
      const row = el("div", "list-row");
      const l = el("span", "l");
      l.append(el("b", "", n.text), el("small", "", n.when));
      const del = el("button", "btn", "Sil");
      del.addEventListener("click", () => api("/api/notes/action", { action: "remove", ts: n.ts }).then(() => { toast("not silindi"); load(); }));
      row.append(l, del);
      list.appendChild(row);
    }
    drawTools(d);
  });
  const addNote = () => {
    const text = inp.value.trim();
    if (!text) return;
    api("/api/notes/action", { action: "add", text }).then((r) => {
      if (!r || !r.ok) return toast((r && r.error) || "kaydedilemedi");
      inp.value = "";
      toast("not alındı");
      load();
    });
  };
  addBtn.addEventListener("click", addNote);
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") addNote(); });
  inp.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(load, 250); });
  const out = el("div");
  function drawTools(d) {
    tools.replaceChildren();
    tools.appendChild(el("div", "sec", "Sabah özeti ve haftalık rapor"));
    const show = (label, action, extra) => {
      const b = el("button", "btn", label);
      b.addEventListener("click", () => {
        b.disabled = true;
        const old = b.textContent;
        b.textContent = "hazırlanıyor…";
        api("/api/notes/action", Object.assign({ action }, extra || {})).then((r) => {
          b.disabled = false;
          b.textContent = old;
          if (!r || !r.ok) return toast((r && r.error) || "olmadı");
          out.replaceChildren(el("pre", "mem", r.text));
          if (r.path) {
            const o = el("button", "btn", "Sayfayı aç");
            o.addEventListener("click", () => api("/api/notes/action", { action: "report_show", open: true }));
            out.appendChild(o);
          }
        });
      });
      return b;
    };
    const br = scheduleRow("Sabah özeti", "Her sabah takvim, hava, gece olanlar ve dünden kalan notlar.", d.brief, "08:00", true, "brief", load);
    br.querySelector(".enroll-act").prepend(show("Şimdi göster", "brief_show"));
    const rp = scheduleRow("Haftalık rapor", "Pazar akşamı: sıcaklıklar, disk, en çok bellek yiyenler, uyarılar.", d.report, "20:00", false, "report", load);
    rp.querySelector(".enroll-act").prepend(show("Şimdi göster", "report_show"));
    tools.append(br, rp, out);
  }
  load();
}
/* Ayarlar > Kayıtlar: ALICE'in kime ne gönderdiği, beğenmediğin cevaplar ve test seti (alice log / alice eval). */
function logsPane(pane) {
  pane.append(el("h2", "", "Kayıtlar"),
    el("p", "", "ALICE'in senin adına gönderdikleri ve beğenmediğin cevaplar. Beğenmediğin her cevap test setine eklenir; "
      + "\"Testleri çalıştır\" o cümleleri yeniden sorar, düzelip düzelmediğini gösterir."));
  const body = el("div");
  pane.appendChild(body);
  let timer = null;
  const act = (action, extra) => api("/api/logs/action", Object.assign({ action }, extra || {}));
  const evalBox = el("div", "enroll");
  const drawEval = (r, d) => {
    evalBox.replaceChildren();
    const head = el("div", "enroll-head");
    const l = el("span", "l");
    const last = d && d.last_run ? ` · son çalıştırma ${d.last_run.when}: ${d.last_run.passed}/${d.last_run.total} geçti` : "";
    l.append(el("b", "", "Test seti"), el("small", "", `${d ? d.cases : "?"} vaka${last}`));
    const ctl = el("span", "enroll-act");
    if (r.state === "running") {
      const stop = el("button", "btn", "Durdur");
      stop.addEventListener("click", () => act("eval_stop").then(poll));
      ctl.appendChild(stop);
    } else {
      const add = el("button", "btn", "voice.log'dan ekle");
      add.title = "Son 20 sesli isteğini vaka olarak ekle";
      add.addEventListener("click", () => act("eval_from_log", { n: 20 }).then((x) => { toast(x && x.ok ? `${x.added} vaka eklendi` : "eklenemedi"); refresh(); }));
      const open = el("button", "btn", "Dosyayı aç");
      open.addEventListener("click", () => act("eval_open").then((x) => { if (!x || !x.ok) toast((x && x.error) || "açılamadı"); }));
      const run = el("button", "btn primary", "Testleri çalıştır");
      run.addEventListener("click", () => act("eval_run").then((x) => { if (!x || !x.ok) toast((x && x.error) || "başlamadı"); poll(); }));
      ctl.append(add, open, run);
    }
    head.append(l, ctl);
    evalBox.appendChild(head);
    if (r.state === "running") {
      const meter = el("div", "enroll-meter dl");
      const bar = el("i");
      bar.style.width = Math.round(100 * r.done / Math.max(1, r.total)) + "%";
      meter.appendChild(bar);
      meter.style.marginTop = "12px";
      evalBox.append(meter, el("small", "enroll-note", `${r.done}/${r.total} · şimdi: ${r.current || "…"}`));
    } else if (r.summary) {
      evalBox.appendChild(el("div", "enroll-result " + (r.state === "error" ? "bad" : ""), r.summary));
    }
    for (const x of r.results || []) {
      const it = el("div", "auto-line");
      it.append(el("span", x.passed ? "ok" : "", x.passed ? "✓" : "✗"), el("b", "", x.label),
        el("small", "", `${x.seconds} sn · ${x.model || "?"}` + (x.passed ? "" : " · " + x.reasons.join("; ") + " · cevap: " + x.reply)));
      evalBox.appendChild(it);
    }
    return r.state === "running";
  };
  let last = null;
  function poll() {
    clearTimeout(timer);
    api("/api/logs/eval").then((r) => {
      if (!r || !evalBox.isConnected) return;
      if (drawEval(r, last)) timer = setTimeout(poll, 1000);
      else if (r.state === "done" || r.state === "stopped") refresh();
    });
  }
  function refresh() {
    api("/api/logs").then((d) => {
      if (!d) return;
      last = d;
      body.replaceChildren();
      body.appendChild(el("div", "sec", `Gönderilenler (${d.sent.length})`));
      if (!d.sent.length) body.appendChild(el("p", "", "ALICE henüz kimseye bir şey göndermedi."));
      for (const r of d.sent) {
        const row = el("div", "list-row" + (r.outcome === "sent" ? "" : " warn"));
        const l = el("span", "l");
        l.append(el("b", "", r.summary), el("small", "", `${r.when} · ${r.tool} · ${r.status}` + (r.detail && r.outcome !== "sent" ? " — " + r.detail : "")));
        row.append(el("span", "chk " + (r.outcome === "sent" ? "ok" : "warn"), r.outcome === "sent" ? "✓" : "!"), l);
        body.appendChild(row);
      }
      body.appendChild(el("div", "sec", `Beğenmediğin cevaplar (${d.feedback.length})`));
      if (!d.feedback.length) body.appendChild(el("p", "", "Yok. Sohbette cevabın altındaki 👎 ya da sesle \"bu cevap kötüydü\"."));
      for (const f of d.feedback) {
        const row = el("div", "list-row");
        const l = el("span", "l");
        l.append(el("b", "", f.say || "?"), el("small", "", `${f.when}${f.source ? " · " + f.source : ""} · cevap: ${f.reply}` + (f.note ? " · not: " + f.note : "")));
        row.appendChild(l);
        body.appendChild(row);
      }
      body.appendChild(el("div", "sec", "Test seti"));
      body.appendChild(evalBox);
      drawEval(d.run, d);
      if (d.run.state === "running") poll();
    });
  }
  refresh();
}
/* Ayarlar > Kurulum: sağlayıcıları dene (doctor), modelleri ölç, Google, telefon, DCS, eklentiler, kısayol. */
function setupPane(pane) {
  pane.append(el("h2", "", "Kurulum"),
    el("p", "", "Bir kere yapılan işler ve sorun giderme. Bir şey çalışmıyorsa önce \"Hepsini dene\"."));
  const body = el("div");
  pane.appendChild(body);
  const act = (action, extra) => api("/api/setup/action", Object.assign({ action }, extra || {}));
  const done = (msg) => (r) => { if (!r || !r.ok) toast((r && r.error) || "olmadı"); else if (msg) toast(typeof msg === "function" ? msg(r) : msg); load(); };
  const btn = (label, fn, primary) => { const b = el("button", primary ? "btn primary" : "btn", label); b.addEventListener("click", () => { b.disabled = true; fn(); }); return b; };
  const card = (title, sub, ...acts) => {
    const c = el("div", "enroll");
    const h = el("div", "enroll-head");
    const l = el("span", "l");
    l.append(el("b", "", title), el("small", "", sub));
    const a = el("span", "enroll-act");
    a.append(...acts);
    h.append(l, a);
    c.appendChild(h);
    return c;
  };
  const jobLines = (j) => {
    const box = el("div");
    if (j.state === "running") {
      box.appendChild(el("div", "enroll-meter dl sys-indet")).appendChild(el("i"));
      box.appendChild(el("small", "enroll-note", (j.lines || []).slice(-1)[0] || "başlıyor…"));
    } else if (j.state === "error") box.appendChild(el("div", "enroll-result bad", j.error));
    return box;
  };
  let timer = null;
  const marks = { ok: ["ok", "✓"], warn: ["warn", "!"], off: ["off", "–"], fail: ["fail", "✗"], skip: ["off", "–"] };
  function draw(d) {
    body.replaceChildren();
    // Sağlayıcılar
    body.appendChild(el("div", "sec", "Sağlayıcılar ve özellikler"));
    const doc = d.doctor;
    const c1 = card("Hepsini dene", "Her model sağlayıcısına kısa bir istek atar, karar motorunu dener, özellikleri kontrol eder (alice doctor).",
      btn(doc.state === "running" ? "deneniyor…" : "Hepsini dene", () => act("doctor_run").then(done()), doc.state !== "running"));
    c1.appendChild(jobLines(doc));
    if (doc.state === "done" && doc.result) {
      for (const p of doc.result.providers) {
        const [cls, m] = marks[p.status] || marks.skip;
        const it = el("div", "auto-line");
        it.append(el("span", "chk " + cls, m), el("b", "", p.name), el("small", "", (p.model ? p.model + " · " : "") + p.detail));
        c1.appendChild(it);
      }
      const dl = el("div", "auto-line");
      dl.append(el("span", "chk " + (doc.result.decider_ok ? "ok" : "fail"), doc.result.decider_ok ? "✓" : "✗"), el("b", "", "karar motoru"), el("small", "", doc.result.decider));
      c1.appendChild(dl);
      if (doc.result.image) {
        const im = doc.result.image;
        const [icls, imk] = marks[im.status] || marks.off;
        const il = el("div", "auto-line");
        il.append(el("span", "chk " + icls, imk), el("b", "", "Jev görsel (Box)"), el("small", "", im.detail));
        c1.appendChild(il);
      }
      for (const ch of doc.result.checks) {
        const [cls, m] = marks[ch.status] || marks.off;
        const it = el("div", "auto-line");
        it.append(el("span", "chk " + cls, m), el("b", "", ch.name), el("small", "", ch.detail + (ch.fix && ch.status !== "ok" ? " → " + ch.fix : "")));
        c1.appendChild(it);
      }
    }
    body.appendChild(c1);
    // Modeller
    const mo = d.models;
    const c2 = card("Ücretsiz modelleri ölç", (mo.probed_at ? "son ölçüm: " + mo.probed_at + ". " : "henüz ölçülmedi. ")
      + "Her modeli gerçek bir araç çağrısıyla dener, hızlı ve doğru olanları öne alır (~1-2 dk).",
      ...(mo.auto ? [] : [btn("Otomatik sıraya geç", () => act("models_auto").then(done("roller ölçüme göre sıralanacak")))]),
      btn(mo.state === "running" ? "ölçülüyor…" : "Yeniden ölç", () => act("models_refresh").then(done()), mo.state !== "running"));
    c2.appendChild(jobLines(mo));
    const chains = (mo.state === "done" && mo.result ? mo.result.chains : mo.chains) || {};
    if (mo.state === "done" && mo.result) c2.appendChild(el("div", "enroll-result good", `${mo.result.working}/${mo.result.total} model çalışıyor`));
    for (const [role, refs] of Object.entries(chains)) {
      const it = el("div", "auto-line");
      it.append(el("span", "", { brain: "ana model", fast: "hızlı", vision: "görsel" }[role] || role), el("small", "", (refs || []).slice(0, 5).join(" → ") || "-"));
      c2.appendChild(it);
    }
    body.appendChild(c2);
    // ChatGPT hesabıyla giriş (Codex limiti; ücretsiz plan dahil)
    const cg = d.chatgpt || {};
    const c2b = card(cg.logged_in ? "ChatGPT bağlı ✓" : "ChatGPT ile giriş",
      cg.logged_in ? `${cg.email || "hesap"}${cg.plan ? " · " + cg.plan : ""}. Modeller ölçülünce otomatik sıraya girer; limit dolunca sıradakine geçilir.`
        : "API anahtarı olmadan ChatGPT hesabınla (ücretsiz plan dahil) GPT modelleri. Kullanım planının Codex limitinden düşer. Resmi olarak onaylı değil (OpenCode da aynı yolu kullanıyor).",
      btn(cg.state === "running" ? "tarayıcıda bekleniyor…" : cg.logged_in ? "Hesap ekle" : "Giriş yap",
        () => act("chatgpt_login").then(done()), cg.state !== "running"));
    c2b.appendChild(jobLines(cg));
    if (cg.logged_in) {
      // Limit: Codex'in 5 saatlik ve haftalık penceresi (her cevapta güncellenir, "Limiti yenile" ile şimdi sorulur)
      const lim = cg.limits || {};
      const fmtReset = (t) => { if (!t) return ""; const m = Math.max(0, Math.round((t * 1000 - Date.now()) / 60000));
        return m >= 60 * 24 ? ` · ${Math.round(m / 1440)} gün sonra sıfırlanır` : m >= 60 ? ` · ${Math.floor(m / 60)} sa ${m % 60} dk sonra sıfırlanır` : ` · ${m} dk sonra sıfırlanır`; };
      const winName = (w) => !w.window_minutes ? "limit" : w.window_minutes >= 60 * 24 * 6 ? "haftalık" : w.window_minutes >= 60 ? `${Math.round(w.window_minutes / 60)} saatlik` : `${w.window_minutes} dk'lık`;
      for (const key of ["primary", "secondary"]) {
        const w = lim[key];
        if (!w) continue;
        const it = el("div", "auto-line");
        const bar = el("div", "enroll-meter dl");
        bar.appendChild(el("i")).style.width = Math.min(100, w.used_percent) + "%";
        it.append(el("span", "", winName(w)), el("small", "", `%${Math.round(w.used_percent)} kullanıldı${fmtReset(w.reset_at)}`), bar);
        c2b.appendChild(it);
      }
      const row = el("div", "auto-line");
      row.append(el("small", "", (lim.primary || lim.secondary) ? "" : "Limit henüz bilinmiyor."),
        btn("Limiti yenile", () => act("chatgpt_usage").then(done())));
      c2b.appendChild(row);
      if ((cg.models || []).length) c2b.appendChild(el("small", "enroll-note",
        `Modeller: ${cg.models.join(", ")}. Sohbetin altındaki model menüsünden seç ya da Otomatik bırak.`));
    }
    // Kayıtlı hesaplar: tek tıkla geç ya da kaldır
    for (const a of cg.accounts || []) {
      const it = el("div", "auto-line");
      const acts = el("span", "enroll-act");
      if (!a.active) acts.appendChild(btn("Kullan", () => act("chatgpt_use", { key: a.key }).then(done(`${a.email || "hesap"} kullanılıyor`))));
      acts.appendChild(btn("Kaldır", () => act("chatgpt_logout", { key: a.key }).then(done("hesap kaldırıldı"))));
      it.append(el("span", "chk " + (a.active ? "ok" : "off"), a.active ? "✓" : "–"), el("b", "", a.email || a.key),
        el("small", "", (a.plan || "") + (a.active ? " · kullanılıyor" : "")), acts);
      c2b.appendChild(it);
    }
    if ((cg.accounts || []).length > 1) c2b.appendChild(el("small", "enroll-note",
      "Hesap elle seçilir; limit dolunca kendiliğinden değişmez. Limiti aşmak için hesap değiştirmek OpenAI kurallarına aykırı, hesaplar kapatılabilir."));
    if (cg.state === "done" && cg.lines && cg.lines.length) c2b.appendChild(el("small", "enroll-note", cg.lines.slice(-1)[0]));
    body.appendChild(c2b);
    // Google
    body.appendChild(el("div", "sec", "Google Takvim ve Gmail"));
    const g = d.google;
    const c3 = card(g.client ? "Google istemcisi kayıtlı ✓" : "Adım adım kurulum",
      g.client ? "Takvimi ve Gmail hesaplarını bağlayabilirsin." : "Her adımda \"Aç\" doğru sayfayı açar; yapınca sıradakine geç. Sonunda indirilen dosyayı burada seç.");
    if (!g.client) {
      g.steps.forEach((st, i) => {
        const it = el("div", "auto-line g-step");
        const ob = el("button", "btn", "Aç");
        ob.addEventListener("click", () => act("google_open", { step: i }));
        it.append(el("span", "", `${i + 1}. ${st.title}`), el("small", "", st.how), ob);
        c3.appendChild(it);
      });
      const row = el("div", "key-edit auto-new");
      const inp = el("input");
      inp.type = "text";
      inp.placeholder = "Client ID (….apps.googleusercontent.com) ya da JSON içeriği";
      const sec = el("input");
      sec.type = "password";
      sec.placeholder = "Client secret";
      const save = btn("Kaydet", () => act("google_client", { text: inp.value, secret: sec.value }).then(done("Google istemcisi kaydedildi")), true);
      row.append(inp, sec, save);
      if (g.found_json) c3.appendChild(card("İndirilenlerde bulundu: " + g.found_json, "Google'dan indirdiğin dosya bu mu?",
        btn("Bunu kullan", () => act("google_client", { use_download: true }).then(done("Google istemcisi kaydedildi")), true)));
      c3.appendChild(row);
    } else {
      const row = el("div", "enroll-act g-connect");
      const cal = btn("Takvimi bağla", () => api("/api/connect/auth", { name: "google_calendar" }).then((r) => toast((r && (r.note || r.error)) || "tarayıcıya bak")));
      const acc = el("input");
      acc.type = "text";
      acc.placeholder = "Gmail hesap adı (ör. okul)";
      const gm = btn("Gmail ekle", () => api("/api/assistant/action", { action: "gmail_connect", account: acc.value.trim() })
        .then((x) => { toast(x && x.ok ? "Tarayıcıda izin sayfası açıldı" : (x && x.error) || "başlatılamadı"); gm.disabled = false; }));
      row.append(cal, acc, gm);
      c3.appendChild(row);
    }
    body.appendChild(c3);
    // Telefon
    body.appendChild(el("div", "sec", "Telefon"));
    const ph = d.phone;
    const c4 = card("Telefondan ALICE", "Aynı Wi-Fi'deki telefonun tarayıcısından yazarak sor, projeleri izle. Çekirdek açıkken çalışır.",
      segmented([[true, "Açık"], [false, "Kapalı"]], !!ph.enabled, (on) => act("phone_enable", { on }).then(done(on ? "telefon köprüsü açılıyor" : "telefon köprüsü kapatıldı"))));
    if (ph.enabled) {
      const box = el("div", "phone-box");
      // Resim olarak: satır içi SVG'yi uygulamanın simge kuralı (svg { fill: none; stroke }) boyayıp siliyordu.
      if (ph.qr) { const q = el("img", "phone-qr"); q.alt = "QR kod"; q.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(ph.qr); box.appendChild(q); }
      const info = el("div", "phone-info");
      info.append(el("small", "", "Telefonun kamerasıyla okut ya da adresi aç:"), el("code", "", ph.url));
      if (!ph.qr_lib) info.appendChild(el("small", "", "QR kod için: pip install qrcode"));
      info.appendChild(btn("Yeni anahtar", () => act("phone_new_token").then(done("eski adres artık çalışmaz"))));
      box.appendChild(info);
      c4.appendChild(box);
    }
    body.appendChild(c4);
    // DCS
    body.appendChild(el("div", "sec", "DCS World"));
    const dc = d.dcs;
    const installed = dc.dirs.some((x) => x.installed);
    const c5 = card("Uçuş verisi ve kneeboard", dc.dirs.length
      ? dc.dirs.map((x) => x.path + (x.installed ? " ✓ kurulu" : " · kurulu değil")).join(" · ") + (dc.mission ? " · son görev: " + dc.mission : "")
      : "DCS Saved Games klasörü bulunamadı (DCS bir kez açılmış olmalı).",
      ...(dc.dirs.length ? [btn("Kneeboard yaz", () => act("dcs_kneeboard").then(done((r) => `${r.pages} sayfa yazıldı` + (r.mission ? " (" + r.mission + ")" : "")))),
        btn(installed ? "Kaldır" : "Kur", () => act("dcs_install", { on: !installed }).then(done(installed ? "kaldırıldı" : "kuruldu; DCS'i yeniden başlat")), !installed)] : []));
    body.appendChild(c5);
    // Eklentiler
    body.appendChild(el("div", "sec", `Eklentiler (${d.plugins.items.length})`));
    const pl = d.plugins;
    const name = el("input");
    name.type = "text";
    name.placeholder = "yeni eklenti adı";
    const c6 = card("Kendi araçların", "Python dosyası olarak " + pl.dir + " içinde. Değişiklik ALICE yeniden açılınca geçerli olur.",
      btn("Klasörü aç", () => act("plugin_folder").then(done())));
    const nr = el("div", "key-edit auto-new");
    nr.append(name, btn("Şablon oluştur", () => act("plugin_new", { name: name.value.trim() }).then(done("oluşturuldu ve açıldı"))));
    c6.appendChild(nr);
    for (const p of pl.items) {
      const it = el("div", "auto-line");
      it.append(el("span", "ok", "✓"), el("b", "", p.name + (p.version ? " " + p.version : "")), el("small", "", p.mode + " · " + (p.tools.join(", ") || "-")));
      c6.appendChild(it);
    }
    for (const e of pl.errors) c6.appendChild(el("div", "enroll-result bad", e));
    body.appendChild(c6);
    if (d.windows) {
      body.appendChild(el("div", "sec", "Kısayol"));
      body.appendChild(card("Masaüstü ve Başlat menüsü", "ALICE'i Windows tuşuna basıp \"ALICE\" yazarak aç.",
        btn("Kısayol oluştur", () => act("shortcut").then(done("kısayol oluşturuldu")))));
    }
    return doc.state === "running" || mo.state === "running" || (d.chatgpt || {}).state === "running";
  }
  let shown = false;
  function load() {
    clearTimeout(timer);
    api("/api/setup").then((d) => {
      if (!d || (shown && !body.isConnected)) return;
      shown = true;
      const keep = body.querySelector("input:focus");  // yazarken tazelenip yazılan silinmesin
      if (keep) { timer = setTimeout(load, 1500); return; }
      if (draw(d)) timer = setTimeout(load, 1000);
    });
  }
  load();
}
/* Ayarlar > Modeller > Puanlama: her model zekâ / hız / ilk kelime / akış'ta 0-100 puan alır; rolün ağırlıklarıyla
   genel puan çıkar ve otomatik sıra buna göre kurulur. Ağırlık değişince modeller tekrar ölçülmeden sıra yenilenir. */
const ROLE_NAMES = { brain: "Ana model", voice: "Sesli", fast: "Hızlı işler", vision: "Görsel" };
const METRIC_SHORT = { zeka: "Zekâ", hiz: "Hız", tepki: "İlk kelime", akis: "Akış", baglam: "Bağlam", guven: "Güven" };
const kTok = (n) => !n ? "?" : n >= 1000000 ? Math.round(n / 100000) / 10 + "M" : Math.round(n / 1000) + "K";
let scoreRole = "brain";
const FAIL_WHY = { quota: "kota doldu", rate_limit: "hız sınırı", network: "zaman aşımı / bağlantı", server: "sunucu hatası",
  not_found: "model yok", auth: "anahtar geçersiz", no_tool: "araç çağırmadı", bad_args: "araç parametresi yanlış",
  bad_request: "isteği reddetti", unreachable: "ulaşılamadı", too_large: "istek sığmadı" };
function scorePane() {
  const box = el("div", "score-pane");
  let data = null;
  let timer = null;
  const draw = () => {
    if (!data) return;
    box.replaceChildren();
    box.appendChild(el("div", "sec", "Puanlama"));
    box.appendChild(el("p", "", "Her model altı ölçütte 0-100 puan alır. Rol için hangisinin ne kadar önemli olduğunu sürgülerle "
      + "ayarla; otomatik sıra buna göre kurulur (modeller tekrar ölçülmez)." + (data.probed_at ? " Son ölçüm: " + data.probed_at.replace("T", " ") + "." : "")));
    box.appendChild(segmented(Object.entries(ROLE_NAMES), scoreRole, (r) => { scoreRole = r; draw(); }));
    const w = data.weights[scoreRole];
    const total = Object.values(w).reduce((a, b) => a + b, 0) || 1;
    const sliders = el("div", "score-weights");
    for (const [k, label] of Object.entries(data.metrics)) {
      const row = el("label", "score-w");
      const name = el("span", "score-wn", METRIC_SHORT[k]);
      name.title = label;
      const inp = el("input");
      inp.type = "range"; inp.min = "0"; inp.max = "100"; inp.step = "5"; inp.value = String(w[k]);
      const pct = el("span", "score-wp", Math.round(100 * w[k] / total) + "%");
      inp.addEventListener("input", () => {
        w[k] = Number(inp.value);
        const t = Object.values(w).reduce((a, b) => a + b, 0) || 1;
        sliders.querySelectorAll(".score-wp").forEach((el_, i) => { el_.textContent = Math.round(100 * w[Object.keys(data.metrics)[i]] / t) + "%"; });
        clearTimeout(timer);
        timer = setTimeout(() => api("/api/setup/action", { action: "weights_set", role: scoreRole, weights: w }).then((r) => {
          if (r && r.ok) { data = r; draw(); } else toast((r && r.error) || "kaydedilemedi");
        }), 400);
      });
      row.append(name, inp, pct);
      sliders.appendChild(row);
    }
    const reset = el("button", "btn", "Varsayılana dön");
    reset.addEventListener("click", () => api("/api/setup/action", { action: "weights_set", role: scoreRole, reset: true })
      .then((r) => { if (r && r.ok) { data = r; draw(); toast(ROLE_NAMES[scoreRole] + " ağırlıkları varsayılana döndü"); } }));
    sliders.appendChild(reset);
    box.appendChild(sliders);
    const chain = data.chains[scoreRole] || [];
    // Önce ALICE'in sırası (gerçekte denenme sırası), sonra sırada olmayanlar puana göre, nedeniyle.
    const rank = (r) => { const i = chain.indexOf(r.ref); return i < 0 ? 1e6 : i; };
    const rows = data.rows.filter((r) => r.ok || chain.includes(r.ref)).sort((a, b) => rank(a) - rank(b) || (b.scores[scoreRole] || 0) - (a.scores[scoreRole] || 0));
    if (!rows.length) {
      box.appendChild(el("p", "", "Henüz ölçüm yok: Ayarlar → Kurulum → Ücretsiz modelleri ölç."));
      return;
    }
    const table = el("div", "score-table");
    const head = el("div", "score-row head");
    head.append(el("span", "score-m", "Model"), ...Object.keys(data.metrics).map((k) => el("span", "score-c", METRIC_SHORT[k])), el("span", "score-c total", "Genel"));
    table.appendChild(head);
    for (const r of rows.slice(0, 25)) {
      const row = el("div", "score-row" + (chain.includes(r.ref) ? " in" : ""));
      const m = el("span", "score-m");
      const pos = chain.indexOf(r.ref);
      const why = !r.ok ? `yedek: ölçümde geçici hata (${r.error || "?"}), çalışan modeller bitince denenir`
        : pos < 0 && r.why ? r.why[scoreRole] : "";
      m.append(el("b", "", (pos >= 0 ? pos + 1 + ". " : "") + r.ref.split(":").slice(1).join(":")), el("small", why ? "why" : "", r.ref.split(":")[0]
        + (why ? (r.ok ? " · sırada değil: " : " · ") + why : ` · ${Number(r.latency || 0).toFixed(1)} sn · ilk kelime ${Number(r.ttft || 0).toFixed(1)} sn · ${Math.round(r.tps || 0)} tok/sn · ${kTok(r.context)} bağlam`)));
      if (why) m.title = "Sırada değil: " + why;
      row.appendChild(m);
      for (const k of Object.keys(data.metrics)) {
        const v = r.metrics[k];
        const c = el("span", "score-c");
        const bar = el("i");
        bar.style.width = Math.round(v) + "%";
        const unmeasured = !r.ok && (k === "hiz" || k === "tepki" || k === "akis");  // yedek: ölçüm hatayla bitti
        if (unmeasured) bar.style.width = "0";
        c.append(bar, el("em", "", k === "zeka" && !r.known ? "?" : k === "baglam" ? kTok(r.context)
          : unmeasured || (k === "akis" && !(r.tps > 0)) ? "–" : String(Math.round(v))));
        if (k === "akis" && !(r.tps > 0)) c.title = "Ölçülemedi (cevap tek parçada geldi); orta puan (50) sayıldı";
        if (k === "baglam") c.title = `${kTok(r.context)} token bağlam → ${Math.round(v)} puan`;
        if (k === "zeka") c.title = r.arena ? `LMArena: ${r.arena.name} (Elo ${r.arena.elo})`
          : r.known ? "LMArena'da bulunamadı: elle yazılmış tablodan tahmin" : "Bu modelin zekâ puanı bilinmiyor; varsayılan 45";
        if ((k === "tepki" || k === "akis") && r.live_calls) c.title = `Gerçek kullanımdan (${r.live_calls} çağrı)`;
        if (k === "guven") c.title = "Gerçek kullanımda takılmadan, saçmalamadan cevap verme";
        row.appendChild(c);
      }
      row.appendChild(el("span", "score-c total", r.ok ? String(Math.round(r.scores[scoreRole] || 0)) : "yedek"));
      table.appendChild(row);
    }
    box.appendChild(table);
    // Ölçümde çalışmayanlar: neden listede olmadıkları görünsün (ör. gemini-3.8-flash zaman aşımı, kota).
    const failed = data.rows.filter((r) => !r.ok && !chain.includes(r.ref));
    if (failed.length) {
      const det = el("details", "score-failed");
      det.appendChild(el("summary", "", `${failed.length} model ölçümde çalışmadı, sıraya girmedi (` +
        [...new Set(failed.map((r) => FAIL_WHY[r.error] || r.error || "?"))].join(", ") + ")"));
      for (const r of failed.sort((a, b) => (b.metrics.zeka || 0) - (a.metrics.zeka || 0))) {
        const line = el("div", "score-failed-row");
        line.append(el("b", "", r.ref), el("span", "", " · zekâ " + (r.known ? Math.round(r.metrics.zeka) : "?") + " · "
          + (FAIL_WHY[r.error] || r.error || "?")));
        if (r.error_text) line.title = r.error_text;
        det.appendChild(line);
      }
      box.appendChild(det);
    }
    box.appendChild(el("small", "enroll-note", "Numaralı olanlar bu rolün otomatik sırasında, denenme sırasıyla. Aynı sağlayıcıdan art arda üç model konmaz (biri kotaya takılınca hepsi birden düşmesin), bu yüzden puanı yakın iki model yer değiştirebilir. Zekâ puanı LMArena\'nın gerçek kullanıcı oylarıyla oluşan sıralamasından (genel, kod, talimata uyma, İngilizce dışı, çok turlu; bulunamazsa tahmin); hız ölçümden; ilk kelime ve akış ALICE\'i kullandıkça gerçek çağrılardan (önce ölçümden); güven gerçek kullanımdaki takılma/saçmalamadan; bağlam katalogdan (ALICE'in o sağlayıcıya gönderebildiği sınırla). Bağlam: 8K 20 puan, 32K 60, 128K ve üstü 100."));
  };
  api("/api/models/scores").then((d) => { data = d; draw(); });
  return box;
}
/* Ayarlar > Hafıza: bildiklerini tek tek gör ve sil; eski rolden ("sevgili") kalan izleri temizle. */
function memoryPane(pane, data) {
  const d = data || {};
  pane.append(el("h2", "", "Hafıza"),
    el("p", "", "ALICE'in senin hakkında bildikleri. Yanlış ya da istemediğin bir şeyi sil; sesle de \"bunu unut\" diyebilirsin."));
  if (!d.enabled) { pane.appendChild(el("p", "", "Hafıza kapalı (config.toml [memory] enabled = false).")); return; }
  const old = d.old_role || {};
  const oldTotal = (old.facts || 0) + (old.summaries || 0) + (old.episodes || 0);
  if (oldTotal) {
    const box = el("div", "list-row warn mem-old");
    const l = el("span", "l");
    l.append(el("b", "", "Eski rolden kalan izler"),
      el("small", "", `${old.facts || 0} bilgi, ${old.summaries || 0} özet, ${old.episodes || 0} eski cevap ("bebeğim", "aşkım" gibi). `
        + "Bunlar kaldıkça ALICE eski tavrına dönebiliyor. Örnek: " + (old.examples || []).join(" / ")));
    const b = el("button", "btn primary", "Temizle");
    b.addEventListener("click", () => api("/api/memory/action", { action: "purge_old_role" }).then((r) => {
      toast(r && r.ok ? "Eski rol izleri temizlendi" : "temizlenemedi");
      showSettings("memory");
    }));
    box.append(l, b);
    pane.appendChild(box);
  }
  const facts = d.facts || [];
  pane.appendChild(el("div", "sec", `Bildikleri (${facts.length})`));
  const f = el("input"); f.type = "text"; f.placeholder = "Hafızada ara"; f.className = "set-filter";
  const list = el("div");
  const draw = () => {
    list.replaceChildren();
    const q = f.value.toLocaleLowerCase("tr");
    const shown = facts.filter((x) => !q || x.text.toLocaleLowerCase("tr").includes(q));
    if (!shown.length) list.appendChild(el("p", "", facts.length ? "Eşleşen yok." : "Henüz bir şey kaydedilmedi."));
    for (const x of shown) {
      const row = el("div", "list-row" + (x.old ? " warn" : ""));
      const l = el("span", "l");
      l.append(el("b", "", x.text), el("small", "", "#" + x.id + " · " + x.kind + (x.old ? " · eski rolden" : "")));
      const del = el("button", "btn", "Sil");
      del.addEventListener("click", () => api("/api/memory/action", { action: "forget", id: x.id }).then((r) => {
        if (r && r.ok) { facts.splice(facts.indexOf(x), 1); draw(); }
      }));
      row.append(l, del);
      list.appendChild(row);
    }
  };
  f.addEventListener("input", draw);
  draw();
  pane.append(f, list);
  if ((d.followups || []).length) {
    pane.appendChild(el("div", "sec", "Takipler (sonra sorulacak / hatırlatılacak)"));
    for (const x of d.followups) {
      const row = el("div", "list-row");
      const b = el("button", "btn", "Kapat");
      b.addEventListener("click", () => api("/api/memory/action", { action: "done", id: x.id }).then((r) => { if (r && r.ok) row.remove(); }));
      row.append(el("span", "l", x.text), b);
      pane.appendChild(row);
    }
  }
}
/* Ayarlar > Anahtarlar: API anahtarlarını uygulamanın içinden gir. Değerin tamamı sunucudan hiç gelmez; sadece son 4 hane. */
function keysPane(pane, data) {
  const d = data || {};
  pane.append(el("h2", "", "Anahtarlar"),
    el("p", "", "Anahtarı yapıştırıp Kaydet: hemen kullanılır, yeniden başlatmak gerekmez. Hepsi bu bilgisayarda "
      + (d.path || "~/.alice/.env") + " dosyasında durur, GitHub'a gitmez. En az iki model anahtarı (ör. Gemini + Groq) "
      + "doldurmak, biri kotaya takılınca ALICE'in durmamasını sağlar."));
  const groups = [["model", "Modeller"], ["connect", "Bağlantılar"]];
  for (const [g, title] of groups) {
    pane.appendChild(el("div", "sec", title));
    // İki değer isteyen servis (Cloudflare: token + hesap kimliği) tek kart: aynı başlık iki kez görünmesin.
    const cards = new Map();
    for (const k of (d.keys || []).filter((x) => x.group === g)) {
      const id = k.part_of || k.key;
      if (!cards.has(id)) cards.set(id, []);
      cards.get(id).push(k);
    }
    for (const parts of cards.values()) pane.appendChild(keyRow(parts));
  }
}
function keyRow(parts) {
  const k = parts[0];
  const many = parts.length > 1;
  const all = parts.every((p) => p.set);
  const some = parts.some((p) => p.set);
  const row = el("div", "list-row key-row");
  const mark = el("span", "chk " + (all ? "ok" : "off"), all ? "✓" : some ? "!" : "–");
  const l = el("span", "l");
  const sub = el("small", "");
  const info = many
    ? (some && !all ? "eksik: " + parts.filter((p) => !p.set).map((p) => p.field).join(", ") + " · " : "") + "iki değer ister"
    : k.key + (k.preview ? " · " + k.preview : "") + (k.source === "env" ? " · Windows ortam değişkeninden" : "");
  sub.append(document.createTextNode(info + (k.help ? " · " + k.help : "")));
  if (k.url) {
    const a = el("a", "", " anahtarı al ↗");
    a.title = k.url;
    a.addEventListener("click", (e) => { e.preventDefault(); api("/api/open", { url: k.url }); });
    sub.appendChild(a);
  }
  l.append(el("b", "", k.label), sub);
  row.append(mark, l);
  for (const p of parts) {
    const edit = el("div", "key-edit");
    if (many) {
      const name = el("span", "key-field");
      name.append(el("b", "", p.field), el("small", "", p.key + (p.preview ? " · " + p.preview : "")
        + (p.source === "env" ? " · ortam değişkeninden" : "")));
      edit.appendChild(name);
    }
    const what = many ? p.field : "anahtarı";
    const inp = el("input");
    inp.type = "password";
    inp.autocomplete = "off";
    inp.spellcheck = false;
    inp.placeholder = p.set ? `değiştirmek için yeni ${many ? p.field : "anahtarı"} yapıştır` : `${what} yapıştır`;
    const save = el("button", "btn primary", "Kaydet");
    const label = many ? `${k.label} ${p.field}` : k.label;
    const send = (value, msg) => api("/api/keys/set", { key: p.key, value }).then((r) => {
      if (!r || !r.ok) return toast((r && r.error) || "kaydedilemedi");
      toast(msg);
      showSettings("apikeys");
    });
    save.addEventListener("click", () => { if (inp.value.trim()) send(inp.value.trim(), label + " kaydedildi"); });
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter" && inp.value.trim()) send(inp.value.trim(), label + " kaydedildi"); });
    edit.append(inp, save);
    if (p.source === "file") {
      const del = el("button", "btn", "Sil");
      del.addEventListener("click", () => send("", label + " silindi"));
      edit.appendChild(del);
    }
    row.appendChild(edit);
  }
  return row;
}
/* Ayarlar > Bağlantılar: servislerin durumu, OAuth bağlanma/çıkış ve izleyen akışlar. */
function connectPane(pane, data) {
  const d = data || {};
  pane.append(el("h2", "", "Bağlantılar"),
    el("p", "", "Servisleri ALICE'e bağlar. Anahtarlar ~/.alice/.env dosyasında durur; yeni bir servis eklemek "
      + "için ~/.alice/connect/<ad>.toml (terminalde: alice connect <ad> --new)."));
  for (const e of d.errors || []) pane.appendChild(el("div", "list-row warn", e));
  for (const c of d.connectors || []) {
    const row = el("div", "list-row conn-row");
    const l = el("span", "l");
    l.append(el("b", "", c.title), el("small", "", c.description || ""));
    // Durum kısa etiket; uzun açıklama ("… yok — sayfa → Bot → Reset Token") alta iner. Eskiden uzun durum sağda
    // daralmadan duruyor, açıklamayı sıfır genişliğe itip harf harf alt alta döküyordu.
    const [short, ...more] = String(c.ready ? "hazır" : c.status || "hazır değil").split(" — ");
    if (more.length) l.appendChild(el("small", "conn-help", more.join(" — ")));
    const right = el("span", "conn-act");
    right.appendChild(el("span", "risk " + (c.ready ? "read" : "write"), c.ready ? "hazır" : (c.env ? "anahtar yok" : short)));
    if (c.kind === "oauth2") {
      const b = el("button", "btn", c.ready ? "Çıkış" : "Bağlan");
      b.addEventListener("click", async () => {
        b.disabled = true;
        const r = c.ready ? await api("/api/connect/logout", { name: c.name })
          : await api("/api/connect/auth", { name: c.name });
        toast((r && (r.note || r.error)) || (c.ready ? "Çıkıldı" : "Tarayıcıya bak"));
        showSettings("connect");
      });
      right.appendChild(b);
    } else if (!c.ready && c.env) {
      const b = el("button", "btn", "Anahtar gir");
      b.title = c.env;
      b.addEventListener("click", () => showSettings("apikeys"));
      right.appendChild(b);
    }
    row.append(l, right);
    row.title = (c.actions || []).join(", ") + (c.help ? "\n" + c.help : "");
    pane.appendChild(row);
  }
  pane.appendChild(el("div", "sec", "Akışlar"));
  pane.appendChild(el("p", "", "Bağlı serviste yeni bir şey olunca ALICE haber verir (~/.alice/feeds.toml, "
    + "çekirdek çalışırken yoklanır)."));
  if (!(d.feeds || []).length) pane.appendChild(el("div", "list-row", "Tanımlı akış yok."));
  for (const f of d.feeds || []) {
    const row = el("div", "list-row");
    const l = el("span", "l");
    l.append(el("b", "", f.name), el("small", "", f.describe || `${f.connector}.${f.action}`));
    const st = f.error ? f.error : !f.enabled ? "kapalı" : f.held ? `${f.held} öğe özeti bekliyor` : "açık";
    row.append(l, el("span", "r" + (f.error ? "" : " ok"), st));
    pane.appendChild(row);
  }
  pane.appendChild(el("div", "sec", "Senin adına gönderilenler"));
  pane.appendChild(el("p", "", "Başkasına giden her mesaj, reddettiklerin dahil (terminalde: alice log)."));
  if (!(d.actions || []).length) pane.appendChild(el("div", "list-row", "Henüz bir şey gönderilmedi."));
  for (const a of d.actions || []) pane.appendChild(el("div", "list-row" + (a.outcome === "sent" ? "" : " warn"), a.text));
}

const SETTINGS_TABS = [
  ["general", "Genel", IC.gear], ["apikeys", "Anahtarlar", IC.key], ["voice", "Ses", IC.sound], ["models", "Modeller", IC.spark], ["tools", "Araçlar", IC.bolt],
  ["assistant", "Asistan", IC.shield], ["system", "Çekirdek", IC.term], ["auto", "Otomasyon", IC.list], ["notes", "Notlar", IC.sun], ["logs", "Kayıtlar", IC.check], ["setup", "Kurulum", IC.update],
  ["connect", "Bağlantılar", IC.plug], ["memory", "Hafıza", IC.brain], ["usage", "Kullanım", IC.gauge],
  ["keys", "Kısayollar", IC.keyboard],
];
async function showSettings(tab) {
  tab = tab || "general";
  const data = {
    models: tab === "models" ? await api("/api/models") : null,
    all: tab === "models" ? await api("/api/models/all") : null,
    tools: tab === "tools" ? await api("/api/tools") : null,
    memory: tab === "memory" ? await api("/api/memory") : null,
    usage: tab === "usage" ? await api("/api/usage") : null,
    connect: tab === "connect" ? await api("/api/connect") : null,
    assistant: tab === "assistant" ? await api("/api/assistant") : null,
    apikeys: tab === "apikeys" ? await api("/api/keys") : null,
    voice: tab === "voice" ? await api("/api/voice") : null,
  };
  showDialog((d) => {
    d.classList.add("settings");
    const nav = el("nav", "set-nav");
    nav.appendChild(el("div", "set-h", "Ayarlar"));
    for (const [k, label, icon] of SETTINGS_TABS) {
      const b = el("button", k === tab ? "on" : "");
      b.append(svg(icon), document.createTextNode(label));
      b.addEventListener("click", () => showSettings(k));
      nav.appendChild(b);
    }
    const pane = el("div", "set-pane");
    const x = el("button", "icon-btn set-x");
    x.title = "Kapat (Esc)";
    x.appendChild(svg(IC.x));
    x.addEventListener("click", closeDialog);
    pane.appendChild(x);
    if (tab === "general") {
      pane.append(el("h2", "", "Genel"));
      pane.append(
        setRow("Tema", "Sistem seçilirse Windows'un açık/koyu ayarını izler",
          segmented([["system", "Sistem"], ["light", "Açık"], ["dark", "Koyu"]], PREFS.theme, (v) => setPref("theme", v))),
        setRow("Yazı boyutu", "Sohbet metninin boyutu",
          segmented([["small", "Küçük"], ["normal", "Normal"], ["large", "Büyük"]], PREFS.size, (v) => setPref("size", v))),
        setRow("Araç ayrıntıları", "Komut çıktıları ve okunan dosyalar açık gelsin (Ctrl+O)",
          segmented([["0", "Kapalı"], ["1", "Açık"]], PREFS.verbose ? "1" : "0", (v) => { if ((v === "1") !== PREFS.verbose) toggleVerbose(); })),
        setRow("Yan panel", "Plan, değişiklikler ve bağlam (Ctrl+J)",
          segmented([["0", "Kapalı"], ["1", "Açık"]], PREFS.panel ? "1" : "0", (v) => (v === "1" ? openPanel() : closePanel()))),
        setRow("İzin modu", (MODES[S.status.mode] || ["", ""])[1],
          segmented(Object.entries(MODES).map(([k, [l]]) => [k, l]), S.status.mode, (v) => setMode(v))),
        setRow("Çalışma klasörü", S.status.cwd || "", (() => { const b = el("button", "btn", "Değiştir"); b.addEventListener("click", askCwd); return b; })()),
        setRow("Güncelleme", "Otomatik: açılışta GitHub'daki main'den çeker ve en son sürümle açılır",
          segmented([["auto", "Otomatik"], ["notify", "Haber ver"], ["off", "Kapalı"]], S.status.update_mode || "auto",
            (v) => api("/api/update/mode", { mode: v }).then((r) => { if (r && r.ok) { S.status.update_mode = r.mode; toast("Güncelleme: " + v); } }))),
        setRow("Sürüm", "ALICE " + (S.status.version || ""), (() => { const b = el("button", "btn", "Şimdi denetle"); b.addEventListener("click", checkUpdate); return b; })()),
      );
    } else if (tab === "models") {
      pane.append(el("h2", "", "Modeller"),
        el("p", "", "Her rol soldan sağa denenir. Limite takılan ya da hata veren model atlanır, kısa süre sonra tekrar denenir."));
      pane.appendChild(setRow("Düşünme", (THINK[S.status.thinking] || ["", ""])[1],
        segmented(Object.entries(THINK).map(([k, [l]]) => [k, l]), S.status.thinking, (v) => setThinking(v))));
      const names = { brain: "Ana model", fast: "Hızlı işler", vision: "Görsel" };
      pane.appendChild(scorePane());
      modelsPane(pane, data.all);
      for (const [role, rows] of Object.entries(data.models || {})) {
        pane.appendChild(el("div", "sec", (names[role] || role) + " sırası"));
        for (const r of rows) {
          const row = el("div", "list-row");
          row.append(el("span", "l", r.ref), el("span", "r" + (r.status === "hazır" ? " ok" : ""), r.status));
          pane.appendChild(row);
        }
      }
    } else if (tab === "tools") {
      const tools = Array.isArray(data.tools) ? data.tools : [];
      pane.append(el("h2", "", "Araçlar"), el("p", "", `${tools.length} araç. MCP sunucularını config.toml'daki [mcp.servers] altına ekleyebilirsin.`));
      const f = el("input"); f.type = "text"; f.placeholder = "Araç ara"; f.className = "set-filter";
      const listBox = el("div");
      const draw = () => {
        listBox.replaceChildren();
        const q = f.value.toLowerCase();
        for (const t of tools.filter((t) => !q || (t.name + t.description).toLowerCase().includes(q))) {
          const row = el("div", "list-row");
          const [, icon] = verbFor(t.name);
          const l = el("span", "l");
          l.append(el("b", "", t.name), el("small", "", t.description));
          row.append(svg(icon), l, el("span", "risk " + t.risk, t.risk));
          listBox.appendChild(row);
        }
      };
      f.addEventListener("input", draw);
      draw();
      pane.append(f, listBox);
      setTimeout(() => f.focus(), 0);
    } else if (tab === "assistant") {
      assistantPane(pane, data.assistant);
    } else if (tab === "voice") {
      voicePane(pane, data.voice);
    } else if (tab === "system") {
      systemPane(pane);
    } else if (tab === "auto") {
      autoPane(pane);
    } else if (tab === "setup") {
      setupPane(pane);
    } else if (tab === "notes") {
      notesPane(pane);
    } else if (tab === "logs") {
      logsPane(pane);
    } else if (tab === "apikeys") {
      keysPane(pane, data.apikeys);
    } else if (tab === "connect") {
      connectPane(pane, data.connect);
    } else if (tab === "memory") {
      memoryPane(pane, data.memory);
    } else if (tab === "usage") {
      pane.append(el("h2", "", "Bugünkü kullanım"));
      const entries = Object.entries(data.usage || {});
      if (!entries.length) pane.appendChild(el("p", "", "Bugün henüz istek yok."));
      const max = Math.max(1, ...entries.map(([, r]) => r.requests));
      for (const [ref, r] of entries) {
        const row = el("div", "use-row");
        const bar = el("i", "use-bar");
        const fill = el("i");
        fill.style.width = (100 * r.requests) / max + "%";
        bar.appendChild(fill);
        row.append(el("span", "l", ref), bar,
          el("span", "r", `${r.requests} istek · ${r.errors} hata · ${(r.prompt_tokens / 1000).toFixed(1)}k girdi · ${(r.completion_tokens / 1000).toFixed(1)}k çıktı`));
        pane.appendChild(row);
      }
    } else {
      pane.append(el("h2", "", "Kısayollar ve komutlar"));
      for (const [k, v] of SHORTCUTS) {
        const row = el("div", "list-row");
        const keys = el("span", "keys");
        for (const part of k.split(" / ")) keys.appendChild(el("kbd", "", part));
        row.append(el("span", "l plain", v), keys);
        pane.appendChild(row);
      }
      pane.appendChild(el("div", "sec", "Slash komutları"));
      for (const c of S.commands) {
        const row = el("div", "list-row");
        row.append(el("span", "l", "/" + c.name), el("span", "r", c.description + (c.kind === "custom" ? " (özel)" : "")));
        pane.appendChild(row);
      }
    }
    d.append(nav, pane);
  });
}
const SHORTCUTS = [
  ["Ctrl+K", "Komut paleti"], ["Ctrl+J", "Yan panel (plan, değişiklikler, bağlam)"], ["Ctrl+O", "Araç ayrıntılarını aç/kapat"],
  ["Ctrl+,", "Ayarlar"], ["Enter", "Gönder (çalışırken sıraya ekler)"], ["Shift+Enter", "Yeni satır"],
  ["↑ / ↓", "Önceki mesajları geri getir (kutu boşken)"], ["Esc", "Durdur / menüyü kapat"],
  ["1 / 2 / 3", "İzin kartını cevapla: izin ver / hep izin ver / reddet"], ["Shift+Tab", "İzin modunu değiştir"],
  ["Ctrl+B", "Kenar çubuğu"], ["Ctrl+Shift+O", "Yeni sohbet"], ["Ctrl+V", "Görsel yapıştır"],
];

/* =========================================================== slash komutları */
async function runCommand(text) {
  const [cmd, ...rest] = text.slice(1).split(" ");
  const arg = rest.join(" ").trim();
  switch (cmd.toLowerCase()) {
    case "help": showHelp(); return true;
    case "clear": case "new": newChat(); return true;
    case "compact": {
      if (S.busy) { toast("Önce mevcut işi durdur"); return true; }
      toast("Özetleniyor…");
      const r = await api("/api/compact", {});
      applyStatus(r); toast(r.ok ? "Context özetlendi" : "Özetlenecek bir şey yok");
      loadHistory();
      return true;
    }
    case "mode":
      if (MODES[arg]) setMode(arg); else modeMenu();
      return true;
    case "plan": setMode("plan"); toast("Plan modu: sadece okur, plan yazar"); return true;
    case "model":
      if (arg) pinModel(arg.includes(":") ? arg : "reset"); else modelMenu();
      return true;
    case "think":
      if (THINK[arg]) setThinking(arg); else thinkMenu();
      return true;
    case "rewind": {
      const users = $$(".turn-user");
      const last = users[users.length - 1];
      if (!last) { toast("Geri sarılacak mesaj yok"); return true; }
      last.querySelector(".act[title^='Bu mesajdan']")?.click();
      return true;
    }
    case "usage": showUsage(); return true;
    case "cwd":
      if (arg) { const r = await api("/api/cwd", { path: arg }); r.ok ? applyStatus(r) : toast(r.error); }
      else askCwd();
      return true;
    case "tools": case "mcp": showTools(); return true;
    case "memory": showMemory(); return true;
    default: return false; // /init ve özel komutlar sunucuya gider
  }
}

/* ===================================================================== popup */
function caretToken() {
  const pos = input.selectionStart;
  const before = input.value.slice(0, pos);
  if (/^\/[\w-]*$/.test(before)) return { kind: "cmd", q: before.slice(1), start: 0 };
  const m = before.match(/(^|\s)@([^\s@]*)$/);
  if (m) return { kind: "file", q: m[2], start: pos - m[2].length - 1 };
  return null;
}
let popupSeq = 0;
async function updatePopup() {
  const tok = caretToken();
  if (!tok) return hidePopup();
  const seq = ++popupSeq;
  let items;
  if (tok.kind === "cmd") {
    items = S.commands.filter((c) => c.name.startsWith(tok.q.toLowerCase()))
      .map((c) => ({ label: "/" + c.name, desc: c.description, tag: c.kind === "custom" ? "özel" : "", value: "/" + c.name + " " }));
  } else {
    const files = await api("/api/files?q=" + encodeURIComponent(tok.q));
    if (seq !== popupSeq) return;
    items = (Array.isArray(files) ? files : []).map((f) => ({ label: f, desc: "", tag: f.endsWith("/") ? "klasör" : "", value: "@" + f + " " }));
  }
  if (!items.length) return hidePopup();
  S.popup = { ...tok, items, sel: 0 };
  drawPopup();
}
function drawPopup() {
  const p = $("#popup");
  p.replaceChildren();
  S.popup.items.forEach((it, i) => {
    const row = el("div", "pi" + (i === S.popup.sel ? " sel" : ""));
    row.append(el("b", "", it.label));
    if (it.desc) row.appendChild(el("span", "", it.desc));
    if (it.tag) row.appendChild(el("span", "tag", it.tag));
    row.addEventListener("mousedown", (e) => { e.preventDefault(); choosePopup(i); });
    p.appendChild(row);
  });
  p.hidden = false;
  p.querySelector(".sel")?.scrollIntoView({ block: "nearest" });
}
function hidePopup() { S.popup = null; $("#popup").hidden = true; }
function choosePopup(i) {
  const it = S.popup.items[i];
  const pos = input.selectionStart;
  const v = input.value;
  input.value = v.slice(0, S.popup.start) + it.value + v.slice(pos);
  const caret = S.popup.start + it.value.length;
  input.setSelectionRange(caret, caret);
  hidePopup();
  autosize();
  input.focus();
  if (it.value.endsWith("/ ")) { // klasör: içine girmeye devam et
    input.value = input.value.slice(0, caret - 1) + input.value.slice(caret);
    input.setSelectionRange(caret - 1, caret - 1);
    updatePopup();
  }
}

/* ================================================================== composer */
function autosize() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 300) + "px";
  updateSend();
}
function updateSend() {
  $("#sendBtn").disabled = !S.busy && !input.value.trim() && !S.attachments.length;
}
function drawAttachments() {
  const box = $("#attachments");
  box.replaceChildren();
  S.attachments.forEach((b64, i) => {
    const a = el("div", "att");
    const img = el("img"); img.src = "data:image/png;base64," + b64;
    const x = el("button"); x.title = "Kaldır"; x.appendChild(svg(IC.x));
    x.addEventListener("click", () => { S.attachments.splice(i, 1); drawAttachments(); });
    a.append(img, x);
    box.appendChild(a);
  });
  box.classList.toggle("has", S.attachments.length > 0);
  updateSend();
}
function addImageFile(file) {
  if (!file || !file.type.startsWith("image/")) return;
  const reader = new FileReader();
  reader.onload = () => {
    const img = new Image();
    img.onload = () => {
      // PNG'ye çevir ve 1600 px'e küçült: her sağlayıcı aynı formatı alsın, token israf olmasın.
      const scale = Math.min(1, 1600 / Math.max(img.width, img.height));
      const c = document.createElement("canvas");
      c.width = Math.round(img.width * scale); c.height = Math.round(img.height * scale);
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      S.attachments.push(c.toDataURL("image/png").split(",")[1]);
      drawAttachments();
    };
    img.src = reader.result;
  };
  reader.readAsDataURL(file);
}

async function submit(textArg) {
  const text = (textArg ?? input.value).trim();
  const images = textArg === undefined ? S.attachments.slice() : [];
  if (!text && !images.length) return;
  hidePopup();
  if (text.startsWith("/") && !images.length && (await runCommand(text))) {
    if (textArg === undefined) { input.value = ""; autosize(); }
    return;
  }
  if (textArg === undefined) remember(text);
  if (S.busy) {
    S.queue.push({ text, images });
    drawQueue();
  } else {
    const r = await api("/api/send", { text, images });
    if (!r.ok) {
      // Sesli asistan aynı anda iş başlatmış olabilir: mesaj kaybolmasın, sıraya girsin.
      if (/çalışıyor/.test(r.error || "")) {
        S.queue.push({ text, images }); drawQueue();
        await refreshStatus();
        if (!S.busy) flushQueue();
      }
      else { toast(r.error || "Gönderilemedi"); return; }
    }
  }
  if (textArg === undefined) {
    input.value = ""; S.attachments = []; drawAttachments(); autosize();
  }
}
// Kutu boşken (ya da geri çağrılmış bir mesaj değişmeden duruyorken) oklar önceki mesajlarda gezer.
function recallSent(dir) {
  const cur = S.sentIdx >= 0 ? S.sent[S.sent.length - 1 - S.sentIdx] : "";
  if (input.value !== cur || !S.sent.length) return false;
  const i = S.sentIdx + dir;
  if (i < -1 || i >= S.sent.length) return true;
  S.sentIdx = i;
  input.value = i < 0 ? "" : S.sent[S.sent.length - 1 - i];
  autosize();
  input.setSelectionRange(input.value.length, input.value.length);
  return true;
}
function remember(text) {
  if (!text || text.startsWith("/")) return;
  S.sent = S.sent.filter((t) => t !== text).concat(text).slice(-50);
  S.sentIdx = -1;
}
function drawQueue() {
  const q = $("#queue");
  q.replaceChildren();
  S.queue.forEach((item, i) => {
    const row = el("div", "queued");
    row.append(el("small", "", "Sırada"), el("span", "", item.text));
    const x = el("button", "act"); x.title = "Sıradan çıkar"; x.appendChild(svg(IC.x));
    x.addEventListener("click", () => { S.queue.splice(i, 1); drawQueue(); });
    row.appendChild(x);
    q.appendChild(row);
  });
}
async function flushQueue() {
  if (S.busy || !S.queue.length) return;
  const item = S.queue.shift();
  drawQueue();
  const r = await api("/api/send", item);
  if (!r.ok) toast(r.error || "Sıradaki mesaj gönderilemedi");
}
function stop() { api("/api/stop", {}); S.queue = []; drawQueue(); }

input.addEventListener("input", () => { autosize(); updatePopup(); });
input.addEventListener("click", updatePopup);
input.addEventListener("keydown", (e) => {
  if (S.popup) {
    if (e.key === "ArrowDown") { e.preventDefault(); S.popup.sel = (S.popup.sel + 1) % S.popup.items.length; drawPopup(); return; }
    if (e.key === "ArrowUp") { e.preventDefault(); S.popup.sel = (S.popup.sel - 1 + S.popup.items.length) % S.popup.items.length; drawPopup(); return; }
    if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) { e.preventDefault(); choosePopup(S.popup.sel); return; }
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); hidePopup(); return; }
  }
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
  if ((e.key === "ArrowUp" || e.key === "ArrowDown") && !e.shiftKey && recallSent(e.key === "ArrowUp" ? 1 : -1)) e.preventDefault();
  if (e.key === "Tab" && e.shiftKey) { e.preventDefault(); cycleMode(); }
});
input.addEventListener("paste", (e) => {
  const files = Array.from(e.clipboardData?.files || []).filter((f) => f.type.startsWith("image/"));
  if (files.length) { e.preventDefault(); files.forEach(addImageFile); }
});
$("#attachBtn").addEventListener("click", () => $("#fileInput").click());
$("#fileInput").addEventListener("change", (e) => { Array.from(e.target.files).forEach(addImageFile); e.target.value = ""; });
let dragDepth = 0;
window.addEventListener("dragenter", (e) => { if (e.dataTransfer?.types.includes("Files")) { dragDepth++; $("#dropzone").hidden = false; } });
window.addEventListener("dragleave", () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) $("#dropzone").hidden = true; });
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => {
  e.preventDefault(); dragDepth = 0; $("#dropzone").hidden = true;
  Array.from(e.dataTransfer?.files || []).forEach(addImageFile);
});

$("#sendBtn").addEventListener("click", () => (S.busy && !input.value.trim() ? stop() : submit()));
$("#modeBtn").addEventListener("click", modeMenu);
$("#modelBtn").addEventListener("click", modelMenu);
$("#thinkBtn").addEventListener("click", thinkMenu);
$("#settingsBtn").addEventListener("click", () => showSettings("general"));
$("#cwdBtn").addEventListener("click", askCwd);
$("#newChat").addEventListener("click", newChat);
$$(".sg").forEach((b) => b.addEventListener("click", () => {
  input.value = b.dataset.text; autosize(); input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
  if (!b.dataset.text.endsWith(": ")) submit();
}));
function toggleSide() {
  if (window.innerWidth <= 760) app.classList.toggle("side-open");
  else app.classList.toggle("side-hidden");
  try { localStorage.setItem("alice.side", app.classList.contains("side-hidden") ? "0" : "1"); } catch { /* yok */ }
}
function closeSideOnMobile() { if (window.innerWidth <= 760) app.classList.remove("side-open"); }
$("#collapseSide").addEventListener("click", toggleSide);
$("#openSide").addEventListener("click", toggleSide);
try { if (localStorage.getItem("alice.side") === "0") app.classList.add("side-hidden"); } catch { /* yok */ }

document.addEventListener("keydown", (e) => {
  const mod = e.ctrlKey || e.metaKey;
  if (mod && !e.shiftKey && e.key.toLowerCase() === "k") { e.preventDefault(); $("#palette").hidden ? openPalette() : closePalette(); return; }
  if (mod && e.shiftKey && e.key.toLowerCase() === "p") { e.preventDefault(); openPalette(); return; }
  if (mod && e.key.toLowerCase() === "j") { e.preventDefault(); togglePanel(); return; }
  if (mod && e.key.toLowerCase() === "o" && !e.shiftKey) { e.preventDefault(); toggleVerbose(); return; }
  if (mod && e.key === ",") { e.preventDefault(); showSettings("general"); return; }
  // İzin kartı 1-4 ile cevaplanır; mesaj yazılırken rakamlar metne gider.
  const typing = (document.activeElement === input && input.value) ||
    (document.activeElement && document.activeElement !== input && /INPUT|TEXTAREA/.test(document.activeElement.tagName));
  if (!mod && !e.altKey && !typing && /^[1-6]$/.test(e.key) && $("#overlay").hidden && $("#palette").hidden) {
    const card = pendingPerm();
    if (card && card._choice) {  // seçenekli soru: rakam o seçeneğe tıklar
      const btn = card._acts.querySelectorAll("button")[Number(e.key) - 1];
      if (btn) { e.preventDefault(); btn.click(); return; }
    } else if (card && /^[1234]$/.test(e.key)) {  // kartta görünen sırayla (kalıcı seçenek yoksa 3 = reddet)
      const btn = card._acts.querySelectorAll("button")[Number(e.key) - 1];
      if (btn) { e.preventDefault(); btn.click(); }
      return;
    }
  }
  if (e.key === "Escape") {
    if (!$("#palette").hidden) closePalette();
    else if (!$("#menu").hidden) closeMenu();
    else if (!$("#overlay").hidden) closeDialog();
    else if (S.busy) stop();
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "b") { e.preventDefault(); toggleSide(); }
  if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === "o") { e.preventDefault(); newChat(); }
});

// Kopyala butonları ve linkler (model çıktısı uygulamanın içinde gezinmesin).
list.addEventListener("click", (e) => {
  const cp = e.target.closest("[data-copy='code']");
  if (cp) {
    const code = cp.closest(".codeblock").querySelector("code, pre");
    copyText(code.textContent, cp);
    return;
  }
  const a = e.target.closest("a[href]");
  if (a) { e.preventDefault(); api("/api/open", { url: a.href }); }
});
