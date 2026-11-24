"use strict";

/* ALICE — server events and startup.
   Plain (non-module) scripts: they all share one scope and load in index.html's order
   (i18n.js → app.js → projects.js → ui.js → main.js). Code that runs at load time uses only earlier files. */

/* ===================================================================== events */
const handlers = {
  status: applyStatus,
  team: onTeamEvent,
  team_live: onTeamLive,
  project_note: onProjectNote,
  subagent: onSubagentEvent,
  user: (ev) => addUser(ev.text, ev.mi, ev.images),
  btw: (ev) => addBtw(ev),
  suggest: (ev) => showSuggest(ev.items),
  busy: (ev) => { S.status.busy = ev.value; setBusy(ev.value); if (ev.value) showSuggest([]); else loadSessions(); },
  stream_start: () => { if (!S.cur) showWorking(_("Thinking")); },
  thinking: (ev) => onThinking(ev.text),
  delta: (ev) => onDelta(ev.text),
  stream_reset: () => { if (S.cur) { S.cur.text = ""; S.cur.prose.replaceChildren(); } },
  stream_end: (ev) => { onStreamEnd(ev.text, ev.source); if (S.busy) showWorking(_("Working")); },
  tool_start: (ev) => {
    toolRow(ev.id, ev.name, ev.summary, true);
    // Ajan web'de iş yaparken yerleşik tarayıcı paneli kendiliğinden açılır (Ayarlar'dan kapatılabilir).
    if ((ev.name === "browser" || ev.name === "preview") && PREFS.autoBrowser !== false
        && !(PREFS.panel && PREFS.tab === "browser")) openPanel("browser");
  },
  tool_end: (ev) => {
    const t = S.tools.get(ev.id) || toolRow(ev.id, ev.name, "", true);
    // Sonuç (diff, plan kartı) eklenmeden önce alttaysak, eklendikten sonra da altta kal.
    const stick = nearBottom();
    finishTool(t, ev, true);
    if (S.busy) showWorking(_("Working"));
    toBottom(stick);
  },
  update: renderUpdate,
  permission: onPermission,
  permission_done: onPermissionDone,
  ask_choice: onChoice,
  ask_choice_done: onChoiceDone,
  info: (ev) => addNote(ev.text, /hata|başarısız|cevap veremedi|başlatılamadı|error|failed|could not/i.test(ev.text) ? "err" :
    /durduruldu|limit/i.test(ev.text) ? "warn" : ""),
  route: (ev) => {
    const m = ev.text.match(/^brain -> (.+)$/);
    if (m) { applyStatus({ model: m[1] }); return; }
    // Model geçişleri ve sağlayıcı hataları: hangi modelin neden atlandığı görünsün.
    if (!/^(fast|vision) -> /.test(ev.text)) addNote(ev.text, /hata|geçildi|meşgul|atlandı|error|switched|busy|skipped/i.test(ev.text) ? "warn" : "");
  },
};
// Çalışıyor göstergesini her zaman en altta tut.
const keepWorkingLast = () => { if (S.working && S.working !== list.lastElementChild) list.appendChild(S.working); };
new MutationObserver(() => { keepWorkingLast(); updateJump(); }).observe(list, { childList: true });

// Yukarı kaydırılmışken yeni içerik gelirse "en alta in" düğmesi çıkar.
function updateJump() { $("#jump").hidden = app.classList.contains("empty") || nearBottom(); }
scroller.addEventListener("scroll", updateJump, { passive: true });
$("#jump").addEventListener("click", () => { scroller.scrollTo({ top: scroller.scrollHeight, behavior: "smooth" }); });

// Sunucu kapanınca (çekirdek yeniden başlarken) sessizce donmak yerine bunu söyle.
function setOnline(v) {
  if (S.online === v) return;
  S.online = v;
  clearTimeout(setOnline._t);
  if (v) { $("#conn").hidden = true; return; }
  // Kısa kopmalarda (uyku, ağ) yanıp sönmesin.
  setOnline._t = setTimeout(() => { if (!S.online) $("#conn").hidden = false; }, 1500);
}

