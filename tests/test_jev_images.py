"""Jev'e görsel ve Box (TypeSafe "Danger Zone"): istek biçimi dokümandaki gibi mi, reflex ekranda görüneni
tıklayabiliyor mu, pencere çerçevesi seçeneklerden çıktı mı."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from PIL import Image

from alice.decider import (IMAGES_HEADER, Answer, Box, Decider, DeciderError, Decision, JevEngine, LLMEngine,
                           Noul, image_check, padded_fraction, prepare_image)
from alice.reflex.loop import VISUAL_KEY, ReflexLoop
from alice.reflex.observers import Element, Observation, is_window_chrome
from tests.conftest import make_config


def jev(handler, images=True) -> JevEngine:
    return JevEngine("https://api.typesafe.ai/v1/systemone", "jev-latest", "k",
                     client=httpx.Client(transport=httpx.MockTransport(handler)), images=images)


def jpeg(w=600, h=400) -> bytes:
    return prepare_image(Image.new("RGB", (w, h), (255, 255, 255)))


def test_image_request_matches_docs_and_box_is_parsed():
    seen = {}

    def handler(req):
        seen["headers"] = req.headers
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"model": "jev-1.14.0", "answers": {
            "there": {"type": "noul", "noul": 0.93},
            "where": {"type": "box", "box": {"top": 0.25, "bottom": 0.82, "left": 0.44, "right": 0.84},
                      "confidence": None},
        }, "usage": {"input_tokens": 290, "output_tokens": 21}})

    img = jpeg()
    d = jev(handler).decide({"photo": "ref-1"}, {"there": Noul("Is there a mug in `photo`?"),
                                                 "where": Box("the mug in `photo`")}, images={"ref-1": img})
    assert seen["headers"][IMAGES_HEADER] == "true"
    body = seen["body"]
    assert body["state"] == {"photo": "ref-1"} and body["questions"]["where"] == {"type": "box",
                                                                                 "instructions": "the mug in `photo`"}
    asset = body["assets"]["ref-1"]
    assert asset["type"] == "image" and asset["format"] == "jpg" and base64.b64decode(asset["data_base64"]) == img
    assert d["there"].value == 0.93
    assert d["where"].value == {"left": 0.44, "top": 0.25, "right": 0.84, "bottom": 0.82}
    assert d["where"].confidence == 0.0  # dokümandaki örnekte confidence null gelebiliyor


def test_text_request_sends_no_image_header():
    seen = {}

    def handler(req):
        seen["h"] = req.headers
        return httpx.Response(200, json={"answers": {"q": {"type": "noul", "noul": 0.2}}})

    jev(handler).decide("text", {"q": Noul("?")})
    assert IMAGES_HEADER not in seen["h"]


def test_images_off_or_llm_engine_refuse_and_decider_skips_them():
    with pytest.raises(DeciderError):
        jev(lambda r: httpx.Response(200, json={}), images=False).decide("s", {"b": Box("x")})
    calls = []

    class Router:
        def chat(self, *a, **k):
            calls.append(1)
            raise AssertionError("LLM'e görsel soru gitmemeli")

    dec = Decider([LLMEngine(Router())])
    assert not dec.supports_images
    with pytest.raises(DeciderError, match="görsel"):
        dec.decide({"s": "r"}, {"b": Box("x")}, images={"r": jpeg()})
    assert not calls


def test_rejected_image_request_only_disables_images_not_text():
    def handler(req):
        if IMAGES_HEADER in req.headers:
            return httpx.Response(400, text="images are not turned on")
        return httpx.Response(200, json={"answers": {"q": {"type": "noul", "noul": 0.8}}})

    dec = Decider([jev(handler)])
    assert dec.supports_images
    with pytest.raises(DeciderError):
        dec.decide({"s": "r"}, {"q": Noul("?")}, images={"r": jpeg()})
    assert not dec.supports_images  # görsel bir süre kapalı
    assert dec.decide("text", {"q": Noul("?")})["q"].value == 0.8  # metin soruları yine Jev'e


def test_prepare_image_fits_jev_limits():
    big = Image.open(__import__("io").BytesIO(prepare_image(Image.new("RGB", (3440, 1440)))))
    assert max(big.size) == 1568 and big.format == "JPEG"
    small = Image.open(__import__("io").BytesIO(prepare_image(Image.new("RGB", (200, 100)))))
    assert min(small.size) >= 280  # kısa kenarı 280'den küçük görüntüyü API reddediyor
    sx, sy = padded_fraction((200, 100))
    assert sx == pytest.approx(280 / 200) and sy == pytest.approx(280 / 100)
    assert padded_fraction((3440, 1440)) == (1.0, 1.0)


def test_window_chrome_is_not_offered():
    # Kullanıcının ekranı: ALICE penceresinde reflex "System" menüsüne tıkladı, seçeneklerde "Close" da vardı.
    assert is_window_chrome("MenuBar", "System") and is_window_chrome("MenuItem", "Restore")
    assert is_window_chrome("MenuItem", "Kapat") and is_window_chrome("TitleBar", "ALICE")
    assert is_window_chrome("Button", "Close") and not is_window_chrome("Button", "Close", goal="pencereyi kapat")
    assert not is_window_chrome("Button", "Ayarlar") and not is_window_chrome("MenuItem", "Save")
    # "Ayarlar katmanını X ile kapat" pencerenin kendisi değil: Unciv'de Kapat düğmesine basılıp oyun kapanmıştı.
    assert is_window_chrome("Button", "Close", goal="close the settings overlay with the large X", title="Unciv")
    assert not is_window_chrome("Button", "Close", goal="Not Defteri'ni kapat", title="*merhaba, Not Defteri")
    assert not is_window_chrome("Button", "Close", goal="close the app", title="Unciv")
    # UWP: adın sonuna pencere başlığı ekleniyor.
    assert is_window_chrome("Button", "Close Solitaire & Casual Games", title="Solitaire & Casual Games")
    assert is_window_chrome("Button", "Minimize Solitaire & Casual Games", title="Solitaire & Casual Games")
    assert not is_window_chrome("Button", "Close Tab", title="Solitaire & Casual Games")


def test_phantom_tabs_any_language():
    from alice.reflex.observers import is_phantom_tab

    doc = [(0, 30, 1000, 800)]
    assert is_phantom_tab("TabItem", "about:blank - Bellek kullanımı - 17,9 MB", (5, 30, 40, 60), doc)
    assert is_phantom_tab("TabItem", "about:blank - Bellek kullanımı - 17,9 MB", None, [], anywhere=True)
    assert not is_phantom_tab("TabItem", "Gelen kutusu - Gmail", (5, 30, 40, 60), doc)


class WebViewObserver:
    """İçi okunamayan pencere (WebView): UIA sadece 'Document' veriyor, ekranda ise 'Ayarlar' düğmesi var."""

    kind = "fake"

    def __init__(self):
        self.clicked = []
        self.page = "home"
        self.goal = ""

    def observe(self):
        return Observation("ALICE", "desktop", [Element(1, "Document", "", True)])

    def snapshot(self):
        return Image.new("RGB", (1200, 800), (30, 30, 30))

    def click_at(self, fx, fy):
        self.clicked.append((round(fx, 2), round(fy, 2)))
        self.page = "settings"

    def click(self, i):
        raise AssertionError("listeden tıklanmamalı")


class VisualDecider:
    supports_images = True
    names = ["fake"]

    def __init__(self, obs, visible=0.95, zoom_visible=0.2, zoom_box=None):
        self.obs, self.visible, self.calls = obs, visible, []
        self.zoom_visible = zoom_visible
        self.zoom_box = zoom_box or {"left": 0, "top": 0, "right": 0.1, "bottom": 0.1}
        self.zooms = []

    def decide(self, state, questions, images=None):
        if "next" not in questions:  # yakından bakış: sadece "var mı?" + Box
            self.zooms.append((state, images[state["screen"]]))
            return Decision({"target_visible": Answer("noul", self.zoom_visible, 0.9),
                             "target": Answer("box", self.zoom_box, 0.8)}, "f", 0)
        self.calls.append((state, questions, images))
        if self.obs.page == "settings":
            return Decision({"next": Answer("choice", "done", 0.9), "done": Answer("noul", 0.95, 0.9),
                             "target_visible": Answer("noul", 0.1, 0.9),
                             "target": Answer("box", {"left": 0, "top": 0, "right": 0.1, "bottom": 0.1}, 0)}, "f", 0)
        return Decision({"next": Answer("choice", VISUAL_KEY, 0.8), "done": Answer("noul", 0.05, 0.9),
                         "target_visible": Answer("noul", self.visible, 0.9),
                         "target": Answer("box", {"left": 0.8, "top": 0.1, "right": 0.9, "bottom": 0.2}, 0.7)}, "f", 0)


def test_reflex_clicks_what_it_sees_when_the_window_has_no_readable_items():
    obs = WebViewObserver()
    dec = VisualDecider(obs)
    res = ReflexLoop(obs, dec, settle=0).run("Ayarlar'ı aç")
    assert res.status == "clicked" and obs.clicked == [(0.85, 0.15)]  # kutunun ortası
    state, questions, images = dec.calls[0]
    assert isinstance(questions["target"], Box) and isinstance(questions["target_visible"], Noul)
    assert VISUAL_KEY in questions["next"].criteria and state["screen"] in images
    assert Image.open(__import__("io").BytesIO(images[state["screen"]])).format == "JPEG"
    assert obs.goal == "Ayarlar'ı aç"  # çerçeve süzgeci hedefi bilsin


def test_reflex_zooms_in_when_unsure_and_clicks_the_close_up_box():
    """Kullanıcının ekranı: 3440x1440 küçülünce Jev 'var mı? 0.68' dedi. Kutunun çevresine yakından bakınca emin."""
    obs = WebViewObserver()
    dec = VisualDecider(obs, visible=0.68, zoom_visible=0.92,
                        zoom_box={"left": 0.4, "top": 0.275, "right": 0.6, "bottom": 0.475})
    lines = []
    res = ReflexLoop(obs, dec, settle=0, emit=lines.append).run("Projelerdeki ilk projeye tıkla")
    assert res.status == "clicked" and obs.clicked == [(0.85, 0.15)]
    crop = Image.open(__import__("io").BytesIO(dec.zooms[0][1]))
    assert crop.size == (360, 320)  # pencerenin %30 x %40'ı, asıl çözünürlükte (küçültülmeden)
    assert any("yakından: var mı? 0.92" in ln for ln in lines)


def test_reflex_gives_up_when_close_up_also_unsure():
    obs = WebViewObserver()
    res = ReflexLoop(obs, VisualDecider(obs, visible=0.6, zoom_visible=0.3), settle=0).run("Ayarlar'ı aç")
    assert res.status == "needs_help" and "yakından 0.30" in res.reason and obs.clicked == []


def test_reflex_does_not_trust_box_when_target_not_visible():
    obs = WebViewObserver()
    res = ReflexLoop(obs, VisualDecider(obs, visible=0.3), settle=0).run("Ayarlar'ı aç")
    assert res.status == "needs_help" and "bulunamadı" in res.reason and obs.clicked == []


def test_reflex_falls_back_to_text_when_image_request_fails():
    class Dec:
        supports_images = True
        names = ["f"]

        def __init__(self):
            self.visual_calls = self.text_calls = 0

        def decide(self, state, questions, images=None):
            if images:
                self.visual_calls += 1
                raise DeciderError("jev HTTP 400: images are not turned on")
            self.text_calls += 1
            assert isinstance(state, str) and VISUAL_KEY not in questions["next"].criteria
            return Decision({"next": Answer("choice", "stuck", 0.9), "done": Answer("noul", 0.0, 1)}, "f", 0)

    d = Dec()
    res = ReflexLoop(WebViewObserver(), d, settle=0).run("Ayarlar'ı aç")
    assert res.status == "needs_help" and d.visual_calls == 1 and d.text_calls == 1


def test_reflex_clicks_seen_target_even_when_jev_picks_stuck():
    """Kullanıcının gördüğü hata: ağaçta sadece 'Document' var, Jev 'stuck' dedi ama hedef ekranda görünüyordu."""
    obs = WebViewObserver()
    dec = VisualDecider(obs)
    first = dec.decide

    def stuck_first(state, questions, images=None):
        d = first(state, questions, images)
        if obs.page == "home":
            d.answers["next"] = Answer("choice", "stuck", 0.5)
        return d

    dec.decide = stuck_first
    lines = []
    res = ReflexLoop(obs, dec, settle=0, emit=lines.append).run("Projelerdeki ilk projeye tıkla")
    assert res.status == "clicked" and obs.clicked == [(0.85, 0.15)]
    assert "[görsel]" in lines[0] and "ekranda var mı? 0.95" in lines[0]


def test_reflex_says_why_it_cannot_look():
    obs = WebViewObserver()

    class TextOnly:
        supports_images = False
        names = ["f"]

        def decide(self, state, questions, images=None):
            assert images is None
            return Decision({"next": Answer("choice", "stuck", 0.5), "done": Answer("noul", 0.0, 1)}, "f", 0)

    lines = []
    res = ReflexLoop(obs, TextOnly(), settle=0, emit=lines.append).run("Ayarlar'ı aç")
    assert res.status == "needs_help"
    assert "ekrana bakılmıyor" in lines[0] and "jev_images" in lines[0] and "[metin]" in lines[1]


def test_image_check_reports_box_position():
    def handler(req):
        body = json.loads(req.content)
        assert set(body["questions"]) == {"has_red", "has_green", "where"} and body["assets"]
        return httpx.Response(200, json={"answers": {
            "has_red": {"type": "noul", "noul": 0.97}, "has_green": {"type": "noul", "noul": 0.04},
            "where": {"type": "box", "box": {"left": 0.56, "top": 0.21, "right": 0.84, "bottom": 0.54},
                      "confidence": 0.9}}})

    r = image_check(Decider([jev(handler)]))
    assert r["status"] == "ok" and "doğru yerde" in r["detail"]

    def wrong(req):
        return httpx.Response(200, json={"answers": {
            "has_red": {"type": "noul", "noul": 0.97}, "has_green": {"type": "noul", "noul": 0.04},
            "where": {"type": "box", "box": {"left": 0.0, "top": 0.6, "right": 0.3, "bottom": 0.9}}}})

    assert image_check(Decider([jev(wrong)]))["status"] == "warn"
    cfg = make_config({"fast": ["a:m"]})
    assert image_check(Decider([LLMEngine(None)]))["status"] == "off" and cfg


def test_reflex_tool_states_failure_plainly(monkeypatch):
    # Kullanıcının ekranı: reflex needs_help döndü ama model "JEV çalışıyor, başardı" dedi.
    from types import SimpleNamespace

    from alice.reflex import loop as loop_mod
    from alice.reflex import observers
    from alice.reflex.loop import ReflexResult
    from alice.tools.meta import Reflex

    monkeypatch.setattr(observers, "DesktopObserver", lambda ctx, title, *a, **k: SimpleNamespace())
    from alice.reflex import agent as agent_mod

    monkeypatch.setattr(agent_mod, "ScreenAgent", lambda *a, **k: SimpleNamespace(
        run=lambda goal, **kw: ReflexResult("needs_help", 1, "düşük güven (0.26)")))
    monkeypatch.setattr(agent_mod, "make_translator", lambda r: None)
    monkeypatch.setattr(loop_mod, "make_text_writer", lambda r: None)
    ctx = SimpleNamespace(decider=object(), router=object(), config=SimpleNamespace(decider={}), emit=lambda s: None)
    out = Reflex().run(ctx, goal="Ayarlar'ı aç", target="desktop")
    assert "YAPILAMADI" in out.text and "başardı deme" in out.text


def test_single_click_goal_finishes_after_the_click():
    """Kullanıcının gördüğü: doğru projeye tıkladı (var mı? 0.96) ama 'bitti mi?' düşük kaldığı için aynı yere üç kez
    tıklayıp 'yapılamadı' dedi. Tek tıklamalık hedefte tıklayınca biter (sonucu beyin doğrular)."""
    from alice.reflex.loop import single_click_goal

    assert single_click_goal("Projeler kısmındaki ilk projeye tıkla")
    assert not single_click_goal("arama kutusuna tıkla ve kedi yaz")
    assert not single_click_goal("Ayarlar'a tıkla sonra Ses'e tıkla")
    obs = WebViewObserver()
    dec = VisualDecider(obs)
    first = dec.decide

    def never_done(state, questions, images=None):
        d = first(state, questions, images)
        if "next" in questions:
            obs.page = "home"  # ekran değişmiş gibi görünmese de
            d = Decision({**d.answers, "next": Answer("choice", VISUAL_KEY, 0.8), "done": Answer("noul", 0.05, 0.9)},
                         "f", 0)
        return d

    dec.decide = never_done
    res = ReflexLoop(obs, dec, settle=0).run("Projelerdeki ilk projeye tıkla")
    assert res.status == "clicked" and obs.clicked == [(0.85, 0.15)]


def test_reflex_tool_says_click_is_unverified(monkeypatch):
    from types import SimpleNamespace

    from alice.reflex import loop as loop_mod
    from alice.reflex import observers
    from alice.reflex.loop import ReflexResult
    from alice.tools.meta import Reflex

    monkeypatch.setattr(observers, "DesktopObserver", lambda ctx, title, *a, **k: SimpleNamespace())
    from alice.reflex import agent as agent_mod

    monkeypatch.setattr(agent_mod, "ScreenAgent", lambda *a, **k: SimpleNamespace(
        run=lambda goal, **kw: ReflexResult("clicked", 1, "tıklandı")))
    monkeypatch.setattr(agent_mod, "make_translator", lambda r: None)
    monkeypatch.setattr(loop_mod, "make_text_writer", lambda r: None)
    ctx = SimpleNamespace(decider=object(), router=object(), config=SimpleNamespace(decider={}), emit=lambda s: None)
    out = Reflex().run(ctx, goal="ilk projeye tıkla", target="desktop").text
    assert "doğrulanmadı" in out and "ekran görüntüsüyle" in out
