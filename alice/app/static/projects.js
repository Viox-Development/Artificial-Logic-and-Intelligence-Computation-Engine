"use strict";

/* ALICE — takım, projeler ve inceleme kurulu.
   Sıradan (modül olmayan) betikler: hepsi aynı kapsamı paylaşır, index.html'deki sırayla yüklenir
   (app.js → projects.js → ui.js → main.js). Yükleme anında çalışan kod sadece önceki dosyaları kullanır. */

/* ===================================================================== takım */
// Sohbetteki mini kart: ALICE'in başlattığı projenin durumu. Ayrıntı ve yönetim Projeler sayfasında.
const TEAM_ST = { queued: _("queued"), running: _("working"), waiting: _("waiting"), asking: _("asking"),
  review: _("awaiting review"), done: _("done"), failed: _("error"), stopped: _("stopped"), idle: _("idle") };
const PROJ_ST = { planning: _("planning"), running: _("working"), review: _("in review"), needs_you: _("asking you"),
  done: _("done"), stopped: _("stopped") };
function renderTeam(t) {
  const box = $("#team");
  S.team = t;
  if (!t || box.dataset.closed === t.id) { box.hidden = true; return; }
  box.hidden = false;
  box.replaceChildren();
  const [done, total] = t.progress || [0, 0];
  const head = el("div", "team-head");
  const open = el("button", "team-open");
  open.title = _("Open the project");
  open.append(el("i", "dot st-" + t.status), el("b", "", t.title), el("span", "pill " + t.status, PROJ_ST[t.status] || t.status),
    progressBar(done, total), el("span", "team-sum", total ? `${done}/${total}` : ""));
  open.addEventListener("click", () => openProject(t.id));
  const x = el("button", "act"); x.title = _("Hide"); x.appendChild(svg(IC.x));
  x.addEventListener("click", () => { box.dataset.closed = t.id; box.hidden = true; });
  head.append(open, x);
  box.appendChild(head);
  const q = (t.questions || []).find((q) => !q.answer);
  const line = q ? _("Alice asks: {text}", { text: q.text }) : t.status === "done" ? (t.summary || "").split("\n")[0] : activeLine(t);
  if (line) box.appendChild(el("div", "team-line" + (q ? " ask" : ""), line));
}
function activeLine(t) {
  const busy = t.members.filter((m) => ["running", "waiting", "asking"].includes(m.status));
  const s = busy.slice(0, 2).map((m) => `${m.name}: ${m.last ? friendlyLast(m.last) : TEAM_ST[m.status]}`).join(" · ");
  return s || (t.lead && t.lead.state === "running" ? _("Alice is planning / reviewing…") : "");
}
function progressBar(done, total) {
  const b = el("span", "prog"), i = el("i");
  i.style.width = total ? `${(100 * done) / total}%` : "0";
  b.appendChild(i);
  return b;
}
// Sohbetteki kart sadece ALICE'in o anki projesini gösterir; Projeler sayfası ve listesi her projeyi izler.
function onTeamEvent(ev) {
  if (!S.team || S.team.id === ev.id || ev.origin === "alice") renderTeam(ev);
  if (S.proj && S.proj.id === ev.id) refreshProjectSoon();
  clearTimeout(onTeamEvent._t);
  onTeamEvent._t = setTimeout(loadProjects, 800);
}
// Ajan ya da yönetici cevabını yazarken metin ve düşünme canlı gelir; açık konuşmanın altında akar.
function onTeamLive(ev) {
  S.live = S.live || {};
  const key = ev.id + "/" + ev.name;
  if (!ev.text && !ev.think && !ev.tool) delete S.live[key]; else S.live[key] = ev;
  const pr = S.proj;
  if (pr && pr.id === ev.id && pr.sel === ev.name && (pr.tab || "chat") === "chat") drawLive();
}
function drawLive() {
  const pr = S.proj;
  const feed = document.querySelector("#projectView .pd-feed");
  if (!pr || !feed) return;
  const ev = (S.live || {})[pr.id + "/" + pr.sel];
  let box = feed.querySelector(".pf-stream");
  if (!ev) { if (box) box.remove(); return; }
  const stick = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 60;
  if (!box) {
    // Normal sohbetteki canlı cevapla aynı: açılır düşünme bölümü, imleçli metin, araç hazırlanırken gösterge.
    box = el("div", "turn-ai pf-stream");
    const th = el("details", "thought");
    const sum = el("summary");
    sum.append(svg(IC.chev), el("span", "shimmer", _("Thinking")));
    th.append(sum, el("div", "tbody"));
    const w = el("div", "working pf-tooling");
    w.append(svg(IC.spark, "spark"), el("span", "lbl shimmer", ""));
    box.append(th, el("div", "prose cursor"), w);
    const live = feed.querySelector(".pf-live");
    if (live) live.remove();
    feed.appendChild(box);
  }
  const th = box.querySelector(".thought");
  th.hidden = !ev.think;
  th.querySelector(".tbody").textContent = ev.think || "";
  const ts = th.querySelector("summary span");
  ts.textContent = ev.text || ev.tool ? _("Thought") : _("Thinking");
  ts.classList.toggle("shimmer", !(ev.text || ev.tool));
  const prose = box.querySelector(".prose");
  setMd(prose, ev.text || "");
  prose.hidden = !ev.text;
  const tl = box.querySelector(".pf-tooling");
  tl.hidden = !ev.tool;
  if (ev.tool) {
    const [name, ...rest] = ev.tool.split(_(" preparing"));
    const size = rest.join("").replace(/^ · /, "");
    const what = TOOL_ING[name] || _("preparing a tool");
    tl.querySelector(".lbl").textContent = what.charAt(0).toUpperCase() + what.slice(1) + "…" + (size ? " " + size : "");
  }
  if (stick) feed.scrollTop = feed.scrollHeight;
}
// Projeden sohbete bildirim (soru, bitiş): tıklanınca projeyi açan kart.
function onProjectNote(ev) {
  const b = el("button", "pnote " + (ev.kind || "info"));
  b.append(el("i", "dot st-" + (ev.kind === "ask" ? "needs_you" : ev.kind === "done" ? "done" : "running")),
    el("span", "", ev.text), el("b", "", _("Open the project →")));
  b.addEventListener("click", () => openProject(ev.id));
  add(b);
  if (ev.kind === "ask" || ev.kind === "done") toast(ev.kind === "ask" ? _("The project is asking you") : _("The project is done"));
  loadProjects();
}
function fmtSecs(n) { return n < 60 ? _("{secs} s", { secs: n }) : n < 3600 ? _("{m} min {s} s", { m: Math.floor(n / 60), s: n % 60 })
  : _("{h} h {m} min", { h: Math.floor(n / 3600), m: Math.floor((n % 3600) / 60) }); }
async function teamAction(action, name, text) {
  if (!S.team) return;
  const r = await api("/api/project/action", { id: S.team.id, action, name, text: text || "" });
  if (!r.ok) return toast(r.error || _("Didn't work"));
  if (r.team) renderTeam(r.team);
}

