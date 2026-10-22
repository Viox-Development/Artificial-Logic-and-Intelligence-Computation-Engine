"use strict";
/* ALICE hızlı bar: soru yaz ya da söyle, cevap altta akar. */

const TOKEN = document.querySelector('meta[name="alice-token"]').content;
// Telefondan Projeler'e geçiş: tam uygulama aynı sunucuda.
document.getElementById("projLink").href = "/?t=" + encodeURIComponent(TOKEN) + "#projects";
const $ = (s) => document.querySelector(s);
const q = $("#q"), out = $("#out"), statusEl = $("#status"), mic = $("#mic");
let image = null;   // base64 PNG
let cur = null;     // akan cevap: {el, text}
let busy = false;

async function api(path, body) {
  const opts = { headers: { "X-Alice-Token": TOKEN } };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  try {
    return await (await fetch(path, opts)).json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

const md = (t) => DOMPurify.sanitize(marked.parse(t || ""));
const show = () => { out.hidden = false; out.scrollTop = out.scrollHeight; };
const setStatus = (t) => { statusEl.textContent = t || ""; };

function autosize() {
  q.style.height = "auto";
  q.style.height = Math.min(q.scrollHeight, 120) + "px";
}

function setImage(b64) {
  image = b64;
  $("#attach").hidden = !b64;
  if (b64) $("#attach img").src = "data:image/png;base64," + b64;
}

async function send(text) {
  text = (text ?? q.value).trim();
  if (!text && !image) return;
  if (busy) { setStatus("önceki iş sürüyor (Esc durdurur)"); return; }
  const r = await api("/api/send", { text: text || "Bu ekran görüntüsünde ne var? Bir hata varsa açıkla ve çözümünü söyle.",
                                     images: image ? [image] : [] });
  if (!r.ok) { addErr(r.error || "gönderilemedi"); return; }
  q.value = "";
  setImage(null);
  autosize();
}

function addErr(t) { out.appendChild(el("div", "err", t)); show(); }

/* ---------------------------------------------------------------- olaylar */
const handlers = {
  user: (ev) => { out.appendChild(el("div", "user", ev.text)); show(); },
  busy: (ev) => { busy = ev.value; setStatus(busy ? "düşünüyor…" : ""); },
  stream_start: () => { cur = null; },
  delta: (ev) => {
    if (!cur) { cur = { el: el("div", "msg"), text: "" }; out.appendChild(cur.el); }
    cur.text += ev.text;
    cur.el.innerHTML = md(cur.text);
    show();
  },
  stream_reset: () => { if (cur) { cur.el.remove(); cur = null; } },
  stream_end: (ev) => {
    if (!cur && ev.text) { cur = { el: el("div", "msg"), text: ev.text }; out.appendChild(cur.el); }
    if (cur) cur.el.innerHTML = md(ev.text || cur.text);
    cur = null;
    show();
  },
  tool_start: (ev) => { out.appendChild(el("div", "tool", `● ${ev.name} ${ev.summary || ""}`.slice(0, 160))); setStatus(`${ev.name} çalışıyor…`); show(); },
  tool_end: (ev) => { if (ev.is_error) addErr(`${ev.name}: ${(ev.text || "").slice(0, 200)}`); },
  permission: (ev) => {
    const box = el("div", "perm", `İzin gerekiyor: ${ev.name} — ${ev.summary}`);
    box.id = "perm-" + ev.id;
    for (const [ans, label] of [["y", "Evet"], ["n", "Hayır"], ["a", "Hep izin ver"], ["p", "Projede kalıcı"]]) {
      const b = el("button", null, label);
      b.onclick = () => api("/api/permission", { id: ev.id, answer: ans });
      box.appendChild(b);
    }
    out.appendChild(box);
    show();
  },
  permission_done: (ev) => { const b = document.getElementById("perm-" + ev.id); if (b) b.remove(); },
  ask_choice: (ev) => {
    const box = el("div", "perm", ev.question);
    box.id = "perm-" + ev.id;
    for (const o of ev.options || []) {
      const b = el("button", null, String(o).split(" — ")[0]);
      b.onclick = () => api("/api/choice", { id: ev.id, answer: o });
      box.appendChild(b);
    }
    out.appendChild(box);
    show();
  },
  ask_choice_done: (ev) => { const b = document.getElementById("perm-" + ev.id); if (b) b.remove(); },
  info: (ev) => { if (/hata|başarısız|veremedi/i.test(ev.text)) addErr(ev.text); },
};

function connect() {
  const es = new EventSource("/api/events?t=" + encodeURIComponent(TOKEN));
  es.onopen = () => { if (statusEl.dataset.offline) { delete statusEl.dataset.offline; setStatus(busy ? "düşünüyor…" : ""); } };
  es.onerror = () => {
    statusEl.dataset.offline = "1";
    setStatus("ALICE'e bağlantı koptu, yeniden bağlanıyor…");
    if (es.readyState === EventSource.CLOSED) { es.close(); setTimeout(connect, 3000); }
  };
  es.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch { return; }
    if (handlers[ev.type]) handlers[ev.type](ev);
  };
}

/* ---------------------------------------------------------------- mikrofon */
let rec = null, chunks = [];

async function startRec() {
  if (rec) return;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    rec = new MediaRecorder(stream);
    chunks = [];
    rec.ondataavailable = (e) => chunks.push(e.data);
    rec.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      const blob = new Blob(chunks, { type: rec.mimeType });
      rec = null;
      mic.classList.remove("rec");
      if (blob.size < 2000) { setStatus(""); return; }
      setStatus("anlaşılıyor…");
      const b64 = await new Promise((res) => {
        const fr = new FileReader();
        fr.onload = () => res(String(fr.result).split(",")[1]);
        fr.readAsDataURL(blob);
      });
      const r = await api("/api/transcribe", { audio: b64, mime: blob.type });
      setStatus("");
      if (!r.ok) { addErr("anlaşılamadı: " + (r.error || "")); return; }
      if (r.text) send(r.text);
    };
    rec.start();
    mic.classList.add("rec");
    setStatus("dinliyorum… (bırakınca gönderilir)");
  } catch (e) {
    addErr("mikrofon açılamadı: " + e);
  }
}
const stopRec = () => { if (rec && rec.state === "recording") rec.stop(); };

