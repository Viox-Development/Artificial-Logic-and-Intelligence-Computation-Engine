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
      m.appendChild(el("div", "menu-title", p.name));
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
        el("span", "r", !p.enabled ? `${p.key} yok` : p.hidden ? `gizli · ${p.models.length} model` : `${shown}/${p.models.length} model açık`));
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
    pane.appendChild(el("p", "", "Google kurulumu yapılmamış. Terminalde: alice setup google (adım adım, sayfaları kendisi açar)."));
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
    for (const k of (d.keys || []).filter((x) => x.group === g)) pane.appendChild(keyRow(k));
  }
}
function keyRow(k) {
  const row = el("div", "list-row key-row");
  const mark = el("span", "chk " + (k.set ? "ok" : "off"), k.set ? "✓" : "–");
  const l = el("span", "l");
  const sub = el("small", "");
  sub.append(document.createTextNode(k.key + (k.preview ? " · " + k.preview : "")
    + (k.source === "env" ? " · Windows ortam değişkeninden" : "") + (k.help ? " · " + k.help : "")));
  if (k.url) {
    const a = el("a", "", " anahtarı al ↗");
    a.title = k.url;
    a.addEventListener("click", (e) => { e.preventDefault(); api("/api/open", { url: k.url }); });
    sub.appendChild(a);
  }
  l.append(el("b", "", k.label), sub);
  const edit = el("div", "key-edit");
  const inp = el("input");
  inp.type = "password";
  inp.autocomplete = "off";
  inp.spellcheck = false;
  inp.placeholder = k.set ? "değiştirmek için yeni anahtarı yapıştır" : "anahtarı yapıştır";
  const save = el("button", "btn primary", "Kaydet");
  const send = (value, msg) => api("/api/keys/set", { key: k.key, value }).then((r) => {
    if (!r || !r.ok) return toast((r && r.error) || "kaydedilemedi");
    toast(msg);
    showSettings("apikeys");
  });
  save.addEventListener("click", () => { if (inp.value.trim()) send(inp.value.trim(), k.label + " kaydedildi"); });
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter" && inp.value.trim()) send(inp.value.trim(), k.label + " kaydedildi"); });
  edit.append(inp, save);
  if (k.source === "file") {
    const del = el("button", "btn", "Sil");
    del.addEventListener("click", () => send("", k.label + " silindi"));
    edit.appendChild(del);
  }
  row.append(mark, l, edit);
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
    const right = el("span", "conn-act");
    right.appendChild(el("span", "risk " + (c.ready ? "read" : "write"), c.ready ? "hazır" : c.status));
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
      right.appendChild(el("code", "", c.env));
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
  ["general", "Genel", IC.gear], ["apikeys", "Anahtarlar", IC.key], ["models", "Modeller", IC.spark], ["tools", "Araçlar", IC.bolt],
  ["assistant", "Asistan", IC.shield],
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
    } else if (tab === "apikeys") {
      keysPane(pane, data.apikeys);
    } else if (tab === "connect") {
      connectPane(pane, data.connect);
    } else if (tab === "memory") {
      pane.append(el("h2", "", "Hafıza"), el("p", "", "ALICE'in oturumlar arası kaydettiği notlar (~/.alice/memory.md)."));
      const box = el("div", "prose mem");
      setMd(box, (data.memory && data.memory.text) || "_(henüz bir şey kaydedilmedi)_");
      pane.appendChild(box);
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
