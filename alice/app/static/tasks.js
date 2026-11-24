/* Tasks: lasting duties ALICE runs in the background, and the approval box (alice/core/tasks.py).
   The app only edits the task database; the background core runs the tasks. Approvals go to the phone too;
   whichever one answers, it disappears from the other. */

const TASK_TRIG = { manual: _("By hand"), schedule: _("On a schedule"), metric: _("Metric"), event: _("Event"), github: "GitHub", folder: _("Folder") };
const TASK_PERM = { read: [_("Read only"), _("Asks before anything that changes things")], act: [_("Works on files"), _("Asks before outgoing and dangerous actions")], full: [_("Full access"), _("Does everything without asking")] };
const TASK_ROLE = { brain: _("Main model"), fast: _("Fast model"), code: _("Code model"), vision: _("Vision model") };
const TASK_LOG = { trigger: _("trigger"), run: _("ran"), result: _("result"), error: _("error"), approval: _("asked for approval"), answer: _("answer"), info: _("info") };
let TASKS = null;

async function loadTasks() {
  const d = await api("/api/tasks");
  if (d && d.tasks) TASKS = d;
  taskBadge();
  return TASKS;
}
function taskBadge() {
  const dot = $("#taskDot");
  if (!dot) return;
  const n = TASKS ? (TASKS.approvals || []).length : 0;
  dot.hidden = !n;
  dot.title = n ? n + " onay bekliyor" : "";
}
// Onay kutusu rozeti: sayfa açık değilken de yeni onayları görmek için ara ara bakılır.
setInterval(() => { if (!document.hidden) api("/api/tasks/approvals").then((r) => {
  if (!r || !r.approvals) return;
  const before = TASKS ? (TASKS.approvals || []).map((a) => a.id).join() : "";
  TASKS = Object.assign(TASKS || { tasks: [] }, { approvals: r.approvals });
  taskBadge();
  if (app.dataset.sec === "tasks" && before !== r.approvals.map((a) => a.id).join()) drawTasks();
}); }, 20000);
setTimeout(loadTasks, 1500);

function taskCall(action, extra) { return api("/api/tasks/action", Object.assign({ action }, extra || {})); }
function taskAfter(msg) {
  return (r) => {
    if (!r || r.ok === false) { toast((r && r.error) || _("didn't work")); return false; }
    if (msg) toast(msg);
    loadTasks().then(drawTasks);
    return true;
  };
}

async function showTasks() {
  if (S.proj) closeProject();
  markSection("tasks");
  $("#pageView").replaceChildren(el("div", "page", "…"));
  await loadTasks();
  drawTasks();
}

function drawTasks() {
  if (app.dataset.sec !== "tasks" || !TASKS) return;
  const d = TASKS;
  const page = el("div", "page tasks-page");
  const head = el("div", "page-head");
  const t = el("div");
  t.append(el("h1", "", _("Tasks")),
    el("p", "", _("Give ALICE a lasting duty: it carries it out on your computer while you're away and asks you when a decision is needed (on the desktop and the phone).")));
  const nb = el("button", "btn primary");
  nb.append(svg(IC.plus), document.createTextNode(_("New task")));
  nb.addEventListener("click", () => taskDialog(null));
  head.append(t, el("span", "grow"), nb);
  page.appendChild(head);
  if (d.core === false) {
    const w = el("div", "list-row warn");
    const l = el("span", "l");
    l.append(el("b", "", _("The core isn't running")), el("small", "", _("Tasks run in the background core; it continues where it left off when it starts.")));
    const go = el("button", "btn", _("Start the core"));
    go.addEventListener("click", () => api("/api/system/action", { action: "start" }).then(() => { toast(_("Starting the core")); setTimeout(showTasks, 3000); }));
    w.append(l, go);
    page.appendChild(w);
  }
  // Onay kutusu
  const inbox = d.approvals || [];
  page.appendChild(el("div", "sec", inbox.length ? `Onay bekleyenler (${inbox.length})` : "Onay kutusu"));
  if (!inbox.length) page.appendChild(el("div", "list-row muted", _("No approvals waiting. When a task needs a decision it shows here and on your phone.")));
  for (const a of inbox) page.appendChild(approvalCard(a));
  // Görevler
  const tasks = d.tasks || [];
  page.appendChild(el("div", "sec", _("Tasks ({n})", { n: tasks.length })));
  if (!tasks.length) {
    page.appendChild(emptyState(IC.list, _("No tasks yet"), _("Start from a template or write what it should do in plain words.")));
    const tl = el("div", "task-tpls");
    for (const tp of d.templates || []) tl.appendChild(templateCard(tp));
    page.appendChild(tl);
  } else {
    const list = el("div", "card-list");
    for (const x of tasks) list.appendChild(taskRow(x));
    page.appendChild(list);
  }
  const done = d.answered || [];
  if (done.length) {
    const det = el("details", "task-done");
    det.appendChild(el("summary", "", `Cevaplananlar (${done.length})`));
    for (const a of done) {
      const r = el("div", "list-row");
      const l = el("span", "l");
      const st = { approved: _("approved"), rejected: _("denied"), replied: _("answered"), running: _("in progress"), done: _("done"), failed: _("failed") }[a.status] || a.status;
      l.append(el("b", "", a.title), el("small", "", [a.task, st, relTime(a.resolved || a.ts)].join(" · ") + (a.result ? " · " + a.result : "")));
      r.appendChild(l);
      det.appendChild(r);
    }
    page.appendChild(det);
  }
  const pv = $("#pageView");
  const keep = pv.scrollTop;
  pv.replaceChildren(page);
  pv.scrollTop = keep;
}

