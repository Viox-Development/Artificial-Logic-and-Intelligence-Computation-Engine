"use strict";

/* First-run setup wizard (and Settings → Features). The installer only sets up ALICE and the app; this screen picks
   the language, connects a model and installs the optional features the person wants (/api/features). */

const WZ_T = {
  en: {
    welcome: "Welcome to ALICE",
    welcomeText: "A personal AI agent that lives on your computer. A few quick steps and it's ready.",
    language: "Language",
    langNote: "The rest of the app is still mostly in Turkish; English is on the way.",
    start: "Get started",
    modelTitle: "Connect a model",
    modelText: "Sign in with a subscription you already have, or use free API keys. One is enough; you can add more later in Settings.",
    subsTitle: "Sign in with a subscription",
    subsText: "Claude Pro / Max, ChatGPT (Codex) or Google Antigravity: higher limits and stronger models than free keys. ALICE installs a small local proxy (Claude Code proxy) for the sign-in.",
    keysTitle: "Or use API keys",
    getKey: "Get a free key",
    paste: "Paste the key",
    save: "Save and test",
    testing: "Testing…",
    works: "Works",
    failed: "Didn't work",
    chatgpt: "Sign in with ChatGPT",
    chatgptText: "Use your ChatGPT account instead of an API key (free plan included).",
    chatgptWait: "Waiting for the browser…",
    chatgptOk: "ChatGPT connected",
    featTitle: "Pick what ALICE can do",
    featText: "Everything here is optional. You can add or remove features later in Settings.",
    autostart: "Start the background core when I sign in",
    installed: "Installed",
    willRemove: "will be removed",
    removing: "Remove",
    apply: "Apply changes",
    recommended: "Recommended",
    needs: "needs",
    install: "Install",
    total: "Download",
    installing: "Applying changes…",
    installDone: "All set",
    installErr: "Something went wrong",
    retry: "Try again",
    doneTitle: "ALICE is ready",
    doneText: "Ask it anything below. Try: \"What's using my disk space?\" or \"Fix the failing test in my project\".",
    doneVoice: "Voice is on: say \"Hey Alice\" once the background core has started.",
    finish: "Start using ALICE",
    back: "Back",
    next: "Next",
    skip: "Skip for now",
    close: "Close",
    features: "Features",
    featuresText: "Voice, computer control, browser, background core, phone app: install what you need.",
    manage: "Manage features",
  },
  tr: {
    welcome: "ALICE'e hoş geldin",
    welcomeText: "Bilgisayarında yaşayan kişisel yapay zekâ ajanın. Birkaç kısa adımda hazır.",
    language: "Dil",
    langNote: "",
    start: "Başlayalım",
    modelTitle: "Bir model bağla",
    modelText: "Zaten olan bir aboneliğinle giriş yap ya da ücretsiz API anahtarları kullan. Biri yeter; sonra Ayarlar'dan daha fazlasını ekleyebilirsin.",
    subsTitle: "Aboneliğinle giriş yap",
    subsText: "Claude Pro / Max, ChatGPT (Codex) ya da Google Antigravity: ücretsiz anahtarlardan daha yüksek limit ve daha güçlü modeller. Giriş için ALICE küçük bir yerel proxy (Claude Code proxy) kurar.",
    keysTitle: "Ya da API anahtarı kullan",
    getKey: "Ücretsiz anahtar al",
    paste: "Anahtarı yapıştır",
    save: "Kaydet ve dene",
    testing: "Deneniyor…",
    works: "Çalışıyor",
    failed: "Çalışmadı",
    chatgpt: "ChatGPT ile giriş yap",
    chatgptText: "API anahtarı yerine ChatGPT hesabını kullan (ücretsiz plan dahil).",
    chatgptWait: "Tarayıcıda bekleniyor…",
    chatgptOk: "ChatGPT bağlı",
    featTitle: "ALICE neler yapabilsin?",
    featText: "Hepsi isteğe bağlı. Sonradan Ayarlar'dan ekleyip çıkarabilirsin.",
    autostart: "Oturum açınca arka plan çekirdeği başlasın",
    installed: "Kurulu",
    willRemove: "kaldırılacak",
    removing: "Kaldırılacak",
    apply: "Değişiklikleri uygula",
    recommended: "Önerilen",
    needs: "gerekir:",
    install: "Kur",
    total: "İndirme",
    installing: "Değişiklikler uygulanıyor…",
    installDone: "Hazır",
    installErr: "Bir şeyler ters gitti",
    retry: "Tekrar dene",
    doneTitle: "ALICE hazır",
    doneText: "Aşağıya istediğini yaz. Örneğin: \"Diskimi ne dolduruyor?\" ya da \"Projemdeki bozuk testi düzelt\".",
    doneVoice: "Ses açık: arka plan çekirdeği başlayınca \"Hey Alice\" de.",
    finish: "ALICE'i kullanmaya başla",
    back: "Geri",
    next: "İleri",
    skip: "Şimdilik geç",
    close: "Kapat",
    features: "Özellikler",
    featuresText: "Ses, bilgisayar kontrolü, tarayıcı, arka plan çekirdeği, telefon uygulaması: ihtiyacın olanı kur.",
    manage: "Özellikleri yönet",
  },
};

