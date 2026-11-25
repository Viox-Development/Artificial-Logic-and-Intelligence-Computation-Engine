"use strict";

/* Komutlar (durum çubuğu): ALICE'in beklediği ve arka planda çalışan komutlar. Beklenen komut buradan ya da
   Ctrl+Shift+B ile arka plana alınır; Durdur komutu öldürmez, arka plana alır, öldürmek buradan yapılır. */
const PROCS = { rows: [], seen: new Set(), timer: 0 };

async function procsRefresh() {
  clearTimeout(PROCS.timer);
  if (S.busy || PROCS.rows.length) {
    const r = await api("/api/procs").catch(() => null);
    if (r && r.procs) procsDraw(r.procs);
  }
  PROCS.timer = setTimeout(procsRefresh, S.busy || PROCS.rows.some((p) => p.state !== "done") ? 1500 : 4000);
}
function procsDraw(all) {
  // Biten komut, menü bir kez açılana kadar durur.
  PROCS.rows = all.filter((p) => p.state !== "done" || !PROCS.seen.has(p.pid)).filter((p) => p.state !== "foreground" || p.secs >= 2);
  const b = $("#stProcs");
  const fg = PROCS.rows.filter((p) => p.state === "foreground"), run = PROCS.rows.filter((p) => p.state === "running");
  for (const p of run) {
    if (p.reason === "stop" && !PROCS.told) toast(_("The command goes on in the background"));
    PROCS.told = p.reason === "stop" || PROCS.told;
  }
  if (!run.some((p) => p.reason === "stop")) PROCS.told = false;
  b.hidden = !PROCS.rows.length;
  if (b.hidden) return;
  b.replaceChildren(el("i"), el("span", "", fg.length ? _("Command running · move to background")
    : run.length ? _("{n} in the background", { n: run.length }) : _("Command finished")));
  b.classList.toggle("run", !!(fg.length || run.length));
  b.title = fg.length ? _("Move the running command to the background (Ctrl+Shift+B)") : _("Background commands");
}
async function procsBackground() {
  const r = await api("/api/procs/background", {});
  if (!r || !r.ok || !r.moved.length) return toast(_("There is no command that can be moved to the background"));
  toast(_("Moved to the background"));
  procsRefresh();
}
function procsMenu() {
  openMenu($("#stProcs"), (m) => {
    m.appendChild(el("div", "menu-title", _("Background commands")));
    for (const p of PROCS.rows) {
      const row = el("div", "proc");
      row.append(el("code", "", p.command), el("small", "", (p.state === "done" ? _("finished · exit code {code}", { code: p.code })
        : p.state === "foreground" ? _("ALICE is waiting for it") : _("running")) + " · " + p.secs + " s"));
      const acts = el("div", "acts");
      const add = (label, fn) => { const x = el("button", "", label); x.addEventListener("click", fn); acts.appendChild(x); };
      if (p.state === "foreground" && p.movable) add(_("Move to background"), () => { closeMenu(); procsBackground(); });
      if (p.state !== "foreground") {
        add(_("Output"), async () => {
          const r = await api("/api/procs/output", { pid: p.pid });
          let pre = $("pre", row);
          if (!pre) { pre = el("pre"); row.appendChild(pre); }
          pre.textContent = (r && r.text) || _("(no output)");
          pre.scrollTop = pre.scrollHeight;
        });
      }
      if (p.state !== "done") add(_("Stop the command"), async () => { closeMenu(); await api("/api/procs/kill", { pid: p.pid }); procsRefresh(); });
      else PROCS.seen.add(p.pid);
      row.appendChild(acts);
      m.appendChild(row);
    }
  }, "right");
}
$("#stProcs").addEventListener("click", () => {
  const fg = PROCS.rows.filter((p) => p.state === "foreground");
  if (fg.length && fg.length === PROCS.rows.length && fg.every((p) => p.movable)) procsBackground(); else procsMenu();
});
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === "b") { e.preventDefault(); e.stopImmediatePropagation(); procsBackground(); }
}, true);
procsRefresh();