/* ================================================================== projeler */
// Claude Code'daki Projeler gibi: her proje kendi yöneticisi (Alice) olan bir ajan takımı; her ajan ayrı bir konuşma.
// Konular duruma göre gruplanır: soru soranlar ve inceleme bekleyenler üstte, bitenler altta.
async function loadProjects() {
  const rows = await api("/api/projects");
  S.projects = Array.isArray(rows) ? rows : [];
  drawProjects();
}
function drawProjects() {
  const nav = $("#projects");
  nav.replaceChildren();
  const rows = S.projects || [];
  // Şeritteki Projeler simgesinde nokta: bir proje cevap ya da onay bekliyor.
  const dot = $("#projDot");
  if (dot) dot.hidden = !rows.some((p) => p.questions || (p.members || []).some((m) => m.status === "review"));
  if (app.dataset.sec === "projects" && !S.proj && typeof showProjects === "function") showProjects();
  if (!rows.length) { nav.appendChild(el("div", "side-empty small", _("No projects yet"))); return; }
  for (const p of rows.slice(0, 30)) {
    const b = el("button", "proj-row" + (S.proj && S.proj.id === p.id ? " active" : ""));
    b.title = `${p.goal || p.title}\n${PROJ_ST[p.status] || p.status} · ${(p.members || []).length} ajan`;
    b.append(el("i", "dot st-" + p.status), el("span", "pt", p.title || _("(untitled)")));
    const rev = (p.members || []).filter((m) => m.status === "review").length;
    if (p.questions) b.appendChild(el("span", "badge ask", "?"));
    else if (rev) b.appendChild(el("span", "badge rev", String(rev)));
    b.appendChild(el("small", "", relTime(p.updated || p.created)));
    b.addEventListener("click", () => openProject(p.id));
    nav.appendChild(b);
  }
}
async function openProject(id, sel) {
  const same = S.proj && S.proj.id === id;
  S.proj = { id, sel: sel || (same ? S.proj.sel : null), tab: same ? S.proj.tab : "chat", data: null };
  $("#app").classList.add("in-project");
  $("#projectView").hidden = false;
  markSection("projects");
  drawProjects();
  closeSideOnMobile();
  await refreshProject();
}
// ---- site önizlemesi: korumalı (ayrı kökenli) çerçeve. Çerçeve, sık yenilenen proje ekranının dışında durur ve
// sadece yerine oturtulur; böylece her güncellemede site baştan yüklenmez, dosyalar değişince kendini yeniler.
function renderPreview(det, d) {
  const pr = S.proj, p = d.preview;
  const h = el("div", "pd-head");
  const r = el("div", "pd-row");
  const nm = el("div", "pd-name");
  nm.append(el("b", "", _("Preview")), el("small", "", p.entry));
  const acts = el("div", "pd-acts");
  const mode = pr.pvMode || "desk";
  for (const [k, label] of [["desk", _("Desktop")], ["phone", _("Phone")]]) {
    const b = el("button", "btn ghost" + (mode === k ? " on" : ""), label);
    b.addEventListener("click", () => { pr.pvMode = k; renderProject(); });
    acts.appendChild(b);
  }
  const rl = el("button", "btn ghost", _("Reload"));
  rl.addEventListener("click", () => reloadPreview());
  const op = el("button", "btn ghost", _("Open in a new tab"));
  op.addEventListener("click", () => window.open(p.base + p.entry, "_blank", "noopener"));
  acts.append(rl, op);
  r.append(avatar("", "", IC.globe), nm, acts);
  h.appendChild(r);
  const wrap = el("div", "pv-frame-wrap" + (mode === "phone" ? " phone" : ""));
  wrap.appendChild(el("div", "pv-frame-slot"));
  det.append(h, wrap);
  det.classList.add("preview");
  const url = p.base + p.entry;
  const host = previewHost();
  if (host._url !== url) { host._url = url; host._frame.src = url; pr.pvChanged = p.changed; }
  else if (p.changed && pr.pvChanged && p.changed > pr.pvChanged) { pr.pvChanged = p.changed; reloadPreview(); }
  else if (p.changed) pr.pvChanged = p.changed;
}
function previewHost() {
  if (S.pvHost) return S.pvHost;
  const host = el("div", "pv-host");
  host.hidden = true;
  const f = el("iframe");
  f.setAttribute("sandbox", "allow-scripts allow-forms allow-popups allow-modals");
  f.setAttribute("referrerpolicy", "no-referrer");
  f.title = _("Site preview");
  host.appendChild(f);
  host._frame = f;
  document.body.appendChild(host);
  window.addEventListener("resize", placePreview);
  S.pvHost = host;
  return host;
}
function reloadPreview() {
  const h = S.pvHost;
  if (h && h._url) { h._frame.src = "about:blank"; setTimeout(() => { h._frame.src = h._url; }, 30); }
}
function placePreview() {
  const h = S.pvHost;
  if (!h) return;
  const slot = S.proj && S.proj.sel === "__preview" ? document.querySelector("#projectView .pv-frame-slot") : null;
  if (!slot || $("#projectView").hidden) { h.hidden = true; return; }
  const r = slot.getBoundingClientRect();
  Object.assign(h.style, { top: r.top + "px", left: r.left + "px", width: r.width + "px", height: r.height + "px",
    borderRadius: getComputedStyle(slot).borderRadius });
  h.hidden = false;
  if (!slot._ro) { slot._ro = new ResizeObserver(placePreview); slot._ro.observe(slot); }
}
function closeProject() {
  if (S.pvHost) { S.pvHost.hidden = true; S.pvHost._url = ""; S.pvHost._frame.src = "about:blank"; }
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
  S.proj = null;
  $("#app").classList.remove("in-project");
  $("#projectView").hidden = true;
  drawProjects();
}
function refreshProjectSoon() {
  if (refreshProjectSoon._t) return;
  refreshProjectSoon._t = setTimeout(() => { refreshProjectSoon._t = null; refreshProject(); }, 700);
}
// İlk açılışta dikkat isteyen konu seçilir: soru soran, inceleme bekleyen, çalışan; yoksa yönetici.
function pickThread(d) {
  for (const st of ["asking", "review", "running", "waiting"]) {
    const m = d.members.find((x) => x.status === st);
    if (m) return m.name;
  }
  return "alice";
}
async function refreshProject() {
  if (!S.proj) return;
  const pr = S.proj;
  if (!pr.sel) {
    const d0 = await api(`/api/project?id=${encodeURIComponent(pr.id)}`);
    if (!d0 || d0.ok === false || !d0.id) { toast((d0 && d0.error) || _("Could not open the project")); closeProject(); loadProjects(); return; }
    pr.sel = pickThread(d0);
  }
  const sel = pr.sel !== "__log" ? pr.sel : "";  // "__council": proje kurulları, "__preview": site önizlemesi
  const q = `/api/project?id=${encodeURIComponent(pr.id)}` + (sel ? `&member=${encodeURIComponent(sel)}` : "");
  const [d, df] = await Promise.all([api(q),
    pr.tab === "diff" && sel && !["alice", "__council", "__timeline", "__preview"].includes(sel) ? api(`/api/project/diff?id=${encodeURIComponent(pr.id)}&member=${encodeURIComponent(sel)}`) : null]);
  if (S.proj !== pr) return;
  if (!d || d.ok === false || !d.id) { toast((d && d.error) || _("Could not open the project")); closeProject(); loadProjects(); return; }
  pr.data = d;
  pr.diff = df ? df.diff || "" : null;
  renderProject();
}
function renderProject() {
  const pr = S.proj, d = pr.data, v = $("#projectView");
  // Yazılmakta olan mesaj ve kaydırma yeri yenilemede kaybolmasın.
  const keep = {};
  for (const t of v.querySelectorAll("textarea[data-k], input[data-k], select[data-k]")) keep[t.dataset.k] = { v: t.value, f: document.activeElement === t };
  // Açılıp kapanan bölümler (kurul kartları, adımlar, ayrıntılar) yenilemede kullanıcının bıraktığı gibi kalsın.
  pr.openState = pr.openState || {};
  for (const x of v.querySelectorAll("details[data-k]")) pr.openState[x.dataset.k] = x.open;
  const feed = v.querySelector(".pd-feed");
  const atBottom = !feed || feed.scrollHeight - feed.scrollTop - feed.clientHeight < 40;
  const oldTop = feed ? feed.scrollTop : 0;
  v.replaceChildren();

  // ---- başlık: başlık, durum ve ilerleme; ikincil işler "⋯" menüsünde
  const head = el("div", "pv-head");
  // Geri: proje listesine (kenar çubuğundaki Projeler bölümü açık kalır).
  const back = el("button", "pv-back"); back.title = _("Back to the project list");
  back.innerHTML = '<svg viewBox="0 0 24 24"><path d="M15 6l-6 6 6 6"/></svg><span>Projeler</span>';
  back.addEventListener("click", () => { markSection("projects"); closeProject(); });
  const mid = el("div", "grow");
  const t1 = el("div", "pv-trow");
  const h2 = el("h2", "pv-title", d.title);
  h2.title = d.goal || d.title;
  t1.append(h2, el("span", "pill " + d.status, PROJ_ST[d.status] || d.status));
  const [done, total] = d.progress || [0, 0];
  const t2 = el("div", "pv-sub");
  t2.append(progressBar(done, total), el("span", "", total ? _("{done}/{total} jobs approved", { done: done, total: total }) : _("no agents yet")), el("span", "", relTime(d.created)));
  const use = usageText(d.usage && d.usage.total);
  if (use) {
    const u = el("span", "pv-usage", use);
    u.title = _("Tokens spent on this project") + (d.usage.total.cost !== undefined ? _(" and the cost the provider reported") : "") +
      "\n" + Object.entries(d.usage.by || {}).map(([k, r]) => `${k === "kurul" ? "kurul" : k}: ${usageText(r)}`).join("\n");
    t2.appendChild(u);
  }
  mid.append(t1, t2);
  const g = (d.goal || "").trim();
  // Başlık hedefin kısaltılmışıysa hedefi ayrıca yazma (aynı cümle iki kez görünüyordu).
  const stem = d.title.trim().replace(/[….]+$/, "");
  if (g && g !== d.title.trim() && !g.startsWith(stem)) {
    const goal = el("div", "pv-goal", g);
    goal.title = _("Show all");
    goal.addEventListener("click", () => goal.classList.toggle("open"));
    mid.appendChild(goal);
  }
  const acts = el("div", "pv-actions");
  const add = el("button", "btn", _("Add an agent"));
  add.addEventListener("click", () => projectDialog(d));
  acts.appendChild(add);
  if (d.active) {
    const st = el("button", "btn danger", _("Stop"));
    st.title = _("Stop all agents and the lead");
    st.addEventListener("click", () => projectAction("stop", ""));
    acts.appendChild(st);
  } else if (d.status === "stopped") {
    const rs = el("button", "btn accent", _("Resume"));
    rs.title = _("Stopped agents continue where they left off");
    rs.addEventListener("click", () => projectAction("resume", ""));
    acts.appendChild(rs);
  }
  const more = el("button", "icon-btn pv-more"); more.title = _("More");
  more.innerHTML = '<svg viewBox="0 0 24 24"><circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/></svg>';
  more.addEventListener("click", () => openMenu(more, (m) => {
    if (d.council && d.members.length)
      menuItem(m, IC.shield, d.council_busy ? _("The council is meeting…") : _("Ask the council"), _("Have the council review the project as it is now"),
        () => { if (!d.council_busy) projectAction("council", ""); });
    if (d.members.length) menuItem(m, IC.copy, _("Save as a template"), _("Goal, settings and agents"), () => saveTemplateDialog(d));
    m.appendChild(el("div", "menu-sep"));
    m.appendChild(el("div", "menu-title", _("Project")));
    for (const x of [_("Lead: {model}{picked}{thinking}", { model: d.lead.model, picked: d.lead.picked ? " (" + d.lead.picked + ")" : "", thinking: d.lead.thinking !== "default" ? " · " + d.lead.thinking : "" }),
      ...d.members.filter((m) => m.picked).map((m) => `${m.name}: ${m.picked}`),
      d.council ? _("Council on") : d.review ? _("Review on") : _("Review off"), d.cwd, d.check ? _("Check: {command}", { command: d.check }) : ""].filter(Boolean))
      m.appendChild(el("div", "menu-info", x));
    if (!d.active) {
      m.appendChild(el("div", "menu-sep"));
      menuItem(m, IC.trash, _("Delete the project"), _("The record and conversations are deleted; files stay"), async () => {
        if (!(await confirmDialog(_("Delete the project?"), _("The project's record and the agents' conversations are deleted. Changes made to files stay."), _("Delete")))) return;
        if (await projectAction("delete", "")) { closeProject(); loadProjects(); }
      });
    }
  }, "right"));
  acts.appendChild(more);
  head.append(back, mid, acts);
  v.appendChild(head);

  // ---- sorular ve özet
  for (const q of (d.questions || []).filter((q) => !q.answer)) {
    const c = el("div", "pv-ask");
    c.append(el("b", "", _("Alice asks")), el("div", "pv-ask-q", q.text));
    const row = el("div", "pv-ask-row");
    const inp = el("input"); inp.type = "text"; inp.placeholder = _("Your answer…"); inp.dataset.k = "q" + q.id;
    const go = el("button", "btn accent", _("Answer"));
    const send = async () => { const t = inp.value.trim(); if (t && await projectAction("answer", q.id, t)) inp.value = ""; };
    go.addEventListener("click", send);
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); send(); } });
    row.append(inp, go);
    c.appendChild(row);
    v.appendChild(c);
  }
  if (d.pr && d.pr.on) {
    // Sonuç PR olan proje: kendi dalında çalışır, bitince PR açılır (alice/team_pr.py).
    const c = el("div", "pv-pr");
    c.appendChild(svg(IC.branch));
    if (d.pr.url) {
      const a = el("a", "", `PR #${d.pr.number} · ${d.pr.branch} → ${d.pr.base}`);
      a.href = d.pr.url; a.target = "_blank"; a.rel = "noopener";
      c.append(a, el("span", "muted", _("watched: red CI and comments come to the chat")));
    } else {
      c.append(el("span", "", d.pr.branch ? _("On its own branch: {branch} → {base}", { branch: d.pr.branch, base: d.pr.base || "?" }) : _("The result will be a PR")),
        el("span", "muted", _("it is pushed and a PR is opened when done")));
    }
    v.appendChild(c);
  }
  if (d.summary) {
    const c = el("details", "pv-summary");
    c.dataset.k = "summary";
    // Kapalı başlar: her sekmede ekranın üstünü kaplıyordu. Açınca açık kalır.
    c.open = !!pr.summaryOpen;
    c.addEventListener("toggle", () => { pr.summaryOpen = c.open; });
    const sm = el("summary", "");
    const peek = (d.summary.split("\n").map((x) => x.replace(/^[#>*\-\s]+/, "").trim()).find((x) => x) || "").slice(0, 160);
    sm.append(el("span", "", _("Project summary")), el("span", "sum-peek", peek));
    const body = el("div", "prose");
    setMd(body, d.summary);
    c.append(sm, body);
    v.appendChild(c);
  }

  // ---- konular: daraltılabilir gruplar; liste gizlenebilir (sadece avatarlar) ve genişliği sürüklenir
  const ui = projUI();
  const body = el("div", "pv-body" + (ui.rail ? " rail" : ""));
  body.style.setProperty("--list-w", ui.w + "px");
  const list = el("div", "pv-threads");
  const lh = el("div", "th-head");
  lh.appendChild(el("span", "", _("Team")));
  const railBtn = el("button", "icon-btn"); railBtn.title = ui.rail ? _("Open the list") : _("Collapse the list");
  railBtn.innerHTML = '<svg viewBox="0 0 24 24"><rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M9.5 4.5v15"/></svg>';
  railBtn.addEventListener("click", () => { projUI({ rail: !ui.rail }); renderProject(); });
  lh.appendChild(railBtn);
  list.appendChild(lh);
  const lead = { name: "alice", status: d.lead.state, task: d.status === "planning" ? _("Planning the work and setting up the agents") : _("Plans, leads, reviews"),
    model: d.lead.model, last: d.lead.last };
  const section = (key, title, items, cls) => {
    if (!items.length) return;
    const closed = !!ui.closed[key];
    const sh = el("button", "th-sec" + (cls ? " " + cls : "") + (closed ? " closed" : ""));
    sh.append(svg(IC.chev, "chev"), el("span", "", title), el("small", "", String(items.length)));
    sh.addEventListener("click", () => { ui.closed[key] = !closed; projUI({ closed: ui.closed }); renderProject(); });
    list.appendChild(sh);
    if (closed && !ui.rail) return;
    const box = el("div", "th-group");
    for (const it of items) box.appendChild(it);
    list.appendChild(box);
  };
  section("general", _("General"), [threadItem(lead, true), logItem(d), timelineItem(d),
    ...((d.councils || []).length || d.council_busy ? [councilItem(d)] : []),
    ...(d.preview ? [navItem("__preview", _("Preview"), d.preview.entry, IC.globe, "")] : [])]);
  const by = (sts) => d.members.filter((m) => sts.includes(m.status)).map((m) => threadItem(m));
  section("asking", _("Asking"), by(["asking"]), "warn");
  section("review", _("Awaiting review"), by(["review"]), "rev");
  section("running", _("Working"), by(["running", "waiting"]));
  section("queued", _("Queued"), by(["queued"]));
  section("done", _("Done"), by(["done"]), "ok");
  section("failed", "Durdu / hata", by(["stopped", "failed"]), "err");
  const grip = el("div", "pv-grip"); grip.title = _("Drag: list width");
  grip.addEventListener("pointerdown", (e) => {
    if (ui.rail) return;
    e.preventDefault(); grip.setPointerCapture(e.pointerId);
    const x0 = e.clientX, w0 = ui.w;
    const mv = (ev) => { ui.w = Math.max(220, Math.min(480, w0 + (ev.clientX - x0) / uiZoom())); body.style.setProperty("--list-w", ui.w + "px"); };
    const up = () => { grip.removeEventListener("pointermove", mv); projUI({ w: ui.w }); };
    grip.addEventListener("pointermove", mv);
    grip.addEventListener("pointerup", up, { once: true });
  });
  const det = el("div", "pv-detail");
  if (pr.sel === "__log") renderTeamLog(det, d);
  else if (pr.sel === "__council") renderProjectCouncils(det, d);
  else if (pr.sel === "__timeline") renderTimeline(det, d);
  else if (pr.sel === "__preview" && d.preview) renderPreview(det, d);
  else if (pr.sel === "__preview") { pr.sel = "alice"; refreshProjectSoon(); det.appendChild(el("div", "pd-empty", _("No site found"))); }
  else if (d.member) renderThread(det, d.member, d);
  else det.appendChild(el("div", "pd-empty", _("Choose a thread")));
  body.append(list, grip, det);
  v.appendChild(body);

  drawLive();
  placePreview();
  if (d.council_busy) setTimeout(() => { if (S.proj === pr) refreshProjectSoon(); }, 4000);  // süren kurulun süresi aksın
  const nf = v.querySelector(".pd-feed");
  if (nf) nf.scrollTop = atBottom || pr.lastSel !== pr.sel ? nf.scrollHeight : oldTop;
  pr.lastSel = pr.sel;
  for (const x of v.querySelectorAll("details[data-k]")) if (x.dataset.k in pr.openState) x.open = pr.openState[x.dataset.k];
  for (const t of v.querySelectorAll("textarea[data-k], input[data-k], select[data-k]")) {
    const k = keep[t.dataset.k];
    if (k) { t.value = k.v; if (k.f) t.focus(); }
  }
}
// Token (ve sağlayıcı bildiriyorsa ücret) özeti: "84k token · $0,12"
function usageText(u) {
  if (!u) return "";
  const n = (u.in || 0) + (u.out || 0);
  if (!n) return "";
  return `${fmtTok(n)} token` + (u.cost ? ` · $${u.cost < 0.01 ? u.cost.toFixed(4) : u.cost.toFixed(2)}` : "");
}
// Proje ekranı tercihleri (liste genişliği, daraltılmış gruplar) bu tarayıcıda hatırlanır.
function projUI(patch) {
  let ui = { w: 288, rail: false, closed: { done: false } };
  try { ui = { ...ui, ...JSON.parse(localStorage.getItem("alice.projUI") || "{}") }; } catch {}
  if (patch) { ui = { ...ui, ...patch }; try { localStorage.setItem("alice.projUI", JSON.stringify(ui)); } catch {} }
  return ui;
}
function hueOf(s) { let h = 0; for (const c of s) h = (h * 31 + c.charCodeAt(0)) % 360; return h; }
function avatar(name, st, icon) {
  const a = el("span", "av" + (icon ? " ic" : ""));
  if (name === "alice") { const img = el("img"); img.src = "/static/alice.png"; img.alt = ""; a.appendChild(img); }
  else if (icon) a.appendChild(svg(icon));
  else { a.style.setProperty("--h", hueOf(name)); a.appendChild(document.createTextNode(name.slice(0, 1).toUpperCase())); }
  if (st) a.appendChild(el("i", "dot st-" + st));
  return a;
}
function navItem(key, title, sub, icon, count) {
  const pr = S.proj;
  const b = el("button", "th nav" + (pr.sel === key ? " sel" : ""));
  b.title = title;
  const tx = el("div", "th-tx");
  const top = el("div", "th-top");
  top.appendChild(el("b", "", title));
  if (count) top.appendChild(el("small", "", count));
  tx.appendChild(top);
  if (sub) tx.appendChild(el("div", "th-last", sub));
  b.append(avatar("", "", icon), tx);
  b.addEventListener("click", () => { pr.sel = key; refreshProject(); });
  return b;
}
function threadItem(m, isLead) {
  const pr = S.proj;
  const b = el("button", "th" + (pr.sel === m.name ? " sel" : "") + (isLead ? " lead" : ""));
  const st = isLead ? (m.status === "running" ? "running" : m.status === "done" ? "done" : "idle") : m.status;
  b.title = isLead ? _("Alice · lead") : m.name;
  const tx = el("div", "th-tx");
  const top = el("div", "th-top");
  top.append(el("b", "", isLead ? "Alice" : m.name), el("small", "st-" + st, TEAM_ST[st] || st));
  tx.appendChild(top);
  // Tek satır: çalışırken ne yaptığı, beklerken neyi beklediği; yoksa görevi.
  let line;
  if (m.status === "queued" && m.after && m.after.length) line = _("starts when {names} finish", { names: m.after.join(", ") });
  else if (m.status === "asking" && m.question) line = m.question;
  else if (m.council_running) line = m.council && m.council.state === "running" ? councilLine(m.council) : _("The council is meeting");
  else if (m.status === "review" && m.council && m.council.decision) line = _("Council: {decision}", { decision: DEC_TR[m.council.decision] || m.council.decision });
  else if (st === "running" && m.last) line = friendlyLast(m.last);
  else line = m.task;
  tx.appendChild(el("div", "th-last", line));
  b.append(avatar(isLead ? "alice" : m.name, st), tx);
  b.addEventListener("click", () => { pr.sel = m.name; pr.tab = "chat"; refreshProject(); });
  return b;
}
function logItem(d) {
  const last = d.log[d.log.length - 1];
  return navItem("__log", _("Team chat"), last ? `${last.from}: ${last.text}` : _("No messages yet"), IC.chat, d.log.length ? String(d.log.length) : "");
}
function renderThread(det, m, d) {
  const pr = S.proj, isLead = m.name === "alice";
  const h = el("div", "pd-head");
  const r1 = el("div", "pd-row");
  const st = isLead ? (m.status === "running" ? "running" : m.status === "done" ? "done" : "idle") : m.status;
  const nm = el("div", "pd-name");
  const mu = usageText(((d.usage || {}).by || {})[m.name]);
  const picked = isLead ? (d.lead || {}).picked : m.picked;
  nm.append(el("b", "", isLead ? "Alice" : m.name),
    el("small", "", [isLead ? _("lead") : TEAM_ST[st] || st, picked, mu].filter(Boolean).join(" · ")));
  nm.title = [m.model, m.thinking !== "default" ? _("thinking ") + m.thinking : "", isLead ? "" : m.isolation,
    m.steps ? _("{n} steps", { n: m.steps }) : "", m.secs ? fmtSecs(m.secs) : ""].filter(Boolean).join(" · ");
  r1.append(avatar(isLead ? "alice" : m.name, st), nm);
  if (!isLead) {
    if (["running", "waiting", "asking"].includes(m.status)) {
      const b = el("button", "btn danger", _("Stop"));
      b.addEventListener("click", () => projectAction("stop", m.name));
      r1.appendChild(b);
    } else {
      if (m.files && m.files.length) {
        const b = el("button", "btn ghost", _("Undo"));
        b.title = _("Undo this agent's file changes");
        b.addEventListener("click", async () => {
          if (await confirmDialog(_("Undo the changes?"), _("The changes the {name} agent made with the file tools are undone ({n} files).", { name: m.name, n: m.files.length }), _("Undo")))
            projectAction("revert", m.name);
        });
        r1.appendChild(b);
      }
      if (d.council && !m.council_running) {
        const k = el("button", "btn ghost", _("Ask the council"));
        k.title = _("Ask the review council about this work (critic, advocate, checker, experts)");
        k.addEventListener("click", () => { pr.tab = "council"; projectAction("council", m.name); });
        r1.appendChild(k);
      }
      const b = el("button", "btn ghost", _("Restart"));
      b.title = _("Redo the task with a clean conversation");
      b.addEventListener("click", () => projectAction("restart", m.name));
      r1.appendChild(b);
    }
  }
  const rawBtn = el("button", "btn ghost" + (rawView() ? " on" : ""), rawView() ? _("Simple view") : _("Raw view"));
  rawBtn.title = _("Show tool calls and internal events raw");
  rawBtn.addEventListener("click", () => { try { localStorage.setItem("alice.rawThreads", rawView() ? "" : "1"); } catch {} renderProject(); });
  r1.appendChild(rawBtn);
  const pacts = el("div", "pd-acts");
  for (const b of [...r1.querySelectorAll(":scope > .btn")]) pacts.appendChild(b);
  r1.appendChild(pacts);
  // Görev tek satır; tıklayınca görev, sınırlar ve kapsam açılır.
  const task = el("div", "pd-task");
  task.appendChild(el("span", "", m.task));
  const more = el("div", "pd-task-more");
  if (m.constraints) more.appendChild(el("div", "", _("Limits: ") + m.constraints));
  if (m.scope && m.scope.length) more.appendChild(el("div", "", _("Where it may write: ") + m.scope.join(", ")));
  if (m.after && m.after.length) more.appendChild(el("div", "", _("Started after these finished: ") + m.after.join(", ")));
  task.appendChild(more);
  task.title = _("Show all");
  pr.taskOpen = pr.taskOpen || {};
  if (pr.taskOpen[m.name]) task.classList.add("open");
  task.addEventListener("click", () => { pr.taskOpen[m.name] = task.classList.toggle("open"); });
  h.appendChild(r1);
  if (!isLead) h.appendChild(task);
  const r2 = el("div", "pd-row");
  if (m.branch) r2.appendChild(el("span", "chip file", _("branch ") + m.branch));
  if (m.limit_hit) r2.appendChild(el("span", "chip warn", _("step limit reached")));
  if (r2.childNodes.length) h.appendChild(r2);
  det.appendChild(h);

  if (m.status === "review") {
    const bar = el("div", "pd-review");
    const rvText = m.council_running ? `${m.council && m.council.state === "running" ? councilLine(m.council) +
      (m.council.phase ? " · " + m.council.phase.toLowerCase() : "") : _("The council met")}. İstersen beklemeden kararı sen ver.`
      : m.council && m.council.decision === "contested" ? _("The council is split; Alice will decide. You can vote in the Council tab or make the call yourself.")
      : d.review ? _("Awaiting review. Alice is reviewing; you can make the call if you like.") : _("Done, awaiting approval.");
    bar.appendChild(el("div", "pd-rv-t", rvText));
    const row = el("div", "pd-ask-row");
    const fb = el("input"); fb.type = "text"; fb.placeholder = _("Feedback for the fix (what's missing, what you expect)…"); fb.dataset.k = "fb" + m.name;
    const no = el("button", "btn", _("Request changes"));
    no.addEventListener("click", async () => { const t = fb.value.trim(); if (!t) { fb.focus(); return toast(_("Write the feedback first")); } if (await projectAction("revise", m.name, t)) fb.value = ""; });
    const ok = el("button", "btn accent", _("Approve"));
    ok.addEventListener("click", () => projectAction("approve", m.name));
    row.append(fb, no, ok);
    bar.appendChild(row);
    det.appendChild(bar);
  } else if (m.status === "failed" && !isLead) {
    const bar = el("div", "pd-review err");
    bar.appendChild(el("div", "pd-rv-t", (m.result || _("Failed.")).replace(/^hata: /, _("Error: "))));
    const row = el("div", "pd-ask-row");
    const go = el("button", "btn accent", _("Resume where it left off"));
    go.title = _("Its conversation is kept; if needed it's summarized and continues with another model");
    go.addEventListener("click", () => projectAction("send", m.name, "Continue; finish the work you left half done."));
    const rs = el("button", "btn", _("Restart"));
    rs.addEventListener("click", () => projectAction("restart", m.name));
    row.append(go, rs);
    bar.appendChild(row);
    det.appendChild(bar);
  } else if (m.status === "asking" && m.question) {
    const bar = el("div", "pd-review ask");
    bar.appendChild(el("div", "pd-rv-t", _("Asking Alice: ") + m.question));
    det.appendChild(bar);
  }

  if (!isLead) {
    const tabs = el("div", "pd-tabs");
    const tabsList = [["chat", _("Conversation")], ["diff", _("Changes{files}", { files: m.files && m.files.length ? " · " + m.files.length : "" })]];
    if ((m.councils || []).length) tabsList.push(["council", `Kurul · ${m.councils.length}${m.council_running ? " …" : ""}`]);
    for (const [k, label] of tabsList) {
      const b = el("button", "pd-tab" + ((pr.tab || "chat") === k ? " on" : ""), label);
      b.addEventListener("click", () => { pr.tab = k; refreshProject(); });
      tabs.appendChild(b);
    }
    det.appendChild(tabs);
  }
  const feed = el("div", "pd-feed" + (!pr.tab || pr.tab === "chat" || isLead ? " chat" : ""));
  if (!isLead && pr.tab === "council") {
    const list = (m.councils || []).slice().reverse();
    list.forEach((c, i) => feed.appendChild(councilCard(c, m.name, i === 0)));
    if (!list.length) feed.appendChild(el("div", "pd-empty", _("The council hasn't met yet")));
  } else if (!isLead && pr.tab === "diff") {
    const text = pr.diff || "";
    if (!text.trim()) feed.appendChild(el("div", "pd-empty", _("This agent didn't change any files")));
    for (const part of text.split(/\n(?=--- )/)) {
      if (!part.trim()) continue;
      if (!part.includes("\n@@")) { feed.appendChild(el("div", "pf-tool", part)); continue; }
      feed.appendChild(renderDiff(part).box);
    }
  } else {
    for (const node of buildFeed(m.history || [], isLead)) feed.appendChild(node);
    if (st === "running") {
      const w = el("div", "working pf-live");
      const live = isLead ? runningCouncil(d) : null;  // yönetici planı kurulda bekletiyorsa bunu söyle
      const lbl = live ? councilLine(live) : m.last ? friendlyLast(m.last) : _("working…");
      w.append(svg(IC.spark, "spark"), el("span", "lbl shimmer", lbl.charAt(0).toUpperCase() + lbl.slice(1)));
      feed.appendChild(w);
    }
    if (!feed.childNodes.length) feed.appendChild(el("div", "pd-empty", _("Nothing yet")));
  }
  det.appendChild(feed);
  const say = el("div", "pd-say");
  const ta = el("textarea"); ta.rows = 1; ta.dataset.k = "say" + m.name;
  ta.placeholder = isLead ? _("Write to Alice: new work, a change of direction, a question…") :
    ["running", "waiting", "asking"].includes(m.status) ? _("Write to {name} (it sees it on its next step)…", { name: m.name }) : _("Write to {name}: it continues where it left off…", { name: m.name });
  const go = el("button", "btn primary", _("Send"));
  const send = async () => { const v = ta.value.trim(); if (!v) return; ta.value = ""; await projectAction("send", m.name, v); };
  go.addEventListener("click", send);
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
  ta.addEventListener("input", () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 160) + "px"; });
  say.append(ta, go);
  det.appendChild(say);
}
// ---- inceleme kurulu
const ROLE_TR = { critic: _("critic"), advocate: _("advocate"), verifier: _("checker"), security: _("security"),
  performance: _("performance"), ux: _("user's eye") };
const VOTE_TR2 = { approve: _("approve"), conditional: _("conditional approve"), reject: _("reject") };
const DEC_TR = { approve: _("approve"), approve_conditions: _("conditional approve"), revise: _("changes needed"), contested: _("contested"),
  stopped: _("stopped") };
const KIND_TR = { plan: _("Plan council"), work: _("Work council"), recheck: _("Check round"), final: _("Final council"), adhoc: _("Ask the council") };
const SEV_TR = { critical: _("critical"), major: _("major"), minor: _("minor") };
const FST_TR = { confirmed: _("verified"), refuted: _("refuted"), unclear: _("unclear"), pending: _("waiting") };
function councilItem(d) {
  const last = (d.councils || [])[d.councils.length - 1];
  const live = runningCouncil(d);
  const sub = live ? councilLine(live) : d.council_busy ? _("Meeting…") :
    last ? _("{kind}: {decision}", { kind: KIND_TR[last.kind] || last.kind, decision: DEC_TR[last.decision] || _("in progress") }) : "";
  return navItem("__council", _("Council"), sub, IC.shield, "");
}
// Süren proje kurulu (plan, final, kurula sor) ve tek satırlık durumu: "Plan kurulu · 2/6 üye bitirdi · 3 dk"
function runningCouncil(d) {
  return [...(d.councils || [])].reverse().find((c) => c.state === "running") || null;
}
function councilLine(c) {
  const parts = [KIND_TR[c.kind] || _("Council")];
  if (c.total) parts.push(_("{done}/{total} members finished", { done: c.done, total: c.total }));
  else parts.push(_("meeting"));
  if (c.started) parts.push(fmtSecs(Math.max(0, Math.round(Date.now() / 1000 - c.started))));
  return parts.join(" · ");
}
// Süren kurulun canlı görünümü: aşama, geçen süre ve her üyenin durumu (inceliyor / bitti + oyu / ne okuyor).
function councilProgress(c) {
  const box = el("div", "cc-live");
  const secs = c.started ? Math.max(0, Math.round(Date.now() / 1000 - c.started)) : 0;
  const top = el("div", "cc-live-top");
  top.append(el("i", "dot st-running"), el("span", "", c.phase || _("Members are meeting")), el("small", "", secs ? fmtSecs(secs) : ""));
  box.appendChild(top);
  const act = c.activity || {};
  const roles = (c.roles || []).length ? c.roles : Object.keys(act);
  for (const r of roles) {
    const a = act[r] || { state: "waiting" };
    const row = el("div", "cc-live-row st-" + a.state);
    const st = a.state === "working" ? (a.last ? friendlyLast(a.last) : _("reviewing…"))
      : a.state === "done" ? (a.vote ? VOTE_TR2[a.vote] : "bitirdi")
      : a.state === "failed" ? _("couldn't answer") : _("waiting for its turn");
    row.append(el("i", "dot " + (a.state === "working" ? "st-running" : a.state === "done" ? "st-done" : a.state === "failed" ? "st-failed" : "st-idle")),
      el("b", "", ROLE_TR[r] || r), el("span", "v-" + (a.vote || "none"), st));
    box.appendChild(row);
  }
  return box;
}
function renderProjectCouncils(det, d) {
  const h = viewHead(IC.shield, _("Council"), _("Decisions of the plan, work and final councils"));
  const feed = el("div", "pd-feed");
  if ((d.lessons || []).length) {
    const box = el("div", "cc lessons");
    box.appendChild(el("b", "", _("Lessons of this project")));
    box.appendChild(el("div", "cc-title", _("Serious problems the council verified; later agents and councils are reminded of them.")));
    const ul = el("ul", "cc-conds");
    for (const x of d.lessons) ul.appendChild(el("li", "", x));
    box.appendChild(ul);
    feed.appendChild(box);
  }
  const list = (d.council_list || []).slice().reverse();
  list.forEach((c, i) => feed.appendChild(councilCard(c, "", i === 0)));
  if (!list.length) feed.appendChild(el("div", "pd-empty", d.council_busy ? _("The council is meeting…") : _("The council hasn't met yet")));
  det.append(h, feed);
}
// ---- zaman çizelgesi
const TL_ICON = { start: "▶", run: "▷", review: "◎", done: "✓", approve: "✓", revise: "↺", failed: "✗", stopped: "■",
  stop: "■", resume: "▶", council: "⚖", ask: "?", answer: "↩", finish: "★", check: "✔", conflict: "⚠", stall: "⏳", plan: "✎", msg: "✉", design: "◐" };
function timelineItem(d) {
  return navItem("__timeline", _("Timeline"), _("Who did what, when"), IC.list, "");
}
function tlText(x) {
  let t = x.text || "";
  if (x.kind === "msg") {
    t = t.replace(/^→ all: takıma yeni ajan katıldı: ([^:]+):[\s\S]*/, _("$1 joined the team"))
      .replace(/^→ all:/, _("→ everyone:")).replace(/^→ alice:/, "→ Alice:").replace(/^→ kullanıcı:/, _("→ you:"));
  }
  return t;
}
function renderTimeline(det, d) {
  const feed = el("div", "pd-feed chat tl");
  let day = "";
  for (const x of d.timeline || []) {
    const dt = new Date(x.at * 1000);
    const dd = dt.toLocaleDateString(ALICE_LOCALE, { day: "numeric", month: "long" });
    if (dd !== day) { day = dd; feed.appendChild(el("div", "tl-day", dd)); }
    const row = el("div", "tl-row k-" + x.kind);
    const who = x.who === "kullanıcı" ? _("You") : x.who === "alice" ? "Alice" : x.who === "kurul" ? _("Council") : x.who === "sistem" ? _("System") : x.who;
    row.append(el("small", "", dt.toLocaleTimeString(ALICE_LOCALE, { hour: "2-digit", minute: "2-digit" })),
      el("i", "", TL_ICON[x.kind] || "•"), el("b", "", who), clamp(tlText(x), 2, "tl-t"));
    feed.appendChild(row);
  }
  if (!(d.timeline || []).length) feed.appendChild(el("div", "pd-empty", _("No events yet")));
  det.append(viewHead(IC.list, _("Timeline"), _("Everything in the project, in order")), feed);
}
function saveTemplateDialog(d) {
  showDialog((dl) => {
    dl.append(el("h2", "", _("Save as a template")),
      el("p", "", _("The goal, settings and agent definitions are saved (not the conversations). It shows under 'My templates' in the new project window.")));
    const inp = txt(d.title, _("template name"));
    const row = el("div", "row");
    const no = el("button", "btn", _("Cancel")); no.addEventListener("click", closeDialog);
    const ok = el("button", "btn accent", _("Save"));
    ok.addEventListener("click", async () => { if (await projectAction("save_template", "", inp.value.trim())) closeDialog(); });
    row.append(no, ok);
    dl.append(inp, row);
    setTimeout(() => inp.focus(), 0);
  });
}
// Eski kayıtlarda döküm ham JSON içerebilir; okunur kısmı al.
function councilProse(t) {
  t = (t || "").replace(/```(?:json)?\s*\{[\s\S]*?\}\s*```/g, "");
  const m = t.search(/\{\s*"(?:vote|checks|stances|findings|reason|conditions)"/);
  if (m >= 0) t = t.slice(0, m);
  t = t.replace(/```(?:json)?\s*$/, "").trim();
  if (/"(?:stance|status|id)"\s*:/.test(t)) return "";  // baştan kırpılmış JSON parçası
  return t;
}
function findingRow(f) {
  const fc = el("div", "cf st-" + f.status);
  const t = el("div", "cf-top");
  const agree = (f.notes || []).filter((n) => n.stance === "agree").length;
  const against = (f.notes || []).length - agree;
  t.append(el("b", "", f.id), el("span", "chip sev-" + f.severity, SEV_TR[f.severity] || f.severity),
    el("span", "cf-title", f.title));
  const meta = el("small", "", [f.area ? f.area + (f.agent && f.agent !== "genel" ? " · " + f.agent : "") : "", ROLE_TR[f.by] || f.by, FST_TR[f.status] || f.status,
    agree ? _("{n} agree", { n: agree }) : "", against ? `${against} itiraz` : ""].filter(Boolean).join(" · "));
  t.appendChild(meta);
  fc.appendChild(t);
  const more = el("div", "cf-more");
  if (f.evidence) more.appendChild(el("div", "cf-ev", f.evidence));
  if (f.fix) more.appendChild(el("div", "cf-fix", _("Suggestion: ") + f.fix));
  if (f.check) more.appendChild(el("div", "cf-chk", _("Checker: ") + f.check));
  for (const n of f.notes || []) more.appendChild(el("div", "cf-note", _("{who} {stance}: {note}", { who: ROLE_TR[n.by] || n.by, stance: n.stance === "agree" ? _("agrees") : _("objects"), note: n.note })));
  if (more.childNodes.length) {
    fc.appendChild(more);
    t.addEventListener("click", () => fc.classList.toggle("open"));
    fc.classList.add("has-more");
  }
  return fc;
}
function councilCard(c, target, latest) {
  const res = c.result || {};
  const running = c.state === "running";
  // Eski toplantılar tek satır başlık olarak kapalı durur; en son ve süren açık.
  const card = el("details", "cc" + (running ? " live" : ""));
  card.open = !!(latest || running);
  card.dataset.k = "cc" + (c.id || c.started || "") + target;
  const head = el("summary", "cc-head");
  head.append(el("b", "", KIND_TR[c.kind] || _("Council")));
  if (running) head.appendChild(el("span", "pill running", c.rounds ? _("meeting · round {round}/{rounds}", { round: c.round || 1, rounds: c.rounds }) : _("meeting")));
  else head.appendChild(el("span", "pill dec-" + (res.decision || "contested"), DEC_TR[res.decision] || _("could not finish")));
  if (c.started) head.appendChild(el("small", "cc-when", new Date(c.started * 1000).toLocaleTimeString(ALICE_LOCALE, { hour: "2-digit", minute: "2-digit" })));
  card.appendChild(head);
  if (res.error) card.appendChild(el("div", "cc-err", _("The council could not finish.")));

  // İlk bakışta sadece özet: oyların tek cümlesi ve düzeltilecek birkaç madde. Gerisi _("Details")da.
  const vs = (c.votes || []).filter((v) => v.last);
  if (running) card.appendChild(councilProgress(c));
  else if (vs.length) {
    const cnt = {};
    for (const v of vs) cnt[v.last] = (cnt[v.last] || 0) + 1;
    const parts = ["reject", "conditional", "approve"].filter((k) => cnt[k]).map((k) => `${cnt[k]} ${VOTE_TR2[k]}`);
    card.appendChild(el("div", "cc-sum", _("{n} members voted: {votes}.", { n: vs.length, votes: parts.join(", ") })));
  }
  if (!running && (res.veto || []).length) {
    // Oylar onay yönündeyken karar "düzeltme" olabiliyor: doğrulanmış kritik bulgu veto eder. Nedenini söyle.
    const byId = Object.fromEntries((c.findings || []).map((f) => [f.id, f]));
    const names = res.veto.map((id) => (byId[id] ? byId[id].title : id)).slice(0, 2).join("; ");
    card.appendChild(el("div", "cc-sum veto", _("A verified critical finding turned the decision into a fix: {names}", { names: names })));
  }
  if (!running && c.rubric && Object.keys(c.rubric).length) {
    // Plan kurulu: ölçüt başına ortalama puan (1-5)
    const rb = el("div", "cc-rubric");
    for (const [k, v] of Object.entries(c.rubric)) {
      const chip = el("span", "rb " + (v >= 4 ? "ok" : v >= 3 ? "mid" : "low"));
      chip.append(el("span", "", k), el("b", "", String(v).replace(".", ",")));
      rb.appendChild(chip);
    }
    card.appendChild(rb);
  }
  if (!running) {
    const sevRank = { critical: 0, major: 1, minor: 2 };
    const todo = [...new Set([...(res.conditions || []),
      ...(c.findings || []).filter((f) => f.status === "confirmed" && f.severity !== "minor")
        .sort((a, b) => sevRank[a.severity] - sevRank[b.severity]).map((f) => f.title)])];
    if (c.kind === "plan" && res.decision === "approve_conditions")
      card.appendChild(el("div", "cc-sum", _("Plan approved; the suggestions were added as notes to the relevant agents' tasks.")));
    else if (todo.length && res.decision !== "approve") {
      const ul = el("ul", "cc-todo");
      for (const x of todo.slice(0, 4)) ul.appendChild(el("li", "", x));
      if (todo.length > 4) ul.appendChild(el("li", "more", `ve ${todo.length - 4} madde daha`));
      card.appendChild(ul);
    } else if (res.decision === "approve") card.appendChild(el("div", "cc-sum ok", _("No blocking problems.")));
  }
  if (latest && !running) {
    const row = el("div", "cc-uv");
    row.appendChild(el("span", "", c.user_vote ? _("Your vote: {vote}. Change it:", { vote: VOTE_TR2[c.user_vote.vote] }) : _("Your vote:")));
    for (const [v, l] of [["approve", _("Approve")], ["conditional", _("Conditional")], ["reject", _("Reject")]]) {
      const b = el("button", "btn ghost" + (c.user_vote && c.user_vote.vote === v ? " on" : ""), l);
      b.addEventListener("click", () => projectAction("vote", target, v, {}));
      row.appendChild(b);
    }
    card.appendChild(row);
  }
  const more = el("details", "cc-more");
  more.dataset.k = "ccm" + (c.id || c.started || "") + target;
  more.appendChild(el("summary", "", _("Details")));
  card.appendChild(more);
  const card0 = card;
  {
  const card = more;
  if (c.title) card.appendChild(el("div", "cc-title", c.title));
  if (res.score !== undefined && !running) card.appendChild(el("div", "cc-title", _("Weighted approval {pct}%", { pct: Math.round((res.score || 0) * 100) }) +
    ((res.veto || []).length ? ` · veto eden bulgular: ${res.veto.join(", ")}` : "") + (c.critical ? _(" · critical work, the strongest models") : "")));
  if (res.error) card.appendChild(el("div", "cc-err", res.error));
  if ((c.screens || []).length) card.appendChild(el("div", "cc-title", _("The user's-eye expert also looked at the site's {screens} screenshots.", { screens: c.screens.join(_(" and ")) })));

  // oylar: rol başına küçük kutu; gerekçe iki satır, tıklayınca tamamı
  const votes = el("div", "cc-votes");
  const voteBox = (name, model, v, weight, reason, off) => {
    const box = el("div", "cv v-" + (v || "none") + (off ? " off" : ""));
    const top = el("div", "cv-top");
    top.append(el("b", "", name), el("span", "vote v-" + (v || "none"), v ? VOTE_TR2[v] : _("no vote")));
    if (weight > 1) top.appendChild(el("span", "cv-w", `×${weight}`));
    box.appendChild(top);
    if (model) box.title = model;
    if (off) box.appendChild(el("div", "cv-off", _("not counted: ") + off));
    if (reason) {
      const r = el("div", "cv-r", reason);
      r.title = _("Show all");
      r.addEventListener("click", () => r.classList.toggle("open"));
      box.appendChild(r);
    }
    return box;
  };
  for (const v of c.votes || []) {
    const changed = v.first && v.last && v.first !== v.last;
    const box = voteBox(ROLE_TR[v.role] || v.role, v.model || "", v.last, v.weight, v.reason, v.counted === false ? v.note : "");
    if (changed) box.querySelector(".vote").textContent = `${VOTE_TR2[v.first]} → ${VOTE_TR2[v.last]}`;
    votes.appendChild(box);
  }
  if (c.user_vote) votes.appendChild(voteBox("sen", "", c.user_vote.vote, 3, c.user_vote.note, ""));
  if (votes.childNodes.length) card.appendChild(votes);

  // bulgular: doğrulanan ve belirsizler görünür, çürütülenler kapalı; ayrıntı tıklayınca
  const order = { confirmed: 0, unclear: 1, pending: 2, refuted: 3 };
  const sev = { critical: 0, major: 1, minor: 2 };
  const fs = (c.findings || []).slice().sort((a, b) => (order[a.status] - order[b.status]) || (sev[a.severity] - sev[b.severity]) || a.id.localeCompare(b.id, undefined, { numeric: true }));
  const live = fs.filter((f) => f.status !== "refuted"), dead = fs.filter((f) => f.status === "refuted");
  if (fs.length) {
    const n = (st) => fs.filter((f) => f.status === st).length;
    card.appendChild(el("div", "cc-sub", [_("Findings"), n("confirmed") ? _("{n} verified", { n: n("confirmed") }) : "", n("unclear") ? `${n("unclear")} belirsiz` : "",
      dead.length ? _("{n} refuted", { n: dead.length }) : ""].filter(Boolean).join(" · ")));
    const box = el("div", "cc-findings");
    for (const f of live) box.appendChild(findingRow(f));
    if (dead.length) {
      const d = el("details", "cc-dead");
      d.dataset.k = "ccd" + (c.id || c.started || "");
      d.appendChild(el("summary", "", _("Refuted findings ({n})", { n: dead.length })));
      for (const f of dead) d.appendChild(findingRow(f));
      box.appendChild(d);
    }
    card.appendChild(box);
  } else if (!running) card.appendChild(el("div", "cc-title", _("No findings.")));
  // tartışma dökümü: tur tur, her rol kapalı; JSON kararlar zaten yukarıda
  const tr = (c.transcript || []).map((t) => ({ ...t, text: councilProse(t.text) })).filter((t) => t.text);
  if (tr.length) {
    const dt = el("details", "cc-tr");
    dt.dataset.k = "cct" + (c.id || c.started || "") + target;
    dt.appendChild(el("summary", "", _("Debate transcript ({n} turns)", { n: tr.length })));
    let round = null;
    for (const t of tr) {
      if (t.round !== round) { round = t.round; dt.appendChild(el("div", "cc-tr-round", round === 1 ? _("Round 1 · independent review") : `Tur ${round}`)); }
      const it = el("details", "cc-tr-it");
      const sm = el("summary");
      sm.append(el("b", "", ROLE_TR[t.role] || t.role), el("span", "", t.text.replace(/[#*_`>]/g, "").split("\n").find((x) => x.trim()) || ""));
      const pz = el("div", "prose");
      setMd(pz, t.text);
      it.append(sm, pz);
      dt.appendChild(it);
    }
    card.appendChild(dt);
  }
  }
  return card0;
}
// ---- konuşmayı insanca göster: iç olaylar küçük notlara, araç çağrıları tek açılır satıra dönüşür.
function rawView() { try { return localStorage.getItem("alice.rawThreads") === "1"; } catch { return false; } }
const HIDDEN_TOOLS = new Set(["ask_user", "finish", "project_status"]);  // içerikleri soru kartında ve özette zaten var
const TOOL_ING = { write_file: _("writing a file"), edit_file: _("editing"), multi_edit: _("editing"), apply_patch: _("preparing a patch"),
  run_command: _("preparing a command"), team_send: _("writing a message"), message: _("writing a message"), assign: _("setting up an agent"), plan: _("preparing the plan"),
  finish: _("writing the summary"), todo_write: _("writing the plan") };
