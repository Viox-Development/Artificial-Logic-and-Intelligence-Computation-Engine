"use strict";

/* ALICE — sunucu olayları ve açılış.
   Sıradan (modül olmayan) betikler: hepsi aynı kapsamı paylaşır, index.html'deki sırayla yüklenir
   (app.js → projects.js → ui.js → main.js). Yükleme anında çalışan kod sadece önceki dosyaları kullanır. */

/* ==================================================================== olaylar */
const handlers = {
  status: applyStatus,
  team: onTeamEvent,
  team_live: onTeamLive,
  project_note: onProjectNote,
  user: (ev) => addUser(ev.text, ev.mi, ev.images),
  busy: (ev) => { S.status.busy = ev.value; setBusy(ev.value); if (!ev.value) loadSessions(); },
  stream_start: () => { if (!S.cur) showWorking("Düşünüyor"); },
  thinking: (ev) => onThinking(ev.text),
  delta: (ev) => onDelta(ev.text),
  stream_reset: () => { if (S.cur) { S.cur.text = ""; S.cur.prose.replaceChildren(); } },
  stream_end: (ev) => { onStreamEnd(ev.text, ev.source); if (S.busy) showWorking("Çalışıyor"); },
  tool_start: (ev) => toolRow(ev.id, ev.name, ev.summary, true),
  tool_end: (ev) => {
    const t = S.tools.get(ev.id) || toolRow(ev.id, ev.name, "", true);
    // Sonuç (diff, plan kartı) eklenmeden önce alttaysak, eklendikten sonra da altta kal.
    const stick = nearBottom();
    finishTool(t, ev, true);
    if (S.busy) showWorking("Çalışıyor");
    toBottom(stick);
  },
  update: renderUpdate,
  permission: onPermission,
  permission_done: onPermissionDone,
  ask_choice: onChoice,
  ask_choice_done: onChoiceDone,
  info: (ev) => addNote(ev.text, /hata|başarısız|cevap veremedi|başlatılamadı/i.test(ev.text) ? "err" :
    /durduruldu|limit/i.test(ev.text) ? "warn" : ""),
  route: (ev) => {
    const m = ev.text.match(/^brain -> (.+)$/);
    if (m) { applyStatus({ model: m[1] }); return; }
    // Model geçişleri ve sağlayıcı hataları: hangi modelin neden atlandığı görünsün.
    if (!/^(fast|vision) -> /.test(ev.text)) addNote(ev.text, /hata|geçildi|meşgul|atlandı/i.test(ev.text) ? "warn" : "");
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
      loadHistory().then(() => { if (S.busy) showWorking("Çalışıyor"); });
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
  return s && s.title ? `“${s.title.slice(0, 40)}”` : "Arkadaki sohbet";
}
function onBackground(ev) {
  if (ev.type === "busy") {
    if (!ev.value) toast(`${sessionTitle(ev.sid)} bitti`);
    refreshStatus();
    loadSessions();
  } else if (ev.type === "permission" || ev.type === "ask_choice") {
    toast(`${sessionTitle(ev.sid)} senden onay bekliyor; ona geçince görürsün`);
    refreshStatus();
  }
}

(async function boot() {
  greet();
  await refreshStatus();
  await loadHistory();
  connect();
  loadSessions();
  await loadProjects();
  // Telefondan/bağlantıdan gelen adres: #projects (listeyi aç) ya da #p=<id> (projeyi aç)
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
  setInterval(() => { if (!S.busy) refreshStatus(); }, 10000);
  const clock = () => {
    const m = Math.floor((Date.now() - S.started) / 60000);
    $("#stClock").textContent = m < 60 ? `${m} dk` : `${Math.floor(m / 60)} sa ${m % 60} dk`;
  };
  clock();
  setInterval(clock, 30000);
})();
