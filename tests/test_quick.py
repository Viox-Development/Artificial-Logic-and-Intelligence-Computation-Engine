from __future__ import annotations

import base64

import httpx
from PIL import Image

from alice.app.quick import EXPLAIN_CLIPBOARD, QuickBar, png_b64
from alice.app.server import AppState
from tests.conftest import FakeProvider, make_config


def make_qb(workdir):
    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    state = AppState(cfg, workdir, "auto", provider_factory=lambda pc: FakeProvider("a", ["tamam"]))
    qb = QuickBar(cfg, state=state)
    urls = []
    qb.launcher = urls.append
    return qb, urls


def test_quick_page_and_pending(workdir, monkeypatch):
    qb, urls = make_qb(workdir)
    try:
        base = qb.url.split("/quick")[0]
        assert httpx.get(base + "/quick").status_code == 403
        qb.open(text="merhaba", autosend=True)
        r = httpx.get(urls[0])
        assert r.status_code == 200 and "quick.js" in r.text and qb.state.token in r.text
        c = httpx.Client(base_url=base, headers={"X-Alice-Token": qb.state.token})
        assert c.get("/static/quick.js").status_code == 200
        assert c.get("/api/quick/pending").json() == {"text": "merhaba", "autosend": True}
        assert c.get("/api/quick/pending").json() == {}  # bir kez okunur

        monkeypatch.setattr("alice.app.quick.read_clipboard", lambda: "KeyError: 'x'")
        qb.explain_clipboard()
        p = c.get("/api/quick/pending").json()
        assert p["autosend"] and p["text"] == EXPLAIN_CLIPBOARD.format(text="KeyError: 'x'")

        class FakeSTT:
            def transcribe_file(self, data, name):
                return f"{len(data)} bayt {name}"

        qb.state._stt = FakeSTT()
        r = c.post("/api/transcribe", json={"audio": base64.b64encode(b"x" * 10).decode(), "mime": "audio/webm"})
        assert r.json() == {"ok": True, "text": "10 bayt speech.webm"}
    finally:
        qb.stop()


def test_quick_idle_starts_new_session(workdir, monkeypatch):
    qb, _ = make_qb(workdir)
    try:
        qb.state.agent.messages.append({"role": "user", "content": "eski"})
        t = [1000.0]
        monkeypatch.setattr("alice.app.quick.time.monotonic", lambda: t[0])
        qb.open()
        assert len(qb.state.agent.messages) == 2
        t[0] += 700
        qb.open()
        assert len(qb.state.agent.messages) == 1
    finally:
        qb.stop()


def test_png_b64_downscales():
    data = base64.b64decode(png_b64(Image.new("RGB", (3200, 400)), max_width=1600))
    from io import BytesIO

    with Image.open(BytesIO(data)) as image:
        assert image.size == (1600, 200)