const TOOL_TR = {
  read_file: [_("read"), _("read a file")], list_dir: [_("looked at a folder"), _("looked at a folder")], glob: [_("searched files"), _("searched files")],
  grep: [_("searched the code"), _("searched the code")], write_file: [_("wrote a file"), _("wrote a file")], edit_file: [_("edited"), _("made an edit")],
  multi_edit: [_("edited"), _("made an edit")], apply_patch: [_("applied a patch"), _("applied a patch")], file_ops: [_("file operation"), _("did a file operation")],
  run_command: [_("ran a command"), _("ran a command")], process_output: [_("looked at output"), _("looked at output")],
  web_search: [_("searched the web"), _("searched the web")], web_fetch: [_("read a page"), _("read a page")], http_request: [_("sent a request"), _("sent a request")],
  git: ["git", _("used git")], todo_write: [_("updated the plan"), _("updated the plan")], team_send: [_("sent a message"), _("sent a message")],
  team_wait: [_("waited for an answer"), _("waited for an answer")], message: [_("sent a message"), _("sent a message")], assign: [_("set up an agent"), _("set up an agent")],
  plan: [_("presented the plan"), _("presented the plan")], review: [_("reviewed"), _("decided")], diff: [_("looked at the diff"), _("looked at the diff")],
  council: [_("asked the council"), _("asked the council")],
};
function toolLine(it) {
  const [verb] = TOOL_TR[it.name] || [it.name];
  const sum = it.summary || "";
  if (it.name === "assign") return _("set up an agent: {summary}", { summary: sum });
  if (it.name === "plan") return _("set up the plan ({plan})", { plan: sum.replace(/^plan:\s*/, "") });
  if (it.name === "review") return sum.includes("approve") ? _("approved {name}'s work", { name: sum.split(":")[0] }) : _("sent {name}'s work back for changes", { name: sum.split(":")[0] });
  if (it.name === "message" || it.name === "team_send") { const i = sum.indexOf(":"); return _("wrote to {name}: {text}", { name: sum.slice(0, i), text: sum.slice(i + 1).trim() }); }
  if (it.name === "team_wait") return _("waited for an answer");
  if (it.name === "run_command") return _("ran a command");
  return sum ? `${verb}: ${sum}` : verb;
}
function friendlyLast(last) {
  const name = last.split(" ")[0];
  if (HIDDEN_TOOLS.has(name)) return _("working…");
  return toolLine({ name, summary: last.slice(name.length + 1) });
}
// Yöneticiye giden olay metinleri (talimat içerir) kısa notlara çevrilir.
const EVENT_RULES = [
  [/^Proje başladı ve henüz ajan yok/, () => _("The project started; Alice is planning the work.")],
  [/^Proje başladı; ajanlar çalışıyor/, () => _("The project started; the agents are working.")],
  [/^(\S+) bitirdi, inceleme bekliyor/, (m) => _("{name} finished its work; it's being reviewed.", { name: m[1] })],
  [/^(\S+) bitirdi \(inceleme yok\)/, (m) => _("{name} finished its work.", { name: m[1] })],
  [/^(\S+) hata verdi/, (m) => _("{name} failed.", { name: m[1] })],
  [/^(\S+) (\d+) dakikadır ilerlemiyor/, (m) => _("{name} hasn't progressed for {m} minutes.", { name: m[1], m: m[2] })],
  [/^Kullanıcı (\S+) ajanının işini onayladı/, (m) => _("You approved {name}'s work.", { name: m[1] })],
  [/^Kullanıcı projeyi sürdürdü/, () => _("You resumed the project.")],
  [/^Bütün ajanlar bitti/, () => _("All agents are done; Alice is verifying the result.")],
  [/^Final kurulu|^Final kurulu projeyi onayladı/, () => _("The final council met.")],
  [/Final kurulu: (\S+)/, (m) => _("Final council: {decision}.", { decision: m[1].toLowerCase() })],
  [/^Kurul (\S+) işinde ikiye bölündü/, (m) => _("The council is split on {name}'s work; Alice decides.", { name: m[1] })],
  [/^Kurul (\S+) işini iki düzeltme/, (m) => _("The council still doesn't accept {name}'s work; Alice decides.", { name: m[1] })],
  [/^İnceleme bekleyen ajanlar var/, () => _("Alice was reminded to review.")],
  [/^Henüz ajan kurmadın/, () => _("Alice was reminded to present the plan.")],
  [/proje kapanmadı/, () => _("Alice was reminded to finish the project.")],
  [/^Kullanıcı kurula sordu/, () => _("You asked the council; the result went to Alice.")],
  [/^Kullanıcı kurulda oy verdi/, () => _("You voted in the council.")],
  [/^Kurul çalışamadı/, () => _("The council couldn't meet; Alice is reviewing itself.")],
];
function eventNotes(body) {
  // "[olaylar]" gövdesi: sistem olayları ve "[kimden -> kime] mesaj" satırları, boş satırla ayrılmış
  const out = [];
  for (const part of body.split(/\n\n(?=\S)/)) {
    const msg = part.match(/^\[([^\]]+?) -> ([^\]]+?)\] ([\s\S]*)$/);
    if (msg) { out.push({ kind: "msg", from: msg[1], to: msg[2], text: msg[3] }); continue; }
    const rule = EVENT_RULES.find(([rx]) => rx.test(part));
    if (rule) out.push({ kind: "note", text: rule[1](part.match(rule[0])) });
    else if (/^(Raporu|Değişen dosyalar|Dal:|İncele:|Kurula sor|İş kurulu|Kontrol turu|-|\(|Bulgular|Şartlar)/.test(part)) continue;
    else out.push({ kind: "note", text: part.split("\n")[0].slice(0, 160) });
  }
  return out;
}
function msgCard(from, to, text) {
  if (from === "sistem") return el("div", "pf-note", text.split("\n")[0].replace(/^takıma yeni ajan katıldı: ([^:]+):.*/, _("$1 joined the team")).slice(0, 160));
  const c = el("div", "pf-msg" + (from === "kullanıcı" ? " me" : ""));
  c.append(el("b", "", from === "kullanıcı" ? _("You") : from === "alice" ? "Alice" : from), el("span", "", to && to !== "alice" ? ` → ${to}` : ""));
  // Soruya verilen cevaplar yöneticiye "Sorduğun soruya cevap (soru): cevap" diye gider; kullanıcıya sadece cevap.
  if (from === "kullanıcı") text = text.replace(/^Sorduğun soruya cevap \([\s\S]*?\): /, "");
  c.appendChild(clamp(text, 6, "pf-msg-t"));
  return c;
}
// Proje konuşması normal sohbetle aynı görünür: balonlar, araç satırları (okumalar gruplanır), terminal ve
// fark kutuları. İç olaylar kısa notlara, takım mesajları kartlara dönüşür.
function chatBubble(text) {
  const wrap = el("div", "turn-user");
  const b = el("div", "bubble");
  b.textContent = text;
  wrap.appendChild(b);
  return wrap;
}
function chatAi(text) {
  const root = el("div", "turn-ai final");
  const prose = el("div", "prose");
  setMd(prose, text);
  root.appendChild(prose);
  aiActions(root, text, "");
  return root;
}
function buildFeed(history, isLead) {
  if (rawView()) return history.map(feedItem);
  const out = [];
  let group = null;
  const tool = (it) => {
    if (it.name === "todo_write" && !it.is_error) { out.push(renderTodo(it.text || "")); return; }
    const t = makeTool(it.name, it.summary, false);
    finishTool(t, { is_error: it.is_error, text: it.text }, false);
    const last = out[out.length - 1];
    if (GROUP_KIND[it.name] && last) {
      if (last === group) { group._body.appendChild(t); updateGroup(group); return; }
      if (last.classList && last.classList.contains("tool") && GROUP_KIND[last._name]) {
        group = makeGroup();
        group._body.append(last, t);
        out[out.length - 1] = group;
        updateGroup(group);
        return;
      }
    }
    out.push(t);
  };
  const note = (text) => out.push(el("div", "note", text));
  const msg = (from, to, text) => out.push(from === "kullanıcı"
    ? chatBubble(text.replace(/^Sorduğun soruya cevap \([\s\S]*?\): /, "")) : msgCard(from, to, text));
  for (const it of history) {
    if (it.type === "tool") { if (!HIDDEN_TOOLS.has(it.name)) tool(it); continue; }
    if (it.type === "assistant") { if ((it.text || "").trim()) out.push(chatAi(it.text)); continue; }
    if (it.type === "info") { note(it.text); continue; }
    const txt = it.text || "";
    if (txt === "Görevine başla.") { note(_("Task started")); continue; }
    if (txt.startsWith("[olaylar]")) {
      const body = txt.replace(/^\[olaylar\]\n/, "").replace(/\n\n\[durum\]\n[\s\S]*$/, "");
      for (const n of eventNotes(body)) n.kind === "msg" ? msg(n.from, n.to, n.text) : note(n.text);
      continue;
    }
    if (txt.startsWith("[takım mesaj")) {
      const body = txt.replace(/^\[takım mesajı\]\n|^\[takım mesajları\]\n/, "").replace(/\n\nBunlara göre işine devam et[\s\S]*$/, "");
      for (const line of body.split(/\n(?=\[)/)) {
        const m = line.match(/^\[([^\]]+?) -> ([^\]]+?)\] ([\s\S]*)$/);
        if (m) msg(m[1], m[2], m[3]); else note(line.slice(0, 200));
      }
      continue;
    }
    if (txt.startsWith("Devam et.")) { note(_("Continuing")); continue; }
    out.push(chatBubble(txt));
  }
  return out;
}
function feedItem(it) {
  if (it.type === "user") {
    const u = el("div", "pf-user");
    const txt = it.text || "";
    const label = txt.startsWith("[olaylar]") ? _("events") : txt.startsWith("[takım mesaj") ? _("team message") : txt === "Görevine başla." ? _("start") : "";
    if (label) u.appendChild(el("small", "", label));
    // Yöneticiye giden olaylardaki "[durum]" tablosu zaten solda görünüyor; konuşmada gizle.
    u.appendChild(document.createTextNode(txt.replace(/^\[(olaylar|takım mesajı|takım mesajları)\]\n/, "").replace(/\n\n\[durum\]\n[\s\S]*$/, "")));
    return u;
  }
  if (it.type === "assistant") {
    const a = el("div", "pf-ai");
    const prose = el("div", "prose");
    setMd(prose, it.text);
    a.appendChild(prose);
    return a;
  }
  if (it.type === "tool") {
    const t = el("div", "pf-tool" + (it.is_error ? " err" : ""));
    t.append(el("b", "", it.name), document.createTextNode(" " + (it.summary || "")));
    t.appendChild(el("pre", "", (it.text || "").slice(0, 4000)));
    t.addEventListener("click", () => t.classList.toggle("open"));
    return t;
  }
  return el("div", "pf-tool", it.text || "");
}
// Uzun metin: birkaç satırla sınırlı, tıklayınca tamamı.
function clamp(text, lines, cls) {
  const t = el("div", "clamp" + (cls ? " " + cls : ""));
  t.style.setProperty("--lines", lines);
  setMd(t, text);
  t.addEventListener("click", (e) => { if (!e.target.closest("a")) t.classList.toggle("open"); });
  return t;
}
function viewHead(icon, title, sub) {
  const h = el("div", "pd-head");
  const r = el("div", "pd-row");
  const nm = el("div", "pd-name");
  nm.append(el("b", "", title), el("small", "", sub));
  r.append(avatar("", "", icon), nm);
  h.appendChild(r);
  return h;
}
function renderTeamLog(det, d) {
  const feed = el("div", "pd-feed chat log");
  let day = "";
  for (const x of d.log) {
    const dt = new Date(x.at * 1000);
    const dd = dt.toLocaleDateString(ALICE_LOCALE, { day: "numeric", month: "long" });
    if (dd !== day) { day = dd; feed.appendChild(el("div", "tl-day", dd)); }
    if (x.from === "sistem") { feed.appendChild(el("div", "pf-note", x.text.split("\n")[0].replace(/^takıma yeni ajan katıldı: ([^:]+):.*/, _("$1 joined the team")).slice(0, 160))); continue; }
    const me = x.from === "kullanıcı";
    const row = el("div", "lg" + (me ? " me" : ""));
    const who = me ? _("You") : x.from === "alice" ? "Alice" : x.from === "kurul" ? _("Council") : x.from;
    if (!me) row.appendChild(avatar(x.from === "kurul" ? "" : x.from, "", x.from === "kurul" ? IC.shield : null));
    const body = el("div", "lg-b");
    const top = el("div", "lg-top");
    top.append(el("b", "", who), el("span", "", x.to === "all" ? "→ herkes" : x.to === "kullanıcı" ? "→ sana" : `→ ${x.to === "alice" ? "Alice" : x.to}`),
      el("small", "", dt.toLocaleTimeString(ALICE_LOCALE, { hour: "2-digit", minute: "2-digit" })));
    body.append(top, clamp(x.text, 6, "lg-t"));
    row.appendChild(body);
    feed.appendChild(row);
  }
  if (!d.log.length) feed.appendChild(el("div", "pd-empty", _("No messages yet")));
  const say = el("div", "pd-say");
  const sel = el("select"); sel.dataset.k = "logto";
  for (const [v, l] of [["alice", _("Alice (lead)")], ["all", _("everyone")], ...d.members.map((m) => [m.name, m.name])]) {
    const o = el("option", "", l); o.value = v; sel.appendChild(o);
  }
  const ta = el("textarea"); ta.rows = 1; ta.placeholder = _("Write to the team…"); ta.dataset.k = "logsay";
  const go = el("button", "btn primary", _("Send"));
  const send = async () => { const v = ta.value.trim(); if (!v) return; ta.value = ""; await projectAction("send", sel.value, v); };
  go.addEventListener("click", send);
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
  say.append(sel, ta, go);
  det.append(viewHead(IC.chat, _("Team chat"), _("What the agents wrote to each other, to Alice and to you")), feed, say);
}
async function projectAction(action, name, text, agent) {
  if (!S.proj) return false;
  const r = await api("/api/project/action", { id: S.proj.id, action, name, text: text || "", agent });
  if (!r.ok) { toast(r.error || _("Didn't work")); return false; }
  if (r.note && !["send", "answer"].includes(action)) toast(r.note);
  if (action !== "delete") refreshProject();
  return true;
}