function approvalCard(a) {
  const box = el("div", "task-ask");
  const top = el("div", "ta-top");
  top.append(el("span", "ta-task", a.task || _("Task")), el("span", "ta-time", relTime(a.ts)));
  box.append(top, el("b", "ta-title", a.title));
  if (a.body) box.appendChild(el("div", "ta-body", a.body));
  if (a.action) {
    const det = el("details", "ta-action");
    det.append(el("summary", "", _("Done if you approve")), el("div", "", a.action));
    box.appendChild(det);
  }
  const acts = el("div", "row ta-acts");
  const inp = el("input");
  inp.type = "text";
  inp.placeholder = _("Write an answer or ask for a change…");
  const answer = (decision) => {
    const reply = inp.value.trim();
    if (decision === "reply" && !reply) { inp.focus(); return; }
    box.classList.add("done");
    taskCall("answer", { id: a.id, decision, reply }).then(taskAfter(decision === "approve" ? _("approved, ALICE is doing it") : decision === "reject" ? _("denied") : _("answer sent")));
  };
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") answer("reply"); });
  const ok = el("button", "btn primary", _("Approve"));
  ok.addEventListener("click", () => answer("approve"));
  const no = el("button", "btn", _("Deny"));
  no.addEventListener("click", () => answer("reject"));
  const send = el("button", "btn", _("Send"));
  send.addEventListener("click", () => answer("reply"));
  acts.append(ok, no, inp, send);
  box.appendChild(acts);
  return box;
}

function taskRow(x) {
  const r = el("div", "card-row task-row" + (x.enabled ? "" : " off"));
  const l = el("span", "l");
  const sub = [x.when, x.running ? _("running") : x.last_run ? _("last: {when}", { when: relTime(x.last_run) }) : _("not run yet"), x.enabled ? "" : _("paused"),
    x.pending ? _("{n} waiting for approval", { n: x.pending }) : ""].filter(Boolean).join(" · ");
  l.append(el("b", "", x.name), el("small", "", sub));
  if (x.last_result) l.appendChild(el("small", "task-last" + (x.last_ok === false ? " err" : ""), x.last_result));
  l.addEventListener("click", () => taskLog(x.id));
  l.title = _("Log and memory");
  const run = el("button", "btn", _("Run now"));
  run.addEventListener("click", () => taskCall("run", { id: x.id }).then(taskAfter(TASKS && TASKS.core ? _("runs on the next core tick") : _("runs when the core starts"))));
  r.append(el("i", "dot " + (x.running ? "st-running" : x.enabled ? "st-done" : "st-paused")), l, run,
    switchEl(x.enabled, x.enabled ? "Duraklat" : "Devam ettir", (on) => taskCall("enable", { id: x.id, enabled: on }).then(taskAfter(on ? "devam ediyor" : _("paused")))),
    iconAct(IC.edit, _("Edit"), () => taskDialog(x)),
    iconAct(IC.trash, "Sil", async () => {
      if (await confirmDialog(_("Delete the task?"), _("The \"{name}\" task, its log and its memory will be deleted.", { name: x.name }), "Sil")) taskCall("remove", { id: x.id }).then(taskAfter(_("task deleted")));
    }, "danger"));
  return r;
}

