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
    m.appendChild(el("div", "menu-title", _("Permission mode")));
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
  off: [_("doesn't think"), _("Fastest: answers right away. For simple tasks")],
  low: [_("thinks a little"), _("Thinks briefly, then acts (recommended)")],
  medium: [_("thinks more"), _("A bit more careful, a bit slower")],
  high: [_("thinks a lot"), _("For hard tasks; needlessly slow on simple ones")],
  xhigh: [_("even more"), _("Extra High: for very hard tasks")],
  max: [_("the most"), _("Max: for the hardest tasks, the slowest")],
  default: [_("the model decides"), _("The model's own setting")],
};
// Yalnız açık/kapalı destekleyen modeller (Qwen, GLM, Kimi...): seviye yerine kapalı/açık gösterilir.
const THINK_BINARY = { off: [_("doesn't think"), _("Answers right away without thinking")], high: [_("thinks"), _("Thinks before answering")],
  default: [_("the model decides"), _("The model's own setting")] };
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
  toast(_("Thinking: {level}", { level: (thinkLabel() || THINK[level] || [level])[0] }));
}

function thinkMenu() {
  openMenu($("#thinkBtn"), (m) => {
    m.appendChild(el("div", "menu-title", _("Thinking · {model}", { model: shortModel(S.status.model) })));
    const opts = thinkOptions();
    if (S.status.thinking_kind === "none") {
      m.appendChild(el("div", "menu-note", _("This model has no thinking setting; it works its own way.")));
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
      m.appendChild(el("div", "menu-title", _("Main model")));
      menuItem(m, IC.spark, _("Automatic"), _("The first suitable model in the chain; moves to the next at its limit"),
        () => pinModel("reset"), !pinned);
      // Sağlayıcıya göre gruplu; gizlenen sağlayıcı/modeller ve anahtarı olmayanlar menüde görünmez.
      for (const p of provs) {
        const rows = p.enabled && !p.hidden ? p.models.filter((x) => !x.hidden) : [];
        if (p.name === "chatgpt" && p.enabled && !p.hidden && !rows.length) {
          // giriş var ama liste henüz yok: boş geçme, nedenini söyle
          m.appendChild(el("div", "menu-title", "chatgpt"));
          menuItem(m, null, p.loading ? _("Reading the model list…") : _("Could not get the model list"),
            p.loading ? _("open the menu again in a few seconds") : (p.error || "").slice(0, 120) + _(" · you can add them by hand in Setup"),
            () => showSettings("setup"));
          continue;
        }
        if (!rows.length && p.enabled && !p.hidden && p.error) {
          // Eklendi ama hiçbir modeli cevap vermiyor (kota, bakiye, yetki): sessizce kaybolmasın, nedenini söyle.
          m.appendChild(el("div", "menu-title", provLabel(p)));
          menuItem(m, null, _("The models aren't working"), p.error.slice(0, 160), () => showSettings("models"));
          continue;
        }
        if (!rows.length) continue;
        m.appendChild(el("div", "menu-title", provLabel(p) + (p.note ? " · limit " + p.note : "")));
        for (const x of rows) {
          menuItem(m, null, x.model, x.ok ? "" : x.status, () => pinModel(x.ref), pinned === x.ref);
        }
      }
      m.appendChild(el("div", "menu-sep"));
      menuItem(m, IC.list, _("Manage models"), _("Hide/show providers or models, see their status"), () => showSettings("models"));
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
  if (!r.ok) return toast(r.error || _("Could not save"));
  applyStatus(r);
  toast((hidden ? _("Hidden: {name}", { name: label || name }) : _("Shown: {name}", { name: label || name })));
}
/* Modeller: üstte şu an kullanılan model ve düşünme, ortada sağlayıcı kartları (hazır / anahtar bekliyor / gizli),
   en altta katlanmış "Gelişmiş": puanlama ağırlıkları ve rol sıraları. */
const PROV_LABEL = { gemini: "Google Gemini", openrouter: "OpenRouter", groq: "Groq", nvidia: "NVIDIA NIM", cerebras: "Cerebras",
  mistral: "Mistral", sambanova: "SambaNova", cloudflare: "Cloudflare", cohere: "Cohere", vercel: "Vercel AI Gateway",
  zai: "Z.ai", ollama: _("Ollama (local)"), chatgpt: _("ChatGPT account"), cliproxy: "Claude Code proxy" };
/* Sağlayıcının görünen adı. Proxy modelleri sağlayıcı sağlayıcı gruplanır (Claude, Kimi...): onlar kendi etiketini getirir. */
function provLabel(p) { return p.label || PROV_LABEL[p.name] || p.name; }
// Sağlayıcı kartlarının açık/kapalı durumunu saklar; bir ayar değiştiğinde sayfa yenilense bile
// kullanıcının açık bıraktığı sağlayıcılar hatırlanır, ilk sıradaki zorla açılmaz.
const _modelsOpenSet = new Set();

function modelsPage(pane, data) {
  const all = data.all || { providers: [] };
  pane.append(el("h2", "", _("Models")),
    el("p", "", _("ALICE picks the best free model for each task and moves to the next one when one hits its limit. You can pin a model if you like.")));
  // Şu an kullanılan
  const hero = el("div", "mp-hero");
  const cur = S.status.model || "";
  const [prov, ...rest] = cur.split(":");
  const hl = el("div", "mp-cur");
  hl.append(el("small", "", S.status.brain_pinned ? _("Pinned model") : _("Automatic choice · now")),
    el("b", "", rest.join(":") || cur || "model yok"), el("span", "mp-prov", PROV_LABEL[prov] || prov || ""));
  const ha = el("div", "mp-acts");
  const change = el("button", "btn"); change.append(svg(IC.spark), document.createTextNode(_("Choose a model")));
  change.addEventListener("click", () => modelMenu(change));
  ha.appendChild(change);
  if (S.status.brain_pinned) {
    const auto = el("button", "btn", _("Back to automatic"));
    auto.addEventListener("click", async () => { await pinModel("reset"); showSettings("models"); });
    ha.appendChild(auto);
  }
  hero.append(hl, ha);
  const think = el("div", "mp-think");
  think.append(el("span", "lbl", _("Thinking level")), segmented(thinkOptions().map(([k, [l]]) => [k, l]),
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
  sh.append(el("div", "sec", _("Providers · {ready}/{total} ready", { ready, total: provs.length })));
  const f = el("input"); f.type = "search"; f.placeholder = _("Search models");
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
          st === "ok" ? _("{shown}/{total} models on", { shown, total: p.models.length }) : st === "hidden" ? _("hidden") :
          !hasModels && p.enabled ? "model yok" :
          p.name === "chatgpt" ? _("not signed in") : _("no {key}", { key: p.key || _("key") })));
      const pill = el("span", "pill " + (st === "ok" ? "done" : ""),
        failing ? _("error") : st === "ok" ? _("ready") : st === "hidden" ? _("hidden") : !hasModels && p.enabled ? _("no model") : _("waiting"));
      head.append(name, pill);
      card.appendChild(head);
      const acts = el("div", "pc-acts");
      if (!p.enabled && !p.hidden) {
        const add = el("button", "btn", p.name === "chatgpt" ? _("Sign in") : _("Add a key"));
        add.addEventListener("click", () => showSettings(p.name === "chatgpt" ? "setup" : "apikeys"));
        acts.appendChild(add);
      }
      if (p.models.length) {
        const more = el("button", "btn ghost", card.classList.contains("open") ? "Modelleri gizle" : `Modeller (${p.models.length})`);
        more.addEventListener("click", () => { openSet.has(p.name) ? openSet.delete(p.name) : openSet.add(p.name); draw(); });
        acts.appendChild(more);
      }
      const hb = el("button", "btn ghost", p.hidden ? _("Show") : _("Hide"));
      hb.title = p.hidden ? _("Put it back in the automatic chain") : _("Don't use it in the automatic chain or the model menu");
      hb.addEventListener("click", async () => { await hideModel(p.name, !p.hidden, provLabel(p)); showSettings("models"); });
      acts.appendChild(hb);
      card.appendChild(acts);
      if (card.classList.contains("open") && rows.length) {
        const list = el("div", "pc-models");
        for (const x of rows) {
          const row = el("div", "pc-model" + (x.hidden ? " off" : "") + (all.pinned === x.ref ? " pinned" : ""));
          const l = el("span", "l");
          l.append(el("span", "m", x.model), el("small", "", x.tested ? x.status : (x.ok ? _("ready · not measured") : x.status)));
          const q = el("span", "q"); q.title = _("Quality score"); q.style.setProperty("--q", (x.quality || 0) + "%");
          const pin = el("button", "icon-btn" + (all.pinned === x.ref ? " on" : ""));
          pin.title = all.pinned === x.ref ? _("Unpin") : _("Pin this model");
          pin.appendChild(svg(IC.pin));
          pin.addEventListener("click", async () => { await pinModel(all.pinned === x.ref ? "reset" : x.ref); showSettings("models"); });
          const tb = switchEl(!x.hidden, x.hidden ? _("Show") : _("Hide"), async (on) => { await hideModel(x.ref, !on); showSettings("models"); });
          row.append(l, q, pin, tb);
          list.appendChild(row);
        }
        card.appendChild(list);
      }
      grid.appendChild(card);
    }
    if (!grid.children.length) grid.appendChild(el("p", "", _("No matching models.")));
  };
  f.addEventListener("input", draw);
  draw();

  // Gelişmiş
  const adv = el("details", "mp-adv");
  adv.appendChild(el("summary", "", _("Advanced: scoring weights and role orders")));
  adv.appendChild(scorePane());
  const names = { brain: _("Main model"), fast: _("Quick tasks"), vision: _("Images"), voice: _("Voice") };
  const chains = el("div", "mp-chains");
  for (const [role, rows] of Object.entries(data.models || {})) {
    const c = el("div", "mp-chain");
    c.appendChild(el("b", "", _("{role} order", { role: names[role] || role })));
    const ol = el("ol");
    for (const r of rows) {
      const li = el("li", r.ok ? "ok" : "");
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
  toast(ref === "reset" ? _("Automatic model choice") : _("Model pinned: {model}", { model: shortModel(ref) }));
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
// Esc ya da dışarı tıklamak da _("Cancel") sayılır; onay bekleyen kod askıda kalmasın.
function closeDialog() {
  $("#overlay").hidden = true;
  const f = onDialogClose;
  onDialogClose = null;
  if (f) f();
}
$("#overlay").addEventListener("mousedown", (e) => { if (e.target.id === "overlay") closeDialog(); });
function dialogClose(d, label) {
  const row = el("div", "row");
  const b = el("button", "btn primary", label || _("Close"));
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
      const no = el("button", "btn", _("Cancel"));
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
      head.append(el("h2", "", opts.title || _("Choose a folder")));
      const tabs = el("div", "seg fp-tabs");
      const tFs = el("button", "on", _("Computer")), tGh = el("button", "", "GitHub");
      if (opts.github) tabs.append(tFs, tGh);
      head.append(el("span", "grow"), tabs);
      const body = el("div", "fp-body");
      const side = el("nav", "fp-side");
      const main = el("div", "fp-main");
      const crumbs = el("div", "fp-crumbs");
      const tools = el("div", "fp-tools");
      const filter = el("input"); filter.type = "text"; filter.placeholder = _("Search this folder");
      const hidden = el("label", "chk"); const hcb = el("input"); hcb.type = "checkbox";
      hidden.append(hcb, document.createTextNode(" " + _("Show hidden")));
      tools.append(filter, hidden);
      const list = el("div", "fp-list");
      const err = el("div", "note err");
      main.append(crumbs, tools, list, err);
      body.append(side, main);
      const foot = el("div", "fp-foot");
      const cur = el("div", "fp-cur");
      const mk = el("button", "btn"); mk.append(svg(IC.plus), document.createTextNode(_("New folder")));
      const no = el("button", "btn", _("Cancel"));
      const ok = el("button", "btn primary", opts.okLabel || _("Choose this folder"));
      foot.append(cur, el("span", "grow"), mk, no, ok);
      const gh = el("div", "fp-gh"); gh.hidden = true;
      d.append(head, body, gh, foot);
      let state = null;
      const go = async (path) => {
        err.textContent = "";
        const r = await api("/api/dirs?path=" + encodeURIComponent(path || "") + (hcb.checked ? "&hidden=1" : ""));
        if (!r || !r.ok) { err.textContent = (r && r.error) || _("could not open"); return; }
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
        side.replaceChildren(el("div", "fp-sec", _("Places")));
        for (const pl of state.places || []) side.appendChild(place(IC[pl.icon] || IC.folder, pl.label, pl.path));
        if ((state.recent || []).length) {
          side.appendChild(el("div", "fp-sec", _("Recent")));
          for (const r of state.recent) side.appendChild(place(IC.clock, short(r), r));
        }
        crumbs.replaceChildren();
        const up = el("button", "icon-btn"); up.title = _("Parent folder"); up.appendChild(svg(IC.up));
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
        const f = filter.value.trim().toLocaleLowerCase(ALICE_LOCALE);
        const rows = state.dirs.filter((x) => !f || x.name.toLocaleLowerCase(ALICE_LOCALE).includes(f));
        list.replaceChildren();
        if (!rows.length) list.appendChild(el("div", "fp-empty", f ? _("No matching folders") : _("No subfolders. You can choose this folder or create a new one.")));
        for (const x of rows) {
          const b = el("button", "fp-row");
          b.append(svg(IC.folder), el("span", "t", x.name));
          if (x.git) b.appendChild(el("span", "tag", "git"));
          b.appendChild(svg(IC.chev, "chev"));
          b.title = _("Open (double-click: choose)");
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
        const inp = el("input"); inp.type = "text"; inp.placeholder = _("Name of the new folder");
        const save = el("button", "btn primary", _("Create"));
        const create = async () => {
          const r = await api("/api/dirs/mkdir", { parent: state.path, name: inp.value });
          if (!r || !r.ok) { err.textContent = (r && r.error) || _("didn't work"); return; }
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
  const path = await pickFolder({ title: _("Working folder"), github: true, okLabel: _("Work here") });
  if (!path) return;
  const r = await api("/api/cwd", { path });
  if (!r.ok) return toast(r.error || _("The folder didn't change"));
  applyStatus(r); toast(_("Folder: {path}", { path }));
}
// GitHub'daki depolar: tıklanınca ~/GitHub altına klonlanır (varsa güncellenir) ve çalışma klasörü olur.
function githubPicker(err) {
  const box = el("div", "gh-pick");
  const q = el("input"); q.type = "text"; q.placeholder = _("Search GitHub repositories…");
  const list = el("div", "gh-list");
  const note = el("div", "note", _("Loading repositories…"));
  box.append(el("h3", "", _("Open from GitHub")), q, note, list);
  let repos = [];
  const draw = () => {
    const f = q.value.trim().toLocaleLowerCase(ALICE_LOCALE);
    const rows = repos.filter((r) => !f || (r.full_name + " " + r.description).toLocaleLowerCase(ALICE_LOCALE).includes(f));
    list.replaceChildren(...rows.slice(0, 50).map((r) => {
      const b = el("button", "list-row gh-row");
      const l = el("span", "l", r.full_name);
      if (r.description) l.title = r.description;
      b.append(l, el("span", "r" + (r.local ? " ok" : ""), r.local ? "bilgisayarda" : r.private ? "gizli" : ""));
      b.addEventListener("click", async () => {
        list.querySelectorAll("button").forEach((x) => (x.disabled = true));
        note.textContent = r.local ? _("Updating {repo}…", { repo: r.full_name }) : _("Cloning {repo}…", { repo: r.full_name });
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
    note.textContent = rows.length ? "" : (repos.length ? _("No matching repositories") : note.textContent);
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
  $("#changesBtn").title = _("{n} files changed in this session", { n });
  $("#chCount").textContent = n ? String(n) : "";
}
async function revertFile(c) {
  const ok = await confirmDialog(_("Undo the change?"),
    (c.status === "eklendi" ? _("{path} goes back to how it was at the start of this session (deleted, since ALICE created it).", { path: c.path })
      : _("{path} goes back to how it was at the start of this session.", { path: c.path })), _("Undo"));
  if (!ok) return;
  const r = await api("/api/changes/revert", { path: c.abs });
  if (!r.ok) return toast(r.error || _("Could not undo"));
  S.changes = r.changes || [];
  drawChangesChip();
  renderPanel();
  toast(_("{path} undone", { path: c.path }));
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
      body.appendChild(emptyState(IC.list, _("No plan yet"),
        _("When ALICE starts a multi-step task it lists the steps here and checks them off as it goes.")));
    } else body.appendChild(renderTodo(S.todo, true));
  } else if (PREFS.tab === "changes") {
    if (!S.changes.length) {
      body.appendChild(emptyState(IC.diff, _("No changes"),
        _("When ALICE changes a file with the file tools in this session, the diff shows here. You can undo it file by file.")));
    } else {
      const { a, d, n } = changeTotals();
      const head = el("div", "ch-sum");
      head.append(el("b", "", _("{n} files changed", { n })), el("span", "a", `+${a}`), el("span", "d", `−${d}`));
      const fold = el("button", "act", _("Collapse all"));
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
        rv.title = _("Undo this file");
        rv.append(svg(IC.rewind), document.createTextNode(_("Undo")));
        rv.addEventListener("click", (e) => { e.stopPropagation(); revertFile(c); });
        row.appendChild(rv);
        row.addEventListener("click", () => f.classList.toggle("open"));
        const inner = el("div", "ch-body");
        if (c.binary) inner.appendChild(el("div", "note", _("Binary file, no diff to show")));
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
    const cp = el("button", "btn", _("Summarize now"));
    cp.addEventListener("click", () => runCommand("/compact"));
    ctx.append(el("div", "sec", _("Context usage")), big, meter,
      el("p", "", _("The model's window is {win} tokens. At {limit} tokens the older conversation is summarized automatically. The number is the tokens the provider counted on the last request (tool definitions included).",
        { win: fmtTok(win), limit: fmtTok(st.ctx_limit || 0) })), cp);
    body.appendChild(ctx);
    const kv = el("dl", "kv");
    const mins = Math.floor((Date.now() - S.started) / 60000);
    const { a, d, n } = changeTotals();
    for (const [k, v] of [[_("Model"), st.model || _("none")], [_("Thinking level"), (THINK[st.thinking] || [st.thinking || "?"])[0]],
      [_("Permission mode"), (MODES[st.mode] || [st.mode])[0]], [_("Decider"), st.decider || "-"], [_("Folder"), st.cwd || ""],
      [_("Git branch"), st.branch || "-"], [_("Session"), st.session || ""], [_("Open for"), mins < 1 ? _("under 1 min") : _("{m} min", { m: mins })],
      [_("Today"), _("{n} requests", { n: st.today_requests || 0 })], [_("Changes"), n ? _("{n} files", { n }) + ` +${a} −${d}` : _("none")]]) {
      kv.append(el("dt", "", k), el("dd", "", v));
    }
    const sec = el("div", "sec", _("Session"));
    body.append(sec, kv);
  }
  body.scrollTop = keep;
}

/* ============================================================ komut paleti */
function paletteItems() {
  const items = [];
  const act = (group, icon, label, hint, run, key) => items.push({ group, icon, label, hint, run, key });
  act(_("Actions"), IC.chat, _("New chat"), "", newChat, "Ctrl+Shift+O");
  act(_("Actions"), IC.panel, PREFS.panel ? _("Close the side panel") : _("Open the side panel"), "", togglePanel, "Ctrl+J");
  act(_("Actions"), IC.list, _("Show the plan"), _("To-do list"), () => openPanel("plan"));
  act(_("Actions"), IC.diff, _("Show changes"), _("Files changed in this session"), () => openPanel("changes"));
  act(_("Actions"), IC.gauge, _("Context and session info"), "", () => openPanel("context"));
  act(_("Actions"), IC.expand, PREFS.verbose ? _("Hide tool details") : _("Show tool details"), _("Expand/collapse all tool output"), toggleVerbose, "Ctrl+O");
  act(_("Actions"), IC.folder, _("Change the working folder"), S.status.cwd || "", askCwd);
  act(_("Actions"), IC.spark, _("Summarize the conversation"), "/compact", () => runCommand("/compact"));
  act(_("Actions"), IC.rewind, _("Rewind to the last message"), "/rewind", () => runCommand("/rewind"));
  act(_("Actions"), IC.update, _("Check for updates"), _("The latest version on GitHub"), checkUpdate);
  act(_("Actions"), IC.gear, _("Settings"), "", () => showSettings("general"), "Ctrl+,");
  act(_("Go to"), IC.list, _("Projects"), _("Agent teams"), () => goSection("projects"));
  act(_("Go to"), IC.check, _("Tasks"), _("Duties it runs in the background, and the approval box"), () => goSection("tasks"));
  act(_("Go to"), IC.bolt, _("Automations"), _("Rules, scheduled tasks, notes, logs"), () => goSection("auto"));
  act(_("Go to"), IC.term, _("System"), _("Core, assistant, voice, setup, usage"), () => goSection("system"));
  act(_("Go to"), IC.plug, _("Connections"), _("Services and API keys"), () => goSection("connect"));
  act(_("Actions"), IC.keyboard, _("Keyboard shortcuts"), "", () => showSettings("keys"));
  for (const [k, label] of [["system", _("System")], ["light", _("Light")], ["dark", _("Dark")]]) {
    act(_("Theme"), k === "dark" ? IC.moon : IC.sun, _("Theme: {name}", { name: label }), "", () => setPref("theme", k));
  }
  for (const [k, [label, sub, icon]] of Object.entries(MODES)) act(_("Permission mode"), icon, _("Mode: {name}", { name: label }), sub, () => setMode(k));
  for (const [k, [label, sub]] of thinkOptions()) act(_("Thinking level"), IC.brain, _("Thinking: {level}", { level: label }), sub, () => setThinking(k));
  act(_("Model"), IC.spark, _("Model: Automatic"), _("The first suitable model in the chain"), () => pinModel("reset"));
  for (const ref of S.status.brain_chain || []) act(_("Model"), IC.spark, _("Model: {name}", { name: shortModel(ref) }), ref, () => pinModel(ref));
  for (const c of S.commands) {
    act(_("Commands"), IC.term, "/" + c.name, c.description, () => {
      input.value = "/" + c.name + " "; autosize(); input.focus();
    });
  }
  for (const s of S.sessions || []) act(_("Chats"), IC.chat, s.title || _("(empty)"), relTime(s.updated), () => resume(s.id));
  return items;
}
// Sırayla geçen harf eşleşmesi; ardışık harfler ve kelime başları daha yüksek puan alır.
function fuzzy(q, text) {
  if (!q) return 1;
  const t = text.toLocaleLowerCase(ALICE_LOCALE);
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
  const q = $("#palInput").value.trim().toLocaleLowerCase(ALICE_LOCALE);
  const scored = PAL.items.map((it) => ({ it, sc: Math.max(fuzzy(q, it.label), fuzzy(q, it.group + " " + it.label) * 0.6, fuzzy(q, it.hint || "") * 0.4) }))
    .filter((x) => x.sc > 0);
  if (q) scored.sort((x, y) => y.sc - x.sc);
  PAL.shown = scored.slice(0, 60).map((x) => x.it);
  PAL.sel = Math.min(PAL.sel, Math.max(0, PAL.shown.length - 1));
  const box = $("#palList");
  box.replaceChildren();
  if (!PAL.shown.length) { box.appendChild(el("div", "pal-empty", _("No results"))); return; }
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
  toast(PREFS.verbose ? _("Tool details shown") : _("Tool details hidden"));
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
  [_("Files and code"), /^(read_file|write_file|edit_file|multi_edit|list_dir|glob|grep|file_ops|notebook_edit|apply_patch|git|github|find_files)$/],
  [_("Command"), /^(run_command|process_output|process_stop|blender|sandbox)/],
  ["Web", /^(web_|http_request|browser)/],
  [_("Computer and screen"), /^(computer|windows|ui_|clipboard|reflex|system_info|process_list|process_kill|pc|screen|vision)/],
  [_("Agents and plans"), /^(todo|task|team|council|run_plan|ask_user|skill|load_tools|project|agent)/],
  [_("Memory and automation"), /^(memory|feedback|rules|schedule|feeds|notes|report|metrics|action_log|weather)/],
  [_("Home, phone, media"), /^(wifi|android|tv|samsung|media|youtube|spotify|discord|steam|phone|modem|home|whatsapp|gmail|calendar)/],
  [_("Games and flight"), /^(flight|dcs|war|sim|game)/],
  [_("MCP tools"), /^mcp__/],
];
const RISK_LABEL = { read: _("reads"), write: _("writes"), exec: _("command"), computer: _("screen"), network: _("network"), send: _("sends"), danger: _("dangerous") };
function toolCat(name) {
  if (name.startsWith("mcp__")) return _("MCP tools");
  for (const [c, re] of TOOL_CATS) if (re.test(name)) return c;
  return _("Other");
}
function toolsPage(pane, tools) {
  pane.append(el("h2", "", _("Tools")),
    el("p", "", _("ALICE can use {n} tools. You can add MCP servers under [mcp.servers] in config.toml.", { n: tools.length })));
  const bar = el("div", "tl-bar");
  const f = el("input"); f.type = "search"; f.placeholder = _("Search tools");
  bar.appendChild(f);
  const chips = el("div", "tl-chips");
  const cats = [_("All"), ...TOOL_CATS.map(([c]) => c), _("Other")].filter((c) => c === _("All") || tools.some((t) => toolCat(t.name) === c));
  let cur = _("All");
  for (const c of cats) {
    const n = c === _("All") ? tools.length : tools.filter((t) => toolCat(t.name) === c).length;
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
    const rows = tools.filter((t) => (cur === _("All") || toolCat(t.name) === cur) && (!q || (t.name + t.description).toLowerCase().includes(q)));
    const groups = new Map();
    for (const t of rows) { const c = toolCat(t.name); if (!groups.has(c)) groups.set(c, []); groups.get(c).push(t); }
    for (const [c, list] of groups) {
      if (cur === _("All")) box.appendChild(el("div", "sec", `${c} · ${list.length}`));
      // MCP Araçları çok fazla olduğunda sunucu bazında alt gruplara ayır
      if (c === _("MCP tools") && list.length > 6) {
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
          if (q || cur === _("MCP tools")) det.open = true;
          const sum = el("summary", "", _("{server} ({n} tools)", { server: srv.toUpperCase(), n: srvTools.length }));
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
    if (!rows.length) box.appendChild(el("p", "", _("No matching tools.")));
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
  pane.append(el("h2", "", _("Health")),
    el("p", "", _("Are ALICE's parts working? Problems are at the top, with how to fix them.")));
  const tiles = el("div", "hl-tiles");
  for (const [n, label, cls] of [[bad.length, _("need attention"), bad.length ? "warn" : "ok"], [ok.length, _("working"), "ok"], [off.length, _("off"), "off"]]) {
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
  if (bad.length) { pane.appendChild(el("div", "sec", _("Needs attention"))); const b = el("div", "hl-list"); bad.forEach((c) => b.appendChild(row(c))); pane.appendChild(b); }
  if (ok.length) { pane.appendChild(el("div", "sec", _("Working"))); const b = el("div", "hl-list"); ok.forEach((c) => b.appendChild(row(c))); pane.appendChild(b); }
  if (off.length) {
    const det = el("details", "hl-off");
    det.appendChild(el("summary", "", _("Off ({n})", { n: off.length })));
    const b = el("div", "hl-list"); off.forEach((c) => b.appendChild(row(c))); det.appendChild(b);
    pane.appendChild(det);
  }
  // Sağlayıcıları dene (alice doctor)
  const sec = el("div", "sec", _("Try the model providers"));
  const box = el("div", "hl-doc");
  pane.append(sec, box);
  const marks = { ok: "ok", warn: "warn", off: "off", fail: "fail", skip: "off" };
  let timer = null;
  const drawDoc = (doc) => {
    box.replaceChildren();
    const head = el("div", "hl-doc-head");
    const l = el("span", "l");
    l.append(el("b", "", _("Try all")), el("small", "", doc.state === "running" ? ((doc.lines || []).slice(-1)[0] || _("starting…"))
      : _("Sends a short request to each provider and tries the decision engine (alice doctor).")));
    const b = el("button", "btn primary", doc.state === "running" ? _("Trying…") : doc.result ? _("Try again") : _("Try all"));
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
      dc.append(el("i", "dot"), el("b", "", _("decision engine")), el("small", "", doc.result.decider));
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
  pane.append(el("div", "sec", _("KAIROS · background watch")), box);
  const when = (ts) => new Date(ts * 1000).toLocaleString(ALICE_LOCALE, { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  const draw = (k) => {
    if (!k) { box.replaceChildren(el("p", "", _("Could not read the status."))); return; }
    const wake = el("button", "btn", _("Wake now"));
    wake.disabled = !k.enabled;
    wake.addEventListener("click", () => api("/api/kairos", { action: "wake" }).then((r) => {
      if (r && r.ok) { toast(_("KAIROS will wake on the core's next tick")); draw(r); }
    }));
    const sub = k.enabled
      ? (k.next ? _("Next wake ") + when(k.next) : _("First wake within a few minutes")) + _(" · {n} wakes today", { n: k.wakes || 0 }) +
        (k.skipped ? ` · son erteleme: ${k.skipped}` : "")
      : _("While off, ALICE works only when you ask");
    box.replaceChildren(
      setRow(_("Watch"), _("ALICE wakes at intervals it picks and keeps an eye on things; it tells you only when something is really useful. It only reads and changes nothing."),
        segmented([["1", _("On")], ["0", _("Off")]], k.enabled ? "1" : "0",
          (x) => api("/api/kairos", { action: x === "1" ? "on" : "off" }).then((r) => { if (r && r.ok) { toast("KAIROS " + (x === "1" ? _("turned on") : _("turned off"))); draw(r); } }))),
      setRow(_("Status"), sub, wake));
    const hist = el("div", "kairos-hist");
    for (const h of k.history || []) {
      const row = el("div", "list-row");
      const l = el("span", "l");
      l.append(el("b", "", when(h.ts)), el("small", "", (h.summary || "") + (h.sleep ? _(" · chose to wake in {m} min", { m: h.sleep }) : "")));
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
  const onOff = (section, key, label) => segmented([["1", _("On")], ["0", _("Off")]], v[section + "." + key] === false ? "0" : "1",
    (x) => set(section, key, x === "1", label));
  pane.append(el("h2", "", _("Assistant")),
    el("p", "", _("Security, search, welcome and notification settings. Whether features work is in the Health tab.")));
  pane.appendChild(el("div", "sec", _("Settings")));
  pane.append(
    setRow(_("Command sandbox"), _("Automatic: in a turn that read outside content (web, email), commands run in a sandbox that can't write outside the project"),
      segmented([["off", _("Off")], ["auto", _("Automatic")], ["on", _("Always")]], v["agent.sandbox"] || "auto",
        (x) => set("agent", "sandbox", x, "Sandbox"))),
    setRow(_("Prompt injection guard"), _("In a turn that read outside content, commands and sending messages need approval"),
      onOff("agent", "taint_guard", _("Guard"))),
    setRow(_("Google search"), _("web_search tries Google (Gemini) first, DuckDuckGo when the quota runs out"),
      onOff("agent", "search_grounding", _("Google search"))),
    setRow(_("Turn token budget"), _("The most tokens one request may spend (cache excluded); past it the turn stops, \"continue\" resumes it"),
      segmented([["0", _("Unlimited")], ["200000", "200K"], ["500000", "500K"], ["1000000", "1M"], ["2000000", "2M"]],
        String(v["agent.turn_token_budget"] || 0), (x) => set("agent", "turn_token_budget", Number(x), _("Turn budget")))),
    setRow(_("Welcome back"), _("After a long absence, a short spoken briefing with follow-ups, calendar and email"),
      onOff("greeting", "enabled", _("Welcome"))),
    setRow(_("Time away"), _("Coming back after this many minutes away counts as a return"),
      segmented([["60", "60"], ["90", "90"], ["120", "120"], ["180", "180"]], String(v["greeting.away_minutes"] || 90),
        (x) => set("greeting", "away_minutes", Number(x), _("Time away")))),
    setRow(_("Conversation summaries"), _("A finished conversation is summarized and follow-ups are extracted"),
      onOff("memory", "digest", _("Summaries"))),
    setRow(_("Email notifications"), _("Tell me when an important new email arrives"), onOff("gmail", "notify", _("Email notifications"))),
    setRow("BUDDY", _("The little companion next to the chat box; talking feeds it, petting cheers it up"),
      segmented([["1", _("On")], ["0", _("Off")]], v["buddy.enabled"] === false ? "0" : "1", (x) => {
        set("buddy", "enabled", x === "1", "BUDDY");
        if (x === "1" && window.buddyShow) window.buddyShow();
        else if (window.buddyRefresh) setTimeout(window.buddyRefresh, 300);
      })),
    setRow(_("BUDDY comments"), _("Let it make a short side comment on conversations now and then"), onOff("buddy", "chatter", _("Comments"))),
  );
  kairosBlock(pane);
  pane.appendChild(el("div", "sec", _("Permission rules")));
  const rules = d.rules || [];
  if (!rules.length) pane.appendChild(el("p", "", _("No rules. They are added here when you choose \"Always allow in this project\" on an approval card.")));
  for (const r of rules) {
    const row = el("div", "list-row");
    const l = el("span", "l");
    l.append(el("b", "", r.rule), el("small", "", (r.kind === "allow" ? "izin ver" : r.kind === "deny" ? "yasak" : "sor") +
      " · " + (r.source === "project" ? _("this project (.alice/permissions.toml)") : _("config.toml (edit the file)")) +
      (r.untrusted ? _(" · not approved, not applied") : "")));
    row.appendChild(l);
    if (r.untrusted) {
      const t = el("button", "btn", _("Approve"));
      t.title = _("This rule file was written or changed outside ALICE (e.g. a cloned repository). It applies once you approve it.");
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
  pane.appendChild(el("div", "sec", _("Gmail accounts")));
  for (const g of d.gmail || []) {
    const row = el("div", "list-row");
    const l = el("span", "l");
    l.append(el("b", "", g.account), el("small", "", g.email || ""));
    const b = el("button", "btn", _("Sign out"));
    b.addEventListener("click", () => api("/api/assistant/action", { action: "gmail_logout", account: g.account })
      .then((x) => { if (x && x.ok) row.remove(); }));
    row.append(l, b);
    pane.appendChild(row);
  }
  if (d.google_ready) {
    const row = el("div", "list-row");
    const inp = el("input", "set-filter");
    inp.placeholder = _("Account name (e.g. school) and Enter: pick that account in the browser");
    inp.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" || !inp.value.trim()) return;
      api("/api/assistant/action", { action: "gmail_connect", account: inp.value.trim() })
        .then((x) => toast(x && x.ok ? _("The permission page opened in the browser") : (x && x.error) || _("could not start")));
      inp.value = "";
    });
    row.appendChild(inp);
    pane.appendChild(row);
  } else {
    const g = el("div", "list-row warn");
    const gl = el("span", "l");
    gl.append(el("b", "", _("Google isn't set up")), el("small", "", _("Step-by-step setup for Calendar and Gmail; it opens the pages itself.")));
    const gb = el("button", "btn primary", _("Go to setup"));
    gb.addEventListener("click", () => showSettings("setup"));
    g.append(gl, gb);
    pane.appendChild(g);
  }
  pane.appendChild(el("div", "sec", _("Skills and subagents")));
  const items = [...(d.skills || []).map((x) => ["skill", x]), ...(d.agents || []).map((x) => ["alt ajan", x])];
  if (!items.length) pane.appendChild(el("p", "", _("None. Add them with ~/.alice/skills/<name>/SKILL.md and ~/.alice/agents/<name>.md (README: skill example).")));
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
  pane.append(el("h2", "", _("Voice")),
    el("p", "", _("ALICE's speaking voice. Listen plays a short sample in that voice (the first time takes a few ")
      + _("seconds). The voice you choose applies from the next \"Hey Alice\" in live conversation.")));
  pane.appendChild(el("div", "sec", _("Enroll my voice")));
  pane.appendChild(enrollCard());
  if (!d.can_preview) pane.appendChild(el("p", "", _("Listening to a sample needs a Gemini key (Connections → Keys).")));
  const voices = (d.voices || []).slice().sort((a, b) => (b.name === d.live) - (a.name === d.live));
  pane.appendChild(el("div", "sec", `Sesler · ${voices.length}`));
  const grid = el("div", "voice-grid");
  for (const v of voices) {
    const on = v.name === d.live;
    const card = el("div", "voice-card" + (on ? " on" : ""));
    const play = el("button", "vc-play");
    play.title = d.can_preview ? "Dinle" : _("A sample needs a Gemini key");
    play.disabled = !d.can_preview;
    play.appendChild(svg('<path d="M8 5v14l11-7z"/>'));
    play.addEventListener("click", () => {
      card.classList.add("loading");
      api("/api/voice/preview", { voice: v.name }).then((r) => {
        card.classList.remove("loading");
        if (!r || !r.ok) return toast((r && r.error) || _("could not make a sample"));
        if (previewAudio) previewAudio.pause();
        for (const c of grid.querySelectorAll(".voice-card.playing")) c.classList.remove("playing");
        card.classList.add("playing");
        previewAudio = new Audio("data:audio/wav;base64," + r.audio);
        previewAudio.addEventListener("ended", () => card.classList.remove("playing"));
        previewAudio.play().catch((e) => toast(_("could not play the audio: ") + (e && e.message || e)));
      });
    });
    const l = el("span", "l");
    l.append(el("b", "", v.name), el("small", "", v.desc));
    card.append(play, l);
    if (on) card.appendChild(el("span", "pill done", _("chosen")));
    else {
      const pick = el("button", "btn ghost", _("Choose"));
      pick.addEventListener("click", () => api("/api/voice/set", { voice: v.name }).then((r) => {
        toast(r && r.ok ? v.name + _(" chosen; it speaks with this voice from the next conversation") : "kaydedilemedi");
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
        l.append(el("b", "", _("Your voice is enrolled ✓")),
          el("small", "", _("{n} sentences · threshold {threshold} · consistency {consistency}. ", { n: prof.samples, threshold: Number(prof.threshold).toFixed(2), consistency: Number(prof.consistency).toFixed(2) })
            + _("When \"Alice\" is said it answers only your voice; friends on Discord or voices in a video don't wake it.")));
      } else {
        l.append(el("b", "", _("Answer only your voice")),
          el("small", "", _("You read 6 short sentences (1 minute). ALICE makes a voiceprint; it won't wake when someone else says \"Alice\". ")
            + _("Read the way you talk on Discord or in a game.")));
      }
      const acts = el("span", "enroll-act");
      if (!s.available) acts.append(btn("Gerekeni kur", "install", true));
      else if (prof) acts.append(btn(_("Try"), "test"), btn(_("Enroll again"), "start"), btn(_("Close"), "remove"));
      else acts.append(btn(_("Enroll my voice"), "start", true));
      head.append(l, acts);
      card.appendChild(head);
      if (!s.available) card.appendChild(el("small", "enroll-note", _("The voice recognition package (sherpa-onnx) isn't installed; \"Install what's needed\" installs it once.")));
      else if (!s.model) card.appendChild(el("small", "enroll-note", _("The voice recognition model downloads the first time (~40 MB).")));
    }
    if (busy) {
      const steps = el("div", "enroll-steps");
      for (let i = 1; i <= s.total; i++) {
        steps.appendChild(el("span", i < s.step || (i === s.step && s.state === "testing") ? "done" : i === s.step && s.state === "recording" ? "on" : ""));
      }
      if (s.state === "recording") {
        card.append(steps, el("div", "enroll-n", _("{step}/{total} · read this sentence aloud", { step: s.step, total: s.total })),
          el("div", "enroll-say", "“" + s.sentence + "”"));
      } else if (s.state === "testing") {
        card.append(steps, el("div", "enroll-say small", _("Now say a sentence of your own and let's try it.")));
      }
      if (s.state === "recording" || s.state === "testing") {
        const meter = el("div", "enroll-meter" + (s.hearing ? " hearing" : ""));
        const bar = el("i");
        bar.style.width = Math.round((s.level || 0) * 100) + "%";
        meter.appendChild(bar);
        // Ne olduğu hep yazsın: dinliyor mu, duyuyor mu, kaç saniyedir, alındı mı.
        const status = s.got ? s.got
          : s.hearing ? _("● listening… {secs} s. It takes it by itself when the sentence ends; if you're done, press \"Done reading\".", { secs: decimal(Number(s.took || 0).toFixed(1)) })
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
      row.appendChild(btn(_("Cancel"), "cancel"));
      if (s.state === "recording" && !s.got) row.appendChild(btn(_("Skip this sentence"), "skip"));
      if ((s.state === "recording" || s.state === "testing") && !s.got) row.appendChild(btn("Okudum", "done", true));
      card.appendChild(row);
    } else if (s.state === "done" && s.result) {
      const r = s.result;
      const msg = r.score == null ? s.message
        : _("Test: similarity {score} (threshold {threshold}) — ", { score: Number(r.score).toFixed(2), threshold: Number(r.threshold).toFixed(2) }) + (r.recognized ? _("recognized you ✓") : _("didn't recognize you ✗"));
      card.appendChild(el("div", "enroll-result " + (r.recognized === false ? "bad" : "good"), msg));
      if (r.recognized === false || r.noisy) {
        card.appendChild(el("small", "enroll-note", _("The recordings didn't sound much alike or the test didn't match: enroll again at a quiet moment, at the same distance from the microphone.")));
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
const MODE_LABELS = { auto: _("Automatic"), normal: _("Normal"), dev: _("Development"), flight: _("Flight"), quiet: "Sessiz" };
function systemPane(pane) {
  pane.append(el("h2", "", _("Core")),
    el("p", "", _("The part of ALICE that runs in the background: listening for \"Hey Alice\", notifications, scheduled tasks, ")
      + _("rules. It is the ALICE icon in the tray. It keeps running when you close this window.")));
  const body = el("div");
  pane.appendChild(body);
  const mic = micTestCard();  // tek kart: durum tazelenirken süren test sıfırlanmasın
  let timer = null;
  const act = (action, extra, msg) => api("/api/system/action", Object.assign({ action }, extra || {})).then((r) => {
    if (!r || !r.ok) return toast((r && r.error) || _("didn't work"));
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
    const state = c.busy ? c.busy + "…" : c.running ? _("Working") : _("Not running");
    const voiceTxt = !c.running ? "" : c.voice === "listening" ? " · \"Hey Alice\" dinliyor"
      : c.voice === "no-mic" ? _(" · no microphone found, it only reads notifications aloud") : _(" · voice off");
    l.append(el("b", "", state),
      el("small", "", c.running ? `mod: ${MODE_LABELS[c.mode] || c.mode || "?"}${c.forced ? " (elle sabit)" : ""}${voiceTxt} · pid ${c.pid}`
        : _("Notifications, tasks and the voice assistant aren't running.")));
    const acts = el("span", "enroll-act");
    if (c.busy) acts.appendChild(el("span", "sys-spin", ""));
    else if (c.running) acts.append(btn(_("Restart"), "restart"), btn(_("Stop"), "stop"));
    else acts.appendChild(btn(_("Start"), "start", null, true));
    st.append(el("span", "chk " + (c.running ? "ok" : "off"), c.running ? "●" : "○"), l, acts);
    body.appendChild(st);
    body.appendChild(setRow(_("Start when I log in"), _("When you log in, the core starts by itself without opening a window."),
      segmented([[true, _("On")], [false, _("Off")]], !!c.autostart, (on) => act("autostart", { on }, on ? _("auto-start set up") : _("auto-start removed")))));
    body.appendChild(setRow("Mod", _("Automatic: it switches by itself while a game, flight or code is open. If you choose by hand, it stays."),
      segmented((d.modes || []).map((m) => [m, MODE_LABELS[m] || m]), c.forced || "auto", (mode) => act("mode", { mode }, "mod: " + (MODE_LABELS[mode] || mode)))));
    // Ses
    body.appendChild(el("div", "sec", _("Voice assistant")));
    body.appendChild(setRow(_("Talk with \"Hey Alice\""),
      _("Turning it on restarts the core, which starts listening to the microphone.") + (v.ptt_key ? _(" Push-to-talk key: {key}.", { key: keyName(v.ptt_key) }) : ""),
      segmented([[true, _("On")], [false, _("Off")]], !!v.enabled, (on) => act("voice", { on }, on ? _("voice assistant starting") : _("voice assistant turned off")))));
    const ok = (x) => x ? "✓" : "✗";
    body.appendChild(setRow(_("Microphone"), (v.mic ? "bulundu" : _("not found")) + _(" · speech to text: ") + ((v.stt || []).join(", ") || _("none (enter a Groq or Gemini key)")),
      el("span", "chk " + (v.mic ? "ok" : "fail"), ok(v.mic))));
    body.appendChild(mic);
    // Paketler
    const missing = (d.packages || []).filter((p) => !p.ok);
    body.appendChild(el("div", "sec", _("Packages")));
    for (const p of d.packages || []) {
      const r = el("div", "list-row");
      const pl = el("span", "l");
      pl.append(el("b", "", p.name), el("small", "", p.why));
      r.append(el("span", "chk " + (p.ok ? "ok" : "off"), p.ok ? "✓" : "–"), pl);
      body.appendChild(r);
    }
    if (missing.length) body.appendChild(el("small", "enroll-note", _("The missing ones are optional. To install: pip install \"alice-harness[voice]\"")));
    // Olaylar
    if ((d.events || []).length) {
      body.appendChild(el("div", "sec", _("Recent events")));
      for (const e of d.events) {
        const r = el("div", "list-row ev-row");
        const el_ = el("span", "l");
        const t = new Date(e.ts * 1000);
        el_.append(el("b", "", e.title), el("small", "", t.toLocaleString(ALICE_LOCALE, { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })
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
    l.append(el("b", "", _("Microphone test")), el("small", "", _("Speak for 5 seconds; it shows the level and what ALICE hears.")));
    const b = el("button", "btn", s.state === "listening" ? `dinliyorum… ${Math.ceil(s.left || 0)}` : s.state === "transcribing" ? _("transcribing…") : "Test et");
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
      const note = s.text ? _("heard{engine} · peak level {peak}", { engine: s.engine ? " (" + s.engine + ")" : "", peak: Number(s.peak).toFixed(3) }) : s.message;
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
  b.title = title || (on ? _("On") : _("Off"));
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
  pane.append(el("h2", "", _("Rules and tasks")),
    el("p", "", _("What ALICE does on its own. Write it in plain words; it shows what it understood and saves it when you approve. ")
      + _("Saying \"every morning at 9 ...\" by voice or in a chat writes to the same place.")));
  const body = el("div");
  pane.appendChild(body);
  const call = (action, extra) => api("/api/automations/action", Object.assign({ action }, extra || {}));
  const reload = () => api("/api/automations").then((d) => d && draw(d));
  const after = (msg) => (r) => {
    if (!r || !r.ok) return toast((r && r.error) || _("didn't work"));
    if (msg) toast(msg);
    reload();
  };
  const small = (label, fn, primary) => {
    const b = el("button", primary ? "btn primary" : "btn", label);
    b.addEventListener("click", fn);
    return b;
  };
  // Cümle yaz -> _("What I understood") önizlemesi -> Kaydet
  const composer = (placeholder, examples, parse, preview, save) => {
    const box = el("div", "auto-new");
    const row = el("div", "key-edit");
    const inp = el("input");
    inp.type = "text";
    inp.placeholder = placeholder;
    const go = el("button", "btn primary", _("Add"));
    const out = el("div");
    const run = () => {
      const text = inp.value.trim();
      if (!text) return;
      go.disabled = true;
      go.textContent = _("understanding…");
      parse(text).then((r) => {
        go.disabled = false;
        go.textContent = _("Add");
        out.replaceChildren();
        if (!r || !r.ok) return toast((r && r.error) || _("not understood"));
        const card = el("div", "auto-preview");
        card.appendChild(el("div", "sec", _("What I understood")));
        for (const [k, v] of preview(r.view)) if (v) {
          const line = el("div", "auto-line");
          line.append(el("span", "", k), el("b", "", v));
          card.appendChild(line);
        }
        const btns = el("div", "row");
        btns.append(small(_("Cancel"), () => out.replaceChildren()),
          small(_("Save"), () => save(r).then((x) => { if (x && x.ok) { inp.value = ""; out.replaceChildren(); } after("kaydedildi")(x); }), true));
        card.appendChild(btns);
        out.appendChild(card);
      });
    };
    go.addEventListener("click", run);
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });
    row.append(inp, go);
    box.append(row, el("small", "enroll-note", _("Example: ") + examples), out);
    return box;
  };
  const kept = {};  // tazelenince yazılan cümle ve önizleme kaybolmasın
  function draw(d) {
    body.replaceChildren();
    if (!d.core_running) {
      const w = el("div", "list-row warn");
      const l = el("span", "l");
      l.append(el("b", "", _("The core isn't running")), el("small", "", _("Rules, tasks and feeds run while the core is running.")));
      w.append(l, small(_("Go to the core"), () => showSettings("system")));
      body.appendChild(w);
    }
    // Kurallar
    body.appendChild(el("div", "sec", `Kurallar (${d.rules.length})`));
    kept.rule = kept.rule || composer("Ne olunca haber vereyim?", _("\"tell me if the GPU stays over 85 degrees for 2 minutes\", \"warn me when the disk is 90% full\""),
      (text) => call("rule_parse", { text }),
      (v) => [[_("Condition"), v.condition], ["Mesaj", v.message], [_("Severity"), v.severity], ["Modlar", (v.modes || []).join(", ")],
        [_("Asks first"), v.confirm], ["ALICE ne yapar", v.agent]],
      (r) => call("rule_add", { rule: r.rule }));
    body.appendChild(kept.rule);
    for (const r of d.rules) {
      const row = el("div", "list-row" + (r.enabled ? "" : " off"));
      const l = el("span", "l");
      l.append(el("b", "", r.text), el("small", "", r.condition + " · " + r.message + (r.agent ? _(" · ALICE looks into it") : "") + (r.enabled ? "" : _(" · off"))));
      row.append(l, switchEl(r.enabled, r.enabled ? _("Turn the rule off") : _("Turn the rule on"), (on) => call("rule_enable", { id: r.id, on }).then(after())),
        iconAct(IC.trash, _("Delete the rule"), () => call("rule_rm", { id: r.id }).then(after("kural silindi")), "danger"));
      body.appendChild(row);
    }
    // Görevler
    body.appendChild(el("div", "sec", _("Scheduled tasks ({n})", { n: d.tasks.length })));
    kept.task = kept.task || composer(_("What should I do, and when?"), _("\"summarize the Discord messages every morning at 9\", \"weekly report on Friday at 18:00\""),
      (text) => call("task_parse", { text }),
      (v) => [["Ne zaman", v.when], [_("First run"), v.next], [_("What to do"), v.prompt], [_("If missed"), v.catch_up ? _("made up for on start") : _("skipped")]],
      (r) => call("task_add", { task: r.task }));
    body.appendChild(kept.task);
    for (const t of d.tasks) {
      const row = el("div", "list-row auto-task" + (t.enabled ? "" : " off"));
      const l = el("span", "l");
      l.append(el("b", "", t.text), el("small", "", t.when + (t.enabled ? " · sonraki: " + t.next : _(" · off"))
        + " · son: " + (t.last || _("never")) + (t.pending ? _(" · runs on the next tick") : "")));
      if (t.result) {
        const det = el("details", "auto-result");
        det.append(el("summary", "", _("last result")), el("div", "", t.result));
        l.appendChild(det);
      }
      row.append(l, small(_("Run now"), () => call("task_run", { id: t.id }).then(after(d.core_running ? _("runs on the next core tick") : _("runs when the core starts")))),
        switchEl(t.enabled, t.enabled ? _("Turn the task off") : _("Turn the task on"), (on) => call("task_enable", { id: t.id, on }).then(after())),
        iconAct(IC.trash, _("Delete the task"), () => call("task_rm", { id: t.id }).then(after(_("task deleted"))), "danger"));
      body.appendChild(row);
    }
    // Hook'lar
    body.appendChild(el("div", "sec", `Hook'lar (${(d.hooks || []).length})`));
    const hk = el("div", "auto-new hook-new");
    const ev = el("select");
    for (const e of d.hook_events || []) { const o = el("option", "", e); o.value = e; ev.appendChild(o); }
    ev.value = "PostToolUse";
    const mt = el("input"); mt.type = "text"; mt.placeholder = _("tool (regex) e.g. edit_file|write_file|multi_edit");
    const cmd = el("input"); cmd.type = "text"; cmd.placeholder = _("command e.g. ruff format \"$ALICE_TOOL_PATH\"");
    const addH = small(_("Add"), async () => {
      if (!cmd.value.trim()) return toast(_("the command is empty"));
      const ok = await confirmDialog(_("Add the hook?"),
        _("On every {event}{matcher} event this command will run:\n{command}", { event: ev.value, matcher: mt.value ? " [" + mt.value + "]" : "", command: cmd.value }), _("Add"));
      if (ok) call("hook_add", { event: ev.value, matcher: mt.value, command: cmd.value }).then(after("hook eklendi"));
    }, true);
    const hrow = el("div", "key-edit");
    hrow.append(ev, mt);
    const hrow2 = el("div", "key-edit");
    hrow2.append(cmd, addH);
    hk.append(hrow, hrow2, el("small", "enroll-note", _("PostToolUse: after a tool (exit 2 = the error goes to ALICE) · ")
      + _("PreToolUse: before (2 = block) · Stop: when the answer ends (2 = keep going) · UserPromptSubmit / SessionStart: the output ")
      + _("is added to the context. File: ") + (d.hooks_path || "")));
    body.appendChild(hk);
    for (const h of d.hooks || []) {
      const row = el("div", "list-row");
      const l = el("span", "l");
      l.append(el("b", "", h.command), el("small", "", h.event + (h.matcher ? " · " + h.matcher : _(" · every tool"))
        + ` · ${h.timeout} sn` + (h.fixed ? " · config.toml'da" : "")));
      const out = el("div", "auto-verdicts");
      l.appendChild(out);
      row.appendChild(l);
      if (!h.fixed) {
        row.append(small(_("Try"), () => call("hook_test", { index: h.index }).then((r) => {
          out.replaceChildren();
          if (!r || !r.ok) return toast((r && r.error) || _("didn't work"));
          out.appendChild(el("small", "", _("exit {code}", { code: r.code }) + (r.stdout ? " · " + r.stdout : "") + (r.stderr ? " · " + r.stderr : "")));
        })), iconAct(IC.trash, _("Delete the hook"), () => call("hook_rm", { index: h.index }).then(after("hook silindi")), "danger"));
      }
      body.appendChild(row);
    }
    // Akışlar
    body.appendChild(el("div", "sec", _("Feeds ({n})", { n: d.feeds.length })));
    const fh = el("div", "list-row");
    const fl = el("span", "l");
    fl.append(el("b", "", _("Watch connected services")), el("small", "", _("Like telling you when your name comes up on Discord or a friend starts a game. ")
      + _("The definitions are in ") + d.feeds_path + _(".")));
    fh.append(fl, small(d.feeds_file ? _("Open the file") : _("Create a sample file"), () => call("feed_open").then((r) => {
      if (!r || !r.ok) return toast((r && r.error) || _("could not open"));
      toast(r.created ? _("sample file created and opened; the core reads it when you save") : _("file opened"));
      reload();
    })));
    body.appendChild(fh);
    for (const e of d.feed_errors || []) body.appendChild(el("div", "list-row warn", e));
    for (const f of d.feeds) {
      const row = el("div", "list-row" + (f.enabled ? "" : " off"));
      const l = el("span", "l");
      l.append(el("b", "", f.name + (f.enabled ? "" : _(" (off)"))), el("small", "", f.describe + (f.agent ? " · ALICE: " + f.agent : "")));
      const out = el("div", "auto-verdicts");
      const test = small(_("Try"), () => {
        test.disabled = true;
        test.textContent = _("checking…");
        call("feed_test", { name: f.name }).then((r) => {
          test.disabled = false;
          test.textContent = _("Try");
          out.replaceChildren();
          if (!r || !r.ok) { out.appendChild(el("div", "enroll-result bad", (r && r.error) || _("didn't work"))); return; }
          out.appendChild(el("small", "", _("{total} items, {passed} meet the condition (no notification sent)", { total: r.total, passed: r.passed })));
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
  l.append(el("b", "", title), el("small", "", info ? `kurulu: ${info.when}${info.enabled ? "" : _(" (off)")}` : sub));
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
    wd.append(c, document.createTextNode(_(" on weekdays")));
    wd.box = c;
    ctl.appendChild(wd);
  }
  const save = el("button", info ? "btn" : "btn primary", info ? _("Update") : "Kur");
  save.addEventListener("click", () => api("/api/notes/action", { action: prefix + "_install", at: at.value, weekdays: wd ? wd.box.checked : false })
    .then((r) => { toast(r && r.ok ? title + " kuruldu" : (r && r.error) || _("could not set up")); after(); }));
  ctl.appendChild(save);
  if (info) {
    const rm = el("button", "btn", _("Remove"));
    rm.addEventListener("click", () => api("/api/notes/action", { action: prefix + "_remove" }).then(() => { toast(title + _(" removed")); after(); }));
    ctl.appendChild(rm);
  }
  row.append(l, ctl);
  return row;
}
function notesPane(pane) {
  pane.append(el("h2", "", _("Notes")),
    el("p", "", _("What you told ALICE with \"take a note: ...\" is here. You can also ask by voice \"what did I note yesterday\".")));
  const add = el("div", "key-edit auto-new");
  const inp = el("input");
  inp.type = "text";
  inp.placeholder = _("Type a new note, or to search");
  const addBtn = el("button", "btn primary", "Not al");
  add.append(inp, addBtn);
  const head = el("div");
  const list = el("div");
  const tools = el("div");
  pane.append(add, head, list, tools);
  let timer = null;
  const load = () => api("/api/notes?q=" + encodeURIComponent(inp.value.trim())).then((d) => {
    if (!d) return;
    head.replaceChildren(el("div", "sec", inp.value.trim() ? _("Matching ({n})", { n: d.notes.length }) : `Notlar (${d.total})`));
    list.replaceChildren();
    if (!d.notes.length) list.appendChild(el("p", "", inp.value.trim() ? _("No matching notes; add one with \"Take a note\".") : _("No notes yet.")));
    for (const n of d.notes) {
      const row = el("div", "list-row");
      const l = el("span", "l");
      l.append(el("b", "", n.text), el("small", "", n.when));
      const del = el("button", "btn", "Sil");
      del.addEventListener("click", () => api("/api/notes/action", { action: "remove", ts: n.ts }).then(() => { toast(_("note deleted")); load(); }));
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
      toast(_("note saved"));
      load();
    });
  };
  addBtn.addEventListener("click", addNote);
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") addNote(); });
  inp.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(load, 250); });
  const out = el("div");
  function drawTools(d) {
    tools.replaceChildren();
    tools.appendChild(el("div", "sec", _("Morning summary and weekly report")));
    const show = (label, action, extra) => {
      const b = el("button", "btn", label);
      b.addEventListener("click", () => {
        b.disabled = true;
        const old = b.textContent;
        b.textContent = _("preparing…");
        api("/api/notes/action", Object.assign({ action }, extra || {})).then((r) => {
          b.disabled = false;
          b.textContent = old;
          if (!r || !r.ok) return toast((r && r.error) || _("didn't work"));
          out.replaceChildren(el("pre", "mem", r.text));
          if (r.path) {
            const o = el("button", "btn", _("Open the page"));
            o.addEventListener("click", () => api("/api/notes/action", { action: "report_show", open: true }));
            out.appendChild(o);
          }
        });
      });
      return b;
    };
    const br = scheduleRow(_("Morning summary"), _("Every morning: calendar, weather, what happened overnight and notes left from yesterday."), d.brief, "08:00", true, "brief", load);
    br.querySelector(".enroll-act").prepend(show(_("Show now"), "brief_show"));
    const rp = scheduleRow(_("Weekly report"), _("Sunday evening: temperatures, disk, the biggest memory users, warnings."), d.report, "20:00", false, "report", load);
    rp.querySelector(".enroll-act").prepend(show(_("Show now"), "report_show"));
    tools.append(br, rp, out);
  }
  load();
}
/* Otomasyonlar > Kayıtlar: ALICE'in kime ne gönderdiği, beğenmediğin cevaplar ve test seti (alice log / alice eval). */
function logsPane(pane) {
  pane.append(el("h2", "", _("Logs")),
    el("p", "", _("What ALICE sent on your behalf, and answers you didn't like. Every answer you didn't like goes into the test set; ")
      + _("\"Run the tests\" asks those again and shows whether they got better.")));
  const body = el("div");
  pane.appendChild(body);
  let timer = null;
  const act = (action, extra) => api("/api/logs/action", Object.assign({ action }, extra || {}));
  const evalBox = el("div", "enroll");
  const drawEval = (r, d) => {
    evalBox.replaceChildren();
    const head = el("div", "enroll-head");
    const l = el("span", "l");
    const last = d && d.last_run ? _(" · last run {when}: {passed}/{total} passed", { when: d.last_run.when, passed: d.last_run.passed, total: d.last_run.total }) : "";
    l.append(el("b", "", _("Test set")), el("small", "", `${d ? d.cases : "?"} vaka${last}`));
    const ctl = el("span", "enroll-act");
    if (r.state === "running") {
      const stop = el("button", "btn", _("Stop"));
      stop.addEventListener("click", () => act("eval_stop").then(poll));
      ctl.appendChild(stop);
    } else {
      const add = el("button", "btn", _("Add from voice.log"));
      add.title = _("Add your last 20 voice requests as cases");
      add.addEventListener("click", () => act("eval_from_log", { n: 20 }).then((x) => { toast(x && x.ok ? `${x.added} vaka eklendi` : "eklenemedi"); refresh(); }));
      const open = el("button", "btn", _("Open the file"));
      open.addEventListener("click", () => act("eval_open").then((x) => { if (!x || !x.ok) toast((x && x.error) || _("could not open")); }));
      const run = el("button", "btn primary", _("Run the tests"));
      run.addEventListener("click", () => act("eval_run").then((x) => { if (!x || !x.ok) toast((x && x.error) || _("didn't start")); poll(); }));
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
      evalBox.append(meter, el("small", "enroll-note", _("{done}/{total} · now: {current}", { done: r.done, total: r.total, current: r.current || "…" })));
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
      body.appendChild(el("div", "sec", _("Sent ({n})", { n: d.sent.length })));
      if (!d.sent.length) body.appendChild(el("p", "", _("ALICE hasn't sent anything to anyone yet.")));
      for (const r of d.sent) {
        const row = el("div", "list-row" + (r.outcome === "sent" ? "" : " warn"));
        const l = el("span", "l");
        l.append(el("b", "", r.summary), el("small", "", `${r.when} · ${r.tool} · ${r.status}` + (r.detail && r.outcome !== "sent" ? " — " + r.detail : "")));
        row.append(el("span", "chk " + (r.outcome === "sent" ? "ok" : "warn"), r.outcome === "sent" ? "✓" : "!"), l);
        body.appendChild(row);
      }
      body.appendChild(el("div", "sec", _("Answers you didn't like ({n})", { n: d.feedback.length })));
      if (!d.feedback.length) body.appendChild(el("p", "", _("None. Use 👎 under an answer in a chat, or say \"that answer was bad\".")));
      for (const f of d.feedback) {
        const row = el("div", "list-row");
        const l = el("span", "l");
        l.append(el("b", "", f.say || "?"), el("small", "", `${f.when}${f.source ? " · " + f.source : ""} · cevap: ${f.reply}` + (f.note ? " · not: " + f.note : "")));
        row.appendChild(l);
        body.appendChild(row);
      }
      body.appendChild(el("div", "sec", _("Test set")));
      body.appendChild(evalBox);
      drawEval(d.run, d);
      if (d.run.state === "running") poll();
    });
  }
  refresh();
}
/* Sistem > Kurulum: sağlayıcıları dene (doctor), modelleri ölç, Google, telefon, DCS, eklentiler, kısayol. */
function setupPane(pane) {
  pane.append(el("h2", "", _("Setup")),
    el("p", "", _("One-time jobs and troubleshooting. If something doesn't work, start with \"Try all\".")));
  const body = el("div");
  pane.appendChild(body);
  const act = (action, extra) => api("/api/setup/action", Object.assign({ action }, extra || {}));
  const done = (msg) => (r) => { if (!r || !r.ok) toast((r && r.error) || _("didn't work")); else if (msg) toast(typeof msg === "function" ? msg(r) : msg); load(); };
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
      box.appendChild(el("small", "enroll-note", (j.lines || []).slice(-1)[0] || _("starting…")));
    } else if (j.state === "error") box.appendChild(el("div", "enroll-result bad", j.error));
    return box;
  };
  let timer = null;
  const marks = { ok: ["ok", "✓"], warn: ["warn", "!"], off: ["off", "–"], fail: ["fail", "✗"], skip: ["off", "–"] };
  function draw(d) {
    body.replaceChildren();
    // Optional features (voice, computer control, browser, core, phone) and the first-run wizard again.
    body.appendChild(card(_("Features"), _("Voice, computer control, browser, background core, phone app: install what you need."),
      btn(_("Manage features"), () => openFeatures().then(() => setTimeout(load, 300)), true),
      btn(_("Setup wizard"), () => openWizard(0))));
    // Sağlayıcılar
    body.appendChild(el("div", "sec", _("Models")));
    // Modeller
    const mo = d.models;
    const c2 = card(_("Measure the free models"), (mo.probed_at ? _("last measured: ") + mo.probed_at + ". " : _("not measured yet. "))
      + _("Tries each model with a real tool call and puts the fast, accurate ones first (~1-2 min)."),
      ...(mo.auto ? [] : [btn(_("Switch to the automatic order"), () => act("models_auto").then(done(_("the roles will be ordered by the measurements"))))]),
      btn(mo.state === "running" ? _("measuring…") : _("Measure again"), () => act("models_refresh").then(done()), mo.state !== "running"));
    c2.appendChild(jobLines(mo));
    const chains = (mo.state === "done" && mo.result ? mo.result.chains : mo.chains) || {};
    if (mo.state === "done" && mo.result) c2.appendChild(el("div", "enroll-result good", _("{working}/{total} models working", { working: mo.result.working, total: mo.result.total })));
    for (const [role, refs] of Object.entries(chains)) {
      const it = el("div", "auto-line");
      it.append(el("span", "", { brain: "ana model", fast: _("fast"), vision: _("vision") }[role] || role), el("small", "", (refs || []).slice(0, 5).join(" → ") || "-"));
      c2.appendChild(it);
    }
    body.appendChild(c2);
    // ChatGPT hesabıyla giriş (Codex limiti; ücretsiz plan dahil)
    const cg = d.chatgpt || {};
    const c2b = card(cg.logged_in ? _("ChatGPT connected ✓") : _("Sign in with ChatGPT"),
      cg.logged_in ? _("{account}{plan}. Once measured, the models join the automatic order; at the limit the next one is used.", { account: cg.email || _("account"), plan: cg.plan ? " · " + cg.plan : "" })
        : _("GPT models with your ChatGPT account (free plan included), without an API key. Usage counts against your plan's Codex limit. Not officially approved (OpenCode uses the same route)."),
      btn(cg.state === "running" ? _("waiting in the browser…") : cg.logged_in ? "Hesap ekle" : _("Sign in"),
        () => act("chatgpt_login").then(done()), cg.state !== "running"));
    c2b.appendChild(jobLines(cg));
    if (cg.logged_in) {
      // Limit: Codex'in 5 saatlik ve haftalık penceresi (her cevapta güncellenir, "Limiti yenile" ile şimdi sorulur)
      const lim = cg.limits || {};
      const fmtReset = (t) => { if (!t) return ""; const m = Math.max(0, Math.round((t * 1000 - Date.now()) / 60000));
        return m >= 60 * 24 ? _(" · resets in {n} days", { n: Math.round(m / 1440) }) : m >= 60 ? _(" · resets in {h} h {m} min", { h: Math.floor(m / 60), m: m % 60 }) : _(" · resets in {m} min", { m: m }); };
      const winName = (w) => !w.window_minutes ? "limit" : w.window_minutes >= 60 * 24 * 6 ? _("weekly") : w.window_minutes >= 60 ? `${Math.round(w.window_minutes / 60)} saatlik` : _("{m}-minute", { m: w.window_minutes });
      for (const key of ["primary", "secondary"]) {
        const w = lim[key];
        if (!w) continue;
        const it = el("div", "auto-line");
        const bar = el("div", "enroll-meter dl");
        bar.appendChild(el("i")).style.width = Math.min(100, w.used_percent) + "%";
        it.append(el("span", "", winName(w)), el("small", "", _("{pct}% used{reset}", { pct: Math.round(w.used_percent), reset: fmtReset(w.reset_at) })), bar);
        c2b.appendChild(it);
      }
      const row = el("div", "auto-line");
      row.append(el("small", "", (lim.primary || lim.secondary) ? "" : _("The limit isn't known yet.")),
        btn("Limiti yenile", () => act("chatgpt_usage").then(done())));
      c2b.appendChild(row);
      if ((cg.models || []).length) c2b.appendChild(el("small", "enroll-note",
        _("Models: {models}. Choose one from the model menu under the chat, or leave it on Automatic.", { models: cg.models.join(", ") })));
      else {
        c2b.appendChild(el("div", "enroll-result " + (cg.models_state === "loading" ? "" : "bad"),
          cg.models_state === "loading" ? _("Reading the model list…") : _("Could not get the model list: ") + (cg.models_error || "bilinmiyor")));
        // Liste gelmese de kullanılabilsin: adı elle ekle
        const row = el("div", "key-edit auto-new");
        const inp = el("input");
        inp.type = "text"; inp.placeholder = _("Model name (e.g. gpt-5.5)");
        row.append(inp, btn(_("Add"), () => act("chatgpt_model_add", { name: inp.value }).then(done(_("model added; it's in the model menu"))), true),
          btn("Tekrar dene", () => act("chatgpt_models_retry").then(done())));
        c2b.appendChild(row);
      }
    }
    // Kayıtlı hesaplar: tek tıkla geç ya da kaldır
    for (const a of cg.accounts || []) {
      const it = el("div", "auto-line");
      const acts = el("span", "enroll-act");
      if (!a.active) acts.appendChild(btn("Kullan", () => act("chatgpt_use", { key: a.key }).then(done(_("{account} in use", { account: a.email || _("account") })))));
      acts.appendChild(btn(_("Remove"), () => act("chatgpt_logout", { key: a.key }).then(done(_("account removed")))));
      it.append(el("span", "chk " + (a.active ? "ok" : "off"), a.active ? "✓" : "–"), el("b", "", a.email || a.key),
        el("small", "", (a.plan || "") + (a.active ? _(" · in use") : "")), acts);
      c2b.appendChild(it);
    }
    if ((cg.accounts || []).length > 1) c2b.appendChild(el("small", "enroll-note",
      _("The account is chosen by hand; it doesn't change by itself at the limit. Switching accounts to get past the limit is against OpenAI's rules and accounts can be closed.")));
    if (cg.state === "done" && cg.lines && cg.lines.length) c2b.appendChild(el("small", "enroll-note", cg.lines.slice(-1)[0]));
    body.appendChild(c2b);
    // Google
    body.appendChild(el("div", "sec", _("Google Calendar and Gmail")));
    const g = d.google;
    const c3 = card(g.client ? _("Google client saved ✓") : _("Step-by-step setup"),
      g.client ? _("You can connect the calendar and Gmail accounts.") : _("At each step \"Open\" opens the right page; do it, then go to the next. At the end, choose the downloaded file here."));
    if (!g.client) {
      g.steps.forEach((st, i) => {
        const it = el("div", "auto-line g-step");
        const ob = el("button", "btn", _("Open"));
        ob.addEventListener("click", () => act("google_open", { step: i }));
        it.append(el("span", "", `${i + 1}. ${st.title}`), el("small", "", st.how), ob);
        c3.appendChild(it);
      });
      const row = el("div", "key-edit auto-new");
      const inp = el("input");
      inp.type = "text";
      inp.placeholder = _("Client ID (….apps.googleusercontent.com) or the JSON content");
      const sec = el("input");
      sec.type = "password";
      sec.placeholder = "Client secret";
      const save = btn(_("Save"), () => act("google_client", { text: inp.value, secret: sec.value }).then(done("Google istemcisi kaydedildi")), true);
      row.append(inp, sec, save);
      if (g.found_json) c3.appendChild(card(_("Found in Downloads: ") + g.found_json, _("Is this the file you downloaded from Google?"),
        btn("Bunu kullan", () => act("google_client", { use_download: true }).then(done("Google istemcisi kaydedildi")), true)));
      c3.appendChild(row);
    } else {
      const row = el("div", "enroll-act g-connect");
      const cal = btn(_("Connect the calendar"), () => api("/api/connect/auth", { name: "google_calendar" }).then((r) => toast((r && (r.note || r.error)) || _("check the browser"))));
      const acc = el("input");
      acc.type = "text";
      acc.placeholder = _("Gmail account name (e.g. school)");
      const gm = btn("Gmail ekle", () => api("/api/assistant/action", { action: "gmail_connect", account: acc.value.trim() })
        .then((x) => { toast(x && x.ok ? _("The permission page opened in the browser") : (x && x.error) || _("could not start")); gm.disabled = false; }));
      row.append(cal, acc, gm);
      c3.appendChild(row);
    }
    body.appendChild(c3);
    // Telefon
    body.appendChild(el("div", "sec", _("Phone")));
    const ph = d.phone;
    const c4 = card("Telefondan ALICE", _("Chat, voice typing, projects, computer control and approval notifications with the Android app. Works on the same Wi-Fi while the core runs."),
      segmented([[true, _("On")], [false, _("Off")]], !!ph.enabled, (on) => act("phone_enable", { on }).then(done(on ? _("phone bridge starting") : _("phone bridge turned off")))));
    if (ph.enabled) {
      const box = el("div", "phone-box");
      // Resim olarak: satır içi SVG'yi uygulamanın simge kuralı (svg { fill: none; stroke }) boyayıp siliyordu.
      if (ph.qr) { const q = el("img", "phone-qr"); q.alt = "QR kod"; q.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(ph.qr); box.appendChild(q); }
      const info = el("div", "phone-info");
      info.append(el("small", "", _("Scan the QR code with the phone's camera. If the app is installed choose \"Open with ALICE\"; otherwise the page opens in the browser and offers to install the app.")), el("code", "", ph.url));
      const app = ph.app || {};
      info.appendChild(el("small", "", app.available
        ? _("Android app {version} is ready on the computer. When a new version comes out, the app on the phone updates itself.", { version: app.version_name })
        : _("The Android app hasn't downloaded to the computer yet") + (app.error ? ` (${app.error})` : "") + _(". It downloads by itself once it's built on GitHub.")));
      if (!ph.qr_lib) info.appendChild(el("small", "", _("For the QR code: pip install qrcode")));
      info.appendChild(btn("Yeni anahtar", () => act("phone_new_token").then(done(_("the old address no longer works")))));
      box.appendChild(info);
      c4.appendChild(box);
    }
    body.appendChild(c4);
    // DCS
    body.appendChild(el("div", "sec", "DCS World"));
    const dc = d.dcs;
    const installed = dc.dirs.some((x) => x.installed);
    const c5 = card(_("Flight data and kneeboard"), dc.dirs.length
      ? dc.dirs.map((x) => x.path + (x.installed ? " ✓ kurulu" : _(" · not installed"))).join(" · ") + (dc.mission ? _(" · last mission: ") + dc.mission : "")
      : _("DCS Saved Games folder not found (DCS must have been opened once)."),
      ...(dc.dirs.length ? [btn("Kneeboard yaz", () => act("dcs_kneeboard").then(done((r) => _("{n} pages written", { n: r.pages }) + (r.mission ? " (" + r.mission + ")" : "")))),
        btn(installed ? _("Remove") : "Kur", () => act("dcs_install", { on: !installed }).then(done(installed ? _("removed") : _("installed; restart DCS"))), !installed)] : []));
    body.appendChild(c5);
    // Eklentiler
    body.appendChild(el("div", "sec", `Eklentiler (${d.plugins.items.length})`));
    const pl = d.plugins;
    const name = el("input");
    name.type = "text";
    name.placeholder = _("new plugin name");
    const c6 = card(_("Your own tools"), _("As Python files in ") + pl.dir + _(". Changes apply when ALICE restarts."),
      btn(_("Open the folder"), () => act("plugin_folder").then(done())));
    const nr = el("div", "key-edit auto-new");
    nr.append(name, btn(_("Create a template"), () => act("plugin_new", { name: name.value.trim() }).then(done(_("created and opened")))));
    c6.appendChild(nr);
    for (const p of pl.items) {
      const it = el("div", "auto-line");
      it.append(el("span", "ok", "✓"), el("b", "", p.name + (p.version ? " " + p.version : "")), el("small", "", p.mode + " · " + (p.tools.join(", ") || "-")));
      c6.appendChild(it);
    }
    for (const e of pl.errors) c6.appendChild(el("div", "enroll-result bad", e));
    body.appendChild(c6);
    if (d.windows) {
      body.appendChild(el("div", "sec", _("Shortcut")));
      body.appendChild(card(_("Desktop and Start menu"), _("Open ALICE by pressing the Windows key and typing \"ALICE\"."),
        btn(_("Create the shortcut"), () => act("shortcut").then(done(_("shortcut created"))))));
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
const ROLE_NAMES = { brain: _("Main model"), voice: _("Voice"), fast: _("Quick tasks"), vision: _("Images") };
const METRIC_SHORT = { zeka: "Zekâ", hiz: _("Speed"), tepki: _("First token"), akis: _("Streaming"), baglam: _("Context"), guven: _("Reliability") };
const kTok = (n) => !n ? "?" : n >= 1000000 ? Math.round(n / 100000) / 10 + "M" : Math.round(n / 1000) + "K";
let scoreRole = "brain";
const FAIL_WHY = { quota: "kota doldu", rate_limit: _("rate limit"), network: _("timeout / connection"), server: _("server error"),
  not_found: "model yok", auth: _("invalid key"), no_tool: _("didn't call the tool"), bad_args: _("wrong tool parameter"),
  bad_request: _("refused the request"), unreachable: _("unreachable"), too_large: _("request too large") };
function scorePane() {
  const box = el("div", "score-pane");
  let data = null;
  let timer = null;
  const draw = () => {
    if (!data) return;
    box.replaceChildren();
    box.appendChild(el("div", "sec", _("Scoring")));
    box.appendChild(el("p", "", _("Each model gets a 0-100 score on six measures. Use the sliders to set how much each ")
      + _("matters for the role; the automatic order is built from that (the models aren't measured again).") + (data.probed_at ? _(" Last measured: ") + data.probed_at.replace("T", " ") + "." : "")));
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
    const reset = el("button", "btn", _("Back to the defaults"));
    reset.addEventListener("click", () => api("/api/setup/action", { action: "weights_set", role: scoreRole, reset: true })
      .then((r) => { if (r && r.ok) { data = r; draw(); toast(ROLE_NAMES[scoreRole] + _(" weights back to the defaults")); } }));
    sliders.appendChild(reset);
    box.appendChild(sliders);
    const chain = data.chains[scoreRole] || [];
    // Önce ALICE'in sırası (gerçekte denenme sırası), sonra sırada olmayanlar puana göre, nedeniyle.
    const rank = (r) => { const i = chain.indexOf(r.ref); return i < 0 ? 1e6 : i; };
    const rows = data.rows.filter((r) => r.ok || chain.includes(r.ref)).sort((a, b) => rank(a) - rank(b) || (b.scores[scoreRole] || 0) - (a.scores[scoreRole] || 0));
    if (!rows.length) {
      box.appendChild(el("p", "", _("No measurements yet: System → Setup → Measure the free models.")));
      return;
    }
    const table = el("div", "score-table");
    const head = el("div", "score-row head");
    head.append(el("span", "score-m", "Model"), ...Object.keys(data.metrics).map((k) => el("span", "score-c", METRIC_SHORT[k])), el("span", "score-c total", _("General")));
    table.appendChild(head);
    for (const r of rows.slice(0, 25)) {
      const row = el("div", "score-row" + (chain.includes(r.ref) ? " in" : ""));
      const m = el("span", "score-m");
      const pos = chain.indexOf(r.ref);
      const why = !r.ok ? _("backup: a temporary error in the measurement ({error}); tried after the working models", { error: r.error || "?" })
        : pos < 0 && r.why ? r.why[scoreRole] : "";
      m.append(el("b", "", (pos >= 0 ? pos + 1 + ". " : "") + r.ref.split(":").slice(1).join(":")), el("small", why ? "why" : "", r.ref.split(":")[0]
        + (why ? (r.ok ? _(" · not in the order: ") : " · ") + why : _(" · {latency} s · first token {ttft} s · {tps} tok/s · {context} context", { latency: decimal(Number(r.latency || 0).toFixed(1)), ttft: decimal(Number(r.ttft || 0).toFixed(1)), tps: Math.round(r.tps || 0), context: kTok(r.context) }))));
      if (why) m.title = _("Not in the order: ") + why;
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
        if (k === "akis" && !(r.tps > 0)) c.title = _("Couldn't measure it (the answer came in one piece); counted as the middle score (50)");
        if (k === "baglam") c.title = _("{context} token context → {score} points", { context: kTok(r.context), score: Math.round(v) });
        if (k === "zeka") c.title = r.arena ? `LMArena: ${r.arena.name} (Elo ${r.arena.elo})`
          : r.known ? _("Not found on LMArena: estimated from a hand-written table") : _("This model's intelligence score is unknown; the default is 45");
        if ((k === "tepki" || k === "akis") && r.live_calls) c.title = _("From real use ({n} calls)", { n: r.live_calls });
        if (k === "guven") c.title = _("Answering in real use without getting stuck or talking nonsense");
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
      det.appendChild(el("summary", "", _("{n} models didn't work in the measurement and are not in the order (", { n: failed.length }) +
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
    box.appendChild(el("small", "enroll-note", _("The numbered ones are in this role's automatic order, in the order they are tried. No three models from the same provider are put in a row (so they don't all drop at once when one hits a quota), which is why two models with close scores can swap places. The intelligence score comes from LMArena's ranking built from real user votes (overall, code, instruction following, non-English, multi-turn; estimated when not found); speed from the measurement; first token and streaming from real calls as you use ALICE (the measurement before that); reliability from getting stuck or talking nonsense in real use; context from the catalog (capped at what ALICE can send to that provider). Context: 8K is 20 points, 32K 60, 128K and above 100.")));
  };
  api("/api/models/scores").then((d) => { data = d; draw(); });
  return box;
}
/* Ayarlar > Hafıza: notlar (genel ve proje), Dream, takipler ve eski rolden ("sevgili") kalan izler. */
function memoryPane(pane, data) {
  const d = data || {};
  pane.append(el("h2", "", _("Memory")),
    el("p", "", _("ALICE's notes about you and your work. Each note is a separate file; the index (MEMORY.md) goes into every chat, and relevant notes are added to the message in full. Fix or delete what's wrong; you can also say \"forget this\".")));
  if (!d.enabled) { pane.appendChild(el("p", "", _("Memory is off (config.toml [memory] enabled = false)."))); return; }
  const old = d.old_role || {};
  const oldTotal = (old.facts || 0) + (old.summaries || 0) + (old.episodes || 0);
  if (oldTotal) {
    const box = el("div", "list-row warn mem-old");
    const l = el("span", "l");
    l.append(el("b", "", _("Traces of the old role")),
      el("small", "", _("{notes} notes, {summaries} summaries, {episodes} old answers (like \"bebeğim\", \"aşkım\"). ", { notes: old.facts || 0, summaries: old.summaries || 0, episodes: old.episodes || 0 })
        + _("While these remain, ALICE can slip back into its old manner. Example: ") + (old.examples || []).join(" / ")));
    const b = el("button", "btn primary", _("Clean up"));
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
  instHead.append(el("b", "", _("Custom instructions & identity")),
    el("small", "", _("Given to the model directly at the start of every conversation (your name, role, preferred language). Stored only on your own device, never in the git repository.")));
  const instArea = el("textarea", "input");
  instArea.style.cssText = "width: 100%; min-height: 80px; font-family: monospace; font-size: 13px; resize: vertical;";
  instArea.value = d.instructions || "";
  const instSaveBtn = el("button", "btn primary", _("Save the instructions"));
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
  const when = dr.at ? new Date(dr.at * 1000).toLocaleString(ALICE_LOCALE) : _("never yet");
  dl.append(el("b", "", _("Dream (tidy the memory)")),
    el("small", "", _("Merges duplicates, fixes what is outdated, adds what lasts from recent conversations and tidies the index. It makes a backup first. ")
      + (dr.enabled ? _("On its own: every {hours} hours, once at least {sessions} conversations have piled up. ", { hours: dr.hours, sessions: dr.sessions }) : _("Running on its own is off. "))
      + `Son: ${when}.` + (dr.summary ? " " + dr.summary : "")));
  const db = el("button", "btn", _("Tidy now"));
  db.addEventListener("click", () => {
    db.disabled = true; db.textContent = _("Tidying…");
    api("/api/memory/action", { action: "dream" }).then((r) => {
      toast(r && r.ok ? ((r.done || []).length ? _("{n} changes", { n: r.done.length }) : "Zaten derli toplu") : (r && r.error) || _("could not tidy"));
      showSettings("memory");
    });
  });
  dbox.append(dl, db);
  pane.appendChild(dbox);

  const f = el("input"); f.type = "text"; f.placeholder = _("Search notes"); f.className = "set-filter";
  pane.appendChild(f);

  // Claude memory categories tabs/sections
  const CAT_TITLES = {
    you: _("👤 You (user & preferences)"),
    people: _("👥 People (people & links)"),
    areas: _("🌐 Areas (projects & areas of life)"),
    topics: _("💡 Topics (hardware & interests)"),
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
      const rb = el("button", "btn", _("Restore a backup"));
      rb.addEventListener("click", () => {
        if (!confirm(_("Restore the {book} memory to the {backup} backup? The current state is backed up too.", { book: book.label, backup: sel.value }))) return;
        api("/api/memory/action", { action: "restore", root: book.root, backup: sel.value }).then((r) => {
          toast(r && r.ok ? `${r.count} not geri geldi` : (r && r.error) || _("could not restore"));
          showSettings("memory");
        });
      });
      const l = el("span", "l"); l.append(el("small", "", _("Backups from before Dream")));
      row.append(l, sel, rb);
      pane.appendChild(row);
    }
  }
  const draw = () => {
    const q = f.value.toLocaleLowerCase(ALICE_LOCALE);
    for (const [list, notes, byCat] of lists) {
      list.replaceChildren();
      if (q) {
        // Flat search filtered view
        const shown = notes.filter((x) => (x.title + " " + x.description + " " + x.body).toLocaleLowerCase(ALICE_LOCALE).includes(q));
        if (!shown.length) list.appendChild(el("p", "", _("No matches.")));
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
        if (!totalShown) list.appendChild(el("p", "", _("No notes yet.")));
      }
    }
  };
  f.addEventListener("input", draw);
  draw();
  historyBlock(pane, d);
  if ((d.followups || []).length) {
    pane.appendChild(el("div", "sec", _("Follow-ups (to ask or remind about later)")));
    for (const x of d.followups) {
      const row = el("div", "list-row");
      const b = el("button", "btn", _("Close"));
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
  pane.appendChild(el("div", "sec", _("Past chats")));
  const row = el("div", "list-row");
  const l = el("span", "l");
  l.append(el("b", "", _("Search past chats")),
    el("small", "", on
      ? _("When you refer to the past (\"what we talked about yesterday…\"), ALICE can search your old chats. Old conversations are not added to messages by themselves.")
      : _("Off: conversations aren't kept and ALICE doesn't know old chats. Your notes are not affected.")));
  const sw = switchEl(on, on ? _("Close") : _("Open"), (v) => api("/api/memory/action", { action: "chat_history", on: v }).then((r) => {
    toast(r && r.ok ? (v ? _("Past chat search turned on") : _("Past chat search turned off")) : "kaydedilemedi");
    showSettings("memory");
  }));
  row.append(l, sw);
  pane.appendChild(row);
  const n = (h.episodes || 0) + (h.summaries || 0);
  if (!n) return;
  const row2 = el("div", "list-row");
  const l2 = el("span", "l");
  l2.append(el("small", "", _("Kept: {turns} turns, {summaries} conversation summaries. Deleting a chat from the sidebar removes its traces too.", { turns: h.episodes || 0, summaries: h.summaries || 0 })));
  const all = el("button", "btn", _("Clear the history"));
  all.addEventListener("click", async () => {
    if (!confirm(_("Delete all saved chat history (turns and summaries)? Notes and follow-ups stay. This can't be undone."))) return;
    const r = await api("/api/memory/action", { action: "forget_history" });
    toast(r && r.ok ? _("{turns} turns and {summaries} summaries deleted", { turns: r.episodes, summaries: r.summaries }) : "silinemedi");
    showSettings("memory");
  });
  row2.append(l2, all);
  pane.appendChild(row2);
}
function noteRow(x, notes, redraw) {
  const row = el("div", "list-row mem-note" + (x.old ? " warn" : ""));
  const l = el("span", "l");
  const al = (x.aliases || []).filter((a) => a.toLocaleLowerCase(ALICE_LOCALE) !== String(x.name).toLocaleLowerCase(ALICE_LOCALE));
  const meta = [al.length ? _("other names: ") + al.join(", ") : "", (x.links || []).length ? _("linked: ") + x.links.join(", ") : "",
    _("updated ") + x.age, x.source === "kullanıcı" ? _("your request") : "", x.old ? "eski rolden" : ""].filter(Boolean).join(" · ");
  l.append(el("b", "", x.title || x.name), el("small", "", String(x.description || "").replace(/\[\[([^\]]+)\]\]/g, "$1")), el("small", "mem-meta", meta));
  const body = el("div", "mem-body");
  body.hidden = true;
  const ta = el("textarea"); ta.value = x.body; ta.rows = Math.min(14, Math.max(3, x.body.split("\n").length + 1));
  const save = el("button", "btn primary", _("Save"));
  save.addEventListener("click", () => {
    save.disabled = true;
    api("/api/memory/action", { action: "save", key: x.key, body: ta.value }).then((r) => {
      save.disabled = false;
      if (r && r.ok) { x.body = ta.value; toast(_("Note saved")); } else toast((r && r.error) || "kaydedilemedi");
    });
  });
  body.append(ta, save);
  l.appendChild(body);
  const open = el("button", "btn", _("Open"));
  open.addEventListener("click", () => { body.hidden = !body.hidden; open.textContent = body.hidden ? _("Open") : _("Close"); });
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
  pane.append(el("h2", "", _("Keys")),
    el("p", "", _("Paste the key and save: it's used right away, no restart needed. They all stay on this computer in ")
      + (d.path || "~/.alice/.env") + _(". At least two model keys (e.g. Gemini + Groq) keep ALICE going when one hits its quota.")));
  const groups = [["model", _("Model providers")], ["connect", _("Services")]];
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
    pane.appendChild(el("div", "sec", _("{title} · {done}/{total} connected", { title: title, done: done, total: list.length })));
    if (g === "model") {
      // Anahtar havuzu: bir sağlayıcıya birden çok anahtar eklenince istekler nasıl paylaştırılsın.
      const row = el("div", "kc-strategy");
      row.append(el("small", "", _("When a provider has more than one key:")),
        segmented([["round-robin", _("Use them in turn")], ["fill-first", _("Switch when one runs out")]], d.strategy || "round-robin",
          (v) => api("/api/keys/strategy", { strategy: v }).then((r) => toast(r && r.ok ? "kaydedildi" : (r && r.error) || _("didn't work")))));
      pane.appendChild(row);
    }
    const grid = el("div", "key-grid");
    for (const parts of list) grid.appendChild(keyRow(parts));
    if (g === "model") grid.appendChild(serverCard());
    pane.appendChild(grid);
    if (g === "model") cliproxySection(pane);  // abonelik hesapları: model sağlayıcılarının hemen altında
  }
}
/* Key pool: a provider's extra keys and the state of each (ready, waiting, off). */
function keyPool(k) {
  const box = el("div", "kc-pool");
  const pool = k.pool || [];
  const extra = k.extra || [];
  if (pool.length > 1) {
    for (const x of pool) {
      const waits = Object.entries(x.waiting || {});
      const label = { ready: _("ready"), waiting: _("waiting"), off: _("off") }[x.state] || x.state;
      const st = x.state === "waiting" && waits.length
        ? label + " · " + waits.slice(0, 2).map(([m, sec]) => m.split("/").pop() + " " + (sec > 3600 ? _("{n} h", { n: Math.round(sec / 3600) })
          : sec > 60 ? _("{n} min", { n: Math.round(sec / 60) }) : _("{secs} s", { secs: sec }))).join(", ")
        : label;
      const r = el("div", "kc-key " + (x.state === "ready" ? "ok" : x.state === "off" ? "bad" : "wait"));
      r.append(el("span", "kc-dot"), el("code", "", x.preview), el("small", "", st + (x.uses ? " · " + _("{n} requests", { n: x.uses }) : "")));
      if (x.error && x.state === "off") r.title = x.error;
      const ex = x.index > 1 ? extra[x.index - 2] : null;
      if (ex && ex.source === "file") r.appendChild(iconAct(IC.trash, _("Delete this key"), () => api("/api/keys/set", { key: ex.key, value: "" })
        .then((res) => { toast(res && res.ok ? _("key deleted") : (res && res.error) || _("could not delete")); showSettings("apikeys"); }), "danger"));
      box.appendChild(r);
    }
  }
  const add = el("div", "kc-add");
  const inp = el("input");
  inp.type = "password"; inp.autocomplete = "off"; inp.spellcheck = false;
  inp.placeholder = _("paste another key (another account)");
  const go = el("button", "btn", _("Add"));
  go.addEventListener("click", () => {
    if (!inp.value.trim()) return inp.focus();
    api("/api/keys/add", { base: k.key, value: inp.value.trim() }).then((r) => {
      if (!r || !r.ok) return toast((r && r.error) || "eklenemedi");
      toast(_("Key added: requests are now shared between them"));
      showSettings("apikeys");
    });
  });
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") go.click(); });
  add.append(inp, go);
  box.appendChild(add);
  box.appendChild(el("small", "kc-hint", _("Keys of other accounts at the same provider: when one hits its limit or daily quota, ALICE goes on with another without waiting.")));
  return box;
}
/* Kendi sunucun ya da OpenAI uyumlu başka bir servis (LM Studio, vLLM, LiteLLM, bir proxy): adresle ekle. */
function serverCard() {
  const card = el("div", "key-card");
  const head = el("div", "kc-head");
  const l = el("span", "l");
  l.append(el("b", "", _("Add an OpenAI-compatible server")), el("small", "", _("LM Studio, vLLM, LiteLLM, your own proxy or another cloud")));
  const open = el("button", "btn", _("Add"));
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
  const name = field(_("Name"), _("e.g. lmstudio"));
  const url = field(_("Address"), "http://localhost:1234/v1");
  const key = field(_("Key"), _("leave empty if none"), "password");
  const models = field(_("Models"), _("read from the server when empty (comma-separated)"));
  const fr = el("div", "kc-btns");
  const cancel = el("button", "btn ghost", _("Cancel"));
  cancel.addEventListener("click", () => { form.hidden = true; });
  const save = el("button", "btn primary", _("Try and add"));
  const send = (force) => {
    save.disabled = true;
    api("/api/providers/add", { name: name.value.trim(), base_url: url.value.trim(), key: key.value.trim(), models: models.value.trim(), force })
      .then((r) => {
        save.disabled = false;
        if (r && r.ok) { toast(`${r.name} eklendi` + (r.models && r.models.length ? ` · ${r.models.length} model` : "")); return showSettings("apikeys"); }
        if (r && r.can_force) { save.textContent = _("Add anyway"); save.onclick = () => send(true); }
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
/* Abonelikler (Claude Code proxy, CLIProxyAPI): her servis ayrı kart. Kartın _("Sign in") düğmesi o servisin giriş
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
    box.appendChild(el("div", "sec", _("Subscriptions · ") + (d.running ? _("{n} accounts, {ready} ready", { n: accs.length, ready: ready }) : "Claude Code proxy")));
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
  return m >= 1440 ? _("resets in {n} days", { n: Math.round(m / 1440) }) : m >= 60 ? _("resets in {h} h {m} min", { h: Math.floor(m / 60), m: m % 60 }) : _("resets in {m} min", { m: m });
}
function proxyStatusCard(d, refresh) {
  const card = el("div", "key-card cp-card" + (d.installed ? " partial" : ""));
  const head = el("div", "kc-head");
  const l = el("span", "l");
  l.append(el("b", "", "Claude Code proxy"), el("small", "", d.installed ? _("not running") : _("not installed")));
  head.append(el("span", "kc-dot"), l);
  const body = el("div", "cp-body");
  if (!d.installed) {
    body.appendChild(el("p", "cp-note", _("CLIProxyAPI isn't installed. ALICE can download and install it from the GitHub release ")
      + _("(~25 MB, checksum verified; ") + (d.dir || "~/.alice/cliproxy") + _("). After installing, the proxy starts by itself ")
      + _("and you connect the accounts below.")));
    const install = el("button", "btn primary", _("Install (automatic)"));
    install.addEventListener("click", () => {
      install.disabled = true;
      install.textContent = _("Downloading…");
      api("/api/cliproxy/install", {}).then((r) => {
        if (!r || !r.ok) {
          toast((r && r.error) || _("could not start the install"));
          install.disabled = false;
          install.textContent = _("Install (automatic)");
          return;
        }
        toast(_("Downloading CLIProxyAPI…"));
        let n = 0;
        const timer = setInterval(async () => {
          n += 1;
          const st = await api("/api/cliproxy");
          if (st && st.installed) {
            clearInterval(timer);
            toast(_("CLIProxyAPI installed"));
            refresh();
          } else if (n > 90) {
            clearInterval(timer);
            toast(_("the install didn't finish; try the manual way in the README"));
            install.disabled = false;
            install.textContent = _("Install (automatic)");
          }
        }, 2000);
      });
    });
    body.appendChild(install);
  } else {
    body.appendChild(el("p", "cp-note cp-err", _("Could not start the proxy") + (d.error ? ": " + d.error : "") + "."));
    const retry = el("button", "btn", _("Try again"));
    retry.addEventListener("click", () => { retry.disabled = true; retry.textContent = _("Starting…"); refresh(); });
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
  l.append(el("b", "", name), el("small", "", accs.length ? _("{n} accounts · {ready} ready", { n: accs.length, ready: ready }) : _("no connected accounts")));
  const go = el("button", accs.length ? "btn" : "btn primary", accs.length ? "Hesap ekle" : _("Sign in"));
  go.title = name + _(" opens the sign-in page in the browser");
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
    flow.replaceChildren(el("small", "kc-hint", name + _(" sign-in page being prepared…")));
    api("/api/cliproxy/login", { provider: key }).then((r) => {
      if (!r || !r.ok) {
        go.hidden = false;
        flow.replaceChildren(el("small", "kc-hint cp-err", (r && r.error) || _("could not start the sign-in")));
        return;
      }
      const msg = el("p", "cp-note", _("The {name} sign-in page opened in the browser. Sign in with the account you want to add; when you finish it shows up here by itself.", { name: name }));
      const again = el("button", "btn", _("Open the page again"));
      again.title = r.url;
      again.addEventListener("click", () => api("/api/open", { url: r.url }));
      const cancel = el("button", "btn ghost", _("Cancel"));
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
        if (st && st.status === "ok") { stop(); toast(name + _(" account added")); refresh(); }
        else if (n > 90 || (st && st.status === "error")) {
          stop();
          msg.textContent = (st && st.error) || (n > 90 ? _("Timed out: the sign-in didn't finish.") : _("The sign-in didn't finish."));
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
    const state = a.needs_sign_in ? _("sign in again") : a.disabled ? _("off")
      : a.cooldown ? "kota doldu · " + a.cooldown + _(" until it opens") : a.quota_blocked ? "kota doldu"
      : a.error ? "hata" : _("ready");
    const q = el("div", "cp-quota");
    // Her kota penceresi (Claude: 5 saat ve 7 gün) ayrı çubuk. Sıfırlanma zamanı geçmiş pencere boş sayılır.
    const wins = (a.quota && a.quota.windows) || (a.quota && a.quota.percent != null ? [a.quota] : []);
    for (const w of wins) {
      const pct = Math.max(0, Math.min(100, w.percent));
      const bar = el("span", "cp-bar");
      const fill = el("i", pct >= 100 ? "full" : pct >= 85 ? "high" : "");
      fill.style.width = pct + "%";
      bar.appendChild(fill);
      const when = w.reset ? " · " + cpUntil(w.reset) : w.stale ? _(" · reset") : "";
      q.append(bar, el("small", "", `${w.label || "kota"} · %${pct}${when}`));
    }
    if (!wins.length) q.appendChild(el("small", "", a.needs_sign_in || a.error ? "" : _("no usage info (no requests sent yet)")));
    if (a.quota && a.quota.observed) q.title = _("Last measured: ") + new Date(a.quota.observed * 1000).toLocaleString(ALICE_LOCALE);
    if (a.error) who.title = a.error;
    const acts = el("div", "cp-acts");
    if (a.needs_sign_in) {
      const re = el("button", "btn", _("Sign in"));
      re.addEventListener("click", login);
      acts.appendChild(re);
    }
    acts.appendChild(iconAct(IC.trash, _("Remove the account"), () => {
      confirmDialog(_("Remove the account?"), (a.email || a.name) + " · " + name, _("Remove")).then((ok) => {
        if (!ok) return;
        api("/api/cliproxy/remove", { name: a.name }).then((r) => { toast(r && r.ok ? _("account removed") : (r && r.error) || _("could not remove")); refresh(); });
      });
    }, "danger"));
    row.append(who, q, el("span", "cp-state " + (blocked ? (a.needs_sign_in || a.error ? "bad" : "wait") : "ok"), state), acts);
    body.appendChild(row);
  }
  if (accs.length > 1) body.appendChild(el("small", "kc-hint", _("Requests are shared between these accounts in turn; when one hits its limit the next is used without waiting.")));
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
  const sub = all ? (many ? _("connected") : (k.preview || _("connected")) + (k.source === "env" ? _(" · environment variable") : ""))
    : some ? "eksik: " + parts.filter((p) => !p.set).map((p) => p.field).join(", ") : (k.help || note || "anahtar yok");
  l.append(el("b", "", title), el("small", "", sub));
  l.title = k.label + (k.help ? "\n" + k.help : "");
  head.append(dot, l);
  if (k.url) {
    const a = el("button", "btn ghost kc-get");
    a.title = k.url;
    a.append(document.createTextNode(_("Get a key")), svg(IC.ext));
    a.addEventListener("click", () => api("/api/open", { url: k.url }));
    head.appendChild(a);
  }
  if (k.custom) head.appendChild(iconAct(IC.trash, _("Remove the server"), () => api("/api/providers/remove", { name: k.provider })
    .then((r) => { toast(r && r.ok ? k.label + _(" removed") : (r && r.error) || _("could not remove")); showSettings("apikeys"); }), "danger"));
  const edit = el("button", "btn", all ? _("Change") : _("Add"));
  head.appendChild(edit);
  card.appendChild(head);
  const pooled = !many && k.provider && k.set;
  if (pooled && (k.pool || []).length > 1) {
    l.querySelector("small").textContent = _("{n} keys · {ready} ready", { n: k.pool.length, ready: k.pool.filter((x) => x.state === "ready").length });
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
    inp.placeholder = p.set ? _("paste a new {what}", { what: many ? p.field : _("key") }) : _("paste the {what}", { what: many ? p.field : _("key") });
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
  const cancel = el("button", "btn ghost", _("Cancel"));
  cancel.addEventListener("click", () => { form.hidden = true; });
  const save = el("button", "btn primary", _("Save"));
  save.addEventListener("click", saveAll);
  fr.append(cancel, save);
  form.appendChild(fr);
  card.appendChild(form);
  if (pooled) {
    form.prepend(keyPool(k));
    if ((k.pool || []).length > 1) inputs[0][1].placeholder = _("Change key 1");
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
  pane.append(el("h2", "", _("Connections")),
    el("p", "", _("Connects services to ALICE. Keys stay in ~/.alice/.env; to add a new service, use ")
      + _("~/.alice/connect/<name>.toml (in a terminal: alice connect <name> --new).")));
  for (const e of d.errors || []) pane.appendChild(el("div", "list-row warn", e));
  for (const c of d.connectors || []) {
    const row = el("div", "list-row conn-row");
    const l = el("span", "l");
    l.append(el("b", "", c.title), el("small", "", c.description || ""));
    // Durum kısa etiket; uzun açıklama ("… yok — sayfa → Bot → Reset Token") alta iner. Eskiden uzun durum sağda
    // daralmadan duruyor, açıklamayı sıfır genişliğe itip harf harf alt alta döküyordu.
    const [short, ...more] = String(c.ready ? _("ready") : c.status || _("not ready")).split(" — ");
    if (more.length) l.appendChild(el("small", "conn-help", more.join(" — ")));
    const right = el("span", "conn-act");
    right.appendChild(el("span", "risk " + (c.ready ? "read" : "write"), c.ready ? _("ready") : (c.env ? "anahtar yok" : short)));
    if (c.kind === "oauth2") {
      const b = el("button", "btn", c.ready ? _("Sign out") : _("Connect"));
      b.addEventListener("click", async () => {
        b.disabled = true;
        const r = c.ready ? await api("/api/connect/logout", { name: c.name })
          : await api("/api/connect/auth", { name: c.name });
        toast((r && (r.note || r.error)) || (c.ready ? _("Signed out") : _("Check the browser")));
        showSettings("connect");
      });
      right.appendChild(b);
    } else if (!c.ready && c.env) {
      const b = el("button", "btn", _("Enter a key"));
      b.title = c.env;
      b.addEventListener("click", () => showSettings("apikeys"));
      right.appendChild(b);
    }
    row.append(l, right);
    row.title = (c.actions || []).join(", ") + (c.help ? "\n" + c.help : "");
    pane.appendChild(row);
  }
  pane.appendChild(el("div", "sec", _("Feeds")));
  pane.appendChild(el("p", "", _("ALICE tells you when something new happens on a connected service (~/.alice/feeds.toml, ")
    + _("checked while the core runs).")));
  if (!(d.feeds || []).length) pane.appendChild(el("div", "list-row", _("No feeds defined.")));
  for (const f of d.feeds || []) {
    const row = el("div", "list-row");
    const l = el("span", "l");
    l.append(el("b", "", f.name), el("small", "", f.describe || `${f.connector}.${f.action}`));
    // Hata metni uzun olabilir; sağ sütuna koyarsak soldaki adı tek harf genişliğine ezer.
    if (f.error) l.appendChild(el("small", "err", f.error));
    const st = f.error ? "hata" : !f.enabled ? _("off") : f.held ? _("{n} items waiting for the summary", { n: f.held }) : _("on");
    row.append(l, el("span", "r" + (f.error ? "" : " ok"), st));
    pane.appendChild(row);
  }
  pane.appendChild(el("div", "sec", _("Sent on your behalf")));
  pane.appendChild(el("p", "", _("Every message to someone else, including the ones you denied (in a terminal: alice log).")));
  if (!(d.actions || []).length) pane.appendChild(el("div", "list-row", _("Nothing sent yet.")));
  for (const a of d.actions || []) pane.appendChild(el("div", "list-row" + (a.outcome === "sent" ? "" : " warn"), a.text));
}

const SETTINGS_TABS = [
  ["health", _("Health"), IC.gauge], ["general", _("General"), IC.gear], ["apikeys", _("Keys"), IC.key], ["voice", _("Voice"), IC.sound], ["models", _("Models"), IC.spark], ["tools", _("Tools"), IC.bolt],
  ["assistant", _("Assistant"), IC.shield], ["system", _("Core"), IC.term], ["auto", _("Rules and tasks"), IC.list], ["notes", _("Notes"), IC.sun], ["logs", _("Logs"), IC.check], ["setup", _("Setup"), IC.update],
  ["connect", _("Connections"), IC.plug], ["memory", _("Memory"), IC.brain], ["usage", _("Usage"), IC.gauge],
  ["keys", _("Shortcuts"), IC.keyboard],
];
/* Sol şeritteki bölümler. Eskiden hepsi tek bir Ayarlar penceresindeydi; özellikler (otomasyon, çekirdek,
   bağlantılar) artık kendi tam sayfalarında, Ayarlar'da sadece gerçek ayarlar kalır. */
const SECTIONS = {
  auto: [_("Automations"), ["auto", "notes", "logs"]],
  system: [_("System"), ["health", "system", "assistant", "voice", "setup", "usage"]],
  connect: [_("Connections"), ["connect", "apikeys"]],
  settings: [_("Settings"), ["general", "models", "tools", "memory", "keys"]],
};
const secOf = (tab) => Object.keys(SECTIONS).find((k) => SECTIONS[k][1].includes(tab)) || "settings";
function markSection(sec) {
  app.dataset.sec = sec;
  for (const b of document.querySelectorAll("#rail [data-sec]")) b.classList.toggle("on", b.dataset.sec === sec && b.classList.contains("rbtn"));
  $("#sideTitle").textContent = sec === "chat" ? _("Chats") : sec === "projects" ? _("Projects") : sec === "tasks" ? _("Tasks") : SECTIONS[sec][0];
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
  t.append(el("h1", "", _("Projects")),
    el("p", "", _("Big jobs are run by agent teams working in parallel, led by ALICE. Choose a project on the left or start a new one.")));
  const nb = el("button", "btn primary");
  nb.append(svg(IC.plus), document.createTextNode(_("New project")));
  nb.addEventListener("click", () => projectDialog(null));
  head.append(t, el("span", "grow"), nb);
  page.appendChild(head);
  const rows = S.projects || [];
  if (!rows.length) {
    page.appendChild(emptyState(IC.list, _("No projects yet"), _("Write a goal; ALICE splits the work between agents, reviews what they finish and writes you a summary at the end.")));
  } else {
    const list = el("div", "card-list");
    for (const p of rows.slice(0, 30)) {
      const r = el("button", "card-row");
      const l = el("span", "l");
      l.append(el("b", "", p.title || _("(untitled)")), el("small", "", p.goal || ""));
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
    [hasModel, IC.key, _("Connect a model"), _("Paste a free API key (Gemini recommended) or sign in with your ChatGPT account."),
      _("Add a key"), () => showSettings("apikeys")],
    [probed, IC.gauge, _("Measure the models"), _("Measures once which models are fast and accurate and builds the order from that (~1-2 min)."),
      _("Measure"), () => api("/api/setup/action", { action: "models_refresh" }).then(() => { toast(_("Measuring started")); drawOnboarding(); })],
    [core, IC.term, _("Start the core"), _("Runs in the background for notifications, scheduled tasks and \"Hey Alice\"."),
      _("Start"), () => api("/api/system/action", { action: "start" }).then(() => { toast(_("Starting the core")); setTimeout(drawOnboarding, 2500); })],
  ];
  const done = steps.filter((x) => x[0]).length;
  if (done === steps.length) { box.hidden = true; return; }
  box.replaceChildren();
  const head = el("div", "ob-head");
  const t = el("div");
  t.append(el("b", "", _("Getting started")), el("small", "", `${done}/${steps.length} tamam`));
  const bar = el("i", "ob-bar"); const fill = el("i"); fill.style.width = (100 * done / steps.length) + "%"; bar.appendChild(fill);
  const x = iconAct(IC.x, _("Hide"), () => { try { localStorage.setItem("alice.onboard", "0"); } catch { /* yok */ } box.hidden = true; });
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
      pane.append(el("h2", "", _("General")));
      pane.append(
        setRow(_("Language / Dil"), _("The language of the interface and ALICE's messages; translation is in progress and some text is still Turkish"),
          segmented([["en", "English"], ["tr", "Türkçe"]], S.status.language || "en",
            (v) => api("/api/features/action", { action: "lang", lang: v }).then((r) => { if (r && r.ok) { S.status.language = v; location.reload(); } }))),
        setRow(_("Theme"), _("System follows the Windows light/dark setting"),
          segmented([["system", _("System")], ["light", _("Light")], ["dark", _("Dark")]], PREFS.theme, (v) => setPref("theme", v))),
        setRow(_("Text size"), "Sohbet metninin boyutu",
          segmented([["small", _("Small")], ["normal", _("Normal")], ["large", _("Large")]], PREFS.size, (v) => setPref("size", v))),
        setRow(_("Tool details"), _("Show command output and read files expanded (Ctrl+O)"),
          segmented([["0", _("Off")], ["1", _("On")]], PREFS.verbose ? "1" : "0", (v) => { if ((v === "1") !== PREFS.verbose) toggleVerbose(); })),
        setRow(_("Side panel"), _("Plan, changes and context (Ctrl+J)"),
          segmented([["0", _("Off")], ["1", _("On")]], PREFS.panel ? "1" : "0", (v) => (v === "1" ? openPanel() : closePanel()))),
        setRow(_("Permission mode"), (MODES[S.status.mode] || ["", ""])[1],
          segmented(Object.entries(MODES).map(([k, [l]]) => [k, l]), S.status.mode, (v) => setMode(v))),
        setRow(_("Working folder"), S.status.cwd || "", (() => { const b = el("button", "btn", _("Change")); b.addEventListener("click", askCwd); return b; })()),
        setRow(_("Updates"), _("Automatic: on start it pulls main from GitHub and opens with the latest version"),
          segmented([["auto", _("Automatic")], ["notify", _("Notify")], ["off", _("Off")]], S.status.update_mode || "auto",
            (v) => api("/api/update/mode", { mode: v }).then((r) => { if (r && r.ok) { S.status.update_mode = r.mode; toast(_("Update: ") + v); } }))),
        setRow(_("Version"), "ALICE " + (S.status.version || ""), (() => { const b = el("button", "btn", _("Check now")); b.addEventListener("click", checkUpdate); return b; })()),
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
      pane.append(el("h2", "", _("Usage today")));
      const entries = Object.entries((data.usage && data.usage.today) || {});
      if (!entries.length) pane.appendChild(el("p", "", _("No requests today yet.")));
      const pct = (r) => (r.prompt_tokens ? Math.round((100 * (r.cached_tokens || 0)) / r.prompt_tokens) : 0);
      const sum = entries.reduce((a, [, r]) => ({ prompt_tokens: a.prompt_tokens + (r.prompt_tokens || 0),
        cached_tokens: a.cached_tokens + (r.cached_tokens || 0), cache_write_tokens: a.cache_write_tokens + (r.cache_write_tokens || 0) }),
      { prompt_tokens: 0, cached_tokens: 0, cache_write_tokens: 0 });
      if (sum.prompt_tokens) {
        // Önbellek oranı: girdinin ne kadarı önbellekten okundu (ucuz ve hızlı). Yazılan, sonraki istekler için önbelleğe alınan.
        const head = el("p", "use-cache", _("Cache hits today {pct}% · {k}k read", { pct: pct(sum), k: decimal((sum.cached_tokens / 1000).toFixed(1)) }) +
          (sum.cache_write_tokens ? _(" · {k}k written", { k: decimal((sum.cache_write_tokens / 1000).toFixed(1)) }) : ""));
        head.title = _("The share of input read from the cache. Expected to be high in long chats; it drops when the model changes often.");
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
            (r.cached_tokens ? _(" (cache {pct}%)", { pct: pct(r) }) : "") + _(" · {k}k output", { k: decimal((r.completion_tokens / 1000).toFixed(1)) })));
        pane.appendChild(row);
      }
    } else {
      pane.append(el("h2", "", _("Shortcuts and commands")));
      for (const [k, v] of SHORTCUTS) {
        const row = el("div", "list-row");
        const keys = el("span", "keys");
        for (const part of k.split(" / ")) keys.appendChild(el("kbd", "", part));
        row.append(el("span", "l plain", v), keys);
        pane.appendChild(row);
      }
      pane.appendChild(el("div", "sec", _("Slash commands")));
      for (const c of S.commands) {
        const row = el("div", "list-row");
        row.append(el("span", "l", "/" + c.name), el("span", "r", c.description + (c.kind === "custom" ? _(" (custom)") : "")));
        pane.appendChild(row);
      }
    }
    d.replaceChildren(pane);
  }
}
const SHORTCUTS = [
  ["Ctrl+K", "Komut paleti"], ["Ctrl+J", _("Side panel (plan, changes, context)")], ["Ctrl+O", _("Expand/collapse tool details")],
  ["Ctrl+,", _("Settings")], ["Enter", _("Send (queues it while working)")], ["Shift+Enter", _("New line")],
  ["Ctrl + / Ctrl −", _("Zoom the interface in / out (Ctrl+0 resets)")], ["Ctrl+Shift+E", _("Widen / narrow the chat")],
  ["↑ / ↓", _("Bring back earlier messages (when the box is empty)")], ["Esc", _("Stop / close the menu")],
  ["1 / 2 / 3", _("Answer the permission card: allow / always allow / deny")], ["Shift+Tab", _("Change the permission mode")],
  ["Ctrl+B", _("Sidebar")], ["Ctrl+Shift+O", _("New chat")], ["Ctrl+V", _("Paste an image")],
];

/* =========================================================== slash komutları */
async function runCommand(text) {
  const [cmd, ...rest] = text.slice(1).split(" ");
  const arg = rest.join(" ").trim();
  switch (cmd.toLowerCase()) {
    case "help": showHelp(); return true;
    case "clear": case "new": newChat(); return true;
    case "compact": {
      if (S.busy) { toast(_("Stop the current job first")); return true; }
      toast(_("Summarizing…"));
      const r = await api("/api/compact", {});
      applyStatus(r); toast(r.ok ? _("Context summarized") : _("Nothing to summarize"));
      loadHistory();
      return true;
    }
    case "mode":
      if (MODES[arg]) setMode(arg); else modeMenu();
      return true;
    case "plan": setMode("plan"); toast(_("Plan mode: only reads and writes a plan")); return true;
    case "model":
      if (arg) pinModel(arg.includes(":") ? arg : "reset"); else modelMenu();
      return true;
    case "think":
      if (THINK[arg]) setThinking(arg); else thinkMenu();
      return true;
    case "rewind": {
      const users = $$(".turn-user");
      const last = users[users.length - 1];
      if (!last) { toast(_("No message to rewind to")); return true; }
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
      .map((c) => ({ label: "/" + c.name, desc: c.description, tag: c.kind === "custom" ? _("custom") : "", value: "/" + c.name + " " }));
  } else {
    const files = await api("/api/files?q=" + encodeURIComponent(tok.q));
    if (seq !== popupSeq) return;
    items = (Array.isArray(files) ? files : []).map((f) => ({ label: f, desc: "", tag: f.endsWith("/") ? _("folder") : "", value: "@" + f + " " }));
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
    const x = el("button"); x.title = _("Remove"); x.appendChild(svg(IC.x));
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
    if (!r || !r.ok) toast((r && r.error) || _("could not send the side question"));
    return;
  }
  if (S.busy && text.startsWith("/")) {
    S.queue.push({ text, images });  // komutlar yeni tur ister: iş bitince çalışır
    drawQueue();
  } else if (S.busy) {
    // Çalışan işe ilet (steer): ALICE bir sonraki adımda görür, iş bitmesi beklenmez. Tur tam o an bittiyse sunucu
    // _("working") der ya da yeni tur başlatır; olmazsa eskisi gibi sıraya girer.
    const r = await api("/api/send", { text, images });
    if (!r || !r.ok) { S.queue.push({ text, images }); drawQueue(); }
  } else {
    const r = await api("/api/send", { text, images });
    if (!r.ok) {
      // Sesli asistan aynı anda iş başlatmış olabilir: mesaj kaybolmasın, sıraya girsin.
      if (/çalışıyor|busy|already running/i.test(r.error || "")) {
        S.queue.push({ text, images }); drawQueue();
        await refreshStatus();
        if (!S.busy) flushQueue();
      }
      else { toast(r.error || _("Could not send")); return; }
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
    row.append(el("small", "", _("Queued")), el("span", "", item.text));
    const x = el("button", "act"); x.title = _("Remove from the queue"); x.appendChild(svg(IC.x));
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
  if (!r.ok) toast(r.error || _("Could not send the queued message"));
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
   yerelde olmayan dal izleyen dal olarak açılır. _("New branch") şu anki daldan yeni bir dal açar. */
async function branchMenu() {
  const r = await api("/api/git/branches", {});
  if (!r || !r.ok) return toast((r && r.error) || _("could not read the branches"));
  const go = async (name, create) => {
    toast(create ? _("Creating '{name}'…", { name }) : _("Switching to '{name}'…", { name }));
    const x = await api("/api/git/switch", { branch: name, create: !!create });
    if (!x || !x.ok) return toast((x && x.error) || _("could not switch the branch"));
    applyStatus(x);
    toast("Dal: " + name);
    refreshChanges();
  };
  openMenu($("#branchChip"), (m) => {
    m.appendChild(el("div", "menu-title", _("Switch branch")));
    for (const b of r.branches || []) {
      const when = b.ts ? new Date(b.ts * 1000).toLocaleDateString(ALICE_LOCALE) : "";
      menuItem(m, IC.branch, b.name, (b.remote ? _("only on GitHub · ") : "") + [when, b.subject].filter(Boolean).join(" · "),
        () => { if (!b.current) go(b.name); }, b.current);
    }
    menuItem(m, IC.plus || IC.branch, _("New branch…"), _("Open a new branch from the current one"), async () => {
      showDialog((d) => {
        const inp = el("input", "text");
        inp.placeholder = _("e.g. feature/new-setting");
        const row = el("div", "row");
        const no = el("button", "btn", _("Cancel"));
        const yes = el("button", "btn accent", _("Create and switch"));
        const ok = () => { const name = inp.value.trim(); if (!name) return; closeDialog(); go(name, true); };
        no.addEventListener("click", closeDialog);
        yes.addEventListener("click", ok);
        inp.addEventListener("keydown", (e) => { if (e.key === "Enter") ok(); });
        row.append(no, yes);
        d.append(el("h2", "", _("New branch")), inp, row);
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
  const pct = _("Zoom {pct}%", { pct: Math.round(zoom * 100) });
  toast(want > zoom && zoom >= limit ? pct + _(" · the most this window width allows")
    : pct + (zoom === 1 ? "" : _(" · Ctrl+0 to reset")));
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
  bar.append(mk(IC.back, _("Back"), () => bbSend({ type: "back" })),
    mk(IC.fwd, _("Forward"), () => bbSend({ type: "forward" })),
    mk(IC.reload, _("Reload"), () => bbSend({ type: "reload" })));
  const url = el("input", "bb-url");
  url.placeholder = _("Address or search");
  url.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { bbSend({ type: "navigate", url: url.value }); url.blur(); }
  });
  url.addEventListener("focus", () => url.select());
  bar.appendChild(url);
  const tabsBtn = mk(IC.list, _("Tabs"), () => bbTabsMenu(tabsBtn));
  bar.appendChild(tabsBtn);
  const view = el("div", "bb-view");
  view.tabIndex = 0;
  const img = el("img", "bb-img");
  img.alt = "";
  img.draggable = false;
  const idle = el("div", "bb-idle");
  const start = el("button", "btn accent", _("Start the browser"));
  start.addEventListener("click", async () => {
    start.disabled = true; start.textContent = _("Opening…");
    const r = await api("/api/browser/input", { type: "start" });
    if (!r.ok) { toast(r.error || _("could not open")); start.disabled = false; start.textContent = _("Start the browser"); }
  });
  idle.append(el("b", "", _("ALICE's browser")),
    el("p", "", _("When ALICE works on the web you see it live here; you can take over at any time by clicking and typing. Sites you sign in to stay signed in next time.")), start);
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
  if (!r || !r.ok) return toast((r && r.error) || _("the browser is closed"));
  openMenu(anchor, (m) => {
    m.appendChild(el("div", "menu-title", _("Tabs")));
    for (const t of r.tabs) {
      menuItem(m, IC.globe || IC.file, (t.title || t.url || "yeni sekme").slice(0, 60), t.url.slice(0, 80),
        () => bbSend({ type: "tab", id: t.id }), t.id === r.current);
    }
    menuItem(m, IC.plus, _("New tab"), "", () => bbSend({ type: "new_tab" }));
    if (r.tabs.length > 1) menuItem(m, IC.x, _("Close this tab"), "", () => bbSend({ type: "close_tab", id: r.current }));
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
  ["auto_fix", _("Fix when CI is red")],
  ["address_comments", _("Apply review comments")],
  ["auto_review", _("Review every commit independently")],
  ["auto_merge", _("Merge when everything is green")],
];
async function prAct(path, body) {
  const r = await api("/api/prs/" + path, body);
  if (!r || !r.ok) toast((r && r.error) || _("didn't work"));
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
  if (!body.querySelector(".prp")) body.replaceChildren(el("div", "prp note", _("Loading PRs…")));
  PRP.busy = true;
  let r;
  try { r = await api("/api/prs" + (force ? "?force=1" : "")); } finally { PRP.busy = false; }
  if (!(PREFS.panel && PREFS.tab === "prs")) return;
  const wrap = el("div", "prp");
  const head = el("div", "prp-head");
  head.append(el("b", "", _("Watched PRs")), el("span", "grow"),
    iconAct(IC.rewind, _("Refresh"), () => renderPrPane(true)));
  wrap.appendChild(head);
  if (!r || (!r.ok && !r.watching.length)) {
    wrap.appendChild(el("div", "note", (r && r.error) || _("Could not reach GitHub")));
  }
  if (r && r.suggest) {
    const s = r.suggest, card = el("div", "prp-suggest");
    card.append(el("div", "", _("The open PR of this branch ({branch}) isn't watched:", { branch: s.head })),
      el("b", "", `#${s.number} ${s.title}`));
    const b = el("button", "btn accent", _("Watch it"));
    b.addEventListener("click", () => { b.disabled = true; prAct("watch", { repo: s.repo, number: s.number }); });
    card.appendChild(b);
    wrap.appendChild(card);
  }
  const rows = (r && r.watching) || [];
  if (!rows.length && !(r && r.suggest)) {
    wrap.appendChild(emptyState(IC.branch, _("No watched PRs"),
      _("Shows up here when ALICE opens a PR or you say \"follow this PR\" in a chat: when CI turns red, or a review comment or conflict arrives, it fixes it itself.")));
  }
  for (const w of rows) wrap.appendChild(prCard(w));
  if (rows.length) wrap.appendChild(el("div", "prp-foot",
    _("The ALICE core checks every few minutes; fixes arrive in the chat that opened the PR.")));
  body.replaceChildren(wrap);
}
function prCard(w) {
  const c = el("div", "prp-card");
  const top = el("div", "prp-top");
  const ci = w.state === "merged" ? "merged" : w.state === "closed" ? "closed" : (w.ci || "none");
  const dot = el("span", "prp-dot " + ci);
  dot.title = { success: _("CI green"), failure: _("CI red"), pending: _("CI running"), none: _("No CI"),
    merged: _("merged"), closed: _("closed") }[ci] || ci;
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
  if (ci === "failure") chip("bad", _("{n} jobs red: {names}", { n: w.failed.length, names: w.failed.slice(0, 3).join(", ") }));
  else if (ci === "pending") chip("wait", _("{n} jobs running", { n: w.pending.length }));
  else if (ci === "success") chip("ok", _("{n} checks green", { n: w.checks }));
  if (w.open_threads > 0) chip("wait", _("{n} open comments", { n: w.open_threads }));
  if (w.mergeable === false) chip("bad", _("has conflicts"));
  if (chips.childNodes.length) c.appendChild(chips);
  if (w.status) c.appendChild(el("div", "prp-status", w.status));
  if (w.finished) {  // merged/closed: no behavior switches, only "izlemeyi bırak"
    const bar = el("div", "prp-acts");
    const un = el("button", "act", _("Remove"));
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
        confirmed = await confirmDialog(_("Automatic merge"),
          _("#{n}: ALICE will merge the PR itself when CI is green and there are no open comments or conflicts.", { n: w.number }), _("Turn on"));
        if (!confirmed) return;
      }
      prAct("flag", { repo: w.repo, number: w.number, flag: k, value: v, confirmed });
    }));
    flags.appendChild(row);
  }
  c.appendChild(flags);
  const acts = el("div", "prp-acts");
  if (ci === "failure") {
    const b = el("button", "act", _("Re-run the failed ones"));
    b.addEventListener("click", () => { b.disabled = true; prAct("rerun", { repo: w.repo, number: w.number }); });
    acts.appendChild(b);
  }
  if (w.history && w.history.length) {
    const b = el("button", "act", _("History"));
    b.addEventListener("click", () => c.classList.toggle("open"));
    acts.appendChild(b);
  }
  const un = el("button", "act", _("Stop watching"));
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
const AG_STATUS = { running: [_("Working"), "pending"], done: [_("Done"), "success"], failed: [_("Error"), "failure"],
  stopped: [_("Stopped"), "none"] };
function onSubagentEvent() {
  if (!(PREFS.panel && PREFS.tab === "agents") || AGP.editing) return;
  // Devam kutusuna yazılırken yeniden çizme: yazılan metin ve odak kayboluyordu. Kutu boşalınca tazelenir.
  const typing = [...document.querySelectorAll("#panelBody .agp-send")].some((i) => i.value || i === document.activeElement);
  if (typing) { AGP.stale = true; return; }
  renderAgentsPane();
}
async function agAct(path, body) {
  const r = await api(path, body);
  if (!r || !r.ok) toast((r && r.error) || _("didn't work"));
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
  head.append(el("b", "", _("Subagents")), el("span", "grow"), iconAct(IC.reload, _("Refresh"), renderAgentsPane));
  wrap.appendChild(head);
  if (!(r.subs || []).length) {
    wrap.appendChild(emptyState(IC.spark, _("No subagents in this chat"),
      _("When ALICE hands a job to a subagent (\"research this in the background\", \"review the three modules in parallel\") it shows here live: what it is doing and how many steps it took. You can stop it, or write a follow-up to a finished agent.")));
  }
  for (const s of r.subs || []) wrap.appendChild(subCard(s));
  const dh = el("div", "prp-head agp-defs");
  const add = el("button", "act", _("New agent"));
  const defsBox = el("div", "prp");
  add.addEventListener("click", () => { AGP.editing = {}; drawAgentDefs(defsBox, r.defs || []); });
  dh.append(el("b", "", _("Custom agents")), el("span", "grow"), add);
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
  const bits = [label, _("{n} steps", { n: s.steps }), _("{secs} s", { secs: s.secs })];
  if (s.kind) bits.push(s.kind);
  if (s.background) bits.push(_("in the background"));
  if (s.turns > 1) bits.push(_("turn {n}", { n: s.turns }));
  c.appendChild(el("div", "prp-meta", bits.join(" · ")));
  if (s.status === "running" && s.last) c.appendChild(el("div", "prp-status", s.last));
  if (s.result) c.appendChild(el("div", "agp-result", s.result));
  const acts = el("div", "prp-acts");
  if (s.status === "running") {
    const b = el("button", "act", _("Stop"));
    b.addEventListener("click", () => { b.disabled = true; agAct("/api/subagents/stop", { id: s.id }); });
    acts.appendChild(b);
  } else {
    const inp = el("input", "agp-send");
    inp.type = "text";
    inp.placeholder = _("A follow-up for this agent…");
    const go = el("button", "act", _("Send"));
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
    box.appendChild(el("div", "prp-foot", _("No custom agents defined. If you define a role (e.g. a read-only reviewer, a test writer), ALICE calls it when it fits the job. You can also say \"define a test-writer agent for me\" in a chat.")));
  }
  for (const a of defs) {
    const c = el("div", "prp-card");
    const top = el("div", "prp-top");
    top.append(el("span", "prp-title", a.name), el("span", "grow"),
      iconAct(IC.edit, _("Edit"), () => { AGP.editing = { ...a, editing: true }; drawAgentDefs(box, defs); }),
      iconAct(IC.trash, "Sil", async () => {
        if (await confirmDialog(_("Delete the agent"), _("The definition of {name} will be deleted ({path}).", { name: a.name, path: a.path }), "Sil")) {
          agAct("/api/agents/delete", { name: a.name });
        }
      }));
    c.append(top, el("div", "prp-status", a.description),
      el("div", "prp-meta", [_("tools: {tools}", { tools: a.tools.length ? a.tools.join(", ") : _("all") }),
        _("model: {model}", { model: a.model || _("the main agent") }), _("{n} steps", { n: a.max_steps }),
        a.where === "project" ? _("in this project") : _("in every project")]
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
  const desc = input(a.description, _("Writes unit tests for changed code; doesn't change the application code"));
  const prompt = el("textarea");
  prompt.rows = 7;
  prompt.value = a.prompt || "";
  prompt.placeholder = _("You are a meticulous test writer. Read the given files, write tests for the missing cases, run them and report briefly…");
  const tools = input((a.tools || []).join(", "), _("empty: all · e.g. read_file, grep, glob, list_dir, git"));
  const model = input(a.model, _("empty: the main agent · brain, fast or provider:model"));
  const steps = el("input");
  steps.type = "number"; steps.min = 1; steps.max = 200; steps.value = a.max_steps || 40;
  const where = el("select");
  for (const [v, t] of [["user", _("In every project (~/.alice/agents)")], ["project", _("Only in this project (.alice/agents)")]]) {
    const o = el("option", "", t); o.value = v; where.appendChild(o);
  }
  where.value = a.where || "user";
  const err = el("div", "prp-chip bad");
  err.hidden = true;
  const save = el("button", "btn accent", a.editing ? _("Save") : _("Add"));
  const cancel = el("button", "act", _("Cancel"));
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
  f.append(field(_("Name"), name), field(_("When to use it"), desc), field(_("Instructions"), prompt), field(_("Tools"), tools),
    field(_("Model"), model), field(_("Max steps"), steps), field(_("Where"), where), err, acts);
  return f;
}