const WZ = { step: 0, lang: "en", data: null, picked: null, autostart: true, timer: 0, mode: "wizard" };
const wt = (k) => (WZ_T[WZ.lang] || WZ_T.en)[k] ?? WZ_T.en[k] ?? k;

async function wzLoad() {
  WZ.data = await api("/api/features");
  if (WZ.data && WZ.data.wizard) WZ.lang = WZ.data.wizard.lang || "en";
  if (!WZ.picked && WZ.data && WZ.data.features) {
    WZ.picked = new Set(WZ.data.features.filter((f) => f.installed || f.recommended).map((f) => f.id));
  }
  return WZ.data;
}

/* Open on start when the installer marked the install as fresh. */
async function maybeWizard() {
  const d = await wzLoad();
  if (d && d.wizard && d.wizard.pending) openWizard();
}

function openWizard(step, mode) {
  WZ.mode = mode || "wizard";
  WZ.step = step || 0;
  let box = $("#wizard");
  if (!box) {
    box = el("div", "wizard");
    box.id = "wizard";
    document.body.appendChild(box);
  }
  box.hidden = false;
  wzDraw();
}

function closeWizard() {
  clearInterval(WZ.timer);
  const box = $("#wizard");
  if (box) box.hidden = true;
  if (typeof drawOnboarding === "function") drawOnboarding();
}

function wzShell(title, text, body, actions, steps) {
  const card = el("div", "wz-card");
  const top = el("div", "wz-top");
  const logo = el("img", "wz-logo");
  logo.src = "/static/alice.png";
  logo.alt = "";
  top.appendChild(logo);
  if (steps) {
    const dots = el("div", "wz-dots");
    for (let i = 1; i <= 3; i++) dots.appendChild(el("i", i <= steps ? "on" : ""));
    top.appendChild(dots);
  }
  card.append(top, el("h1", "", title));
  if (text) card.appendChild(el("p", "wz-lead", text));
  if (body) { body.classList.add("wz-body"); card.appendChild(body); }
  const foot = el("div", "wz-foot");
  for (const a of actions) if (a) foot.appendChild(a);
  card.appendChild(foot);
  $("#wizard").replaceChildren(card);
}

function wzBtn(label, fn, primary, disabled) {
  const b = el("button", "btn" + (primary ? " primary" : ""), label);
  b.disabled = !!disabled;
  b.addEventListener("click", fn);
  return b;
}

function wzDraw() {
  clearInterval(WZ.timer);
  [wzWelcome, wzModel, wzFeatures, wzInstall, wzDone][WZ.step]();
}

