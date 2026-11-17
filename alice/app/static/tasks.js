/* Görevler: ALICE'in arka planda yürüttüğü kalıcı sorumluluklar ve onay kutusu (alice/core/tasks.py).
   Uygulama yalnız görev veritabanını düzenler; görevleri arka plandaki çekirdek çalıştırır. Onaylar telefona da
   gider; hangisinden cevaplanırsa cevaplansın diğerinde düşer. */

const TASK_TRIG = { manual: "Elle", schedule: "Zamanla", metric: "Ölçüm", event: "Olay", github: "GitHub", folder: "Klasör" };
const TASK_PERM = { read: ["Sadece okur", "Değiştiren her şey için onay ister"], act: ["Dosyalarda çalışır", "Dışarıya giden ve tehlikeli işler için onay ister"], full: ["Tam yetki", "Hiçbir şey sormadan yapar"] };
const TASK_ROLE = { brain: "Ana model", fast: "Hızlı model", code: "Kod modeli", vision: "Görsel model" };
const TASK_LOG = { trigger: "tetik", run: "çalıştı", result: "sonuç", error: "hata", approval: "onay istedi", answer: "cevap", info: "bilgi" };
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
    if (!r || r.ok === false) { toast((r && r.error) || "olmadı"); return false; }
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
  t.append(el("h1", "", "Görevler"),
    el("p", "", "ALICE'e kalıcı bir sorumluluk ver: kendi bilgisayarında, sen başında değilken yürütür ve karar gerektiğinde sana sorar (masaüstünde ve telefonda)."));
  const nb = el("button", "btn primary");
  nb.append(svg(IC.plus), document.createTextNode("Yeni görev"));
  nb.addEventListener("click", () => taskDialog(null));
  head.append(t, el("span", "grow"), nb);
  page.appendChild(head);
  if (d.core === false) {
    const w = el("div", "list-row warn");
    const l = el("span", "l");
    l.append(el("b", "", "Çekirdek çalışmıyor"), el("small", "", "Görevler arka plandaki çekirdekte çalışır; açılınca kaldığı yerden devam eder."));
    const go = el("button", "btn", "Çekirdeği başlat");
    go.addEventListener("click", () => api("/api/system/action", { action: "start" }).then(() => { toast("Çekirdek başlatılıyor"); setTimeout(showTasks, 3000); }));
    w.append(l, go);
    page.appendChild(w);
  }
  // Onay kutusu
  const inbox = d.approvals || [];
  page.appendChild(el("div", "sec", inbox.length ? `Onay bekleyenler (${inbox.length})` : "Onay kutusu"));
  if (!inbox.length) page.appendChild(el("div", "list-row muted", "Bekleyen onay yok. Bir görev karar gerektirdiğinde burada ve telefonunda görünür."));
  for (const a of inbox) page.appendChild(approvalCard(a));
  // Görevler
  const tasks = d.tasks || [];
  page.appendChild(el("div", "sec", `Görevler (${tasks.length})`));
  if (!tasks.length) {
    page.appendChild(emptyState(IC.list, "Henüz görev yok", "Bir şablonla başla ya da ne yapmasını istediğini düz cümleyle yaz."));
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
      const st = { approved: "onaylandı", rejected: "reddedildi", replied: "cevaplandı", running: "yapılıyor", done: "tamam", failed: "başarısız" }[a.status] || a.status;
      l.append(el("b", "", a.title), el("small", "", `${a.task} · ${st} · ${relTime(a.resolved || a.ts)}` + (a.result ? " · " + a.result : "")));
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
  top.append(el("span", "ta-task", a.task || "Görev"), el("span", "ta-time", relTime(a.ts)));
  box.append(top, el("b", "ta-title", a.title));
  if (a.body) box.appendChild(el("div", "ta-body", a.body));
  if (a.action) {
    const det = el("details", "ta-action");
    det.append(el("summary", "", "Onaylarsan yapılacak"), el("div", "", a.action));
    box.appendChild(det);
  }
  const acts = el("div", "row ta-acts");
  const inp = el("input");
  inp.type = "text";
  inp.placeholder = "Cevap yaz ya da düzeltme iste…";
  const answer = (decision) => {
    const reply = inp.value.trim();
    if (decision === "reply" && !reply) { inp.focus(); return; }
    box.classList.add("done");
    taskCall("answer", { id: a.id, decision, reply }).then(taskAfter(decision === "approve" ? "onaylandı, ALICE yapıyor" : decision === "reject" ? "reddedildi" : "cevap gönderildi"));
  };
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") answer("reply"); });
  const ok = el("button", "btn primary", "Onayla");
  ok.addEventListener("click", () => answer("approve"));
  const no = el("button", "btn", "Reddet");
  no.addEventListener("click", () => answer("reject"));
  const send = el("button", "btn", "Gönder");
  send.addEventListener("click", () => answer("reply"));
  acts.append(ok, no, inp, send);
  box.appendChild(acts);
  return box;
}