let connectedOnce = false;
function connect() {
  const es = new EventSource("/api/events?t=" + encodeURIComponent(TOKEN));
  es.onopen = () => {
    setOnline(true);
    if (connectedOnce) {
      loadHistory().then(() => { if (S.busy) showWorking(_("Working")); });
      loadSessions();
      loadProjects();
      if (S.proj) refreshProject();
    }
    connectedOnce = true;
  };
  es.onerror = () => {
    setOnline(false);
    // Tarayıcı kendisi yeniden dener; bağlantı tamamen kapandıysa biz deneriz.
    if (es.readyState === EventSource.CLOSED) { es.close(); setTimeout(connect, 3000); }
  };
  es.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch { return; }
    // Arkada çalışan sohbetin olayları öndeki sohbete çizilmez; sadece haber verilir.
    if (ev.sid && S.status.session && ev.sid !== S.status.session) return onBackground(ev);
    const h = handlers[ev.type];
    if (h) h(ev);
  };
}

function sessionTitle(id) {
  const s = (S.sessions || []).find((x) => x.id === id);
  return s && s.title ? `“${s.title.slice(0, 40)}”` : _("A background chat");
}
function onBackground(ev) {
  if (ev.type === "busy") {
    if (!ev.value) toast(_("{chat} finished", { chat: sessionTitle(ev.sid) }));
    refreshStatus();
    loadSessions();
  } else if (ev.type === "permission" || ev.type === "ask_choice") {
    toast(_("{chat} is waiting for your approval; you'll see it when you switch to it", { chat: sessionTitle(ev.sid) }));
    refreshStatus();
  }
}

(async function boot() {
  greet();
  await refreshStatus();
  await loadHistory();
  connect();
  loadSessions();
  maybeWizard();  // first start after the installer: language, a model, optional features
  await loadProjects();
  // Address from the phone or a link: #projects (open the list) or #p=<id> (open the project)
  const h = location.hash;
  if (h.startsWith("#p=")) openProject(decodeURIComponent(h.slice(3)));
  else if (h === "#projects") {
    if (window.innerWidth <= 760) $("#app").classList.add("side-open");
    const first = (S.projects || []).find((p) => p.status !== "done") || (S.projects || [])[0];
    if (first) openProject(first.id);
  }
  const cmds = await api("/api/commands");
  S.commands = Array.isArray(cmds) ? cmds : [];
  autosize();
  input.focus();
  refreshChanges();
  if (PREFS.panel) openPanel();
  // If SSE drops briefly the turn-done event can be missed and the UI stays on "Working" forever.
  // Always reconcile the state with the server; even while busy this is only a light GET.
  setInterval(refreshStatus, 10000);
  const clock = () => {
    const m = Math.floor((Date.now() - S.started) / 60000);
    $("#stClock").textContent = m < 60 ? _("{m} min", { m }) : _("{h} h {m} min", { h: Math.floor(m / 60), m: m % 60 });
  };
  clock();
  setInterval(clock, 30000);
})();

/* "While you were away": if the window was hidden for over 20 minutes, summarize where the chat in front stands. */
(() => {
  let hiddenAt = 0;
  document.addEventListener("visibilitychange", async () => {
    if (document.hidden) { hiddenAt = Date.now(); return; }
    if (!hiddenAt || Date.now() - hiddenAt < 20 * 60 * 1000 || S.busy) return;
    hiddenAt = 0;
    const r = await api("/api/recap").catch(() => null);
    if (!r || !r.text) return;
    const card = el("div", "btw-card recap");
    card.append(el("div", "btw-q", _("While you were away")), el("div", "btw-a", r.text));
    add(card);
    toBottom(true);
  });
})();

/* Next-message suggestions: at most two buttons when a turn ends. A click puts it in the box (edit it before sending);
   they disappear when you start typing or a new turn starts. */
function showSuggest(items) {
  let box = document.getElementById("turnSuggest");
  if (!box) {
    box = el("div", "suggest");
    box.id = "turnSuggest";
    const wrap = document.getElementById("composerWrap");
    wrap.insertBefore(box, document.getElementById("composer"));
    document.getElementById("input").addEventListener("input", () => { if (box.childElementCount) box.replaceChildren(); });
  }
  box.replaceChildren();
  for (const t of items || []) {
    const b = el("button", "sg", t);
    b.addEventListener("click", () => {
      const inp = document.getElementById("input");
      inp.value = t;
      inp.focus();
      inp.dispatchEvent(new Event("input", { bubbles: true }));
      box.replaceChildren();
    });
    box.appendChild(b);
  }
}