function wzWelcome() {
  const body = el("div", "wz-langs");
  body.appendChild(el("label", "wz-label", wt("language")));
  const row = el("div", "wz-seg");
  for (const [code, name] of [["en", "English"], ["tr", "Türkçe"]]) {
    const b = el("button", code === WZ.lang ? "on" : "", name);
    b.addEventListener("click", async () => {
      WZ.lang = code;
      await api("/api/features/action", { action: "lang", lang: code });
      await wzLoad();
      wzDraw();
    });
    row.appendChild(b);
  }
  body.appendChild(row);
  if (wt("langNote")) body.appendChild(el("small", "wz-note", wt("langNote")));
  wzShell(wt("welcome"), wt("welcomeText"), body, [
    wzBtn(wt("start"), async () => {
      await api("/api/features/action", { action: "lang", lang: WZ.lang });
      WZ.step = 1;
      wzDraw();
    }, true),
  ]);
}

function wzModel() {
  const d = WZ.data || {};
  const body = el("div", "wz-list");
  // Subscriptions first (#459): the same card as Settings → Keys, so installing the proxy and signing in work the same.
  if (typeof cliproxySection === "function") {
    const subs = el("div", "wz-key first wz-subs");
    const sh = el("div", "wz-key-head");
    const sn = el("div");
    const st = el("b", "", wt("subsTitle"));
    st.appendChild(el("span", "wz-badge", wt("recommended")));
    sn.append(st, el("small", "", wt("subsText")));
    sh.appendChild(sn);
    subs.appendChild(sh);
    cliproxySection(subs);
    body.appendChild(subs);
    body.appendChild(el("div", "wz-sec", wt("keysTitle")));
  }
  const keys = d.keys || [];
  keys.forEach((k, i) => {
    const row = el("div", "wz-key");
    const head = el("div", "wz-key-head");
    const name = el("b", "", k.label);
    head.appendChild(name);
    if (k.set) head.appendChild(el("span", "wz-ok", "✓ " + wt("works")));
    else if (k.url) {
      const a = el("button", "btn ghost", wt("getKey"));
      a.addEventListener("click", () => api("/api/open", { url: k.url }));
      head.appendChild(a);
    }
    row.appendChild(head);
    if (!k.set && row.dataset.open) row.appendChild(wzKeyForm(k));
    else if (!k.set) {
      head.addEventListener("click", (e) => {
        if (e.target.closest("button")) return;
        if (!row.querySelector(".wz-form")) row.appendChild(wzKeyForm(k));
      });
      head.classList.add("click");
    }
    body.appendChild(row);
  });
  const cg = el("div", "wz-key");
  const cgHead = el("div", "wz-key-head");
  const cgName = el("div");
  cgName.append(el("b", "", wt("chatgpt")), el("small", "", wt("chatgptText")));
  cgHead.appendChild(cgName);
  cgHead.appendChild(wzBtn(wt("chatgpt"), async (e) => {
    e.target.disabled = true;
    e.target.textContent = wt("chatgptWait");
    await api("/api/setup/action", { action: "chatgpt_login" });
    const poll = setInterval(async () => {
      const s = await api("/api/setup");
      const c = (s && s.chatgpt) || {};
      if (c.logged_in) { clearInterval(poll); await wzLoad(); wzDraw(); }
      else if (c.state !== "running") { clearInterval(poll); e.target.disabled = false; e.target.textContent = wt("chatgpt"); }
    }, 1500);
  }));
  cg.appendChild(cgHead);
  body.appendChild(cg);
  const skip = wzBtn(wt("skip"), () => { WZ.step = 2; wzDraw(); });
  skip.hidden = !!d.has_model;
  const next = wzBtn(wt("next"), () => { WZ.step = 2; wzDraw(); }, true, !d.has_model);
  wzShell(wt("modelTitle"), wt("modelText"), body, [
    wzBtn(wt("back"), () => { WZ.step = 0; wzDraw(); }),
    el("span", "grow"),
    skip,
    next,
  ], 1);
  // A subscription signed in through the proxy card doesn't redraw the wizard: check now and then, without redrawing
  // (that would reset the card in the middle of a sign-in).
  WZ.timer = setInterval(async () => {
    if (WZ.step !== 1 || (WZ.data && WZ.data.has_model)) return;
    const f = await api("/api/features");
    if (f && f.has_model) { WZ.data = f; next.disabled = false; skip.hidden = true; }
  }, 3000);
}

