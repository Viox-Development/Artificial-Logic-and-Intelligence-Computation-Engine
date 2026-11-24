"use strict";

/* ALICE — menüler, pencereler, yan panel, komut paleti, ayarlar ve yazma kutusu.
   Sıradan (modül olmayan) betikler: hepsi aynı kapsamı paylaşır, index.html'deki sırayla yüklenir
   (app.js → projects.js → ui.js → main.js). Yükleme anında çalışan kod sadece önceki dosyaları kullanır. */

/* ==================================================================== menüler */
function openMenu(anchor, build, align, below) {
  const m = $("#menu");
  m.replaceChildren();
  build(m);
  m.hidden = false;
  const scale = uiZoom();
  const box = anchor.getBoundingClientRect();
  // getBoundingClientRect ekran (ölçeklenmiş) pikseli verir; menü ise zoomLayer'ın mantıksal koordinatındadır.
  const r = { top: box.top / scale, bottom: box.bottom / scale, left: box.left / scale, right: box.right / scale };
  const vh = window.innerHeight / scale, vw = window.innerWidth / scale;
  const mh = m.offsetHeight, mw = m.offsetWidth;
  let top = below ? r.bottom + 4 : r.top - mh - 6;
  if (below && top + mh > vh - 8) top = r.top - mh - 4;
  if (top < 8) top = r.bottom + 6;
  let left = align === "right" ? r.right - mw : r.left;
  left = Math.max(8, Math.min(left, vw - mw - 8));
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
  xhigh: ["daha da çok", "Extra High: çok zor işler için"],
  max: ["en çok", "Max: en zor işler için, en yavaşı"],
  default: ["model karar verir", "Modelin kendi ayarı"],
};
// Yalnız açık/kapalı destekleyen modeller (Qwen, GLM, Kimi...): seviye yerine kapalı/açık gösterilir.
const THINK_BINARY = { off: ["düşünmez", "Düşünmeden hemen cevap verir"], high: ["düşünür", "Cevaptan önce düşünür"],
  default: ["model karar verir", "Modelin kendi ayarı"] };
// Seçili modelin desteklediği seviyeler (sunucu modele göre verir); bilinmiyorsa hepsi.
function thinkOptions(st) {
  st = st || S.status;
  const table = st.thinking_kind === "binary" ? THINK_BINARY : THINK;
  const levels = st.thinking_levels || Object.keys(THINK);
  return levels.filter((k) => table[k]).map((k) => [k, table[k]]);
}
function thinkLabel(st) {
  st = st || S.status;
  const k = st.thinking_effective || st.thinking;
  const table = st.thinking_kind === "binary" ? THINK_BINARY : THINK;
  return table[k] || THINK[k] || null;
}
async function setThinking(level) {
  applyStatus(await api("/api/thinking", { level }));
  toast("Düşünme: " + ((thinkLabel() || THINK[level] || [level])[0]));
}

function thinkMenu() {
  openMenu($("#thinkBtn"), (m) => {
    m.appendChild(el("div", "menu-title", "Düşünme · " + shortModel(S.status.model)));
    const opts = thinkOptions();
    if (S.status.thinking_kind === "none") {
      m.appendChild(el("div", "menu-note", "Bu model düşünme ayarı desteklemiyor; kendi bildiği gibi çalışır."));
    }
    const cur = S.status.thinking_effective || S.status.thinking;
    for (const [k, [label, sub]] of opts) {
      menuItem(m, k === "off" ? IC.bolt : IC.brain, label[0].toUpperCase() + label.slice(1), sub,
        () => setThinking(k), cur === k);
    }
  }, "right");
}
let _modelsMenuCache = null;
async function modelMenu(anchor) {
  // Önbellekte model listesi varsa menüyü gecikmesiz (0ms) anında aç.
  // Arka planda /api/models/all ile taze veriyi çek.
  const render = (all) => {
    const provs = (all && all.providers) || [];
    const pinned = all ? all.pinned : (S.status.brain_pinned ? S.status.brain_chain[0] : null);
    openMenu(anchor instanceof Element ? anchor : $("#modelBtn"), (m) => {
      m.appendChild(el("div", "menu-title", "Ana model"));
      menuItem(m, IC.spark, "Otomatik", "Zincirdeki ilk uygun model, limit dolunca sıradakine geçer",
        () => pinModel("reset"), !pinned);
      // Sağlayıcıya göre gruplu; gizlenen sağlayıcı/modeller ve anahtarı olmayanlar menüde görünmez.
      for (const p of provs) {
        const rows = p.enabled && !p.hidden ? p.models.filter((x) => !x.hidden) : [];
        if (p.name === "chatgpt" && p.enabled && !p.hidden && !rows.length) {
          // giriş var ama liste henüz yok: boş geçme, nedenini söyle
          m.appendChild(el("div", "menu-title", "chatgpt"));
          menuItem(m, null, p.loading ? "Model listesi okunuyor…" : "Model listesi alınamadı",
            p.loading ? "birkaç saniye sonra menüyü tekrar aç" : (p.error || "").slice(0, 120) + " · Kurulum'dan elle ekleyebilirsin",
            () => showSettings("setup"));
          continue;
        }
        if (!rows.length && p.enabled && !p.hidden && p.error) {
          // Eklendi ama hiçbir modeli cevap vermiyor (kota, bakiye, yetki): sessizce kaybolmasın, nedenini söyle.
          m.appendChild(el("div", "menu-title", provLabel(p)));
          menuItem(m, null, "Modeller çalışmıyor", p.error.slice(0, 160), () => showSettings("models"));
          continue;
        }
        if (!rows.length) continue;
        m.appendChild(el("div", "menu-title", provLabel(p) + (p.note ? " · limit " + p.note : "")));
        for (const x of rows) {
          menuItem(m, null, x.model, x.status === "hazır" ? "" : x.status, () => pinModel(x.ref), pinned === x.ref);
        }
      }
      m.appendChild(el("div", "menu-sep"));
      menuItem(m, IC.list, "Modelleri yönet", "Sağlayıcı ya da model gizle/göster, durumları gör", () => showSettings("models"));
    }, "right", anchor instanceof Element);
  };

  if (_modelsMenuCache) {
    render(_modelsMenuCache);
    api("/api/models/all").then((data) => { if (data) _modelsMenuCache = data; });
  } else {
    const all = await api("/api/models/all");
    if (all) _modelsMenuCache = all;
    render(all);
  }
}
async function hideModel(name, hidden, label) {
  const r = await api("/api/models/hide", { name, hidden });
  if (!r.ok) return toast(r.error || "Kaydedilemedi");
  applyStatus(r);
  toast((hidden ? "Gizlendi: " : "Gösteriliyor: ") + (label || name));
}
/* Modeller: üstte şu an kullanılan model ve düşünme, ortada sağlayıcı kartları (hazır / anahtar bekliyor / gizli),
   en altta katlanmış "Gelişmiş": puanlama ağırlıkları ve rol sıraları. */
const PROV_LABEL = { gemini: "Google Gemini", openrouter: "OpenRouter", groq: "Groq", nvidia: "NVIDIA NIM", cerebras: "Cerebras",
  mistral: "Mistral", sambanova: "SambaNova", cloudflare: "Cloudflare", cohere: "Cohere", vercel: "Vercel AI Gateway",
  zai: "Z.ai", ollama: "Ollama (yerel)", chatgpt: "ChatGPT hesabı", cliproxy: "Claude Code proxy" };
/* Sağlayıcının görünen adı. Proxy modelleri sağlayıcı sağlayıcı gruplanır (Claude, Kimi...): onlar kendi etiketini getirir. */
function provLabel(p) { return p.label || PROV_LABEL[p.name] || p.name; }
// Sağlayıcı kartlarının açık/kapalı durumunu saklar; bir ayar değiştiğinde sayfa yenilense bile
// kullanıcının açık bıraktığı sağlayıcılar hatırlanır, ilk sıradaki zorla açılmaz.
const _modelsOpenSet = new Set();