// ---- yeni proje / ajan ekle penceresi
const THINK_OPTS = [["default", _("Default")], ["off", _("No thinking")], ["low", _("Low")], ["medium", _("Medium")], ["high", _("High")]];
const PLACE_OPTS = [["shared", _("Same folder")], ["worktree", _("Separate git branch")], ["readonly", _("Read-only")]];
// A template adds a "done when" line under the goal; the first line (the project title) stays the user's own sentence.
const TEMPLATES = [
  [_("Add a feature"), _("Feature to add: "), _("Done when: the feature works, and its tests are written and pass.")],
  [_("Fix a bug"), _("Bug to fix: "), _("Done when: the root cause is found and fixed, and a test that prevents a repeat is added.")],
  [_("Code review"), _("Review the project"), _("Report bugs, security holes and risky spots in order of priority. Don't change code.")],
  [_("Refactor"), _("Part to refactor: "), _("Done when: the behavior is the same and all tests pass.")],
  [_("Write tests"), _("Write tests for the parts that lack them"), _("Done when: the important paths are tested and the bugs found are reported.")],
];
function field(label, input, hint) {
  const f = el("label", "fld");
  f.appendChild(el("span", "fl", label));
  f.appendChild(input);
  if (hint) f.appendChild(el("span", "fh", hint));
  return f;
}
function sel(opts, val) {
  const s = el("select");
  for (const [v, l] of opts) { const o = el("option", "", l); o.value = v; s.appendChild(o); }
  s.value = val;
  return s;
}
function txt(val, ph, list) {
  const i = el("input"); i.type = "text"; i.value = val || ""; if (ph) i.placeholder = ph; if (list) i.setAttribute("list", list);
  return i;
}
function agentCard(spec, n) {
  spec = spec || {};
  const box = el("div", "np-agent");
  const top = el("div", "np-row");
  const nm = txt(spec.name || "", "ad"); nm.className = "nm";
  const model = txt(spec.model || "auto", _("auto: the best model for the job"), "npModels");
  const th = sel(THINK_OPTS, spec.thinking || "default");
  const iso = sel(PLACE_OPTS, spec.isolation || "shared");
  top.append(el("span", "np-n", String(n || "")), field("Ad", nm), field("Model", model), field(_("Thinking level"), th), field(_("Workplace"), iso));
  const x = el("button", "act"); x.title = _("Remove"); x.appendChild(svg(IC.x));
  x.addEventListener("click", () => box.remove());
  top.appendChild(x);
  const task = el("textarea"); task.placeholder = _("What will it do, in which files, and how will it verify?"); task.value = spec.task || "";
  const adv = el("details", "np-adv");
  adv.appendChild(el("summary", "", _("Scope, limits, order")));
  const sc = txt((spec.scope || []).join(", "), _("e.g. src/api, tests (empty: anywhere)"));
  const cn = txt(spec.constraints || "", _("e.g. don't touch the database schema"));
  const af = txt((spec.after || []).join(", "), _("e.g. backend"));
  const g = el("div", "np-grid");
  g.append(field(_("Where it may write"), sc), field(_("Limits"), cn), field(_("Start after these finish"), af));
  adv.appendChild(g);
  box.append(top, field(_("Task"), task), adv);
  box.read = () => ({ name: nm.value.trim().toLowerCase(), model: model.value.trim() || "auto", thinking: th.value, isolation: iso.value,
    task: task.value.trim(), constraints: cn.value.trim(),
    scope: sc.value.split(",").map((s) => s.trim()).filter(Boolean), after: af.value.split(",").map((s) => s.trim().toLowerCase()).filter(Boolean) });
  return box;
}
function projectDialog(project) {
  showDialog((d) => {
    d.classList.add("wide", "np");
    const adding = !!project;
    const dl = el("datalist"); dl.id = "npModels";
    for (const v of ["auto", "brain", "fast", ...((S.status.brain_chain || []).slice(0, 8))]) { const o = el("option"); o.value = v; dl.appendChild(o); }
    d.appendChild(dl);
    const err = el("div", "note err");
    if (adding) {
      d.append(el("h2", "", _("Add an agent")), el("p", "", _("Joins the \"{project}\" project and starts right away; the others are told.", { project: project.title })));
      const card = agentCard({}, 0);
      d.append(card, err);
      const row = el("div", "row");
      const no = el("button", "btn", _("Cancel")); no.addEventListener("click", closeDialog);
      const ok = el("button", "btn accent", _("Add and start"));
      ok.addEventListener("click", async () => { if (await projectAction("add", "", "", card.read())) closeDialog(); });
      row.append(no, ok);
      d.appendChild(row);
      setTimeout(() => card.querySelector("input").focus(), 0);
      return;
    }
    d.append(el("h2", "", _("New project")),
      el("p", "", _("Alice splits the work between agents, answers their questions, reviews each finished job and writes you a summary at the end. You follow along on the Projects page and step in whenever you like.")));
    const goal = el("textarea", "np-goal");
    goal.placeholder = _("What needs doing? Also write what counts as done and anything that should be known.");
    const chips = el("div", "np-chips");
    for (const [label, first, crit] of TEMPLATES) {
      const c = el("button", "chip-btn", label);
      c.addEventListener("click", () => {
        const head = goal.value.split("\n\n")[0].trim() || first;
        goal.value = head + "\n\n" + crit;
        goal.focus();
        goal.setSelectionRange(head.length, head.length);
      });
      chips.appendChild(c);
    }
    d.append(field(_("Goal"), goal), chips);
    const mine = el("div", "np-chips mine");
    d.appendChild(mine);

    const seg = el("div", "seg");
    const m1 = el("button", "on", _("Let Alice plan")), m2 = el("button", "", _("I'll define the agents"));
    m1.title = _("Alice studies the code and picks the agents, their models and order itself (recommended)");
    seg.append(m1, m2);
    d.appendChild(seg);
    const manual = el("div", "np-manual"); manual.hidden = true;
    const list = el("div", "np-agents");
    const more = el("button", "btn", _("+ Add an agent"));
    const renum = () => [...list.children].forEach((c, i) => { c.querySelector(".np-n").textContent = i + 1; });
    more.addEventListener("click", () => { if (list.children.length < 8) { list.appendChild(agentCard({}, list.children.length + 1)); renum(); } });
    manual.append(list, more);
    d.appendChild(manual);
    const setMode = (man) => {
      m1.classList.toggle("on", !man); m2.classList.toggle("on", man); manual.hidden = !man;
      if (man && !list.children.length) list.appendChild(agentCard({ name: "backend" }, 1));
    };
    m1.addEventListener("click", () => setMode(false));
    m2.addEventListener("click", () => setMode(true));

    const opts = el("details", "np-opts");
    opts.appendChild(el("summary", "", _("Settings")));
    const g = el("div", "np-grid");
    const cwd = txt(S.status.cwd || "", _("working folder"));
    const lm = txt("auto", _("auto: the best model for the job"), "npModels");
    const lt = sel(THINK_OPTS, "default");
    const ck = txt("", _("e.g. python -m pytest -q"));
    const guess = async () => {
      const r = await api(`/api/project/check_guess?cwd=${encodeURIComponent(cwd.value.trim())}`);
      ck.placeholder = r && r.check ? _("suggestion: {command} (nothing runs if you leave it empty)", { command: r.check }) : _("e.g. python -m pytest -q");
      ck.dataset.guess = (r && r.check) || "";
    };
    cwd.addEventListener("change", guess);
    guess();
    ck.addEventListener("focus", () => { if (!ck.value && ck.dataset.guess) ck.value = ck.dataset.guess; });
    const rv = el("input"); rv.type = "checkbox"; rv.checked = true;
    const rvl = el("label", "chk"); rvl.append(rv, document.createTextNode(_(" Alice reviews finished work (approves it or sends it back)")));
    const kc = el("input"); kc.type = "checkbox"; kc.checked = true;
    const kcl = el("label", "chk"); kcl.append(kc, document.createTextNode(_(" Review council: plan, each job and final; critic, advocate, checker and experts debate and vote")));
    const pc = el("input"); pc.type = "checkbox"; pc.checked = true;
    const pcl = el("label", "chk sub"); pcl.append(pc, document.createTextNode(_(" Send the plan to the council too (when off, Alice's plan starts directly)")));
    rv.addEventListener("change", () => { kc.disabled = !rv.checked; if (!rv.checked) kc.checked = false; pc.disabled = !kc.checked; });
    kc.addEventListener("change", () => { pc.disabled = !kc.checked; });
    const pr = el("input"); pr.type = "checkbox";
    const prl = el("label", "chk"); prl.append(pr, document.createTextNode(_(" Make the result a PR: the project works on its own branch, and a PR is opened and watched when done (your folder isn't touched)")));
    g.append(field(_("Folder"), cwd), field(_("Lead model"), lm), field(_("Lead thinking"), lt));
    const ckf = field(_("Check command"), ck, _("Tests/lint: runs automatically before every council; its output becomes evidence."));
    ckf.classList.add("wide");
    g.appendChild(ckf);
    opts.append(g, rvl, kcl, pcl, prl);
    d.append(opts, err);
    const row = el("div", "row");
    const no = el("button", "btn", _("Cancel")); no.addEventListener("click", closeDialog);
    const ok = el("button", "btn accent", _("Start"));
    ok.addEventListener("click", async () => {
      const g2 = goal.value.trim();
      if (!g2) { err.textContent = _("Write the goal"); goal.focus(); return; }
      const agents = manual.hidden ? [] : [...list.children].map((b) => b.read()).filter((a) => a.name || a.task);
      if (!manual.hidden && !agents.length) { err.textContent = _("Add at least one agent or leave the plan to Alice"); return; }
      ok.disabled = true;
      const r = await api("/api/project/create", { goal: g2, agents, cwd: cwd.value.trim(), lead_model: lm.value.trim() || "auto",
        lead_thinking: lt.value, review: rv.checked, council: kc.checked, plan_council: pc.checked, check: ck.value.trim(), pr: pr.checked });
      ok.disabled = false;
      if (!r.ok) { err.textContent = r.error || _("Could not start"); return; }
      closeDialog();
      await loadProjects();
      openProject(r.id);
    });
    row.append(no, ok);
    d.appendChild(row);
    setTimeout(() => goal.focus(), 0);
    // Şablonlarım: kayıtlı projeden hedef, ayarlar ve ajanlar tek tıkla gelir
    const fill = (t) => {
      goal.value = t.goal || "";
      lm.value = t.lead_model || "auto"; lt.value = t.lead_thinking || "default";
      rv.checked = t.review !== false; kc.checked = t.council !== false; pc.checked = t.plan_council !== false; pc.disabled = !kc.checked; ck.value = t.check || "";
      pr.checked = !!t.pr; if (t.cwd) cwd.value = t.cwd;
      list.replaceChildren();
      (t.agents || []).forEach((a, i) => list.appendChild(agentCard(a, i + 1)));
      setMode((t.agents || []).length > 0);
      opts.open = true;
    };
    api("/api/project/templates").then((rows) => {
      if (!Array.isArray(rows) || !rows.length) return;
      mine.appendChild(el("span", "np-mine-l", _("My templates:")));
      for (const t of rows) {
        const c = el("span", "chip-btn tpl");
        const b = el("button", "", t.name);
        b.title = `${(t.agents || []).length} ajan · ${(t.goal || "").slice(0, 120)}`;
        b.addEventListener("click", () => fill(t));
        const x = el("button", "x", "×"); x.title = _("Delete the template");
        x.addEventListener("click", async () => { await api("/api/project/template/delete", { name: t.name }); c.remove(); });
        c.append(b, x);
        mine.appendChild(c);
      }
    });
  });
}
$("#newProject").addEventListener("click", () => projectDialog(null));