function wzKeyForm(k) {
  const form = el("div", "wz-form");
  const inp = el("input");
  inp.type = "password";
  inp.placeholder = wt("paste");
  inp.autocomplete = "off";
  inp.spellcheck = false;
  const status = el("small", "wz-status");
  const go = wzBtn(wt("save"), async () => {
    const value = inp.value.trim();
    if (!value) { inp.focus(); return; }
    go.disabled = true;
    status.className = "wz-status";
    status.textContent = wt("testing");
    const r = await api("/api/features/action", { action: "key", key: k.key, value });
    if (!r.ok) { go.disabled = false; status.className = "wz-status bad"; status.textContent = r.error || wt("failed"); return; }
    const poll = setInterval(async () => {
      const d = await api("/api/features");
      const t = (d && d.key_test) || {};
      if (t.state === "running") return;
      clearInterval(poll);
      go.disabled = false;
      const res = t.result || {};
      if (t.state === "done" && res.status === "ok") {
        WZ.data = d;
        wzDraw();
      } else {
        status.className = "wz-status " + (res.status === "warn" ? "warn" : "bad");
        status.textContent = `${res.status === "warn" ? wt("works") + " · " : wt("failed") + ": "}${res.detail || t.error || ""}`;
        if (res.status === "warn") { WZ.data = d; WZ.data.has_model = true; }
      }
    }, 1200);
  }, true);
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") go.click(); });
  form.append(inp, go, status);
  setTimeout(() => inp.focus(), 50);
  return form;
}

function wzFeatureCards(onChange) {
  const d = WZ.data || {};
  const list = el("div", "wz-feats");
  const byId = Object.fromEntries((d.features || []).map((f) => [f.id, f]));
  for (const f of d.features || []) {
    const on = WZ.picked.has(f.id);
    const change = on !== f.installed;
    const card = el("label", "wz-feat" + (change ? (on ? " adding" : " removing") : ""));
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = on;
    cb.addEventListener("change", () => {
      if (cb.checked) {
        WZ.picked.add(f.id);
        for (const n of f.needs || []) if (byId[n]) WZ.picked.add(n);
      } else {
        WZ.picked.delete(f.id);
        // Nothing can stay on without what it builds on (no voice means no offline speech).
        let again = true;
        while (again) {
          again = false;
          for (const o of d.features) {
            if (WZ.picked.has(o.id) && (o.needs || []).some((n) => !WZ.picked.has(n))) { WZ.picked.delete(o.id); again = true; }
          }
        }
      }
      onChange();
    });
    const txt = el("div", "wz-feat-txt");
    const title = el("b", "", f.title);
    if (f.recommended && !f.installed && !change) title.appendChild(el("span", "wz-badge", wt("recommended")));
    txt.append(title, el("small", "", f.text));
    if ((f.needs || []).length) {
      txt.appendChild(el("small", "wz-needs", `${wt("needs")} ${f.needs.map((n) => (byId[n] || {}).title || n).join(", ")}`));
    }
    const side = el("span", "wz-size", change ? (on ? `+ ~${f.size_mb} MB` : wt("willRemove"))
      : f.installed ? "✓ " + wt("installed") : `~${f.size_mb} MB`);
    card.append(cb, txt, side);
    list.appendChild(card);
    if (f.id === "core" && !f.installed) {
      const auto = el("label", "wz-sub" + (on ? "" : " off"));
      const acb = el("input");
      acb.type = "checkbox";
      acb.checked = WZ.autostart;
      acb.disabled = !on;
      acb.addEventListener("change", () => { WZ.autostart = acb.checked; });
      auto.append(acb, el("span", "", wt("autostart")));
      list.appendChild(auto);
    }
  }
  return list;
}

function wzChanges() {
  const fs = (WZ.data || {}).features || [];
  return {
    add: fs.filter((f) => WZ.picked.has(f.id) && !f.installed),
    remove: fs.filter((f) => !WZ.picked.has(f.id) && f.installed),
  };
}