function templateCard(tp) {
  const b = el("button", "task-tpl");
  b.append(el("b", "", tp.name), el("small", "", tp.responsibility));
  b.addEventListener("click", () => taskDialog(Object.assign({}, tp, { id: "" })));
  return b;
}

/* Yeni görev / düzenle. Düz cümle yazılırsa hızlı model formu doldurur; kullanıcı görüp kaydeder. */
function taskDialog(x) {
  const editing = !!(x && x.id);
  const v = Object.assign({ name: "", responsibility: "", trigger: { type: "manual" }, tools: [], permission: "read", role: "brain", notify: true }, x || {});
  v.trigger = Object.assign({}, v.trigger);
  showDialog((d) => {
    d.classList.add("task-dlg", "np");  // np: the project dialog's form styles (inputs, selects, textarea)
    d.appendChild(el("h2", "", editing ? _("Edit the task") : _("New task")));
    if (!editing) {
      const quick = el("div", "key-edit");
      const q = el("input");
      q.type = "text";
      q.placeholder = _("Write it in plain words: \"draft a reply when a new issue comes to my repo\"");
      const go = el("button", "btn", _("Fill in"));
      const fill = async () => {
        if (!q.value.trim()) return;
        go.disabled = true; go.textContent = _("understanding…");
        const r = await taskCall("parse", { text: q.value.trim() });
        go.disabled = false; go.textContent = _("Fill in");
        if (!r || !r.ok) return toast((r && r.error) || _("not understood"));
        Object.assign(v, r.draft);
        draw();
      };
      go.addEventListener("click", fill);
      q.addEventListener("keydown", (e) => { if (e.key === "Enter") fill(); });
      quick.append(q, go);
      d.appendChild(quick);
      const tl = el("div", "task-tpls small");
      for (const tp of (TASKS && TASKS.templates) || []) {
        const b = el("button", "chip", tp.name);
        b.addEventListener("click", () => { Object.assign(v, JSON.parse(JSON.stringify(tp))); draw(); });
        tl.appendChild(b);
      }
      d.appendChild(tl);
    }
    const form = el("div", "task-form");
    d.appendChild(form);
    const row = el("div", "row");
    const cancel = el("button", "btn", _("Cancel"));
    cancel.addEventListener("click", closeDialog);
    const save = el("button", "btn primary", editing ? "Kaydet" : _("Create"));
    save.addEventListener("click", async () => {
      const body = { name: v.name, responsibility: v.responsibility, trigger: v.trigger, tools: v.tools, permission: v.permission, role: v.role, notify: v.notify };
      if (v.template) body.template = v.template;
      const r = await taskCall(editing ? "update" : "create", editing ? Object.assign(body, { id: x.id }) : body);
      if (taskAfter(editing ? "kaydedildi" : _("task created"))(r)) closeDialog();
    });
    row.append(cancel, save);
    d.appendChild(row);

    function draw() {
      form.replaceChildren();
      const name = txt(v.name, _("A short name"));
      name.addEventListener("input", () => { v.name = name.value; });
      const resp = el("textarea");
      resp.rows = 4;
      resp.value = v.responsibility;
      resp.placeholder = _("What should it do? What should it watch, and when should it ask you?");
      resp.addEventListener("input", () => { v.responsibility = resp.value; });
      form.append(field("Ad", name), field(_("Duty"), resp, _("In plain words; ALICE reads this on every run.")));
      // Tetik
      const kind = sel(Object.entries(TASK_TRIG), v.trigger.type || "manual");
      const tf = el("div", "task-trig");
      kind.addEventListener("change", () => { v.trigger = { type: kind.value }; drawTrig(); });
      form.append(field(_("When"), kind), tf);
      const drawTrig = () => {
        tf.replaceChildren();
        const tg = v.trigger;
        const inp = (key, ph, label, hint, num) => {
          const i = txt(tg[key] != null ? String(tg[key]) : "", ph);
          i.addEventListener("input", () => { tg[key] = num ? (i.value === "" ? undefined : Number(i.value)) : i.value; });
          tf.appendChild(field(label, i, hint));
        };
        if (tg.type === "schedule") inp("cron", "0 9 * * *", "Cron", _("minute hour day month weekday · e.g. every morning at 9: 0 9 * * *"));
        if (tg.type === "metric") {
          inp("metric", "pc.disk.*.percent", _("Metric"), "pc.gpu.temp, pc.disk.*.percent, watch.hr…");
          if (!tg.op) tg.op = ">";
          const op = sel([">", ">=", "<", "<=", "==", "!="].map((o) => [o, o]), tg.op);
          op.addEventListener("change", () => { tg.op = op.value; });
          tf.appendChild(field(_("Condition"), op));
          inp("threshold", "90", _("Threshold"), "", true);
          inp("duration", "0", _("Duration (s)"), _("the condition must last this long; 0 = at once"), true);
        }
        if (tg.type === "event") {
          inp("kind", "flight", _("Event type"), _("e.g. flight (flight debrief), task, info"));
          inp("match", "Uçuş değerlendirmesi", _("Match"), _("pattern to look for in the title/content (regex)"));
        }
        if (tg.type === "github") {
          inp("repo", _("owner/repo"), _("Repository"));
          if (!tg.what) tg.what = "issues";
          const w = sel([["issues", _("New issue")], ["pulls", _("New PR")]], tg.what);
          w.addEventListener("change", () => { tg.what = w.value; });
          tf.appendChild(field(_("On what"), w));
          inp("every_minutes", "10", _("Check every how many minutes"), "", true);
        }
        if (tg.type === "folder") {
          inp("path", "C:\\Users\\…\\Downloads", _("Folder"));
          inp("pattern", "*", "Dosya deseni", _("e.g. *.pdf"));
        }
        if (tg.type === "manual") tf.appendChild(el("small", "enroll-note", _("Runs only with \"Run now\".")));
      };
      drawTrig();
      // İzin seviyesi
      const perm = el("div", "task-perm");
      for (const [k, [label, hint]] of Object.entries(TASK_PERM)) {
        const b = el("button", "perm-opt" + (v.permission === k ? " on" : ""));
        b.append(el("b", "", label), el("small", "", hint));
        b.addEventListener("click", () => { v.permission = k; draw(); });
        perm.appendChild(b);
      }
      form.appendChild(field(_("Permission"), perm, v.permission === "full" ? _("Careful: it also does things that can't be undone without asking.") : _("Approval requests come to the desktop and the phone.")));
      // Araçlar
      const tools = el("div", "task-tools");
      for (const g of (TASKS && TASKS.groups) || []) {
        const on = (v.tools || []).includes(g);
        const c = el("button", "chip" + (on ? " on" : ""), g);
        c.addEventListener("click", () => { v.tools = on ? v.tools.filter((x2) => x2 !== g) : (v.tools || []).concat([g]); draw(); });
        tools.appendChild(c);
      }
      form.appendChild(field(_("Tools"), tools, _("The basic tools (reading files, web, memory) are always there; add the ones it needs.")));
      const role = sel(Object.entries(TASK_ROLE), v.role || "brain");
      role.addEventListener("change", () => { v.role = role.value; });
      form.appendChild(field("Model", role));
      form.appendChild(setRow(_("Report the result"), _("Send a summary of each run as a notification"), switchEl(v.notify, "", (on) => { v.notify = on; draw(); })));
    }
    draw();
  });
}

