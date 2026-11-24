/* BUDDY: sohbet kutusunun üstünde, sağda duran küçük arkadaş. Tıklayınca okşanır; menüden beslenir, adı değişir
   ya da gizlenir. Ara sıra konuşmaya kısa bir yorum yapar (balon). Durum /api/buddy'den 20 sn'de bir okunur. */
(() => {
  const B = { box: null, last: 0, data: null, bubbleTimer: 0 };
  const hidden = () => { try { return localStorage.getItem("buddyHidden") === "1"; } catch { return false; } };
  const setHidden = (v) => { try { localStorage.setItem("buddyHidden", v ? "1" : "0"); } catch { /* yok */ } };

  function bar(label, value) {
    const row = el("div", "bd-bar");
    const fill = el("i");
    fill.style.width = Math.max(0, Math.min(100, value)) + "%";
    const track = el("span", "bd-track");
    track.appendChild(fill);
    row.append(el("span", "bd-l", label), track);
    return row;
  }

  function bubble(text) {
    const b = B.box && B.box.querySelector(".bd-say");
    if (!b || !text) return;
    b.textContent = text;
    b.hidden = false;
    clearTimeout(B.bubbleTimer);
    B.bubbleTimer = setTimeout(() => { b.hidden = true; }, 12000);
  }

  function hearts() {
    const h = el("span", "bd-heart", "♥");
    B.box.appendChild(h);
    setTimeout(() => h.remove(), 1200);
  }

  function render(d) {
    B.data = d;
    if (!B.box) return;
    B.box.hidden = !d || d.enabled === false || hidden();
    if (B.box.hidden) return;
    B.box.className = "buddy" + (d.shiny ? " shiny" : "") + " r-" + d.rarity;
    const art = B.box.querySelector(".bd-art");
    art.textContent = (d.art || []).join("\n");
    art.title = `${d.name} · ${d.rarity} ${d.species} ${d.stars}\n${d.persona}\n` + _("Click to pet");
    const info = B.box.querySelector(".bd-info");
    info.replaceChildren(
      el("div", "bd-name", `${d.name} · sv ${d.level}`),
      el("div", "bd-mood", d.mood),
      bar("tok", d.food), bar("enerji", d.energy), bar("keyif", d.happy));
    if (d.say && d.say_ts > B.last) {
      B.last = d.say_ts;
      bubble(d.say);
    }
  }

  async function refresh() {
    if (document.hidden || hidden()) return;
    const d = await api("/api/buddy").catch(() => null);
    if (d && d.name) render(d);
  }

  async function act(action, value) {
    const r = await api("/api/buddy", { action, value }).catch(() => null);
    if (r && r.ok) {
      render(r.buddy);
      bubble(r.text);
    }
  }

  function menu(ev) {
    ev.stopPropagation();
    const old = B.box.querySelector(".bd-menu");
    if (old) { old.remove(); return; }
    const m = el("div", "bd-menu");
    const item = (label, fn) => {
      const b = el("button", "", label);
      b.addEventListener("click", (e) => { e.stopPropagation(); m.remove(); fn(); });
      m.appendChild(b);
    };
    item("Besle", () => act("feed"));
    item(_("Pet"), () => { hearts(); act("pet"); });
    item("Oyna", () => { act("play"); });
    item("Ad koy", () => {
      const v = prompt(_("New name:"), (B.data && B.data.name) || "");
      if (v) act("rename", v);
    });
    item(_("Reroll"), () => {
      if (confirm(_("Roll a new species and personality?"))) act("reroll");
    });
    item("Gizle", () => { setHidden(true); B.box.hidden = true; toast(_("BUDDY hidden; you can bring it back in Settings > Assistant")); });
    B.box.appendChild(m);
  }

  function mount() {
    const wrap = document.getElementById("composerWrap");
    if (!wrap || B.box) return;
    B.box = el("div", "buddy");
    B.box.hidden = true;
    const say = el("div", "bd-say");
    say.hidden = true;
    const art = el("pre", "bd-art");
    art.addEventListener("click", () => { hearts(); act("pet"); });
    const more = el("button", "bd-more", "⋯");
    more.title = _("BUDDY menu");
    more.addEventListener("click", menu);
    B.box.append(say, art, el("div", "bd-info"), more);
    wrap.appendChild(B.box);
    document.addEventListener("click", () => { const m = B.box.querySelector(".bd-menu"); if (m) m.remove(); });
    refresh();
    setInterval(refresh, 20000);
    document.addEventListener("visibilitychange", refresh);
  }

  window.buddyShow = () => { setHidden(false); refresh(); };
  window.buddyRefresh = refresh;
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
  else mount();
})();