mic.addEventListener("pointerdown", (e) => { e.preventDefault(); startRec(); });
mic.addEventListener("pointerup", stopRec);
mic.addEventListener("pointerleave", stopRec);

/* ------------------------------------------------------------------ tuşlar */
q.addEventListener("input", autosize);
q.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
let spaceHeld = false;
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (busy) api("/api/stop", {}); else window.close();
  } else if (e.ctrlKey && e.key.toLowerCase() === "n") {
    e.preventDefault();
    api("/api/new", {}).then(() => { out.innerHTML = ""; out.hidden = true; });
  } else if (e.ctrlKey && e.code === "Space" && !spaceHeld) {
    e.preventDefault();
    spaceHeld = true;
    startRec();
  }
});
document.addEventListener("keyup", (e) => {
  if (spaceHeld && (e.code === "Space" || e.key === "Control")) { spaceHeld = false; stopRec(); }
});
$("#dropImg").onclick = () => setImage(null);
q.addEventListener("paste", (e) => {
  const item = [...(e.clipboardData?.items || [])].find((i) => i.type.startsWith("image/"));
  if (!item) return;
  e.preventDefault();
  const fr = new FileReader();
  fr.onload = () => setImage(String(fr.result).split(",")[1]);
  fr.readAsDataURL(item.getAsFile());
});

/* ----------------------------------------------------------------- açılış */
(async function boot() {
  connect();
  const p = await api("/api/quick/pending");
  if (p.image) setImage(p.image);
  if (p.text) { q.value = p.text; autosize(); }
  if (p.autosend) send();
  if (p.listen) startRec();
  q.focus();
})();