function taskRow(x) {
  const r = el("div", "card-row task-row" + (x.enabled ? "" : " off"));
  const l = el("span", "l");
  const sub = [x.when, x.running ? "çalışıyor" : x.last_run ? "son: " + relTime(x.last_run) : "henüz çalışmadı", x.enabled ? "" : "duraklatıldı",
    x.pending ? x.pending + " onay bekliyor" : ""].filter(Boolean).join(" · ");
  l.append(el("b", "", x.name), el("small", "", sub));
  if (x.last_result) l.appendChild(el("small", "task-last" + (x.last_ok === false ? " err" : ""), x.last_result));
  l.addEventListener("click", () => taskLog(x.id));
  l.title = "Kayıt ve hafıza";
  const run = el("button", "btn", "Şimdi çalıştır");
  run.addEventListener("click", () => taskCall("run", { id: x.id }).then(taskAfter(TASKS && TASKS.core ? "bir sonraki çekirdek turunda çalışacak" : "çekirdek açılınca çalışacak")));
  r.append(el("i", "dot " + (x.running ? "st-running" : x.enabled ? "st-done" : "st-paused")), l, run,
    switchEl(x.enabled, x.enabled ? "Duraklat" : "Devam ettir", (on) => taskCall("enable", { id: x.id, enabled: on }).then(taskAfter(on ? "devam ediyor" : "duraklatıldı"))),
    iconAct(IC.edit, "Düzenle", () => taskDialog(x)),
    iconAct(IC.trash, "Sil", async () => {
      if (await confirmDialog("Görev silinsin mi?", `"${x.name}" görevi, kaydı ve hafızası silinecek.`, "Sil")) taskCall("remove", { id: x.id }).then(taskAfter("görev silindi"));
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
    d.appendChild(el("h2", "", editing ? "Görevi düzenle" : "Yeni görev"));
    if (!editing) {
      const quick = el("div", "key-edit");
      const q = el("input");
      q.type = "text";
      q.placeholder = "Düz cümleyle yaz: \"repoma yeni issue gelince cevap taslağı hazırla\"";
      const go = el("button", "btn", "Doldur");
      const fill = async () => {
        if (!q.value.trim()) return;
        go.disabled = true; go.textContent = "anlıyorum…";
        const r = await taskCall("parse", { text: q.value.trim() });
        go.disabled = false; go.textContent = "Doldur";
        if (!r || !r.ok) return toast((r && r.error) || "anlaşılamadı");
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
    const cancel = el("button", "btn", "Vazgeç");
    cancel.addEventListener("click", closeDialog);
    const save = el("button", "btn primary", editing ? "Kaydet" : "Oluştur");
    save.addEventListener("click", async () => {
      const body = { name: v.name, responsibility: v.responsibility, trigger: v.trigger, tools: v.tools, permission: v.permission, role: v.role, notify: v.notify };
      if (v.template) body.template = v.template;
      const r = await taskCall(editing ? "update" : "create", editing ? Object.assign(body, { id: x.id }) : body);
      if (taskAfter(editing ? "kaydedildi" : "görev oluşturuldu")(r)) closeDialog();
    });
    row.append(cancel, save);
    d.appendChild(row);

    function draw() {
      form.replaceChildren();
      const name = txt(v.name, "Kısa bir ad");
      name.addEventListener("input", () => { v.name = name.value; });
      const resp = el("textarea");
      resp.rows = 4;
      resp.value = v.responsibility;
      resp.placeholder = "Ne yapmalı? Neye bakmalı, ne zaman sana sormalı?";
      resp.addEventListener("input", () => { v.responsibility = resp.value; });
      form.append(field("Ad", name), field("Sorumluluk", resp, "Düz cümleyle; ALICE her çalıştırmada bunu okur."));
      // Tetik
      const kind = sel(Object.entries(TASK_TRIG), v.trigger.type || "manual");
      const tf = el("div", "task-trig");
      kind.addEventListener("change", () => { v.trigger = { type: kind.value }; drawTrig(); });
      form.append(field("Ne zaman", kind), tf);
      const drawTrig = () => {
        tf.replaceChildren();
        const tg = v.trigger;
        const inp = (key, ph, label, hint, num) => {
          const i = txt(tg[key] != null ? String(tg[key]) : "", ph);
          i.addEventListener("input", () => { tg[key] = num ? (i.value === "" ? undefined : Number(i.value)) : i.value; });
          tf.appendChild(field(label, i, hint));
        };
        if (tg.type === "schedule") inp("cron", "0 9 * * *", "Cron", "dakika saat gün ay haftanın_günü · ör. her sabah 9: 0 9 * * *");
        if (tg.type === "metric") {
          inp("metric", "pc.disk.*.percent", "Ölçüm", "pc.gpu.temp, pc.disk.*.percent, watch.hr…");
          if (!tg.op) tg.op = ">";
          const op = sel([">", ">=", "<", "<=", "==", "!="].map((o) => [o, o]), tg.op);
          op.addEventListener("change", () => { tg.op = op.value; });
          tf.appendChild(field("Koşul", op));
          inp("threshold", "90", "Eşik", "", true);
          inp("duration", "0", "Süre (sn)", "koşul bu kadar sürmeli; 0 = hemen", true);
        }
        if (tg.type === "event") {
          inp("kind", "flight", "Olay türü", "ör. flight (uçuş değerlendirmesi), task, info");
          inp("match", "Uçuş değerlendirmesi", "Eşleşme", "başlıkta/içerikte aranacak ifade (regex)");
        }
        if (tg.type === "github") {
          inp("repo", "sahip/depo", "Depo");
          if (!tg.what) tg.what = "issues";
          const w = sel([["issues", "Yeni issue"], ["pulls", "Yeni PR"]], tg.what);
          w.addEventListener("change", () => { tg.what = w.value; });
          tf.appendChild(field("Ne gelince", w));
          inp("every_minutes", "10", "Kaç dakikada bir bak", "", true);
        }
        if (tg.type === "folder") {
          inp("path", "C:\\Users\\…\\Downloads", "Klasör");
          inp("pattern", "*", "Dosya deseni", "ör. *.pdf");
        }
        if (tg.type === "manual") tf.appendChild(el("small", "enroll-note", "Yalnız \"Şimdi çalıştır\" ile çalışır."));
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
      form.appendChild(field("İzin", perm, v.permission === "full" ? "Dikkat: geri alınamayan işleri de sormadan yapar." : "Onay istekleri masaüstüne ve telefona gelir."));
      // Araçlar
      const tools = el("div", "task-tools");
      for (const g of (TASKS && TASKS.groups) || []) {
        const on = (v.tools || []).includes(g);
        const c = el("button", "chip" + (on ? " on" : ""), g);
        c.addEventListener("click", () => { v.tools = on ? v.tools.filter((x2) => x2 !== g) : (v.tools || []).concat([g]); draw(); });
        tools.appendChild(c);
      }
      form.appendChild(field("Araçlar", tools, "Temel araçlar (dosya okuma, web, hafıza) her zaman var; gerekenleri ekle."));
      const role = sel(Object.entries(TASK_ROLE), v.role || "brain");
      role.addEventListener("change", () => { v.role = role.value; });
      form.appendChild(field("Model", role));
      form.appendChild(setRow("Sonucu bildir", "Her çalıştırmanın özetini bildirim olarak gönder", switchEl(v.notify, "", (on) => { v.notify = on; draw(); })));
    }
    draw();
  });
}

async function taskLog(id) {
  const r = await api("/api/tasks/log?id=" + encodeURIComponent(id));
  if (!r || !r.ok) return toast((r && r.error) || "görev yok");
  const t = r.task;
  showDialog((d) => {
    d.classList.add("task-dlg");
    d.append(el("h2", "", t.name), el("p", "muted", t.responsibility));
    d.appendChild(el("div", "sec", "Hafıza"));
    if (!(t.memory || []).length) d.appendChild(el("div", "list-row muted", "Henüz not yok. Görev öğrendiklerini buraya yazar."));
    for (const m of t.memory || []) d.appendChild(el("div", "list-row", relTime(m.ts) + " · " + m.text));
    if ((t.memory || []).length) {
      const f = el("button", "btn", "Hafızayı sil");
      f.addEventListener("click", () => taskCall("forget", { id }).then((x) => { if (taskAfter("hafıza silindi")(x)) taskLog(id); }));
      d.appendChild(f);
    }
    d.appendChild(el("div", "sec", "Kayıt"));
    const log = el("div", "task-log");
    if (!(r.log || []).length) log.appendChild(el("div", "list-row muted", "Henüz çalışmadı."));
    for (const e of r.log || []) {
      const row = el("div", "tl-row k-" + e.kind);
      row.append(el("span", "tl-t", relTime(e.ts)), el("span", "tl-k", TASK_LOG[e.kind] || e.kind), el("span", "tl-x", e.text));
      log.appendChild(row);
    }
    d.appendChild(log);
    dialogClose(d);
  });
}