async function taskLog(id) {
  const r = await api("/api/tasks/log?id=" + encodeURIComponent(id));
  if (!r || !r.ok) return toast((r && r.error) || _("no task"));
  const t = r.task;
  showDialog((d) => {
    d.classList.add("task-dlg");
    d.append(el("h2", "", t.name), el("p", "muted", t.responsibility));
    d.appendChild(el("div", "sec", _("Memory")));
    if (!(t.memory || []).length) d.appendChild(el("div", "list-row muted", _("No notes yet. The task writes what it learns here.")));
    for (const m of t.memory || []) d.appendChild(el("div", "list-row", relTime(m.ts) + " · " + m.text));
    if ((t.memory || []).length) {
      const f = el("button", "btn", _("Delete the memory"));
      f.addEventListener("click", () => taskCall("forget", { id }).then((x) => { if (taskAfter(_("memory deleted"))(x)) taskLog(id); }));
      d.appendChild(f);
    }
    d.appendChild(el("div", "sec", _("Log")));
    const log = el("div", "task-log");
    if (!(r.log || []).length) log.appendChild(el("div", "list-row muted", _("Hasn't run yet.")));
    for (const e of r.log || []) {
      const row = el("div", "tl-row k-" + e.kind);
      row.append(el("span", "tl-t", relTime(e.ts)), el("span", "tl-k", TASK_LOG[e.kind] || e.kind), el("span", "tl-x", e.text));
      log.appendChild(row);
    }
    d.appendChild(log);
    dialogClose(d);
  });
}