function modelsPage(pane, data) {
  const all = data.all || { providers: [] };
  pane.append(el("h2", "", "Modeller"),
    el("p", "", "ALICE her iş için en uygun ücretsiz modeli seçer; biri limite takılınca sıradakine geçer. İstersen bir modeli sabitleyebilirsin."));
  // Şu an kullanılan
  const hero = el("div", "mp-hero");
  const cur = S.status.model || "";
  const [prov, ...rest] = cur.split(":");
  const hl = el("div", "mp-cur");
  hl.append(el("small", "", S.status.brain_pinned ? "Sabitlenmiş model" : "Otomatik seçim · şu an"),
    el("b", "", rest.join(":") || cur || "model yok"), el("span", "mp-prov", PROV_LABEL[prov] || prov || ""));
  const ha = el("div", "mp-acts");
  const change = el("button", "btn"); change.append(svg(IC.spark), document.createTextNode("Model seç"));
  change.addEventListener("click", () => modelMenu(change));
  ha.appendChild(change);
  if (S.status.brain_pinned) {
    const auto = el("button", "btn", "Otomatiğe dön");
    auto.addEventListener("click", async () => { await pinModel("reset"); showSettings("models"); });
    ha.appendChild(auto);
  }
  hero.append(hl, ha);
  const think = el("div", "mp-think");
  think.append(el("span", "lbl", "Düşünme"), segmented(thinkOptions().map(([k, [l]]) => [k, l]),
    S.status.thinking_effective || S.status.thinking, (v) => setThinking(v)), el("small", "", (thinkLabel() || ["", ""])[1]));
  hero.appendChild(think);
  pane.appendChild(hero);

  // Sağlayıcılar: Akıllı sıralama
  // 1. Hazır ve açık modelleri olanlar en üstte
  // 2. Hazır ama modelleri hata verenler
  // 3. Anahtar bekleyenler (kapalı olanlar)
  // 4. Kullanıcının gizlediği sağlayıcılar en altta
  const provRank = (p) => {
    if (p.hidden) return 4;
    if (p.enabled && p.models.length && !p.error) return 1;
    if (p.enabled && p.error) return 2;
    if (!p.enabled) return 3;
    return 3;
  };
  const provs = (all.providers || []).slice().sort((a, b) => {
    const rA = provRank(a), rB = provRank(b);
    if (rA !== rB) return rA - rB;
    return (b.models.length - a.models.length) || provLabel(a).localeCompare(provLabel(b));
  });
  const ready = provs.filter((p) => p.enabled && !p.hidden && p.models.length && !p.error).length;
  const sh = el("div", "mp-sec");
  sh.append(el("div", "sec", `Sağlayıcılar · ${ready}/${provs.length} hazır`));
  const f = el("input"); f.type = "search"; f.placeholder = "Model ara";
  sh.appendChild(f);
  pane.appendChild(sh);
  const grid = el("div", "prov-grid");
  pane.appendChild(grid);
  const openSet = _modelsOpenSet;
  const draw = () => {
    grid.replaceChildren();
    const q = f.value.trim().toLowerCase();
    for (const p of provs) {
      const rows = p.models.filter((x) => !q || x.ref.toLowerCase().includes(q));
      if (q && !rows.length) continue;
      const failing = p.enabled && !p.hidden && !p.models.length && !!p.error;
      // 0/0 model açık olan sağlayıcılar hazır görünmemeli
      const hasModels = p.models.length > 0;
      const st = p.hidden ? "hidden" : failing ? "off" : (p.enabled && hasModels) ? "ok" : "off";
      const card = el("div", "prov-card " + st + (openSet.has(p.name) || q ? " open" : ""));
      const head = el("div", "pc-head");
      const name = el("div", "pc-name");
      const shown = p.models.filter((x) => !x.hidden).length;
      name.append(el("b", "", provLabel(p)),
        el("small", "", failing ? "modeller cevap vermiyor: " + p.error.slice(0, 200) :
          st === "ok" ? `${shown}/${p.models.length} model açık` : st === "hidden" ? "gizli" :
          !hasModels && p.enabled ? "model yok" :
          p.name === "chatgpt" ? "giriş yapılmamış" : `${p.key || "anahtar"} yok`));
      const pill = el("span", "pill " + (st === "ok" ? "done" : ""),
        failing ? "hata" : st === "ok" ? "hazır" : st === "hidden" ? "gizli" : !hasModels && p.enabled ? "model yok" : "bekliyor");
      head.append(name, pill);
      card.appendChild(head);
      const acts = el("div", "pc-acts");
      if (!p.enabled && !p.hidden) {
        const add = el("button", "btn", p.name === "chatgpt" ? "Giriş yap" : "Anahtar ekle");
        add.addEventListener("click", () => showSettings(p.name === "chatgpt" ? "setup" : "apikeys"));
        acts.appendChild(add);
      }
      if (p.models.length) {
        const more = el("button", "btn ghost", card.classList.contains("open") ? "Modelleri gizle" : `Modeller (${p.models.length})`);
        more.addEventListener("click", () => { openSet.has(p.name) ? openSet.delete(p.name) : openSet.add(p.name); draw(); });
        acts.appendChild(more);
      }
      const hb = el("button", "btn ghost", p.hidden ? "Göster" : "Gizle");
      hb.title = p.hidden ? "Otomatik zincire geri al" : "Otomatik zincirde ve model menüsünde kullanılmasın";
      hb.addEventListener("click", async () => { await hideModel(p.name, !p.hidden, provLabel(p)); showSettings("models"); });
      acts.appendChild(hb);
      card.appendChild(acts);
      if (card.classList.contains("open") && rows.length) {
        const list = el("div", "pc-models");
        for (const x of rows) {
          const row = el("div", "pc-model" + (x.hidden ? " off" : "") + (all.pinned === x.ref ? " pinned" : ""));
          const l = el("span", "l");
          l.append(el("span", "m", x.model), el("small", "", x.tested ? x.status : (x.status === "hazır" ? "hazır · ölçülmedi" : x.status)));
          const q = el("span", "q"); q.title = "Kalite puanı"; q.style.setProperty("--q", (x.quality || 0) + "%");
          const pin = el("button", "icon-btn" + (all.pinned === x.ref ? " on" : ""));
          pin.title = all.pinned === x.ref ? "Sabitlemeyi kaldır" : "Bu modeli sabitle";
          pin.appendChild(svg(IC.pin));
          pin.addEventListener("click", async () => { await pinModel(all.pinned === x.ref ? "reset" : x.ref); showSettings("models"); });
          const tb = switchEl(!x.hidden, x.hidden ? "Göster" : "Gizle", async (on) => { await hideModel(x.ref, !on); showSettings("models"); });
          row.append(l, q, pin, tb);
          list.appendChild(row);
        }
        card.appendChild(list);
      }
      grid.appendChild(card);
    }
    if (!grid.children.length) grid.appendChild(el("p", "", "Eşleşen model yok."));
  };
  f.addEventListener("input", draw);
  draw();

  // Gelişmiş
  const adv = el("details", "mp-adv");
  adv.appendChild(el("summary", "", "Gelişmiş: puanlama ağırlıkları ve rol sıraları"));
  adv.appendChild(scorePane());
  const names = { brain: "Ana model", fast: "Hızlı işler", vision: "Görsel", voice: "Sesli" };
  const chains = el("div", "mp-chains");
  for (const [role, rows] of Object.entries(data.models || {})) {
    const c = el("div", "mp-chain");
    c.appendChild(el("b", "", (names[role] || role) + " sırası"));
    const ol = el("ol");
    for (const r of rows) {
      const li = el("li", r.status === "hazır" ? "ok" : "");
      li.append(el("span", "", r.ref), el("small", "", r.status));
      ol.appendChild(li);
    }
    c.appendChild(ol);
    chains.appendChild(c);
  }
  adv.appendChild(chains);
  pane.appendChild(adv);
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
/* Klasör seçici: elle yol yazmadan gez ve seç. Solda hızlı yerler ve son kullanılanlar, sağda klasörün alt
   klasörleri; üstte tıklanabilir yol. GitHub'dan açma ikinci sekmede. pickFolder sadece yolu döndürür
   (proje penceresi de kullanır); askCwd seçileni çalışma klasörü yapar. */
function pickFolder(opts) {
  opts = opts || {};
  return new Promise((resolve) => {
    showDialog((d) => {
      d.classList.add("fp");
      onDialogClose = () => resolve(null);
      const head = el("div", "fp-head");
      head.append(el("h2", "", opts.title || "Klasör seç"));
      const tabs = el("div", "seg fp-tabs");
      const tFs = el("button", "on", "Bilgisayar"), tGh = el("button", "", "GitHub");
      if (opts.github) tabs.append(tFs, tGh);
      head.append(el("span", "grow"), tabs);
      const body = el("div", "fp-body");
      const side = el("nav", "fp-side");
      const main = el("div", "fp-main");
      const crumbs = el("div", "fp-crumbs");
      const tools = el("div", "fp-tools");
      const filter = el("input"); filter.type = "text"; filter.placeholder = "Bu klasörde ara";
      const hidden = el("label", "chk"); const hcb = el("input"); hcb.type = "checkbox";
      hidden.append(hcb, document.createTextNode(" Gizlileri göster"));
      tools.append(filter, hidden);
      const list = el("div", "fp-list");
      const err = el("div", "note err");
      main.append(crumbs, tools, list, err);
      body.append(side, main);
      const foot = el("div", "fp-foot");
      const cur = el("div", "fp-cur");
      const mk = el("button", "btn"); mk.append(svg(IC.plus), document.createTextNode("Yeni klasör"));
      const no = el("button", "btn", "Vazgeç");
      const ok = el("button", "btn primary", opts.okLabel || "Bu klasörü seç");
      foot.append(cur, el("span", "grow"), mk, no, ok);
      const gh = el("div", "fp-gh"); gh.hidden = true;
      d.append(head, body, gh, foot);
      let state = null;
      const go = async (path) => {
        err.textContent = "";
        const r = await api("/api/dirs?path=" + encodeURIComponent(path || "") + (hcb.checked ? "&hidden=1" : ""));
        if (!r || !r.ok) { err.textContent = (r && r.error) || "açılamadı"; return; }
        state = r; filter.value = ""; draw();
      };
      const place = (icon, label, path, sub) => {
        const b = el("button", "fp-place" + (state && state.path === path ? " on" : ""));
        b.title = path;
        b.append(svg(icon), el("span", "t", label));
        if (sub) b.appendChild(el("small", "", sub));
        b.addEventListener("click", () => go(path));
        return b;
      };
      const short = (p) => p.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || p;
      const draw = () => {
        side.replaceChildren(el("div", "fp-sec", "Yerler"));
        for (const pl of state.places || []) side.appendChild(place(IC[pl.icon] || IC.folder, pl.label, pl.path));
        if ((state.recent || []).length) {
          side.appendChild(el("div", "fp-sec", "Son kullanılanlar"));
          for (const r of state.recent) side.appendChild(place(IC.clock, short(r), r));
        }
        crumbs.replaceChildren();
        const up = el("button", "icon-btn"); up.title = "Üst klasör"; up.appendChild(svg(IC.up));
        up.disabled = !state.parent; up.addEventListener("click", () => go(state.parent));
        crumbs.appendChild(up);
        state.crumbs.forEach((c, i) => {
          const prevName = i ? state.crumbs[i - 1].name.replace(/[\\/]+$/, "") : "x";
          if (i && prevName) crumbs.appendChild(el("span", "sep", "/"));
          const b = el("button", "crumb" + (i === state.crumbs.length - 1 ? " on" : ""), c.name.replace(/[\\/]+$/, "") || "/");
          b.addEventListener("click", () => go(c.path));
          crumbs.appendChild(b);
        });
        cur.replaceChildren(svg(state.git ? IC.branch : IC.folder), el("span", "", state.path));
        drawList();
      };
      const drawList = () => {
        const f = filter.value.trim().toLocaleLowerCase("tr");
        const rows = state.dirs.filter((x) => !f || x.name.toLocaleLowerCase("tr").includes(f));
        list.replaceChildren();
        if (!rows.length) list.appendChild(el("div", "fp-empty", f ? "Eşleşen klasör yok" : "Alt klasör yok. Bu klasörü seçebilir ya da yeni klasör oluşturabilirsin."));
        for (const x of rows) {
          const b = el("button", "fp-row");
          b.append(svg(IC.folder), el("span", "t", x.name));
          if (x.git) b.appendChild(el("span", "tag", "git"));
          b.appendChild(svg(IC.chev, "chev"));
          b.title = "Aç (çift tıkla: seç)";
          b.addEventListener("click", () => go(x.path));
          b.addEventListener("dblclick", (e) => { e.preventDefault(); finish(x.path); });
          list.appendChild(b);
        }
      };
      const finish = (path) => { onDialogClose = null; closeDialog(); resolve(path); };
      filter.addEventListener("input", drawList);
      filter.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { const first = list.querySelector(".fp-row"); if (first) first.click(); }
        if (e.key === "Backspace" && !filter.value && state.parent) go(state.parent);
      });
      hcb.addEventListener("change", () => go(state.path));
      no.addEventListener("click", closeDialog);
      ok.addEventListener("click", () => state && finish(state.path));
      mk.addEventListener("click", () => {
        const row = el("div", "fp-new");
        const inp = el("input"); inp.type = "text"; inp.placeholder = "Yeni klasörün adı";
        const save = el("button", "btn primary", "Oluştur");
        const create = async () => {
          const r = await api("/api/dirs/mkdir", { parent: state.path, name: inp.value });
          if (!r || !r.ok) { err.textContent = (r && r.error) || "olmadı"; return; }
          go(r.path);
        };
        save.addEventListener("click", create);
        inp.addEventListener("keydown", (e) => { if (e.key === "Enter") create(); if (e.key === "Escape") { e.stopPropagation(); row.remove(); } });
        row.append(inp, save);
        list.prepend(row);
        setTimeout(() => inp.focus(), 0);
      });
      if (opts.github) {
        const show = (g) => {
          tFs.classList.toggle("on", !g); tGh.classList.toggle("on", g);
          body.hidden = g; gh.hidden = !g; ok.hidden = g; mk.hidden = g; cur.hidden = g;
          if (g && !gh.childElementCount) gh.appendChild(githubPicker(err));
        };
        tFs.addEventListener("click", () => show(false));
        tGh.addEventListener("click", () => show(true));
      }
      go(opts.start || S.status.cwd || "");
      setTimeout(() => filter.focus(), 50);
    });
  });
}
async function askCwd() {
  const path = await pickFolder({ title: "Çalışma klasörü", github: true, okLabel: "Burada çalış" });
  if (!path) return;
  const r = await api("/api/cwd", { path });
  if (!r.ok) return toast(r.error || "Klasör değişmedi");
  applyStatus(r); toast("Klasör: " + path);
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
  // Yan panel sohbetin parçası: bir bölüm sayfasındayken önce sohbete dönülür.
  if (app.classList.contains("in-page") && app.dataset.sec !== "projects") showChat();
  if (tab) PREFS.tab = tab;
  PREFS.panel = true;
  savePrefs();
  app.classList.add("panel-open");
  renderPanel();
  if (PREFS.tab === "changes") refreshChanges();
  browserLoop();
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
  app.classList.toggle("panel-browser", PREFS.tab === "browser");
  if (PREFS.tab === "browser") { renderBrowserPane(); return; }
  if (PREFS.tab === "prs") { renderPrPane(); return; }
  if (PREFS.tab === "agents") { renderAgentsPane(); return; }
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
  act("Git", IC.list, "Projeler", "Ajan takımları", () => goSection("projects"));
  act("Git", IC.check, "Görevler", "Arka planda yürüttüğü sorumluluklar ve onay kutusu", () => goSection("tasks"));
  act("Git", IC.bolt, "Otomasyonlar", "Kurallar, zamanlanmış görevler, notlar, kayıtlar", () => goSection("auto"));
  act("Git", IC.term, "Sistem", "Çekirdek, asistan, ses, kurulum, kullanım", () => goSection("system"));
  act("Git", IC.plug, "Bağlantılar", "Servisler ve API anahtarları", () => goSection("connect"));
  act("Eylemler", IC.keyboard, "Klavye kısayolları", "", () => showSettings("keys"));
  for (const [k, label] of [["system", "Sistem"], ["light", "Açık"], ["dark", "Koyu"]]) {
    act("Tema", k === "dark" ? IC.moon : IC.sun, `Tema: ${label}`, "", () => setPref("theme", k));
  }
  for (const [k, [label, sub, icon]] of Object.entries(MODES)) act("İzin modu", icon, `Mod: ${label}`, sub, () => setMode(k));
  for (const [k, [label, sub]] of thinkOptions()) act("Düşünme", IC.brain, `Düşünme: ${label}`, sub, () => setThinking(k));
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
/* Sistem > Asistan: özelliklerin durumu, güvenlik/karşılama ayarları, izin kuralları, Gmail hesapları, skill'ler. */
/* Sistem > Sağlık: özelliklerin durumu (alice doctor'ın kontrol listesi) ve sağlayıcıları tek tuşla deneme. Sorunlar
   en üstte, çalışanlar altta, kapalı olanlar en sonda ve katlanmış. */
/* Ayarlar > Araçlar: ALICE'in kullanabildiği araçlar, ne işe yaradıklarına göre gruplu. Risk rozeti: okur / yazar /
   komut çalıştırır / ekranı kullanır / dışarı gönderir. */
const TOOL_CATS = [
  ["Dosya ve kod", /^(read_file|write_file|edit_file|multi_edit|list_dir|glob|grep|file_ops|notebook_edit|apply_patch|git|github|find_files)$/],
  ["Komut", /^(run_command|process_output|process_stop|blender|sandbox)/],
  ["Web", /^(web_|http_request|browser)/],
  ["Bilgisayar ve ekran", /^(computer|windows|ui_|clipboard|reflex|system_info|process_list|process_kill|pc|screen|vision)/],
  ["Ajanlar ve plan", /^(todo|task|team|council|run_plan|ask_user|skill|load_tools|project|agent)/],
  ["Hafıza ve otomasyon", /^(memory|feedback|rules|schedule|feeds|notes|report|metrics|action_log|weather)/],
  ["Ev, telefon, medya", /^(wifi|android|tv|samsung|media|youtube|spotify|discord|steam|phone|modem|home|whatsapp|gmail|calendar)/],
  ["Oyun ve uçuş", /^(flight|dcs|war|sim|game)/],
  ["MCP Araçları", /^mcp__/],
];
const RISK_LABEL = { read: "okur", write: "yazar", exec: "komut", computer: "ekran", network: "ağ", send: "gönderir", danger: "tehlikeli" };
function toolCat(name) {
  if (name.startsWith("mcp__")) return "MCP Araçları";
  for (const [c, re] of TOOL_CATS) if (re.test(name)) return c;
  return "Diğer";
}
function toolsPage(pane, tools) {
  pane.append(el("h2", "", "Araçlar"),
    el("p", "", `ALICE'in kullanabildiği ${tools.length} araç. MCP sunucularını config.toml'daki [mcp.servers] altına ekleyebilirsin.`));
  const bar = el("div", "tl-bar");
  const f = el("input"); f.type = "search"; f.placeholder = "Araç ara";
  bar.appendChild(f);
  const chips = el("div", "tl-chips");
  const cats = ["Hepsi", ...TOOL_CATS.map(([c]) => c), "Diğer"].filter((c) => c === "Hepsi" || tools.some((t) => toolCat(t.name) === c));
  let cur = "Hepsi";
  for (const c of cats) {
    const n = c === "Hepsi" ? tools.length : tools.filter((t) => toolCat(t.name) === c).length;
    const b = el("button", "chip-btn" + (c === cur ? " on" : ""));
    b.append(document.createTextNode(c), el("span", "n", String(n)));
    b.addEventListener("click", () => { cur = c; chips.querySelectorAll(".chip-btn").forEach((x) => x.classList.toggle("on", x === b)); draw(); });
    chips.appendChild(b);
  }
  pane.append(bar, chips);
  const box = el("div");
  pane.appendChild(box);
  const draw = () => {
    box.replaceChildren();
    const q = f.value.toLowerCase();
    const rows = tools.filter((t) => (cur === "Hepsi" || toolCat(t.name) === cur) && (!q || (t.name + t.description).toLowerCase().includes(q)));
    const groups = new Map();
    for (const t of rows) { const c = toolCat(t.name); if (!groups.has(c)) groups.set(c, []); groups.get(c).push(t); }
    for (const [c, list] of groups) {
      if (cur === "Hepsi") box.appendChild(el("div", "sec", `${c} · ${list.length}`));
      // MCP Araçları çok fazla olduğunda sunucu bazında alt gruplara ayır
      if (c === "MCP Araçları" && list.length > 6) {
        const byServer = new Map();
        for (const t of list) {
          const parts = t.name.split("__");
          const srv = parts.length >= 3 ? parts[1] : "mcp";
          if (!byServer.has(srv)) byServer.set(srv, []);
          byServer.get(srv).push(t);
        }
        for (const [srv, srvTools] of byServer) {
          const det = el("details", "tl-mcp-group");
          // Arama yapılıyorsa veya filtre aktifse otomatik açık olsun
          if (q || cur === "MCP Araçları") det.open = true;
          const sum = el("summary", "", `${srv.toUpperCase()} (${srvTools.length} araç)`);
          det.appendChild(sum);
          const grid = el("div", "tool-grid");
          for (const t of srvTools) {
            const card = el("div", "tool-card");
            const [, icon] = verbFor(t.name);
            const h = el("div", "tc-head");
            const displayName = t.name.replace(/^mcp__.*?__/, "");
            h.append(svg(icon), el("b", "", displayName), el("span", "risk " + t.risk, RISK_LABEL[t.risk] || t.risk));
            const desc = el("p", "", t.description);
            desc.title = t.name + ": " + t.description;
            card.append(h, desc);
            grid.appendChild(card);
          }
          det.appendChild(grid);
          box.appendChild(det);
        }
      } else {
        const grid = el("div", "tool-grid");
        for (const t of list) {
          const card = el("div", "tool-card");
          const [, icon] = verbFor(t.name);
          const h = el("div", "tc-head");
          h.append(svg(icon), el("b", "", t.name), el("span", "risk " + t.risk, RISK_LABEL[t.risk] || t.risk));
          const desc = el("p", "", t.description);
          desc.title = t.description;
          card.append(h, desc);
          grid.appendChild(card);
        }
        box.appendChild(grid);
      }
    }
    if (!rows.length) box.appendChild(el("p", "", "Eşleşen araç yok."));
  };
  f.addEventListener("input", draw);
  draw();
  setTimeout(() => f.focus(), 0);
}
function healthPane(pane, data) {
  const d = data || {};
  const checks = d.checks || [];
  const by = (st) => checks.filter((c) => c.status === st);
  const bad = [...by("fail"), ...by("warn")], ok = by("ok"), off = by("off");
  pane.append(el("h2", "", "Sağlık"),
    el("p", "", "ALICE'in parçaları çalışıyor mu? Sorun olanlar en üstte, yanında nasıl düzeltileceği yazar."));
  const tiles = el("div", "hl-tiles");
  for (const [n, label, cls] of [[bad.length, "dikkat istiyor", bad.length ? "warn" : "ok"], [ok.length, "çalışıyor", "ok"], [off.length, "kapalı", "off"]]) {
    const t = el("div", "hl-tile " + cls);
    t.append(el("b", "", String(n)), el("small", "", label));
    tiles.appendChild(t);
  }
  pane.appendChild(tiles);
  const icon = { ok: IC.check, warn: IC.bolt, fail: IC.x, off: IC.x };
  const row = (c) => {
    const r = el("div", "hl-row " + c.status);
    const ic = el("span", "hl-ic"); ic.appendChild(svg(icon[c.status] || IC.check));
    const l = el("span", "l");
    l.append(el("b", "", c.name), el("small", "", c.detail));
    r.append(ic, l);
    if (c.fix && c.status !== "ok") r.appendChild(el("span", "hl-fix", c.fix));
    return r;
  };
  if (bad.length) { pane.appendChild(el("div", "sec", "Dikkat istiyor")); const b = el("div", "hl-list"); bad.forEach((c) => b.appendChild(row(c))); pane.appendChild(b); }
  if (ok.length) { pane.appendChild(el("div", "sec", "Çalışıyor")); const b = el("div", "hl-list"); ok.forEach((c) => b.appendChild(row(c))); pane.appendChild(b); }
  if (off.length) {
    const det = el("details", "hl-off");
    det.appendChild(el("summary", "", `Kapalı (${off.length})`));
    const b = el("div", "hl-list"); off.forEach((c) => b.appendChild(row(c))); det.appendChild(b);
    pane.appendChild(det);
  }
  // Sağlayıcıları dene (alice doctor)
  const sec = el("div", "sec", "Model sağlayıcılarını dene");
  const box = el("div", "hl-doc");
  pane.append(sec, box);
  const marks = { ok: "ok", warn: "warn", off: "off", fail: "fail", skip: "off" };
  let timer = null;
  const drawDoc = (doc) => {
    box.replaceChildren();
    const head = el("div", "hl-doc-head");
    const l = el("span", "l");
    l.append(el("b", "", "Hepsini dene"), el("small", "", doc.state === "running" ? ((doc.lines || []).slice(-1)[0] || "başlıyor…")
      : "Her sağlayıcıya kısa bir istek atar, karar motorunu dener (alice doctor)."));
    const b = el("button", "btn primary", doc.state === "running" ? "Deneniyor…" : doc.result ? "Yeniden dene" : "Hepsini dene");
    b.disabled = doc.state === "running";
    b.addEventListener("click", () => api("/api/setup/action", { action: "doctor_run" }).then(poll));
    head.append(l, b);
    box.appendChild(head);
    if (doc.state === "running") box.appendChild(el("div", "enroll-meter dl sys-indet")).appendChild(el("i"));
    if (doc.state === "error") box.appendChild(el("div", "enroll-result bad", doc.error));
    if (doc.state === "done" && doc.result) {
      const g = el("div", "hl-provs");
      for (const p of doc.result.providers || []) {
        const c = el("div", "hl-prov " + (marks[p.status] || "off"));
        c.append(el("i", "dot"), el("b", "", p.name), el("small", "", (p.model ? p.model + " · " : "") + p.detail));
        g.appendChild(c);
      }
      const dc = el("div", "hl-prov " + (doc.result.decider_ok ? "ok" : "fail"));
      dc.append(el("i", "dot"), el("b", "", "karar motoru"), el("small", "", doc.result.decider));
      g.appendChild(dc);
      box.appendChild(g);
    }
  };
  function poll() {
    clearTimeout(timer);
    api("/api/setup").then((s) => {
      if (!s || !box.isConnected) return;
      drawDoc(s.doctor || {});
      if (s.doctor && s.doctor.state === "running") timer = setTimeout(poll, 1000);
    });
  }
  poll();
}
/* KAIROS: arka plan nöbeti. Aç/kapa, sıradaki uyanış, şimdi uyandır ve son uyanışların özeti. */
function kairosBlock(pane) {
  const box = el("div");
  pane.append(el("div", "sec", "KAIROS · arka plan nöbeti"), box);
  const when = (ts) => new Date(ts * 1000).toLocaleString("tr-TR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  const draw = (k) => {
    if (!k) { box.replaceChildren(el("p", "", "Durum okunamadı.")); return; }
    const wake = el("button", "btn", "Şimdi uyandır");
    wake.disabled = !k.enabled;
    wake.addEventListener("click", () => api("/api/kairos", { action: "wake" }).then((r) => {
      if (r && r.ok) { toast("KAIROS çekirdeğin bir sonraki turunda uyanacak"); draw(r); }
    }));
    const sub = k.enabled
      ? (k.next ? "Sıradaki uyanış " + when(k.next) : "İlk uyanış birkaç dakika içinde") + ` · bugün ${k.wakes || 0} uyanış` +
        (k.skipped ? ` · son erteleme: ${k.skipped}` : "")
      : "Kapalıyken ALICE yalnız sen sorunca çalışır";
    box.replaceChildren(
      setRow("Nöbet", "ALICE kendi seçtiği aralıklarla uyanır, durumu kollar; yalnız gerçekten faydalı bir şey varsa haber verir. Yalnız okur, değişiklik yapmaz.",
        segmented([["1", "Açık"], ["0", "Kapalı"]], k.enabled ? "1" : "0",
          (x) => api("/api/kairos", { action: x === "1" ? "on" : "off" }).then((r) => { if (r && r.ok) { toast("KAIROS " + (x === "1" ? "açıldı" : "kapatıldı")); draw(r); } }))),
      setRow("Durum", sub, wake));
    const hist = el("div", "kairos-hist");
    for (const h of k.history || []) {
      const row = el("div", "list-row");
      const l = el("span", "l");
      l.append(el("b", "", when(h.ts)), el("small", "", (h.summary || "") + (h.sleep ? ` · ${h.sleep} dk sonra uyanmayı seçti` : "")));
      row.appendChild(l);
      hist.appendChild(row);
    }
    if ((k.history || []).length) box.appendChild(hist);
  };
  api("/api/kairos").then(draw).catch(() => draw(null));
}

function assistantPane(pane, data) {
  const d = data || {};
  const v = d.settings || {};
  const set = (section, key, value, label) =>
    api("/api/assistant/set", { section, key, value }).then((r) => toast(r && r.ok ? label + " kaydedildi" : "kaydedilemedi"));
  const onOff = (section, key, label) => segmented([["1", "Açık"], ["0", "Kapalı"]], v[section + "." + key] === false ? "0" : "1",
    (x) => set(section, key, x === "1", label));
  pane.append(el("h2", "", "Asistan"),
    el("p", "", "Güvenlik, arama, karşılama ve bildirim ayarları. Özelliklerin çalışıp çalışmadığı Sağlık sekmesinde."));
  pane.appendChild(el("div", "sec", "Ayarlar"));
  pane.append(
    setRow("Komut sandbox'ı", "Otomatik: dış içerik (web, e-posta) okunan turda komutlar proje dışına yazamayan sandbox'ta çalışır",
      segmented([["off", "Kapalı"], ["auto", "Otomatik"], ["on", "Her zaman"]], v["agent.sandbox"] || "auto",
        (x) => set("agent", "sandbox", x, "Sandbox"))),
    setRow("Prompt injection koruması", "Dış içerik okunan turda komut ve mesaj gönderme onaya bağlanır",
      onOff("agent", "taint_guard", "Koruma")),
    setRow("Google araması", "web_search önce Google'da (Gemini) arar, kota dolarsa DuckDuckGo",
      onOff("agent", "search_grounding", "Google araması")),
    setRow("Tur token bütçesi", "Tek bir istekte harcanabilecek en çok token (önbellek hariç); aşılınca tur durur, \"devam et\" ile sürer",
      segmented([["0", "Sınırsız"], ["200000", "200K"], ["500000", "500K"], ["1000000", "1M"], ["2000000", "2M"]],
        String(v["agent.turn_token_budget"] || 0), (x) => set("agent", "turn_token_budget", Number(x), "Tur bütçesi"))),
    setRow("Dönüşte karşılama", "Uzun yokluktan sonra takip, takvim ve e-postalarla kısa sesli brifing",
      onOff("greeting", "enabled", "Karşılama")),
    setRow("Yokluk süresi", "Bu kadar dakika uzakta kalınca dönüş sayılır",
      segmented([["60", "60"], ["90", "90"], ["120", "120"], ["180", "180"]], String(v["greeting.away_minutes"] || 90),
        (x) => set("greeting", "away_minutes", Number(x), "Yokluk süresi"))),
    setRow("Konuşma özetleri", "Biten konuşma özetlenir, takipler çıkarılır",
      onOff("memory", "digest", "Özetler")),
    setRow("E-posta bildirimi", "Yeni önemli e-posta gelince haber ver", onOff("gmail", "notify", "E-posta bildirimi")),
    setRow("BUDDY", "Sohbet kutusunun yanındaki küçük arkadaş; konuştukça doyar, okşanınca keyiflenir",
      segmented([["1", "Açık"], ["0", "Kapalı"]], v["buddy.enabled"] === false ? "0" : "1", (x) => {
        set("buddy", "enabled", x === "1", "BUDDY");
        if (x === "1" && window.buddyShow) window.buddyShow();
        else if (window.buddyRefresh) setTimeout(window.buddyRefresh, 300);
      })),
    setRow("BUDDY yorumları", "Konuşmalara ara sıra kısa bir yan yorum yapsın", onOff("buddy", "chatter", "Yorumlar")),
  );
  kairosBlock(pane);
  pane.appendChild(el("div", "sec", "İzin kuralları"));
  const rules = d.rules || [];
  if (!rules.length) pane.appendChild(el("p", "", "Kural yok. Onay kartında \"Bu projede hep izin ver\" deyince buraya eklenir."));
  for (const r of rules) {
    const row = el("div", "list-row");
    const l = el("span", "l");
    l.append(el("b", "", r.rule), el("small", "", (r.kind === "allow" ? "izin ver" : r.kind === "deny" ? "yasak" : "sor") +
      " · " + (r.source === "project" ? "bu proje (.alice/permissions.toml)" : "config.toml (dosyadan düzenle)") +
      (r.untrusted ? " · onaylanmadı, uygulanmıyor" : "")));
    row.appendChild(l);
    if (r.untrusted) {
      const t = el("button", "btn", "Onayla");
      t.title = "Bu kural dosyası ALICE dışında yazılmış ya da değişmiş (ör. klonlanan repo). Onaylarsan uygulanır.";
      t.addEventListener("click", () => api("/api/assistant/action", { action: "rules_trust" })
        .then((x) => { if (x && x.ok) { t.remove(); } }));
      row.appendChild(t);
    }
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
/* Sistem > Ses: Gemini seslerini dinle ve seç (Live ve normal sesli cevap aynı sesle konuşur). */
let previewAudio = null;
function voicePane(pane, data) {
  const d = data || {};
  pane.append(el("h2", "", "Ses"),
    el("p", "", "ALICE'in konuşma sesi. Dinle'ye basınca o sesle kısa bir örnek çalar (ilk seferde birkaç saniye "
      + "sürer). Seçtiğin ses canlı konuşmada bir sonraki \"Hey Alice\"te geçerli olur."));
  pane.appendChild(el("div", "sec", "Sesimi tanıt"));
  pane.appendChild(enrollCard());
  if (!d.can_preview) pane.appendChild(el("p", "", "Örnek dinlemek için Gemini anahtarı gerekli (Bağlantılar → Anahtarlar)."));
  const voices = (d.voices || []).slice().sort((a, b) => (b.name === d.live) - (a.name === d.live));
  pane.appendChild(el("div", "sec", `Sesler · ${voices.length}`));
  const grid = el("div", "voice-grid");
  for (const v of voices) {
    const on = v.name === d.live;
    const card = el("div", "voice-card" + (on ? " on" : ""));
    const play = el("button", "vc-play");
    play.title = d.can_preview ? "Dinle" : "Örnek için Gemini anahtarı gerekli";
    play.disabled = !d.can_preview;
    play.appendChild(svg('<path d="M8 5v14l11-7z"/>'));
    play.addEventListener("click", () => {
      card.classList.add("loading");
      api("/api/voice/preview", { voice: v.name }).then((r) => {
        card.classList.remove("loading");
        if (!r || !r.ok) return toast((r && r.error) || "örnek üretilemedi");
        if (previewAudio) previewAudio.pause();
        for (const c of grid.querySelectorAll(".voice-card.playing")) c.classList.remove("playing");
        card.classList.add("playing");
        previewAudio = new Audio("data:audio/wav;base64," + r.audio);
        previewAudio.addEventListener("ended", () => card.classList.remove("playing"));
        previewAudio.play().catch((e) => toast("ses çalınamadı: " + (e && e.message || e)));
      });
    });
    const l = el("span", "l");
    l.append(el("b", "", v.name), el("small", "", v.desc));
    card.append(play, l);
    if (on) card.appendChild(el("span", "pill done", "seçili"));
    else {
      const pick = el("button", "btn ghost", "Seç");
      pick.addEventListener("click", () => api("/api/voice/set", { voice: v.name }).then((r) => {
        toast(r && r.ok ? v.name + " seçildi; bir sonraki konuşmada bu sesle konuşur" : "kaydedilemedi");
        showSettings("voice");
      }));
      card.appendChild(pick);
    }
    grid.appendChild(card);
  }
  pane.appendChild(grid);
}
/* Sistem > Ses > Sesimi tanıt: okunacak cümle büyük yazılır, mikrofon seviyesi canlı görünür. Kayıt sunucuda
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
/* Sistem > Çekirdek: arka plandaki ALICE (bildirimler, görevler, "Hey Alice") ve sesli asistan. Komut satırındaki
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
/* Otomasyonlar > Otomasyon: kurallar ("GPU 85'i geçerse haber ver"), zamanlanmış görevler ("her sabah 9'da ...") ve
   akışlar (bağlı servisleri izleyip haber verenler). Kural/görev düz cümleyle yazılır; ne anlaşıldığı gösterilir,
   onaylanınca kaydedilir. */
/* Ortak küçük bileşenler: aç/kapa anahtarı ve simgeli düğme. Sayfalar "Kapat / Aç / Sil" yazılı düğme yığını yerine
   bunları kullanır. */
function switchEl(on, title, onChange) {
  const b = el("button", "switch" + (on ? " on" : ""));
  b.setAttribute("role", "switch");
  b.setAttribute("aria-checked", on ? "true" : "false");
  b.title = title || (on ? "Açık" : "Kapalı");
  b.addEventListener("click", () => onChange(!on));
  return b;
}
function iconAct(icon, title, fn, cls) {
  const b = el("button", "icon-btn" + (cls ? " " + cls : ""));
  b.title = title;
  b.setAttribute("aria-label", title);
  b.appendChild(svg(icon));
  b.addEventListener("click", fn);
  return b;
}
function autoPane(pane) {
  pane.append(el("h2", "", "Kurallar ve görevler"),
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
      row.append(l, switchEl(r.enabled, r.enabled ? "Kuralı kapat" : "Kuralı aç", (on) => call("rule_enable", { id: r.id, on }).then(after())),
        iconAct(IC.trash, "Kuralı sil", () => call("rule_rm", { id: r.id }).then(after("kural silindi")), "danger"));
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
        switchEl(t.enabled, t.enabled ? "Görevi kapat" : "Görevi aç", (on) => call("task_enable", { id: t.id, on }).then(after())),
        iconAct(IC.trash, "Görevi sil", () => call("task_rm", { id: t.id }).then(after("görev silindi")), "danger"));
      body.appendChild(row);
    }
    // Hook'lar
    body.appendChild(el("div", "sec", `Hook'lar (${(d.hooks || []).length})`));
    const hk = el("div", "auto-new hook-new");
    const ev = el("select");
    for (const e of d.hook_events || []) { const o = el("option", "", e); o.value = e; ev.appendChild(o); }
    ev.value = "PostToolUse";
    const mt = el("input"); mt.type = "text"; mt.placeholder = "araç (regex) ör. edit_file|write_file|multi_edit";
    const cmd = el("input"); cmd.type = "text"; cmd.placeholder = "komut ör. ruff format \"$ALICE_TOOL_PATH\"";
    const addH = small("Ekle", async () => {
      if (!cmd.value.trim()) return toast("komut boş");
      const ok = await confirmDialog("Hook eklensin mi?",
        `${ev.value}${mt.value ? " [" + mt.value + "]" : ""} olayında her seferinde şu komut çalışacak:\n${cmd.value}`, "Ekle");
      if (ok) call("hook_add", { event: ev.value, matcher: mt.value, command: cmd.value }).then(after("hook eklendi"));
    }, true);
    const hrow = el("div", "key-edit");
    hrow.append(ev, mt);
    const hrow2 = el("div", "key-edit");
    hrow2.append(cmd, addH);
    hk.append(hrow, hrow2, el("small", "enroll-note", "PostToolUse: araçtan sonra (çıkış 2 = hata ALICE'e gider) · "
      + "PreToolUse: önce (2 = engelle) · Stop: cevap bitince (2 = devam et) · UserPromptSubmit / SessionStart: çıktı "
      + "bağlama eklenir. Dosya: " + (d.hooks_path || "")));
    body.appendChild(hk);
    for (const h of d.hooks || []) {
      const row = el("div", "list-row");
      const l = el("span", "l");
      l.append(el("b", "", h.command), el("small", "", h.event + (h.matcher ? " · " + h.matcher : " · her araç")
        + ` · ${h.timeout} sn` + (h.fixed ? " · config.toml'da" : "")));
      const out = el("div", "auto-verdicts");
      l.appendChild(out);
      row.appendChild(l);
      if (!h.fixed) {
        row.append(small("Dene", () => call("hook_test", { index: h.index }).then((r) => {
          out.replaceChildren();
          if (!r || !r.ok) return toast((r && r.error) || "olmadı");
          out.appendChild(el("small", "", `çıkış ${r.code}` + (r.stdout ? " · " + r.stdout : "") + (r.stderr ? " · " + r.stderr : "")));
        })), iconAct(IC.trash, "Hook'u sil", () => call("hook_rm", { index: h.index }).then(after("hook silindi")), "danger"));
      }
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
/* Otomasyonlar > Notlar: ALICE'e "not al" dediklerin, sabah özeti ve haftalık rapor (alice notes / brief / report). */
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
/* Otomasyonlar > Kayıtlar: ALICE'in kime ne gönderdiği, beğenmediğin cevaplar ve test seti (alice log / alice eval). */
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
/* Sistem > Kurulum: sağlayıcıları dene (doctor), modelleri ölç, Google, telefon, DCS, eklentiler, kısayol. */
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
    // Optional features (voice, computer control, browser, core, phone) and the first-run wizard again.
    body.appendChild(card("Özellikler", "Ses, bilgisayar kontrolü, tarayıcı, arka plan çekirdeği, telefon uygulaması: ihtiyacın olanı kur.",
      btn("Özellikleri yönet", () => openFeatures().then(() => setTimeout(load, 300)), true),
      btn("Kurulum sihirbazı", () => openWizard(0))));
    // Sağlayıcılar
    body.appendChild(el("div", "sec", "Modeller"));
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
      else {
        c2b.appendChild(el("div", "enroll-result " + (cg.models_state === "loading" ? "" : "bad"),
          cg.models_state === "loading" ? "Model listesi okunuyor…" : "Model listesi alınamadı: " + (cg.models_error || "bilinmiyor")));
        // Liste gelmese de kullanılabilsin: adı elle ekle
        const row = el("div", "key-edit auto-new");
        const inp = el("input");
        inp.type = "text"; inp.placeholder = "Model adı (ör. gpt-5.5)";
        row.append(inp, btn("Ekle", () => act("chatgpt_model_add", { name: inp.value }).then(done("model eklendi, model menüsünde")), true),
          btn("Tekrar dene", () => act("chatgpt_models_retry").then(done())));
        c2b.appendChild(row);
      }
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
    const c4 = card("Telefondan ALICE", "Android uygulamasıyla sohbet, sesle yazma, projeler, bilgisayar kontrolü ve onay bildirimleri. Aynı Wi-Fi'de, çekirdek açıkken çalışır.",
      segmented([[true, "Açık"], [false, "Kapalı"]], !!ph.enabled, (on) => act("phone_enable", { on }).then(done(on ? "telefon köprüsü açılıyor" : "telefon köprüsü kapatıldı"))));
    if (ph.enabled) {
      const box = el("div", "phone-box");
      // Resim olarak: satır içi SVG'yi uygulamanın simge kuralı (svg { fill: none; stroke }) boyayıp siliyordu.
      if (ph.qr) { const q = el("img", "phone-qr"); q.alt = "QR kod"; q.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(ph.qr); box.appendChild(q); }
      const info = el("div", "phone-info");
      info.append(el("small", "", "QR'yi telefonun kamerasıyla okut. Uygulama kuruluysa \"ALICE ile aç\"ı seç; değilse sayfa tarayıcıda açılır ve uygulamayı kurmayı önerir."), el("code", "", ph.url));
      const app = ph.app || {};
      info.appendChild(el("small", "", app.available
        ? `Android uygulaması ${app.version_name} bilgisayarda hazır. Yeni sürüm çıkınca telefondaki uygulama kendini günceller.`
        : "Android uygulaması henüz bilgisayara inmedi" + (app.error ? ` (${app.error})` : "") + ". GitHub'da derlenince kendiliğinden iner."));
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
    return (d.doctor || {}).state === "running" || mo.state === "running" || (d.chatgpt || {}).state === "running"
      || (d.chatgpt || {}).models_state === "loading";
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
      box.appendChild(el("p", "", "Henüz ölçüm yok: Sistem → Kurulum → Ücretsiz modelleri ölç."));
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
/* Ayarlar > Hafıza: notlar (genel ve proje), Dream, takipler ve eski rolden ("sevgili") kalan izler. */
function memoryPane(pane, data) {
  const d = data || {};
  pane.append(el("h2", "", "Hafıza"),
    el("p", "", "ALICE'in seninle ve işlerinle ilgili notları. Her not ayrı bir dosya; dizini (MEMORY.md) her sohbete girer, ilgili notların tamamı mesaja eklenir. Yanlış olanı düzelt ya da sil; sesle de \"bunu unut\" diyebilirsin."));
  if (!d.enabled) { pane.appendChild(el("p", "", "Hafıza kapalı (config.toml [memory] enabled = false).")); return; }
  const old = d.old_role || {};
  const oldTotal = (old.facts || 0) + (old.summaries || 0) + (old.episodes || 0);
  if (oldTotal) {
    const box = el("div", "list-row warn mem-old");
    const l = el("span", "l");
    l.append(el("b", "", "Eski rolden kalan izler"),
      el("small", "", `${old.facts || 0} not, ${old.summaries || 0} özet, ${old.episodes || 0} eski cevap ("bebeğim", "aşkım" gibi). `
        + "Bunlar kaldıkça ALICE eski tavrına dönebiliyor. Örnek: " + (old.examples || []).join(" / ")));
    const b = el("button", "btn primary", "Temizle");
    b.addEventListener("click", () => api("/api/memory/action", { action: "purge_old_role" }).then((r) => {
      toast(r && r.ok ? "Eski rol izleri temizlendi" : "temizlenemedi");
      showSettings("memory");
    }));
    box.append(l, b);
    pane.appendChild(box);
  }
  // Instructions (Özel Kullanıcı Talimatları - Claude tarzı her konuşmaya doğrudan enjekte edilen metin)
  const instBox = el("div", "list-row");
  instBox.style.cssText = "flex-direction: column; align-items: stretch; gap: 8px; margin-bottom: 16px;";
  const instHead = el("div", "l");
  instHead.append(el("b", "", "Özel Talimatlar & Kimlik (Instructions)"),
    el("small", "", "Her konuşmanın başında modele doğrudan verilir (adın, rolün, temel dil tercihin). Yalnızca senin yerel cihazında saklanır, git reposuna gitmez."));
  const instArea = el("textarea", "input");
  instArea.style.cssText = "width: 100%; min-height: 80px; font-family: monospace; font-size: 13px; resize: vertical;";
  instArea.value = d.instructions || "";
  const instSaveBtn = el("button", "btn primary", "Talimatları Kaydet");
  instSaveBtn.style.alignSelf = "flex-end";
  instSaveBtn.addEventListener("click", () => {
    instSaveBtn.disabled = true;
    api("/api/memory/action", { action: "save_instructions", text: instArea.value }).then((r) => {
      instSaveBtn.disabled = false;
      toast(r && r.ok ? "Talimatlar kaydedildi" : "kaydedilemedi");
    });
  });
  instBox.append(instHead, instArea, instSaveBtn);
  pane.appendChild(instBox);

  // Dream: günde bir (yeterince konuşma birikince) kendiliğinden; buradan şimdi de çalıştırılabilir.
  const dr = d.dream || {};
  const dbox = el("div", "list-row");
  const dl = el("span", "l");
  const when = dr.at ? new Date(dr.at * 1000).toLocaleString("tr-TR") : "henüz hiç";
  dl.append(el("b", "", "Dream (hafızayı toparla)"),
    el("small", "", `Tekrarları birleştirir, eskiyeni düzeltir, son konuşmalardan kalıcı olanı ekler, dizini toparlar. Önce yedek alır. `
      + (dr.enabled ? `Kendiliğinden: ${dr.hours} saatte bir, en az ${dr.sessions} konuşma birikince. ` : "Kendiliğinden çalışma kapalı. ")
      + `Son: ${when}.` + (dr.summary ? " " + dr.summary : "")));
  const db = el("button", "btn", "Şimdi toparla");
  db.addEventListener("click", () => {
    db.disabled = true; db.textContent = "Toparlanıyor…";
    api("/api/memory/action", { action: "dream" }).then((r) => {
      toast(r && r.ok ? ((r.done || []).length ? `${r.done.length} değişiklik` : "Zaten derli toplu") : (r && r.error) || "toparlanamadı");
      showSettings("memory");
    });
  });
  dbox.append(dl, db);
  pane.appendChild(dbox);

  const f = el("input"); f.type = "text"; f.placeholder = "Notlarda ara"; f.className = "set-filter";
  pane.appendChild(f);

  // Claude memory categories tabs/sections
  const CAT_TITLES = {
    you: "👤 You (Kullanıcı & Tercihler)",
    people: "👥 People (İnsanlar & Çapraz Bağlar)",
    areas: "🌐 Areas (Projeler & Hayat Alanları)",
    topics: "💡 Topics (Donanım & İlgi Alanları)",
    general: "📝 Genel Notlar"
  };

  const lists = [];
  for (const book of d.books || []) {
    const notes = book.notes || [];
    if (!notes.length && book.scope !== "genel") continue;
    pane.appendChild(el("div", "sec", `${book.label} (${notes.length})` + (book.scope === "proje" && book.current ? " · bu sohbetin projesi" : "")));
    
    // Group notes by category (Claude style)
    const byCat = { you: [], people: [], areas: [], topics: [], general: [] };
    for (const n of notes) {
      const c = (n.category || "general").toLowerCase();
      if (byCat[c]) byCat[c].push(n);
      else byCat.general.push(n);
    }

    const list = el("div");
    pane.appendChild(list);
    lists.push([list, notes, byCat]);
    if ((book.backups || []).length) {
      const row = el("div", "list-row");
      const sel = el("select");
      for (const s of book.backups) { const o = el("option", "", s); o.value = s; sel.appendChild(o); }
      const rb = el("button", "btn", "Yedeğe dön");
      rb.addEventListener("click", () => {
        if (!confirm(`${book.label} hafızası ${sel.value} yedeğine dönsün mü? Şimdiki hâli de yedeklenir.`)) return;
        api("/api/memory/action", { action: "restore", root: book.root, backup: sel.value }).then((r) => {
          toast(r && r.ok ? `${r.count} not geri geldi` : (r && r.error) || "dönülemedi");
          showSettings("memory");
        });
      });
      const l = el("span", "l"); l.append(el("small", "", "Dream öncesi yedekler"));
      row.append(l, sel, rb);
      pane.appendChild(row);
    }
  }
  const draw = () => {
    const q = f.value.toLocaleLowerCase("tr");
    for (const [list, notes, byCat] of lists) {
      list.replaceChildren();
      if (q) {
        // Flat search filtered view
        const shown = notes.filter((x) => (x.title + " " + x.description + " " + x.body).toLocaleLowerCase("tr").includes(q));
        if (!shown.length) list.appendChild(el("p", "", "Eşleşen yok."));
        for (const x of shown) list.appendChild(noteRow(x, notes, draw));
      } else {
        // Categorized view (You, People, Areas, Topics, General)
        let totalShown = 0;
        for (const [cat, catNotes] of Object.entries(byCat)) {
          if (!catNotes.length) continue;
          totalShown += catNotes.length;
          const catHeader = el("h3", "mem-cat-head", CAT_TITLES[cat] || cat);
          catHeader.style.cssText = "margin: 14px 0 6px 0; font-size: 13px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.5px;";
          list.appendChild(catHeader);
          for (const x of catNotes) {
            list.appendChild(noteRow(x, notes, draw));
          }
        }
        if (!totalShown) list.appendChild(el("p", "", "Henüz not yok."));
      }
    }
  };
  f.addEventListener("input", draw);
  draw();
  historyBlock(pane, d);
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
/* Geçmiş sohbetler: Claude'daki "Search and reference chats" gibi tek bir anahtar. Turlar burada tek tek listelenmez
   (Hafıza sayfası notlar içindir); ALICE gerekince eski sohbetleri kendisi arar. Kapalıyken turlar hiç saklanmaz. */
function historyBlock(pane, d) {
  const on = d.chat_history !== false, h = d.history || {};
  pane.appendChild(el("div", "sec", "Geçmiş sohbetler"));
  const row = el("div", "list-row");
  const l = el("span", "l");
  l.append(el("b", "", "Geçmiş sohbetlerde ara"),
    el("small", "", on
      ? "ALICE, sen geçmişe değinince (\"dün konuştuğumuz…\") eski sohbetlerini arayıp bulabilir. Eski konuşmalar mesajlara kendiliğinden eklenmez."
      : "Kapalı: konuşmalar saklanmaz, ALICE eski sohbetleri bilmez. Notların etkilenmez."));
  const sw = switchEl(on, on ? "Kapat" : "Aç", (v) => api("/api/memory/action", { action: "chat_history", on: v }).then((r) => {
    toast(r && r.ok ? (v ? "Geçmiş sohbet araması açıldı" : "Geçmiş sohbet araması kapatıldı") : "kaydedilemedi");
    showSettings("memory");
  }));
  row.append(l, sw);
  pane.appendChild(row);
  const n = (h.episodes || 0) + (h.summaries || 0);
  if (!n) return;
  const row2 = el("div", "list-row");
  const l2 = el("span", "l");
  l2.append(el("small", "", `Saklanan: ${h.episodes || 0} tur, ${h.summaries || 0} konuşma özeti. Bir sohbeti kenar çubuğundan silmek onun izlerini de siler.`));
  const all = el("button", "btn", "Kaydı temizle");
  all.addEventListener("click", async () => {
    if (!confirm("Saklanan bütün sohbet geçmişi (turlar ve özetler) silinsin mi? Notlar ve takipler kalır. Geri alınamaz.")) return;
    const r = await api("/api/memory/action", { action: "forget_history" });
    toast(r && r.ok ? `${r.episodes} tur, ${r.summaries} özet silindi` : "silinemedi");
    showSettings("memory");
  });
  row2.append(l2, all);
  pane.appendChild(row2);
}
function noteRow(x, notes, redraw) {
  const row = el("div", "list-row mem-note" + (x.old ? " warn" : ""));
  const l = el("span", "l");
  const al = (x.aliases || []).filter((a) => a.toLocaleLowerCase("tr") !== String(x.name).toLocaleLowerCase("tr"));
  const meta = [al.length ? "diğer adları: " + al.join(", ") : "", (x.links || []).length ? "bağlı: " + x.links.join(", ") : "",
    "güncellendi " + x.age, x.source === "kullanıcı" ? "senin isteğin" : "", x.old ? "eski rolden" : ""].filter(Boolean).join(" · ");
  l.append(el("b", "", x.title || x.name), el("small", "", String(x.description || "").replace(/\[\[([^\]]+)\]\]/g, "$1")), el("small", "mem-meta", meta));
  const body = el("div", "mem-body");
  body.hidden = true;
  const ta = el("textarea"); ta.value = x.body; ta.rows = Math.min(14, Math.max(3, x.body.split("\n").length + 1));
  const save = el("button", "btn primary", "Kaydet");
  save.addEventListener("click", () => {
    save.disabled = true;
    api("/api/memory/action", { action: "save", key: x.key, body: ta.value }).then((r) => {
      save.disabled = false;
      if (r && r.ok) { x.body = ta.value; toast("Not kaydedildi"); } else toast((r && r.error) || "kaydedilemedi");
    });
  });
  body.append(ta, save);
  l.appendChild(body);
  const open = el("button", "btn", "Aç");
  open.addEventListener("click", () => { body.hidden = !body.hidden; open.textContent = body.hidden ? "Aç" : "Kapat"; });
  const del = el("button", "btn", "Sil");
  del.addEventListener("click", () => api("/api/memory/action", { action: "forget", key: x.key }).then((r) => {
    if (r && r.ok) { notes.splice(notes.indexOf(x), 1); redraw(); }
  }));
  row.append(l, open, del);
  return row;
}
/* Bağlantılar > Anahtarlar: API anahtarlarını uygulamanın içinden gir. Değerin tamamı sunucudan hiç gelmez; sadece son 4 hane. */
function keysPane(pane, data) {
  const d = data || {};
  pane.append(el("h2", "", "Anahtarlar"),
    el("p", "", "Anahtarı yapıştırıp kaydet: hemen kullanılır, yeniden başlatmak gerekmez. Hepsi bu bilgisayarda "
      + (d.path || "~/.alice/.env") + " dosyasında durur. En az iki model anahtarı (ör. Gemini + Groq), biri kotaya takılınca ALICE'in durmamasını sağlar."));
  const groups = [["model", "Model sağlayıcıları"], ["connect", "Servisler"]];
  for (const [g, title] of groups) {
    const cards = new Map();
    // Model sağlayıcısına bağlı anahtarlar bir grupta, gerisi (GitHub, ses, Steam...) servisler grubunda.
    const byKey = Object.fromEntries((d.keys || []).map((x) => [x.key, x]));
    const isModel = (x) => !!(x.provider || (byKey[x.part_of] || {}).provider);
    for (const k of (d.keys || []).filter((x) => (g === "model") === isModel(x))) {
      const id = k.part_of || k.key;
      if (!cards.has(id)) cards.set(id, []);
      cards.get(id).push(k);
    }
    const list = [...cards.values()].sort((a, b) => (b.every((p) => p.set) - a.every((p) => p.set)));
    const done = list.filter((parts) => parts.every((p) => p.set)).length;
    pane.appendChild(el("div", "sec", `${title} · ${done}/${list.length} bağlı`));
    if (g === "model") {
      // Anahtar havuzu: bir sağlayıcıya birden çok anahtar eklenince istekler nasıl paylaştırılsın.
      const row = el("div", "kc-strategy");
      row.append(el("small", "", "Bir sağlayıcıda birden çok anahtar varsa:"),
        segmented([["round-robin", "Sırayla kullan"], ["fill-first", "Biri bitince diğerine geç"]], d.strategy || "round-robin",
          (v) => api("/api/keys/strategy", { strategy: v }).then((r) => toast(r && r.ok ? "kaydedildi" : (r && r.error) || "olmadı"))));
      pane.appendChild(row);
    }
    const grid = el("div", "key-grid");
    for (const parts of list) grid.appendChild(keyRow(parts));
    if (g === "model") grid.appendChild(serverCard());
    pane.appendChild(grid);
    if (g === "model") cliproxySection(pane);  // abonelik hesapları: model sağlayıcılarının hemen altında
  }
}
/* Anahtar havuzu: sağlayıcının ek anahtarları ve her birinin durumu (hazır, beklemede, kapalı). */
function keyPool(k) {
  const box = el("div", "kc-pool");
  const pool = k.pool || [];
  const extra = k.extra || [];
  if (pool.length > 1) {
    for (const x of pool) {
      const waits = Object.entries(x.waiting || {});
      const st = x.state === "beklemede" && waits.length
        ? "beklemede · " + waits.slice(0, 2).map(([m, sec]) => `${m.split("/").pop()} ${sec > 3600 ? Math.round(sec / 3600) + " sa" : sec > 60 ? Math.round(sec / 60) + " dk" : sec + " sn"}`).join(", ")
        : x.state;
      const r = el("div", "kc-key " + (x.state === "hazır" ? "ok" : x.state === "kapalı" ? "bad" : "wait"));
      r.append(el("span", "kc-dot"), el("code", "", x.preview), el("small", "", st + (x.uses ? ` · ${x.uses} istek` : "")));
      if (x.error && x.state === "kapalı") r.title = x.error;
      const ex = x.index > 1 ? extra[x.index - 2] : null;
      if (ex && ex.source === "file") r.appendChild(iconAct(IC.trash, "Bu anahtarı sil", () => api("/api/keys/set", { key: ex.key, value: "" })
        .then((res) => { toast(res && res.ok ? "anahtar silindi" : (res && res.error) || "silinemedi"); showSettings("apikeys"); }), "danger"));
      box.appendChild(r);
    }
  }
  const add = el("div", "kc-add");
  const inp = el("input");
  inp.type = "password"; inp.autocomplete = "off"; inp.spellcheck = false;
  inp.placeholder = "bir anahtar daha yapıştır (başka hesap)";
  const go = el("button", "btn", "Ekle");
  go.addEventListener("click", () => {
    if (!inp.value.trim()) return inp.focus();
    api("/api/keys/add", { base: k.key, value: inp.value.trim() }).then((r) => {
      if (!r || !r.ok) return toast((r && r.error) || "eklenemedi");
      toast("Anahtar eklendi: istekler artık aralarında paylaşılıyor");
      showSettings("apikeys");
    });
  });
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") go.click(); });
  add.append(inp, go);
  box.appendChild(add);
  box.appendChild(el("small", "kc-hint", "Aynı sağlayıcıdan başka hesapların anahtarları: biri sınıra ya da günlük kotaya takılınca ALICE beklemeden diğeriyle devam eder."));
  return box;
}
/* Kendi sunucun ya da OpenAI uyumlu başka bir servis (LM Studio, vLLM, LiteLLM, bir proxy): adresle ekle. */
function serverCard() {
  const card = el("div", "key-card");
  const head = el("div", "kc-head");
  const l = el("span", "l");
  l.append(el("b", "", "OpenAI uyumlu sunucu ekle"), el("small", "", "LM Studio, vLLM, LiteLLM, kendi proxy'n ya da başka bir bulut"));
  const open = el("button", "btn", "Ekle");
  head.append(el("span", "kc-dot"), l, open);
  card.appendChild(head);
  const form = el("div", "kc-form");
  form.hidden = true;
  const field = (label, ph, type) => {
    const row = el("div", "kc-row");
    const i = el("input");
    i.type = type || "text"; i.placeholder = ph; i.spellcheck = false; i.autocomplete = "off";
    row.append(el("span", "kc-field", label), i);
    form.appendChild(row);
    return i;
  };
  const name = field("Ad", "ör. lmstudio");
  const url = field("Adres", "http://localhost:1234/v1");
  const key = field("Anahtar", "yoksa boş bırak", "password");
  const models = field("Modeller", "boşsa sunucudan okunur (virgülle)");
  const fr = el("div", "kc-btns");
  const cancel = el("button", "btn ghost", "Vazgeç");
  cancel.addEventListener("click", () => { form.hidden = true; });
  const save = el("button", "btn primary", "Dene ve ekle");
  const send = (force) => {
    save.disabled = true;
    api("/api/providers/add", { name: name.value.trim(), base_url: url.value.trim(), key: key.value.trim(), models: models.value.trim(), force })
      .then((r) => {
        save.disabled = false;
        if (r && r.ok) { toast(`${r.name} eklendi` + (r.models && r.models.length ? ` · ${r.models.length} model` : "")); return showSettings("apikeys"); }
        if (r && r.can_force) { save.textContent = "Yine de ekle"; save.onclick = () => send(true); }
        toast((r && r.error) || "eklenemedi");
      });
  };
  save.addEventListener("click", () => send(false));
  fr.append(cancel, save);
  form.appendChild(fr);
  card.appendChild(form);
  open.addEventListener("click", () => { form.hidden = !form.hidden; if (!form.hidden) setTimeout(() => name.focus(), 0); });
  return card;
}
/* Abonelikler (Claude Code proxy, CLIProxyAPI): her servis ayrı kart. Kartın "Giriş yap" düğmesi o servisin giriş
   sayfasını tarayıcıda açar; kullanıcı kendi hesabıyla girince proxy hesabı saklar, kart kendiliğinden tazelenir.
   Aynı serviste birden çok hesap varsa istekler sırayla paylaşılır, biri sınıra takılınca diğerine geçilir. */
function cliproxySection(pane) {
  const box = el("div", "cp-section");
  pane.appendChild(box);
  const busy = cpBlocked;
  const refresh = () => api("/api/cliproxy").then((d) => draw(d || {}));
  const draw = (d) => {
    box.replaceChildren();
    const accs = d.accounts || [];
    const ready = accs.filter((a) => !busy(a)).length;
    box.appendChild(el("div", "sec", "Abonelikler · " + (d.running ? `${accs.length} hesap, ${ready} hazır` : "Claude Code proxy")));
    if (!d.installed || !d.running) { box.appendChild(proxyStatusCard(d, refresh)); return; }
    const grid = el("div", "key-grid");
    const names = Object.fromEntries((d.providers || []).map((p) => [p.key, p.name]));
    const keys = (d.providers || []).map((p) => p.key);
    for (const a of accs) if (!keys.includes(a.provider)) { keys.push(a.provider); names[a.provider] = a.provider_name || a.provider; }
    // Hesabı olan servisler önce.
    keys.sort((x, y) => accs.some((a) => a.provider === y) - accs.some((a) => a.provider === x));
    for (const k of keys) grid.appendChild(serviceCard(k, names[k], accs.filter((a) => a.provider === k), refresh));
    box.appendChild(grid);
  };
  refresh();
}
// Hesap şu an istek alamıyor mu: kapalı, kotada, girişi düşmüş ya da proxy hata bildiriyor.
function cpBlocked(a) { return !!(a.disabled || a.quota_blocked || a.cooldown || a.needs_sign_in || a.error || a.ready === false); }
function cpUntil(ts) {
  const m = Math.max(0, Math.round((ts * 1000 - Date.now()) / 60000));
  return m >= 1440 ? `${Math.round(m / 1440)} gün sonra sıfırlanır` : m >= 60 ? `${Math.floor(m / 60)} sa ${m % 60} dk sonra sıfırlanır` : `${m} dk sonra sıfırlanır`;
}
function proxyStatusCard(d, refresh) {
  const card = el("div", "key-card cp-card" + (d.installed ? " partial" : ""));
  const head = el("div", "kc-head");
  const l = el("span", "l");
  l.append(el("b", "", "Claude Code proxy"), el("small", "", d.installed ? "çalışmıyor" : "kurulu değil"));
  head.append(el("span", "kc-dot"), l);
  const body = el("div", "cp-body");
  if (!d.installed) {
    body.appendChild(el("p", "cp-note", "CLIProxyAPI kurulu değil. ALICE GitHub sürümünden otomatik indirip kurabilir "
      + "(~25 MB, checksum doğrulanır; " + (d.dir || "~/.alice/cliproxy") + "). Kurulumdan sonra proxy kendisi başlar, "
      + "hesapları aşağıdan bağlarsın."));
    const install = el("button", "btn primary", "Kur (otomatik)");
    install.addEventListener("click", () => {
      install.disabled = true;
      install.textContent = "İndiriliyor…";
      api("/api/cliproxy/install", {}).then((r) => {
        if (!r || !r.ok) {
          toast((r && r.error) || "kurulum başlatılamadı");
          install.disabled = false;
          install.textContent = "Kur (otomatik)";
          return;
        }
        toast("CLIProxyAPI indiriliyor…");
        let n = 0;
        const timer = setInterval(async () => {
          n += 1;
          const st = await api("/api/cliproxy");
          if (st && st.installed) {
            clearInterval(timer);
            toast("CLIProxyAPI kuruldu");
            refresh();
          } else if (n > 90) {
            clearInterval(timer);
            toast("kurulum tamamlanmadı; README'deki manuel yolu dene");
            install.disabled = false;
            install.textContent = "Kur (otomatik)";
          }
        }, 2000);
      });
    });
    body.appendChild(install);
  } else {
    body.appendChild(el("p", "cp-note cp-err", "Proxy açılamadı" + (d.error ? ": " + d.error : "") + "."));
    const retry = el("button", "btn", "Yeniden dene");
    retry.addEventListener("click", () => { retry.disabled = true; retry.textContent = "Başlatılıyor…"; refresh(); });
    head.appendChild(retry);
  }
  card.append(head, body);
  return card;
}
function serviceCard(key, name, accs, refresh) {
  const ready = accs.filter((a) => !cpBlocked(a)).length;
  const card = el("div", "key-card" + (accs.length ? " cp-card" : "") + (ready ? " set" : accs.length ? " partial" : ""));
  const head = el("div", "kc-head");
  const l = el("span", "l");
  l.append(el("b", "", name), el("small", "", accs.length ? `${accs.length} hesap · ${ready} hazır` : "bağlı hesap yok"));
  const go = el("button", accs.length ? "btn" : "btn primary", accs.length ? "Hesap ekle" : "Giriş yap");
  go.title = name + " giriş sayfasını tarayıcıda açar";
  head.append(el("span", "kc-dot"), l, go);
  const flow = el("div", "cp-flow");
  flow.hidden = true;
  const body = el("div", "cp-body");
  card.append(head, flow, body);
  let poll = null;
  const stop = () => { if (poll) clearInterval(poll); poll = null; };
  const login = () => {
    go.hidden = true;
    flow.hidden = false;
    flow.replaceChildren(el("small", "kc-hint", name + " giriş sayfası hazırlanıyor…"));
    api("/api/cliproxy/login", { provider: key }).then((r) => {
      if (!r || !r.ok) {
        go.hidden = false;
        flow.replaceChildren(el("small", "kc-hint cp-err", (r && r.error) || "giriş başlatılamadı"));
        return;
      }
      const msg = el("p", "cp-note", `Tarayıcıda ${name} giriş sayfası açıldı. Eklemek istediğin hesapla giriş yap; bitince hesap burada kendiliğinden görünür.`);
      const again = el("button", "btn", "Sayfayı yeniden aç");
      again.title = r.url;
      again.addEventListener("click", () => api("/api/open", { url: r.url }));
      const cancel = el("button", "btn ghost", "Vazgeç");
      cancel.addEventListener("click", () => { stop(); api("/api/cliproxy/login/cancel", { state: r.state }); flow.hidden = true; go.hidden = false; });
      const row = el("div", "cp-flow-btns");
      row.append(again, cancel);
      flow.replaceChildren(msg, row);
      api("/api/open", { url: r.url });  // masaüstünde sistem tarayıcısında (iç pencere giriş sayfasını açamayabilir)
      let n = 0;
      stop();
      poll = setInterval(async () => {
        n += 1;
        const st = await api("/api/cliproxy/login/status", { state: r.state });
        if (st && st.status === "ok") { stop(); toast(name + " hesabı eklendi"); refresh(); }
        else if (n > 90 || (st && st.status === "error")) {
          stop();
          msg.textContent = (st && st.error) || (n > 90 ? "Zaman aşımı: giriş tamamlanmadı." : "Giriş tamamlanmadı.");
          msg.classList.add("cp-err");
        }
      }, 2000);
    });
  };
  go.addEventListener("click", login);
  for (const a of accs) {
    const blocked = cpBlocked(a);
    const row = el("div", "cp-acc" + (blocked ? " off" : ""));
    const who = el("div", "cp-who");
    who.appendChild(el("b", "", a.email || a.name));
    const state = a.needs_sign_in ? "yeniden giriş gerekli" : a.disabled ? "kapalı"
      : a.cooldown ? "kota doldu · " + a.cooldown + " sonra açılır" : a.quota_blocked ? "kota doldu"
      : a.error ? "hata" : "hazır";
    const q = el("div", "cp-quota");
    // Her kota penceresi (Claude: 5 saat ve 7 gün) ayrı çubuk. Sıfırlanma zamanı geçmiş pencere boş sayılır.
    const wins = (a.quota && a.quota.windows) || (a.quota && a.quota.percent != null ? [a.quota] : []);
    for (const w of wins) {
      const pct = Math.max(0, Math.min(100, w.percent));
      const bar = el("span", "cp-bar");
      const fill = el("i", pct >= 100 ? "full" : pct >= 85 ? "high" : "");
      fill.style.width = pct + "%";
      bar.appendChild(fill);
      const when = w.reset ? " · " + cpUntil(w.reset) : w.stale ? " · sıfırlandı" : "";
      q.append(bar, el("small", "", `${w.label || "kota"} · %${pct}${when}`));
    }
    if (!wins.length) q.appendChild(el("small", "", a.needs_sign_in || a.error ? "" : "kullanım bilgisi yok (henüz istek gitmedi)"));
    if (a.quota && a.quota.observed) q.title = "Son ölçüm: " + new Date(a.quota.observed * 1000).toLocaleString("tr-TR");
    if (a.error) who.title = a.error;
    const acts = el("div", "cp-acts");
    if (a.needs_sign_in) {
      const re = el("button", "btn", "Giriş yap");
      re.addEventListener("click", login);
      acts.appendChild(re);
    }
    acts.appendChild(iconAct(IC.trash, "Hesabı kaldır", () => {
      confirmDialog("Hesap kaldırılsın mı?", (a.email || a.name) + " · " + name, "Kaldır").then((ok) => {
        if (!ok) return;
        api("/api/cliproxy/remove", { name: a.name }).then((r) => { toast(r && r.ok ? "hesap kaldırıldı" : (r && r.error) || "kaldırılamadı"); refresh(); });
      });
    }, "danger"));
    row.append(who, q, el("span", "cp-state " + (blocked ? (a.needs_sign_in || a.error ? "bad" : "wait") : "ok"), state), acts);
    body.appendChild(row);
  }
  if (accs.length > 1) body.appendChild(el("small", "kc-hint", "İstekler bu hesaplar arasında sırayla paylaşılır; biri sınıra takılınca beklemeden diğerine geçilir."));
  return card;
}
function keyRow(parts) {
  const k = parts[0];
  const many = parts.length > 1;
  const all = parts.every((p) => p.set);
  const some = parts.some((p) => p.set);
  const card = el("div", "key-card" + (all ? " set" : some ? " partial" : ""));
  const head = el("div", "kc-head");
  const dot = el("span", "kc-dot");
  const l = el("span", "l");
  // "Cohere (deneme anahtarı ücretsiz, ...)": ad başlıkta, parantez içi açıklama alt satırda.
  const m = /^(.*?)\s*\((.*)\)\s*$/.exec(k.label);
  const title = m ? m[1] : k.label, note = m ? m[2] : "";
  const sub = all ? (many ? "bağlı" : (k.preview || "bağlı") + (k.source === "env" ? " · ortam değişkeni" : ""))
    : some ? "eksik: " + parts.filter((p) => !p.set).map((p) => p.field).join(", ") : (k.help || note || "anahtar yok");
  l.append(el("b", "", title), el("small", "", sub));
  l.title = k.label + (k.help ? "\n" + k.help : "");
  head.append(dot, l);
  if (k.url) {
    const a = el("button", "btn ghost kc-get");
    a.title = k.url;
    a.append(document.createTextNode("Anahtar al"), svg(IC.ext));
    a.addEventListener("click", () => api("/api/open", { url: k.url }));
    head.appendChild(a);
  }
  if (k.custom) head.appendChild(iconAct(IC.trash, "Sunucuyu kaldır", () => api("/api/providers/remove", { name: k.provider })
    .then((r) => { toast(r && r.ok ? k.label + " kaldırıldı" : (r && r.error) || "kaldırılamadı"); showSettings("apikeys"); }), "danger"));
  const edit = el("button", "btn", all ? "Değiştir" : "Ekle");
  head.appendChild(edit);
  card.appendChild(head);
  const pooled = !many && k.provider && k.set;
  if (pooled && (k.pool || []).length > 1) {
    l.querySelector("small").textContent = `${k.pool.length} anahtar · ` + k.pool.filter((x) => x.state === "hazır").length + " hazır";
  }
  const form = el("div", "kc-form");
  form.hidden = true;
  const label = (p) => many ? `${k.label} ${p.field}` : k.label;
  const send = (p, value, msg) => api("/api/keys/set", { key: p.key, value }).then((r) => {
    if (!r || !r.ok) return toast((r && r.error) || "kaydedilemedi");
    toast(msg);
    showSettings("apikeys");
  });
  const inputs = [];
  for (const p of parts) {
    const row = el("div", "kc-row");
    if (many) row.appendChild(el("span", "kc-field", p.field));
    const inp = el("input");
    inp.type = "password"; inp.autocomplete = "off"; inp.spellcheck = false;
    inp.placeholder = p.set ? `yeni ${many ? p.field : "anahtar"} yapıştır` : `${many ? p.field : "anahtarı"} yapıştır`;
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") saveAll(); if (e.key === "Escape") { form.hidden = true; } });
    row.appendChild(inp);
    if (p.source === "file") {
      const del = iconAct(IC.trash, "Sil", () => send(p, "", label(p) + " silindi"), "danger");
      row.appendChild(del);
    }
    inputs.push([p, inp]);
    form.appendChild(row);
  }
  const saveAll = async () => {
    const filled = inputs.filter(([, inp]) => inp.value.trim());
    if (!filled.length) return;
    for (const [p, inp] of filled) await api("/api/keys/set", { key: p.key, value: inp.value.trim() });
    toast(k.label + " kaydedildi");
    showSettings("apikeys");
  };
  const fr = el("div", "kc-btns");
  const cancel = el("button", "btn ghost", "Vazgeç");
  cancel.addEventListener("click", () => { form.hidden = true; });
  const save = el("button", "btn primary", "Kaydet");
  save.addEventListener("click", saveAll);
  fr.append(cancel, save);
  form.appendChild(fr);
  card.appendChild(form);
  if (pooled) {
    form.prepend(keyPool(k));
    if ((k.pool || []).length > 1) inputs[0][1].placeholder = "1. anahtarı değiştir";
  }
  edit.addEventListener("click", () => {
    form.hidden = !form.hidden;
    if (!form.hidden) setTimeout(() => inputs[0][1].focus(), 0);
  });
  return card;
}
/* Bağlantılar: servislerin durumu, OAuth bağlanma/çıkış ve izleyen akışlar. */
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
    // Hata metni uzun olabilir; sağ sütuna koyarsak soldaki adı tek harf genişliğine ezer.
    if (f.error) l.appendChild(el("small", "err", f.error));
    const st = f.error ? "hata" : !f.enabled ? "kapalı" : f.held ? `${f.held} öğe özeti bekliyor` : "açık";
    row.append(l, el("span", "r" + (f.error ? "" : " ok"), st));
    pane.appendChild(row);
  }
  pane.appendChild(el("div", "sec", "Senin adına gönderilenler"));
  pane.appendChild(el("p", "", "Başkasına giden her mesaj, reddettiklerin dahil (terminalde: alice log)."));
  if (!(d.actions || []).length) pane.appendChild(el("div", "list-row", "Henüz bir şey gönderilmedi."));
  for (const a of d.actions || []) pane.appendChild(el("div", "list-row" + (a.outcome === "sent" ? "" : " warn"), a.text));
}

const SETTINGS_TABS = [
  ["health", "Sağlık", IC.gauge], ["general", "Genel", IC.gear], ["apikeys", "Anahtarlar", IC.key], ["voice", "Ses", IC.sound], ["models", "Modeller", IC.spark], ["tools", "Araçlar", IC.bolt],
  ["assistant", "Asistan", IC.shield], ["system", "Çekirdek", IC.term], ["auto", "Kurallar ve görevler", IC.list], ["notes", "Notlar", IC.sun], ["logs", "Kayıtlar", IC.check], ["setup", "Kurulum", IC.update],
  ["connect", "Bağlantılar", IC.plug], ["memory", "Hafıza", IC.brain], ["usage", "Kullanım", IC.gauge],
  ["keys", "Kısayollar", IC.keyboard],
];
/* Sol şeritteki bölümler. Eskiden hepsi tek bir Ayarlar penceresindeydi; özellikler (otomasyon, çekirdek,
   bağlantılar) artık kendi tam sayfalarında, Ayarlar'da sadece gerçek ayarlar kalır. */
const SECTIONS = {
  auto: ["Otomasyonlar", ["auto", "notes", "logs"]],
  system: ["Sistem", ["health", "system", "assistant", "voice", "setup", "usage"]],
  connect: ["Bağlantılar", ["connect", "apikeys"]],
  settings: ["Ayarlar", ["general", "models", "tools", "memory", "keys"]],
};
const secOf = (tab) => Object.keys(SECTIONS).find((k) => SECTIONS[k][1].includes(tab)) || "settings";
function markSection(sec) {
  app.dataset.sec = sec;
  for (const b of document.querySelectorAll("#rail [data-sec]")) b.classList.toggle("on", b.dataset.sec === sec && b.classList.contains("rbtn"));
  $("#sideTitle").textContent = sec === "chat" ? "Sohbetler" : sec === "projects" ? "Projeler" : sec === "tasks" ? "Görevler" : SECTIONS[sec][0];
  const inPage = sec in SECTIONS || sec === "tasks" || (sec === "projects" && !S.proj);
  if (!(sec in SECTIONS)) $("#sideNav").replaceChildren();
  app.classList.toggle("in-page", inPage);
  $("#pageView").hidden = !inPage;
  if (!inPage) $("#pageView").replaceChildren();
}
// Sohbete dön: açık proje ya da sayfa kapanır, son sohbet olduğu gibi durur.
function showChat() {
  markSection("chat");
  if (S.proj) closeProject();
}
function showProjects() {
  if (S.proj) { markSection("projects"); return; }
  markSection("projects");
  const page = el("div", "page");
  const head = el("div", "page-head");
  const t = el("div");
  t.append(el("h1", "", "Projeler"),
    el("p", "", "Büyük işleri ALICE'in yönettiği, paralel çalışan ajan takımları yürütür. Soldan bir proje seç ya da yenisini başlat."));
  const nb = el("button", "btn primary");
  nb.append(svg(IC.plus), document.createTextNode("Yeni proje"));
  nb.addEventListener("click", () => projectDialog(null));
  head.append(t, el("span", "grow"), nb);
  page.appendChild(head);
  const rows = S.projects || [];
  if (!rows.length) {
    page.appendChild(emptyState(IC.list, "Henüz proje yok", "Bir hedef yaz; ALICE işi ajanlara böler, biten işi inceler ve sonunda sana özet yazar."));
  } else {
    const list = el("div", "card-list");
    for (const p of rows.slice(0, 30)) {
      const r = el("button", "card-row");
      const l = el("span", "l");
      l.append(el("b", "", p.title || "(adsız)"), el("small", "", p.goal || ""));
      r.append(el("i", "dot st-" + p.status), l, el("span", "r", `${PROJ_ST[p.status] || p.status} · ${(p.members || []).length} ajan · ${relTime(p.updated || p.created)}`));
      r.addEventListener("click", () => openProject(p.id));
      list.appendChild(r);
    }
    page.appendChild(list);
  }
  $("#pageView").replaceChildren(page);
}
/* Başlarken: boş sohbette, kurulum bitmemişse 3 adım. Her adım gerçek durumdan okunur (anahtar/model var mı,
   modeller ölçüldü mü, çekirdek çalışıyor mu); hepsi tamamsa ya da kapatılırsa görünmez. */
async function drawOnboarding() {
  const box = $("#onboard");
  if (!box) return;
  let hidden = false;
  try { hidden = localStorage.getItem("alice.onboard") === "0"; } catch { /* yok */ }
  if (hidden) { box.hidden = true; return; }
  const [all, setup, sys] = await Promise.all([api("/api/models/all"), api("/api/setup"), api("/api/system")]);
  const hasModel = !!(all && (all.providers || []).some((p) => p.enabled && !p.hidden && (p.models || []).length));
  const probed = !!(setup && setup.models && (setup.models.probed_at || setup.models.state === "running"));
  const core = !!(sys && sys.core && sys.core.running);
  const steps = [
    [hasModel, IC.key, "Bir model bağla", "Ücretsiz bir API anahtarı yapıştır (Gemini önerilir) ya da ChatGPT hesabınla gir.",
      "Anahtar ekle", () => showSettings("apikeys")],
    [probed, IC.gauge, "Modelleri ölç", "Hangi modelin hızlı ve doğru olduğunu bir kez ölçer, sırayı ona göre kurar (~1-2 dk).",
      "Ölç", () => api("/api/setup/action", { action: "models_refresh" }).then(() => { toast("Ölçüm başladı"); drawOnboarding(); })],
    [core, IC.term, "Çekirdeği başlat", "Bildirimler, zamanlanmış görevler ve \"Hey Alice\" için arka planda çalışır.",
      "Başlat", () => api("/api/system/action", { action: "start" }).then(() => { toast("Çekirdek başlatılıyor"); setTimeout(drawOnboarding, 2500); })],
  ];
  const done = steps.filter((x) => x[0]).length;
  if (done === steps.length) { box.hidden = true; return; }
  box.replaceChildren();
  const head = el("div", "ob-head");
  const t = el("div");
  t.append(el("b", "", "Başlarken"), el("small", "", `${done}/${steps.length} tamam`));
  const bar = el("i", "ob-bar"); const fill = el("i"); fill.style.width = (100 * done / steps.length) + "%"; bar.appendChild(fill);
  const x = iconAct(IC.x, "Gizle", () => { try { localStorage.setItem("alice.onboard", "0"); } catch { /* yok */ } box.hidden = true; });
  head.append(t, bar, x);
  box.appendChild(head);
  let nextGiven = false;
  for (const [ok, icon, title, text, label, run] of steps) {
    const row = el("div", "ob-step" + (ok ? " done" : ""));
    const ic = el("span", "ob-ic"); ic.appendChild(svg(ok ? IC.check : icon));
    const l = el("span", "l"); l.append(el("b", "", title), el("small", "", text));
    row.append(ic, l);
    if (!ok) {
      const b = el("button", nextGiven ? "btn" : "btn primary", label);
      nextGiven = true;
      b.addEventListener("click", run);
      row.appendChild(b);
    }
    box.appendChild(row);
  }
  box.hidden = false;
}
function goSection(sec) {
  if (sec === "chat") showChat();
  else if (sec === "projects") showProjects();
  else if (sec === "tasks") showTasks();
  else showSettings(SECTIONS[sec][1][0]);
  closeSideOnMobile();
}
for (const b of document.querySelectorAll("#rail [data-sec]")) b.addEventListener("click", () => goSection(b.dataset.sec));
markSection("chat");

async function showSettings(tab) {
  tab = tab || "general";
  const data = {
    models: tab === "models" ? await api("/api/models") : null,
    all: tab === "models" ? await api("/api/models/all") : null,
    tools: tab === "tools" ? await api("/api/tools") : null,
    memory: tab === "memory" ? await api("/api/memory") : null,
    usage: tab === "usage" ? await api("/api/usage") : null,
    connect: tab === "connect" ? await api("/api/connect") : null,
    assistant: tab === "assistant" || tab === "health" ? await api("/api/assistant") : null,
    apikeys: tab === "apikeys" ? await api("/api/keys") : null,
    voice: tab === "voice" ? await api("/api/voice") : null,
  };
  const sec = secOf(tab);
  if (S.proj) closeProject();
  if (!$("#overlay").hidden && $("#dialog").classList.contains("settings")) closeDialog();
  markSection(sec);
  // Bölümün alt sekmeleri kenar çubuğunda.
  const nav = $("#sideNav");
  nav.replaceChildren();
  const labels = Object.fromEntries(SETTINGS_TABS.map(([k, l, i]) => [k, [l, i]]));
  for (const k of SECTIONS[sec][1]) {
    const [label, icon] = labels[k];
    const b = el("button", "side-item" + (k === tab ? " on" : ""));
    b.append(svg(icon), el("span", "t", label));
    b.addEventListener("click", () => showSettings(k));
    nav.appendChild(b);
  }
  {
    const d = $("#pageView");
    d.scrollTop = 0;
    const pane = el("div", "set-pane page");
    // Dar pencerede kenar çubuğu kapalı: alt sekmeler sayfanın üstünde yatay durur.
    const tabs = el("div", "page-tabs");
    for (const b of nav.children) {
      const c = b.cloneNode(true);
      c.addEventListener("click", () => b.click());
      tabs.appendChild(c);
    }
    pane.appendChild(tabs);
    if (tab === "general") {
      pane.append(el("h2", "", "Genel"));
      pane.append(
        setRow("Dil / Language", "Arayüzün ve ALICE'in mesajlarının dili; çeviri sürüyor, bir kısmı henüz Türkçe",
          segmented([["en", "English"], ["tr", "Türkçe"]], S.status.language || "en",
            (v) => api("/api/features/action", { action: "lang", lang: v }).then((r) => { if (r && r.ok) { S.status.language = v; location.reload(); } }))),
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
      modelsPage(pane, data);
    } else if (tab === "tools") {
      toolsPage(pane, Array.isArray(data.tools) ? data.tools : []);
    } else if (tab === "health") {
      healthPane(pane, data.assistant);
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
      const entries = Object.entries((data.usage && data.usage.today) || {});
      if (!entries.length) pane.appendChild(el("p", "", "Bugün henüz istek yok."));
      const pct = (r) => (r.prompt_tokens ? Math.round((100 * (r.cached_tokens || 0)) / r.prompt_tokens) : 0);
      const sum = entries.reduce((a, [, r]) => ({ prompt_tokens: a.prompt_tokens + (r.prompt_tokens || 0),
        cached_tokens: a.cached_tokens + (r.cached_tokens || 0), cache_write_tokens: a.cache_write_tokens + (r.cache_write_tokens || 0) }),
      { prompt_tokens: 0, cached_tokens: 0, cache_write_tokens: 0 });
      if (sum.prompt_tokens) {
        // Önbellek oranı: girdinin ne kadarı önbellekten okundu (ucuz ve hızlı). Yazılan, sonraki istekler için önbelleğe alınan.
        const head = el("p", "use-cache", `Önbellek isabeti bugün %${pct(sum)} · ${(sum.cached_tokens / 1000).toFixed(1)}k okundu` +
          (sum.cache_write_tokens ? ` · ${(sum.cache_write_tokens / 1000).toFixed(1)}k yazıldı` : ""));
        head.title = "Girdinin önbellekten okunan kısmı. Uzun sohbetlerde yüksek olması beklenir; model sık değişirse düşer.";
        pane.appendChild(head);
      }
      const max = Math.max(1, ...entries.map(([, r]) => r.requests));
      for (const [ref, r] of entries) {
        const row = el("div", "use-row");
        const bar = el("i", "use-bar");
        const fill = el("i");
        fill.style.width = (100 * r.requests) / max + "%";
        bar.appendChild(fill);
        row.append(el("span", "l", ref), bar,
          el("span", "r", `${r.requests} istek · ${r.errors} hata · ${(r.prompt_tokens / 1000).toFixed(1)}k girdi` +
            (r.cached_tokens ? ` (önbellek %${pct(r)})` : "") + ` · ${(r.completion_tokens / 1000).toFixed(1)}k çıktı`));
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
    d.replaceChildren(pane);
  }
}
const SHORTCUTS = [
  ["Ctrl+K", "Komut paleti"], ["Ctrl+J", "Yan panel (plan, değişiklikler, bağlam)"], ["Ctrl+O", "Araç ayrıntılarını aç/kapat"],
  ["Ctrl+,", "Ayarlar"], ["Enter", "Gönder (çalışırken sıraya ekler)"], ["Shift+Enter", "Yeni satır"],
  ["Ctrl + / Ctrl −", "Arayüzü büyüt / küçült (Ctrl+0 sıfırlar)"], ["Ctrl+Shift+E", "Sohbeti genişlet / daralt"],
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
  if (/^\/btw(\s|$)/i.test(text)) {
    // Yan soru işi beklemez ve bölmez: hemen gider, cevap kart olarak gelir.
    if (textArg === undefined) { input.value = ""; autosize(); }
    const r = await api("/api/send", { text });
    if (!r || !r.ok) toast((r && r.error) || "yan soru gönderilemedi");
    return;
  }
  if (S.busy && text.startsWith("/")) {
    S.queue.push({ text, images });  // komutlar yeni tur ister: iş bitince çalışır
    drawQueue();
  } else if (S.busy) {
    // Çalışan işe ilet (steer): ALICE bir sonraki adımda görür, iş bitmesi beklenmez. Tur tam o an bittiyse sunucu
    // "çalışıyor" der ya da yeni tur başlatır; olmazsa eskisi gibi sıraya girer.
    const r = await api("/api/send", { text, images });
    if (!r || !r.ok) { S.queue.push({ text, images }); drawQueue(); }
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
$("#cwdBtn").addEventListener("click", askCwd);
/* Dal menüsü: üst çubuktaki dal etiketine tıklayınca yerel ve uzaktaki dallar; seçilene geçilir, uzakta olup
   yerelde olmayan dal izleyen dal olarak açılır. "Yeni dal" şu anki daldan yeni bir dal açar. */
async function branchMenu() {
  const r = await api("/api/git/branches", {});
  if (!r || !r.ok) return toast((r && r.error) || "dallar okunamadı");
  const go = async (name, create) => {
    toast(create ? `'${name}' açılıyor…` : `'${name}' dalına geçiliyor…`);
    const x = await api("/api/git/switch", { branch: name, create: !!create });
    if (!x || !x.ok) return toast((x && x.error) || "dal değiştirilemedi");
    applyStatus(x);
    toast("Dal: " + name);
    refreshChanges();
  };
  openMenu($("#branchChip"), (m) => {
    m.appendChild(el("div", "menu-title", "Dal değiştir"));
    for (const b of r.branches || []) {
      const when = b.ts ? new Date(b.ts * 1000).toLocaleDateString("tr-TR") : "";
      menuItem(m, IC.branch, b.name, (b.remote ? "yalnız GitHub'da · " : "") + [when, b.subject].filter(Boolean).join(" · "),
        () => { if (!b.current) go(b.name); }, b.current);
    }
    menuItem(m, IC.plus || IC.branch, "Yeni dal…", "Şu anki daldan yeni bir dal aç", async () => {
      showDialog((d) => {
        const inp = el("input", "text");
        inp.placeholder = "ör. ozellik/yeni-ayar";
        const row = el("div", "row");
        const no = el("button", "btn", "Vazgeç");
        const yes = el("button", "btn accent", "Aç ve geç");
        const ok = () => { const name = inp.value.trim(); if (!name) return; closeDialog(); go(name, true); };
        no.addEventListener("click", closeDialog);
        yes.addEventListener("click", ok);
        inp.addEventListener("keydown", (e) => { if (e.key === "Enter") ok(); });
        row.append(no, yes);
        d.append(el("h2", "", "Yeni dal"), inp, row);
        setTimeout(() => inp.focus(), 0);
      });
    });
  }, "left", true);
}
$("#branchChip").addEventListener("click", branchMenu);
$("#newChat").addEventListener("click", newChat);
$("#chatTitle").addEventListener("click", () => {
  const s = (S.sessions || []).find((x) => x.id === S.status.session);
  if (s) renameSession(s, $("#chatTitle"));
});
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

// Geniş sohbet: mesajlar ve yazma kutusu pencere genişliğine yayılır; seçim tarayıcıda hatırlanır.
function toggleWide() {
  app.classList.toggle("wide");
  try { localStorage.setItem("alice.wide", app.classList.contains("wide") ? "1" : "0"); } catch { /* yok */ }
}
$("#wideBtn").addEventListener("click", toggleWide);
try { if (localStorage.getItem("alice.wide") === "1") app.classList.add("wide"); } catch { /* yok */ }

// Yakınlaştırma: Ctrl +, Ctrl -, Ctrl 0 bütün arayüzü aynı oranda büyütür (#zoomLayer ölçeklenir, bkz. app.css).
// Seçim tarayıcıda hatırlanır. Ekran koordinatı kullanan kod uiZoom() ile katmanın koordinatına çevirir.
const ZOOM_MIN = 0.6, ZOOM_MAX = 2, ZOOM_MIN_WIDTH = 780;
function cleanZoom(val) {
  const z = Math.round(parseFloat(val) * 20) / 20;  // 0.05 adım: 1.1 + 0.1 = 1.2000000000000002 birikmesin
  return Number.isFinite(z) ? Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z)) : 1;
}
// Pencerenin kaldırabildiği en büyük oran: dar ekran düzenine geçiren @media kuralları pencerenin gerçek genişliğine
// bakar, ölçeklenmiş katmanınkine değil. Katman 780px'in altına inerse masaüstü düzeni sığmaz (yazma kutusunun
// düğmeleri taşar); o yüzden oran pencereye göre sınırlanır, pencere büyüyünce seçilen orana geri döner.
function zoomLimit() {
  const w = window.innerWidth || 0;
  return w ? Math.max(1, Math.min(ZOOM_MAX, Math.floor(w / ZOOM_MIN_WIDTH * 20) / 20)) : ZOOM_MAX;
}
let zoomPref = 1;  // kullanıcının seçtiği
try { zoomPref = cleanZoom(localStorage.getItem("alice.zoom") || 1); } catch { /* yok */ }
let zoom = 1;      // uygulanan (pencere dar ise seçilenden küçük)
function applyZoom() {
  const z = Math.min(zoomPref, zoomLimit());
  if (z === zoom) return false;
  zoom = z;
  document.documentElement.style.setProperty("--zoom", zoom);
  closeMenu();  // açık menü eski ölçeğe göre konumlanmıştı
  return true;
}
applyZoom();
window.addEventListener("resize", applyZoom);
function uiZoom() { return zoom; }

function setZoom(val) {
  const want = cleanZoom(val), limit = zoomLimit();
  zoomPref = Math.min(want, Math.max(limit, zoom));  // sınırın üstüne birikmesin: Ctrl - ilk basışta küçültsün
  try { localStorage.setItem("alice.zoom", String(zoomPref)); } catch { /* yok */ }
  if (applyZoom()) window.dispatchEvent(new Event("resize"));  // ölçüye bağlı yerleşimler (site önizlemesi) yenilensin
  const pct = `Yakınlaştırma %${Math.round(zoom * 100)}`;
  toast(want > zoom && zoom >= limit ? `${pct} · bu pencere genişliğinde en fazla bu kadar`
    : pct + (zoom === 1 ? "" : " · sıfırlamak için Ctrl+0"));
}
// Ctrl+tekerlek: pencerenin kendi yakınlaştırması devreye girip bizimkiyle üst üste binmesin, aynı ayarı değiştirsin.
let wheelZoomAt = 0;
window.addEventListener("wheel", (e) => {
  if (!e.ctrlKey) return;
  e.preventDefault();
  if (e.timeStamp - wheelZoomAt < 140) return;  // dokunmatik yüzeyde sıkıştırma saniyede onlarca olay yollar
  wheelZoomAt = e.timeStamp;
  setZoom(zoom + (e.deltaY < 0 ? 0.1 : -0.1));
}, { passive: false });

document.addEventListener("keydown", (e) => {
  const mod = e.ctrlKey || e.metaKey;
  if (mod && e.shiftKey && e.key.toLowerCase() === "e") { e.preventDefault(); toggleWide(); return; }
  // Tuşun yerine (e.code) değil ürettiği karaktere bakılır: Türkçe Q klavyede "-" tuşu ABD düzeninin "=" yerinde
  // durduğu için Ctrl+- büyütüyordu. AltGr (Windows'ta Ctrl+Alt) hariç: yoksa AltGr+0, AltGr+* ve AltGr+- ile
  // yazılan } \ | karakterleri yakınlaştırmaya gidip yazılamıyordu.
  if (mod && !e.altKey) {
    const isPlus = e.key === "+" || e.key === "=" || e.code === "NumpadAdd";
    const isMinus = e.key === "-" || e.key === "_" || e.code === "NumpadSubtract";
    const isZero = e.key === "0" || e.code === "Numpad0";
    if (isPlus) { e.preventDefault(); setZoom(zoom + 0.1); return; }
    if (isMinus) { e.preventDefault(); setZoom(zoom - 0.1); return; }
    if (isZero) { e.preventDefault(); setZoom(1); return; }
  }
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
  // If input is not focused and user starts typing a character, focus input without losing the keystroke
  if (!mod && !e.altKey && e.key.length === 1 && $("#palette").hidden && $("#overlay").hidden && $("#menu").hidden) {
    const active = document.activeElement;
    if (!active || (!/^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName) && !active.isContentEditable)) {
      input.focus();
    }
  }
});

// Auto-focus input when the window receives focus or is activated
window.addEventListener("focus", () => {
  if ($("#palette").hidden && $("#overlay").hidden && $("#menu").hidden &&
      !document.querySelector("input:focus, textarea:focus, select:focus, [contenteditable]:focus")) {
    input.focus();
  }
});

document.addEventListener("pointerdown", (e) => {
  // Clicking anywhere inside the composer (except buttons/inputs) immediately focuses the textarea
  if (e.target.closest("#composer") && !e.target.closest("button, select, input")) {
    input.focus();
    return;
  }
  // Clicking empty chat background or scroller area focuses input (unless selecting text or clicking controls)
  if (e.target.closest("#scroller") &&
      !e.target.closest("button, a, input, textarea, select, pre, code, .dialog, #menu, .st-chip, .sess, .tab, [contenteditable]")) {
    setTimeout(() => {
      const sel = window.getSelection();
      if ((!sel || sel.isCollapsed) && $("#palette").hidden && $("#overlay").hidden && $("#menu").hidden &&
          !document.querySelector("input:focus, textarea:focus, select:focus")) {
        input.focus();
      }
    }, 0);
  }
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

/* ============================================================= yerleşik tarayıcı paneli
   ALICE'in kendi tarayıcısı (alice/app/builtin_browser.py): ajan sayfayı sürerken burada canlı görünür. Görüntüye
   tıklamak, yazmak, kaydırmak kontrolü devralır; adres çubuğu, geri/ileri/yenile ve sekmeler. */
const BB = { timer: null, busy: false, frame: null, tabs: [], cur: "" };
function renderBrowserPane() {
  const body = $("#panelBody");
  if (body.querySelector(".bb")) return;  // zaten çizili: yalnız kare güncellenir
  body.replaceChildren();
  const wrap = el("div", "bb");
  const bar = el("div", "bb-bar");
  const mk = (icon, title, fn) => { const b = iconAct(icon, title, fn); b.classList.add("bb-btn"); return b; };
  bar.append(mk(IC.back, "Geri", () => bbSend({ type: "back" })),
    mk(IC.fwd, "İleri", () => bbSend({ type: "forward" })),
    mk(IC.reload, "Yenile", () => bbSend({ type: "reload" })));
  const url = el("input", "bb-url");
  url.placeholder = "Adres ya da arama";
  url.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { bbSend({ type: "navigate", url: url.value }); url.blur(); }
  });
  url.addEventListener("focus", () => url.select());
  bar.appendChild(url);
  const tabsBtn = mk(IC.list, "Sekmeler", () => bbTabsMenu(tabsBtn));
  bar.appendChild(tabsBtn);
  const view = el("div", "bb-view");
  view.tabIndex = 0;
  const img = el("img", "bb-img");
  img.alt = "";
  img.draggable = false;
  const idle = el("div", "bb-idle");
  const start = el("button", "btn accent", "Tarayıcıyı başlat");
  start.addEventListener("click", async () => {
    start.disabled = true; start.textContent = "Açılıyor…";
    const r = await api("/api/browser/input", { type: "start" });
    if (!r.ok) { toast(r.error || "açılamadı"); start.disabled = false; start.textContent = "Tarayıcıyı başlat"; }
  });
  idle.append(el("b", "", "ALICE'in tarayıcısı"),
    el("p", "", "ALICE web'de bir iş yapınca burada canlı görürsün; istediğin an tıklayıp yazarak devralabilirsin. " +
      "Giriş yaptığın siteler sonraki seferde de açık kalır."), start);
  view.append(img, idle);
  const pos = (e) => {
    const f = BB.frame, r = img.getBoundingClientRect();
    return f ? { x: ((e.clientX - r.left) / r.width) * f.w, y: ((e.clientY - r.top) / r.height) * f.h } : null;
  };
  img.addEventListener("click", (e) => { const p = pos(e); if (p) { view.focus(); bbSend({ type: "click", ...p }); } });
  img.addEventListener("dblclick", (e) => { const p = pos(e); if (p) bbSend({ type: "click", ...p, count: 2 }); });
  img.addEventListener("contextmenu", (e) => { e.preventDefault(); const p = pos(e); if (p) bbSend({ type: "click", ...p, button: "right" }); });
  view.addEventListener("wheel", (e) => {
    e.preventDefault();
    const p = pos(e) || { x: 0, y: 0 };
    bbSend({ type: "scroll", ...p, dy: e.deltaY });
  }, { passive: false });
  const SPECIAL = new Set(["Enter", "Backspace", "Tab", "Escape", "Delete", "ArrowLeft", "ArrowUp", "ArrowRight",
    "ArrowDown", "Home", "End", "PageUp", "PageDown"]);
  view.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;  // kısayollar uygulamada kalsın (yapıştırma paste olayıyla gelir)
    if (SPECIAL.has(e.key)) { e.preventDefault(); bbSend({ type: "key", key: e.key }); }
    else if (e.key.length === 1) { e.preventDefault(); bbSend({ type: "text", text: e.key }); }
  });
  view.addEventListener("paste", (e) => {
    const t = e.clipboardData && e.clipboardData.getData("text");
    if (t) { e.preventDefault(); bbSend({ type: "text", text: t }); }
  });
  const foot = el("div", "bb-foot");
  wrap.append(bar, view, foot);
  body.appendChild(wrap);
  BB.url = url; BB.img = img; BB.idle = idle; BB.foot = foot;
  browserLoop();
}
async function bbSend(cmd) {
  const r = await api("/api/browser/input", cmd);
  if (r && !r.ok && r.error) toast(r.error);
  bbTick(true);
}
async function bbTabsMenu(anchor) {
  const r = await api("/api/browser/tabs");
  if (!r || !r.ok) return toast((r && r.error) || "tarayıcı kapalı");
  openMenu(anchor, (m) => {
    m.appendChild(el("div", "menu-title", "Sekmeler"));
    for (const t of r.tabs) {
      menuItem(m, IC.globe || IC.file, (t.title || t.url || "yeni sekme").slice(0, 60), t.url.slice(0, 80),
        () => bbSend({ type: "tab", id: t.id }), t.id === r.current);
    }
    menuItem(m, IC.plus, "Yeni sekme", "", () => bbSend({ type: "new_tab" }));
    if (r.tabs.length > 1) menuItem(m, IC.x, "Bu sekmeyi kapat", "", () => bbSend({ type: "close_tab", id: r.current }));
  }, "right", true);
}
async function bbTick(force) {
  if (BB.busy || !BB.img || !BB.img.isConnected) return;
  if (!force && document.hidden) return;
  BB.busy = true;
  try {
    // The viewport is emulated at the view's own pixel size: the shot then fills the panel exactly instead of being
    // a 1280x800 picture squeezed into a ~760px column with blank space underneath.
    const box = BB.img.parentElement ? BB.img.parentElement.getBoundingClientRect() : { width: 0, height: 0 };
    const w = Math.max(320, Math.round(box.width)), h = Math.max(240, Math.round(box.height));
    const f = await api(`/api/browser/frame?q=55&w=${w}&h=${h}`);
    if (f && f.ok) {
      BB.frame = f;
      BB.img.src = "data:image/jpeg;base64," + f.b64;
      BB.img.hidden = false;
      BB.idle.hidden = true;
      if (document.activeElement !== BB.url) BB.url.value = f.url === "about:blank" ? "" : f.url;
      BB.foot.textContent = f.title || "";
    } else {
      BB.img.hidden = true;
      BB.idle.hidden = false;
      BB.foot.textContent = f && !f.idle ? (f.error || "") : "";
    }
  } finally { BB.busy = false; }
}
// Panel açık ve Tarayıcı sekmesindeyken saniyede ~3 kare; değilse durur.
function browserLoop() {
  clearInterval(BB.timer);
  if (!(PREFS.panel && PREFS.tab === "browser")) return;
  bbTick(true);
  BB.timer = setInterval(() => {
    if (!(PREFS.panel && PREFS.tab === "browser")) { clearInterval(BB.timer); return; }
    bbTick(false);
  }, 330);
}

/* ============================================================= PR paneli
   İzlenen PR'lar (alice/prwatch.py): CI durumu, açık inceleme konuları, çakışma ve davranış düğmeleri. Bulunulan dalın
   açık PR'ı izlenmiyorsa tek tıkla izlemeye alınır. Sekme açıkken 30 sn'de bir tazelenir. */
const PRP = { timer: null, busy: false };
const PR_FLAGS = [
  ["auto_fix", "CI kırmızıysa düzelt"],
  ["address_comments", "İnceleme yorumlarını uygula"],
  ["auto_review", "Her commit'i bağımsız incele"],
  ["auto_merge", "Hepsi yeşilken birleştir"],
];
async function prAct(path, body) {
  const r = await api("/api/prs/" + path, body);
  if (!r || !r.ok) toast((r && r.error) || "olmadı");
  else if (r.note && path !== "flag") toast(r.note);
  renderPrPane(true);
}
async function renderPrPane(force) {
  const body = $("#panelBody");
  clearInterval(PRP.timer);
  PRP.timer = setInterval(() => {
    if (!(PREFS.panel && PREFS.tab === "prs")) { clearInterval(PRP.timer); return; }
    if (!document.hidden) renderPrPane(false);
  }, 30000);
  if (PRP.busy) return;
  if (!body.querySelector(".prp")) body.replaceChildren(el("div", "prp note", "PR'lar yükleniyor…"));
  PRP.busy = true;
  let r;
  try { r = await api("/api/prs" + (force ? "?force=1" : "")); } finally { PRP.busy = false; }
  if (!(PREFS.panel && PREFS.tab === "prs")) return;
  const wrap = el("div", "prp");
  const head = el("div", "prp-head");
  head.append(el("b", "", "İzlenen PR'lar"), el("span", "grow"),
    iconAct(IC.rewind, "Tazele", () => renderPrPane(true)));
  wrap.appendChild(head);
  if (!r || (!r.ok && !r.watching.length)) {
    wrap.appendChild(el("div", "note", (r && r.error) || "GitHub'a ulaşılamadı"));
  }
  if (r && r.suggest) {
    const s = r.suggest, card = el("div", "prp-suggest");
    card.append(el("div", "", `Bu dalın (${s.head}) açık PR'ı izlenmiyor:`),
      el("b", "", `#${s.number} ${s.title}`));
    const b = el("button", "btn accent", "İzlemeye al");
    b.addEventListener("click", () => { b.disabled = true; prAct("watch", { repo: s.repo, number: s.number }); });
    card.appendChild(b);
    wrap.appendChild(card);
  }
  const rows = (r && r.watching) || [];
  if (!rows.length && !(r && r.suggest)) {
    wrap.appendChild(emptyState(IC.branch, "İzlenen PR yok",
      "ALICE bir PR açınca ya da sohbette \"bu PR'ı takip et\" deyince burada görünür: CI kırmızıya dönünce, " +
      "inceleme yorumu ya da çakışma gelince kendisi düzeltir."));
  }
  for (const w of rows) wrap.appendChild(prCard(w));
  if (rows.length) wrap.appendChild(el("div", "prp-foot",
    "Kontrolleri ALICE çekirdeği birkaç dakikada bir yapar; düzeltmeler PR'ı açan sohbete gelir."));
  body.replaceChildren(wrap);
}
function prCard(w) {
  const c = el("div", "prp-card");
  const top = el("div", "prp-top");
  const ci = w.state === "merged" ? "merged" : w.state === "closed" ? "closed" : (w.ci || "none");
  const dot = el("span", "prp-dot " + ci);
  dot.title = { success: "CI yeşil", failure: "CI kırmızı", pending: "CI sürüyor", none: "CI yok",
    merged: "birleşti", closed: "kapandı" }[ci] || ci;
  const title = el("a", "prp-title", `#${w.number} ${w.title || ""}`);
  if (w.url) { title.href = w.url; title.target = "_blank"; title.rel = "noopener"; }
  top.append(dot, title);
  c.appendChild(top);
  const meta = el("div", "prp-meta");
  const bits = [w.repo];
  if (w.head) bits.push(`${w.head} → ${w.base}`);
  if (w.sha) bits.push(w.sha);
  if (w.draft) bits.push("taslak");
  meta.textContent = bits.join(" · ");
  c.appendChild(meta);
  const chips = el("div", "prp-chips");
  const chip = (cls, text) => chips.appendChild(el("span", "prp-chip " + cls, text));
  if (w.error) chip("bad", w.error);
  if (ci === "failure") chip("bad", `${w.failed.length} iş kırmızı: ${w.failed.slice(0, 3).join(", ")}`);
  else if (ci === "pending") chip("wait", `${w.pending.length} iş sürüyor`);
  else if (ci === "success") chip("ok", `${w.checks} kontrol yeşil`);
  if (w.open_threads > 0) chip("wait", `${w.open_threads} açık yorum`);
  if (w.mergeable === false) chip("bad", "çakışma var");
  if (chips.childNodes.length) c.appendChild(chips);
  if (w.status) c.appendChild(el("div", "prp-status", w.status));
  if (w.finished) {  // merged/closed: no behavior switches, only "izlemeyi bırak"
    const bar = el("div", "prp-acts");
    const un = el("button", "act", "Kaldır");
    un.addEventListener("click", () => prAct("unwatch", { repo: w.repo, number: w.number }));
    bar.append(el("span", "grow"), un);
    c.appendChild(bar);
    return c;
  }
  const flags = el("div", "prp-flags");
  for (const [k, label] of PR_FLAGS) {
    const row = el("div", "prp-flag");
    row.append(el("span", "", label), switchEl(!!w[k], label, async (v) => {
      let confirmed = false;
      if (k === "auto_merge" && v) {
        confirmed = await confirmDialog("Otomatik birleştirme",
          `#${w.number}: CI yeşil, açık yorum ve çakışma yokken ALICE PR'ı kendisi birleştirecek.`, "Aç");
        if (!confirmed) return;
      }
      prAct("flag", { repo: w.repo, number: w.number, flag: k, value: v, confirmed });
    }));
    flags.appendChild(row);
  }
  c.appendChild(flags);
  const acts = el("div", "prp-acts");
  if (ci === "failure") {
    const b = el("button", "act", "Başarısızları yeniden çalıştır");
    b.addEventListener("click", () => { b.disabled = true; prAct("rerun", { repo: w.repo, number: w.number }); });
    acts.appendChild(b);
  }
  if (w.history && w.history.length) {
    const b = el("button", "act", "Geçmiş");
    b.addEventListener("click", () => c.classList.toggle("open"));
    acts.appendChild(b);
  }
  const un = el("button", "act", "İzlemeyi bırak");
  un.addEventListener("click", () => prAct("unwatch", { repo: w.repo, number: w.number }));
  acts.append(el("span", "grow"), un);
  c.appendChild(acts);
  const hist = el("div", "prp-hist");
  for (const h of w.history || []) hist.appendChild(el("div", "", h));
  c.appendChild(hist);
  return c;
}

/* ============================================================= Ajanlar paneli
   Bu sohbetin alt ajanları (task aracı, alice/subagents.py): canlı durum, son araç, durdurma ve biten ajana devam
   mesajı. Altında özel alt ajan tanımları (~/.alice/agents, <proje>/.alice/agents): ekle, düzenle, sil. */
const AGP = { busy: false, editing: null, again: false, stale: false };
const AG_STATUS = { running: ["Çalışıyor", "pending"], done: ["Bitti", "success"], failed: ["Hata", "failure"],
  stopped: ["Durduruldu", "none"] };
function onSubagentEvent() {
  if (!(PREFS.panel && PREFS.tab === "agents") || AGP.editing) return;
  // Devam kutusuna yazılırken yeniden çizme: yazılan metin ve odak kayboluyordu. Kutu boşalınca tazelenir.
  const typing = [...document.querySelectorAll("#panelBody .agp-send")].some((i) => i.value || i === document.activeElement);
  if (typing) { AGP.stale = true; return; }
  renderAgentsPane();
}
async function agAct(path, body) {
  const r = await api(path, body);
  if (!r || !r.ok) toast((r && r.error) || "olmadı");
  else if (r.note) toast(r.note);
  renderAgentsPane();
  return r && r.ok;
}
async function renderAgentsPane() {
  const body = $("#panelBody");
  if (AGP.busy) { AGP.again = true; return; }
  AGP.busy = true;
  let r;
  try { r = await api("/api/subagents"); } finally { AGP.busy = false; }
  if (AGP.again) { AGP.again = false; setTimeout(renderAgentsPane, 50); }
  if (!(PREFS.panel && PREFS.tab === "agents") || !r) return;
  const wrap = el("div", "prp");
  const head = el("div", "prp-head");
  head.append(el("b", "", "Alt ajanlar"), el("span", "grow"), iconAct(IC.reload, "Tazele", renderAgentsPane));
  wrap.appendChild(head);
  if (!(r.subs || []).length) {
    wrap.appendChild(emptyState(IC.spark, "Bu sohbette alt ajan yok",
      "ALICE bir işi alt ajana verince (\"bunu arka planda araştır\", \"üç modülü paralel incele\") burada canlı " +
      "görünür: ne yaptığı, kaç adım attığı. Durdurabilir, biten ajana devam mesajı yazabilirsin."));
  }
  for (const s of r.subs || []) wrap.appendChild(subCard(s));
  const dh = el("div", "prp-head agp-defs");
  const add = el("button", "act", "Yeni ajan");
  const defsBox = el("div", "prp");
  add.addEventListener("click", () => { AGP.editing = {}; drawAgentDefs(defsBox, r.defs || []); });
  dh.append(el("b", "", "Özel ajanlar"), el("span", "grow"), add);
  wrap.appendChild(dh);
  drawAgentDefs(defsBox, r.defs || []);
  wrap.appendChild(defsBox);
  body.replaceChildren(wrap);
}
function subCard(s) {
  const c = el("div", "prp-card");
  const [label, cls] = AG_STATUS[s.status] || [s.status, "none"];
  const top = el("div", "prp-top");
  const dot = el("span", "prp-dot " + cls);
  dot.title = label;
  top.append(dot, el("span", "prp-title", `${s.id} · ${s.label}`));
  c.appendChild(top);
  const bits = [label, `${s.steps} adım`, `${s.secs} sn`];
  if (s.kind) bits.push(s.kind);
  if (s.background) bits.push("arka planda");
  if (s.turns > 1) bits.push(`${s.turns}. tur`);
  c.appendChild(el("div", "prp-meta", bits.join(" · ")));
  if (s.status === "running" && s.last) c.appendChild(el("div", "prp-status", s.last));
  if (s.result) c.appendChild(el("div", "agp-result", s.result));
  const acts = el("div", "prp-acts");
  if (s.status === "running") {
    const b = el("button", "act", "Durdur");
    b.addEventListener("click", () => { b.disabled = true; agAct("/api/subagents/stop", { id: s.id }); });
    acts.appendChild(b);
  } else {
    const inp = el("input", "agp-send");
    inp.type = "text";
    inp.placeholder = "Bu ajana devam mesajı…";
    const go = el("button", "act", "Gönder");
    const send = () => {
      const t = inp.value.trim();
      if (!t || go.disabled) return;
      go.disabled = true;
      agAct("/api/subagents/send", { id: s.id, text: t });
    };
    go.addEventListener("click", send);
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
    inp.addEventListener("blur", () => { if (!inp.value && AGP.stale) { AGP.stale = false; renderAgentsPane(); } });
    acts.append(inp, go);
  }
  c.appendChild(acts);
  return c;
}
function drawAgentDefs(box, defs) {
  box.replaceChildren();
  if (AGP.editing) box.appendChild(agentForm(AGP.editing, box, defs));
  if (!defs.length && !AGP.editing) {
    box.appendChild(el("div", "prp-foot", "Tanımlı özel ajan yok. Bir rol (ör. salt okuyan inceleyici, test yazıcı) " +
      "tanımlarsan ALICE işe uyunca onu çağırır. Sohbette \"bana bir test yazıcı ajan tanımla\" da diyebilirsin."));
  }
  for (const a of defs) {
    const c = el("div", "prp-card");
    const top = el("div", "prp-top");
    top.append(el("span", "prp-title", a.name), el("span", "grow"),
      iconAct(IC.edit, "Düzenle", () => { AGP.editing = { ...a, editing: true }; drawAgentDefs(box, defs); }),
      iconAct(IC.trash, "Sil", async () => {
        if (await confirmDialog("Ajanı sil", `${a.name} tanımı silinecek (${a.path}).`, "Sil")) {
          agAct("/api/agents/delete", { name: a.name });
        }
      }));
    c.append(top, el("div", "prp-status", a.description),
      el("div", "prp-meta", [`araçlar: ${a.tools.length ? a.tools.join(", ") : "hepsi"}`,
        `model: ${a.model || "ana ajan"}`, `${a.max_steps} adım`, a.where === "project" ? "bu projede" : "her projede"]
        .join(" · ")));
    box.appendChild(c);
  }
}
function agentForm(a, box, defs) {
  const f = el("div", "prp-card agp-form");
  const field = (label, node) => { const w = el("label", "agp-field"); w.append(el("span", "", label), node); return w; };
  const input = (v, ph) => { const i = el("input"); i.type = "text"; i.value = v || ""; i.placeholder = ph; return i; };
  const name = input(a.name, "test-yazici");
  if (a.editing) name.disabled = true;
  const desc = input(a.description, "Değişen koda birim test yazar; uygulama kodunu değiştirmez");
  const prompt = el("textarea");
  prompt.rows = 7;
  prompt.value = a.prompt || "";
  prompt.placeholder = "Sen titiz bir test yazarısın. Verilen dosyaları oku, eksik durumlar için test yaz, testleri " +
    "koş ve sonucu kısa raporla…";
  const tools = input((a.tools || []).join(", "), "boş: hepsi · ör. read_file, grep, glob, list_dir, git");
  const model = input(a.model, "boş: ana ajan · brain, fast ya da sağlayıcı:model");
  const steps = el("input");
  steps.type = "number"; steps.min = 1; steps.max = 200; steps.value = a.max_steps || 40;
  const where = el("select");
  for (const [v, t] of [["user", "Her projede (~/.alice/agents)"], ["project", "Yalnız bu projede (.alice/agents)"]]) {
    const o = el("option", "", t); o.value = v; where.appendChild(o);
  }
  where.value = a.where || "user";
  const err = el("div", "prp-chip bad");
  err.hidden = true;
  const save = el("button", "btn accent", a.editing ? "Kaydet" : "Ekle");
  const cancel = el("button", "act", "Vazgeç");
  cancel.addEventListener("click", () => { AGP.editing = null; drawAgentDefs(box, defs); });
  save.addEventListener("click", async () => {
    save.disabled = true;
    const r = await api("/api/agents/save", { name: name.value, description: desc.value, prompt: prompt.value,
      tools: tools.value.split(",").map((t) => t.trim()).filter(Boolean), model: model.value,
      max_steps: Number(steps.value) || 40, where: where.value });
    save.disabled = false;
    if (!r || !r.ok) { err.textContent = (r && r.error) || "kaydedilemedi"; err.hidden = false; return; }
    AGP.editing = null;
    toast(r.note || "kaydedildi");
    renderAgentsPane();
  });
  const acts = el("div", "prp-acts");
  acts.append(save, cancel);
  f.append(field("Ad", name), field("Ne zaman kullanılır", desc), field("Talimat", prompt), field("Araçlar", tools),
    field("Model", model), field("En fazla adım", steps), field("Nerede", where), err, acts);
  return f;
}