function wzFeatures() {
  const wrap = el("div");
  const total = el("small", "wz-total");
  const draw = () => {
    wrap.replaceChildren(wzFeatureCards(draw));
    const { add, remove } = wzChanges();
    const mb = add.reduce((s, f) => s + f.size_mb, 0);
    const parts = [];
    if (mb) parts.push(`${wt("total")}: ~${mb >= 1000 ? (mb / 1000).toFixed(1) + " GB" : mb + " MB"}`);
    if (remove.length) parts.push(`${wt("removing")}: ${remove.map((f) => f.title).join(", ")}`);
    total.textContent = parts.join(" · ");
    applyBtn.textContent = remove.length ? wt("apply") : wt("install");
    applyBtn.disabled = !add.length && !remove.length;
  };
  const applyBtn = wzBtn(wt("install"), async () => {
    const { add, remove } = wzChanges();
    const r = await api("/api/features/action", { action: "apply", ids: add.map((f) => f.id),
      remove: remove.map((f) => f.id), autostart: WZ.autostart });
    if (r.ok) { WZ.step = 3; wzDraw(); } else toast(r.error || wt("installErr"));
  }, true);
  draw();
  const settings = WZ.mode === "settings";
  wzShell(wt("featTitle"), wt("featText"), wrap, [
    settings ? wzBtn(wt("close"), closeWizard) : wzBtn(wt("back"), () => { WZ.step = 1; wzDraw(); }),
    total,
    el("span", "grow"),
    settings ? null : wzBtn(wt("skip"), () => { WZ.step = 4; wzDraw(); }),
    applyBtn,
  ], settings ? 0 : 2);
}

function wzInstall() {
  const body = el("div", "wz-progress");
  const bar = el("i", "wz-bar");
  const fill = el("i");
  bar.appendChild(fill);
  const state = el("b", "", wt("installing"));
  const log = el("pre", "wz-log");
  body.append(state, bar, log);
  const nextBtn = wzBtn(wt("next"), () => { WZ.step = 4; wzDraw(); }, true, true);
  const retry = wzBtn(wt("retry"), () => { WZ.step = 2; wzDraw(); });
  retry.hidden = true;
  wzShell(wt("featTitle"), "", body, [el("span", "grow"), retry, nextBtn], WZ.mode === "settings" ? 0 : 3);
  const t0 = Date.now();
  const tick = async () => {
    const d = await wzLoad();
    const s = (d && d.install) || {};
    log.textContent = (s.lines || []).join("\n");
    log.scrollTop = log.scrollHeight;
    if (s.state === "running") {
      // pip gives no overall percentage: ease towards 90% over a few minutes.
      const p = 90 * (1 - Math.exp(-(Date.now() - t0) / 90000));
      fill.style.width = p.toFixed(1) + "%";
      return;
    }
    clearInterval(WZ.timer);
    fill.style.width = "100%";
    if (s.state === "done") {
      state.textContent = "✓ " + wt("installDone");
      nextBtn.disabled = false;
      if (WZ.mode === "settings") setTimeout(closeWizard, 800);
    } else {
      bar.classList.add("bad");
      state.textContent = `${wt("installErr")}: ${s.error || ""}`;
      retry.hidden = false;
      nextBtn.disabled = false;
    }
  };
  tick();
  WZ.timer = setInterval(tick, 1000);
}

function wzDone() {
  const d = WZ.data || {};
  const voice = (d.features || []).some((f) => f.id === "voice" && f.installed);
  const body = el("div");
  if (voice) body.appendChild(el("p", "wz-lead", wt("doneVoice")));
  wzShell(wt("doneTitle"), wt("doneText"), body, [
    el("span", "grow"),
    wzBtn(wt("finish"), async () => {
      await api("/api/features/action", { action: "done" });
      // The page was built in the old language; reload so the rest of the app uses the one chosen here.
      if (WZ.lang !== window.ALICE_LANG) { location.reload(); return; }
      closeWizard();
      const inp = $("#input");
      if (inp) inp.focus();
    }, true),
  ]);
}

/* Settings → Features: the same cards, installing what is picked. */
async function openFeatures() {
  await wzLoad();
  WZ.mode = "settings";
  WZ.picked = new Set(((WZ.data || {}).features || []).filter((f) => f.installed).map((f) => f.id));
  openWizard(2, "settings");
}
