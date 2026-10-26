"""System One karar motoru (Jev ve yedeği).

Jev metin üretmez; bir durumu (state) ve 1-8 adet tipli soruyu alır, her soru için
etiket + olasılık döndürür. Bilgisayar kullanımında işe yarayan kısım şu: ekrandaki
tıklanabilir şeyleri ID'li aday listesine çevirirsin, Jev ~100 ms'de hangisinin
seçileceğini söyler, sen de o adayı uygularsın. Uzun metin üretimi ve planlama
yine büyük modelde (System 2) kalır.

Soru tipleri (Jev ile aynı isimler):
  Choice(instructions, criteria={etiket: açıklama})  -> tek etiket
  Noul(instructions)                                   -> 0..1 arası "evet" olasılığı
  Score(instructions, levels={1: "...", 5: "..."})     -> seviye (beklenen değer)
  Box(instructions)                                    -> görüntüde yer: {left, top, right, bottom} (0-1)

Görseller (Jev 1.13+, Box Jev 1.14+; TypeSafe'in "Danger Zone" deneysel özelliği): decide(..., images={ref: bytes})
ile gönderilir; ref, state'te ya da soru metninde görselin geçtiği yerdir. Box her zaman bir kutu döndürür ("yok"
cevabı yoktur): yanına aynı istekte "var mı?" diye bir Noul sorulur. Görsel/Box desteklemeyen motorlar (LLM yedeği)
böyle bir istekte atlanır. Ayrıntı: https://docs.typesafe.ai/go/images.md

Zincir: config [decider].chain, örn. ["jev", "jev-openrouter", "llm:fast"]. Jev anahtarı yoksa ya da
hata verirse aynı soru ücretsiz hızlı bir LLM'e JSON formatında sorulur.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from alice.config import Config
from alice.providers.router import AllProvidersFailed, Router


@dataclass
class Choice:
    instructions: str
    criteria: dict[str, str]


@dataclass
class Noul:
    instructions: str
    # İsteğe bağlı: "true" ve "false"un somut tarifleri (TypeSafe: false'u "değil X" yerine bir durum olarak yaz).
    criteria: dict[str, str] | None = None


@dataclass
class Score:
    instructions: str
    levels: dict[int, str]


@dataclass
class Box:
    """Görüntüde bir şeyin yeri. Cevap: {"left","top","right","bottom"}, görüntünün eni/boyunun 0-1 kesri."""
    instructions: str


Question = Choice | Noul | Score | Box
IMAGES_HEADER = "TypeSafe-Danger-Zone-Images"
IMAGE_MIN, IMAGE_MAX = 280, 1568  # piksel: Jev'in kabul ettiği kenar aralığı (dışındaki görüntü reddedilir)


def prepare_image(img: Any, quality: int = 85) -> bytes:
    """PIL görüntüsünü Jev'in istediği hale getir: uzun kenar en fazla 1568, kısa kenar en az 280 (beyazla
    doldurulur, oran bozulmaz), RGB, düz (baseline) JPEG. Bu sınırların dışındaki görüntüyü API reddeder."""
    import io

    from PIL import Image as PILImage

    img = img.convert("RGB")
    if max(img.size) > IMAGE_MAX:
        img = img.copy()
        img.thumbnail((IMAGE_MAX, IMAGE_MAX))
    w, h = img.size
    if min(w, h) < IMAGE_MIN:
        canvas = PILImage.new("RGB", (max(w, IMAGE_MIN), max(h, IMAGE_MIN)), (255, 255, 255))
        canvas.paste(img, (0, 0))
        img = canvas
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def padded_fraction(img_size: tuple[int, int]) -> tuple[float, float]:
    """prepare_image küçük görüntüyü sağdan/alttan doldurduysa: Box kesirlerini asıl görüntüye çevirmek için çarpan."""
    w, h = img_size
    scale = min(1.0, IMAGE_MAX / max(w, h))
    sw, sh = w * scale, h * scale
    return max(sw, IMAGE_MIN) / sw, max(sh, IMAGE_MIN) / sh


@dataclass
class Answer:
    type: str
    value: Any
    confidence: float
    probabilities: dict[str, float] = field(default_factory=dict)


@dataclass
class Decision:
    answers: dict[str, Answer]
    engine: str
    latency: float

    def __getitem__(self, key: str) -> Answer:
        return self.answers[key]


class DeciderError(Exception):
    def __init__(self, message: str, cooldown: float = 60.0):
        super().__init__(message)
        self.cooldown = cooldown


class Engine(Protocol):
    name: str

    def decide(self, state: Any, questions: dict[str, Question], images: dict[str, bytes] | None = None) -> Decision:
        ...


def _needs_images(questions: dict[str, Question], images: dict[str, bytes] | None) -> bool:
    return bool(images) or any(isinstance(q, Box) for q in questions.values())


def _image_format(data: bytes) -> str:
    return "png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "jpg"


def _validate(questions: dict[str, Question]) -> None:
    # Jev'de soru sayısı sınırı yok (istek bağlam sınırına sığmalı); LLM yedeği için makul bir üst sınır.
    if not 1 <= len(questions) <= 16:
        raise ValueError("System One'a 1 ile 16 arasında soru sorulur")
    for k, q in questions.items():
        if isinstance(q, Choice) and len(q.criteria) < 2:
            raise ValueError(f"{k}: Choice en az 2 seçenek ister")


def _score_as_choice(q: Score) -> Choice:
    return Choice(q.instructions, {str(k): v for k, v in sorted(q.levels.items())})


def _expected(probs: dict[str, float]) -> float:
    total = sum(probs.values()) or 1.0
    return sum(float(k) * v for k, v in probs.items()) / total


# --------------------------------------------------------------------------- Jev
class JevEngine:
    """TypeSafe Jev REST istemcisi: POST {jev_url} {model, state, questions}.

    Score soruları güvenli olsun diye Jev'e Choice olarak gönderilir (seviyeler
    etiket olur) ve beklenen değer olasılıklardan hesaplanır; Choice ve Noul
    formatı resmi quickstart ile aynıdır. Cevap ayrıştırıcı toleranslıdır çünkü
    API hâlâ erken erişimde ve alan adları değişebilir.
    """

    def __init__(self, url: str, model: str, api_key: str, client: httpx.Client | None = None, name: str = "jev",
                 images: bool = False):
        self.name = name
        self.url = url
        self.model = model
        self.api_key = api_key
        self.images = images  # görsel + Box (TypeSafe'in deneysel "Danger Zone" özelliği): config [decider] jev_images
        self._client = client or httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0))

    def close(self) -> None:
        self._client.close()

    def decide(self, state: Any, questions: dict[str, Question], images: dict[str, bytes] | None = None) -> Decision:
        import base64

        _validate(questions)
        visual = _needs_images(questions, images)
        if visual and not self.images:
            raise DeciderError(f"{self.name}: görsel kapalı", 0.0)
        wire: dict[str, Any] = {}
        for k, q in questions.items():
            if isinstance(q, Noul):
                wire[k] = {"type": "noul", "instructions": q.instructions}
                if q.criteria:
                    wire[k]["criteria"] = dict(q.criteria)
            elif isinstance(q, Box):
                wire[k] = {"type": "box", "instructions": q.instructions}
            else:
                c = _score_as_choice(q) if isinstance(q, Score) else q
                wire[k] = {"type": "choice", "instructions": c.instructions, "criteria": c.criteria}
        body: dict[str, Any] = {"model": self.model, "state": state, "questions": wire}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        if visual:
            headers[IMAGES_HEADER] = "true"
        if images:
            body["assets"] = {ref: {"type": "image", "format": _image_format(data),
                                    "data_base64": base64.b64encode(data).decode("ascii")}
                              for ref, data in images.items()}
        t0 = time.monotonic()
        try:
            r = self._client.post(self.url, headers=headers, json=body)
        except httpx.HTTPError as e:
            raise DeciderError(f"jev ağ hatası: {e}") from e
        if r.status_code != 200:
            # 402 = kredi yok (OpenRouter), 401/403 = anahtar/erişim yok: bunlar bir dakikada düzelmez.
            cooldown = 3600.0 if r.status_code in (401, 402, 403) else 60.0
            raise DeciderError(f"{self.name} HTTP {r.status_code}: {r.text[:300]}", cooldown)
        data = r.json()
        raw = data.get("answers", data)
        answers = {k: self._parse(q, raw.get(k)) for k, q in questions.items()}
        return Decision(answers, self.name, time.monotonic() - t0)

    @staticmethod
    def _parse(q: Question, a: Any) -> Answer:
        if a is None:
            raise DeciderError("jev cevabında soru eksik")
        if isinstance(q, Box):
            b = a.get("box") if isinstance(a, dict) else None
            if not isinstance(b, dict) or not all(k in b for k in ("left", "top", "right", "bottom")):
                raise DeciderError(f"jev box cevabı çözülemedi: {a!r}"[:200])
            box = {k: min(1.0, max(0.0, float(b[k]))) for k in ("left", "top", "right", "bottom")}
            conf = a.get("confidence")
            return Answer("box", box, float(conf) if isinstance(conf, (int, float)) else 0.0)
        if isinstance(q, Noul):
            if isinstance(a, (int, float)):
                p = float(a)
            else:
                p = float(next(a[k] for k in ("value", "probability", "answer", "noul") if k in a))
            return Answer("noul", p, max(p, 1 - p), {"yes": p, "no": 1 - p})
        probs = {str(k): float(v) for k, v in (a.get("probabilities") or {}).items()} if isinstance(a, dict) else {}
        label = None
        if isinstance(a, dict):
            for key in ("choice", "label", "value", "answer", "selected"):
                if key in a and not isinstance(a[key], (dict, list)):
                    label = str(a[key])
                    break
        elif isinstance(a, str):
            label = a
        if label is None and probs:
            label = max(probs, key=probs.get)
        if label is None:
            raise DeciderError(f"jev cevabı çözülemedi: {a!r}"[:200])
        conf = float(a.get("confidence", probs.get(label, 0.0))) if isinstance(a, dict) else probs.get(label, 0.0)
        if isinstance(q, Score):
            value = _expected(probs) if probs else float(label)
            return Answer("score", value, conf, probs)
        return Answer("choice", label, conf, probs)


# ------------------------------------------------------------------ LLM emülasyonu
_SYSTEM = (
    "You are a System One decision engine. You never explain, you only decide. "
    "Read the STATE, answer every QUESTION and reply with ONE JSON object only, no prose, no code fences."
)


class LLMEngine:
    """Jev'in ücretsiz yedeği: hızlı bir LLM'e aynı soruları JSON olarak sorar."""

    def __init__(self, router: Router, role: str = "fast"):
        self.router = router
        self.role = role
        self.name = f"llm:{role}"

    def decide(self, state: Any, questions: dict[str, Question], images: dict[str, bytes] | None = None) -> Decision:
        _validate(questions)
        if _needs_images(questions, images):
            raise DeciderError(f"{self.name}: görsel/Box desteklemiyor", 0.0)
        if not isinstance(state, str):
            state = json.dumps(state, ensure_ascii=False, indent=1)
        lines = []
        shape = {}
        for k, q in questions.items():
            if isinstance(q, Choice):
                opts = "\n".join(f'    "{lab}": {desc}' for lab, desc in q.criteria.items())
                lines.append(f'- "{k}" (choice): {q.instructions}\n  Options:\n{opts}')
                shape[k] = {"choice": "<one option key>", "confidence": "<0..1>"}
            elif isinstance(q, Score):
                lv = "\n".join(f"    {lev}: {desc}" for lev, desc in sorted(q.levels.items()))
                lines.append(f'- "{k}" (score): {q.instructions}\n  Levels:\n{lv}')
                shape[k] = {"score": "<level number>", "confidence": "<0..1>"}
            else:
                crit = getattr(q, "criteria", None) or {}
                extra = "".join(f"\n  {side}: {desc}" for side, desc in crit.items())
                lines.append(f'- "{k}" (yes/no): {q.instructions}{extra}')
                shape[k] = {"yes_probability": "<0..1>"}
        user = (
            f"STATE:\n{state}\n\nQUESTIONS:\n" + "\n".join(lines)
            + f"\n\nReply exactly in this JSON shape:\n{json.dumps(shape)}"
        )
        msgs = [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]
        t0 = time.monotonic()
        try:
            res = self.router.chat(self.role, msgs, temperature=0, max_tokens=600,
                                   response_format={"type": "json_object"})
        except AllProvidersFailed:
            # Bazı modeller response_format'ı reddediyor, bir de onsuz dene.
            res = self.router.chat(self.role, msgs, temperature=0, max_tokens=600)
        data = extract_json(res.text)
        if not isinstance(data, dict):
            raise DeciderError(f"LLM decider JSON döndürmedi: {res.text[:200]}")
        answers = {k: self._parse(q, data.get(k)) for k, q in questions.items()}
        return Decision(answers, f"{self.name} ({res.provider}:{res.model})", time.monotonic() - t0)

    @staticmethod
    def _parse(q: Question, a: Any) -> Answer:
        if isinstance(q, Noul):
            p = a.get("yes_probability", a.get("value")) if isinstance(a, dict) else a
            if isinstance(p, bool):
                p = 1.0 if p else 0.0
            p = _num(p, 0.5)
            return Answer("noul", p, max(p, 1 - p), {"yes": p, "no": 1 - p})
        if isinstance(a, dict):
            raw = a.get("choice", a.get("score", a.get("value")))
            conf = _num(a.get("confidence"), 0.6)
        else:
            raw, conf = a, 0.6
        if isinstance(q, Score):
            v = _num(raw, None)
            if v is None:
                raise DeciderError(f"score çözülemedi: {a!r}")
            return Answer("score", v, conf, {})
        label = str(raw).strip() if raw is not None else ""
        if label not in q.criteria:
            # Model açıklamayı ya da tırnaklı hali döndürmüş olabilir.
            norm = {k.lower(): k for k in q.criteria}
            label = norm.get(label.lower().strip('"\''), "")
            if not label:
                raise DeciderError(f"geçersiz seçenek: {raw!r}")
        return Answer("choice", label, conf, {label: conf})


def _num(v: Any, default: float | None) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return min(max(f, 0.0), 1.0) if default is not None and default <= 1 else f


def extract_json(text: str) -> Any:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


# ------------------------------------------------------------------------ zincir
class Decider:
    def __init__(self, engines: list[Engine], on_event=None):
        if not engines:
            raise ValueError("en az bir karar motoru gerekli")
        self.engines = engines
        self.on_event = on_event or (lambda s: None)
        self._broken: dict[str, float] = {}

    @classmethod
    def from_config(cls, config: Config, router: Router, on_event=None) -> "Decider":
        dc = config.decider
        engines: list[Engine] = []
        for item in dc.get("chain", ["llm:fast"]):
            if item == "jev":
                key = os.environ.get(dc.get("jev_api_key_env", "TYPESAFE_API_KEY"))
                if key:
                    engines.append(JevEngine(dc.get("jev_url"), dc.get("jev_model", "jev-latest"), key,
                                             images=bool(dc.get("jev_images", True))))
            elif item == "jev-openrouter":
                # Aynı Jev, OpenRouter hesabından faturalanır. Ücretsiz (:free) sürümü yok, kredi gerekir.
                key = os.environ.get(dc.get("openrouter_jev_api_key_env", "OPENROUTER_API_KEY"))
                if key:
                    engines.append(JevEngine(dc.get("openrouter_jev_url", "https://openrouter.ai/api/v1/systemone"),
                                             dc.get("openrouter_jev_model", "~typesafe/jev-latest"), key,
                                             name="jev-openrouter"))
            elif item.startswith("llm:"):
                engines.append(LLMEngine(router, item.split(":", 1)[1]))
        if not engines:
            engines.append(LLMEngine(router, "fast"))
        return cls(engines, on_event)

    @property
    def names(self) -> list[str]:
        return [e.name for e in self.engines]

    def close(self) -> None:
        """Kendi HTTP istemcisi olan motorları kapat (yönlendirici paylaşımlı; onu sahibi kapatır)."""
        for e in self.engines:
            close = getattr(e, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass

    @property
    def supports_images(self) -> bool:
        """Görsel/Box sorabilecek, şu an bozuk sayılmayan bir motor var mı (TypeSafe anahtarı + jev_images)."""
        now = time.monotonic()
        return any(getattr(e, "images", False) and self._broken.get(f"{e.name}:img", 0) <= now
                   and self._broken.get(e.name, 0) <= now for e in self.engines)

    def decide(self, state: Any, questions: dict[str, Question], images: dict[str, bytes] | None = None) -> Decision:
        # Geçersiz soru çağıranın hatası: motorlara gitmeden düşsün. Önceden her motor bu yüzden "bozuk" sayılıp
        # soğumaya alınıyordu ve sonraki geçerli sorular da bir dakika boyunca cevapsız kalıyordu.
        _validate(questions)
        visual = _needs_images(questions, images)
        errors = []
        for e in self.engines:
            if self._broken.get(e.name, 0) > time.monotonic():
                continue
            if visual and not getattr(e, "images", False):
                continue  # görsel soramayan motor (LLM yedeği): bozuk sayma, atla
            if visual and self._broken.get(f"{e.name}:img", 0) > time.monotonic():
                continue
            try:
                return e.decide(state, questions, images) if visual else e.decide(state, questions)
            except Exception as err:  # noqa: BLE001 - beklenmeyen cevap (JSON değil, eksik anahtar) da sıradaki motora geçsin
                errors.append(f"{e.name}: {err}")
                self.on_event(f"decider {e.name} başarısız, sıradakine geçiliyor")
                # Görsel istek reddedildiyse (400: görsel izni yok, görüntü kabul edilmedi) sadece görsel kısmı
                # soğusun; aynı motor metin sorularını yine cevaplasın.
                key = f"{e.name}:img" if visual and "HTTP 4" in str(err) else e.name
                self._broken[key] = time.monotonic() + getattr(err, "cooldown", 60.0)
        raise DeciderError("; ".join(errors) or ("görsel soru sorabilecek karar motoru yok (TypeSafe anahtarı gerekli)"
                                                 if visual else "kullanılabilir karar motoru yok"))


# ------------------------------------------------------------------- görsel testi
TEST_RECT = (0.55, 0.2, 0.85, 0.55)  # left, top, right, bottom: test görüntüsündeki kırmızı dikdörtgen


def image_check(decider: "Decider") -> dict[str, Any]:
    """Jev görselleri görüyor ve Box doğru yeri buluyor mu? Bilinen bir test görüntüsüyle tek istek (Kurulum →
    Hepsini dene). status: ok | warn (çalışıyor ama cevap şüpheli) | fail | off (görsel motoru yok)."""
    from PIL import Image as PILImage
    from PIL import ImageDraw

    if not decider.supports_images:
        return {"status": "off", "detail": "TypeSafe anahtarı yok ya da [decider] jev_images kapalı"}
    w, h = 640, 400
    img = PILImage.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(img)
    l, t, r, b = TEST_RECT
    d.rectangle([l * w, t * h, r * w, b * h], fill=(220, 30, 30))
    d.ellipse([60, 220, 200, 360], fill=(40, 80, 220))
    ref = "alice-test-3b9e5c71"
    try:
        dec = decider.decide({"picture": ref}, {
            "has_red": Noul("Is there a red rectangle in `picture`?"),
            "has_green": Noul("Is there a green triangle in `picture`?"),
            "where": Box("the red rectangle in `picture`"),
        }, images={ref: prepare_image(img)})
    except DeciderError as e:
        return {"status": "fail", "detail": str(e)[:200]}
    box = dec["where"].value
    cx, cy = (box["left"] + box["right"]) / 2, (box["top"] + box["bottom"]) / 2
    inside = l <= cx <= r and t <= cy <= b
    red, green = dec["has_red"].value, dec["has_green"].value
    detail = (f"kırmızı var mı {red:.2f}, yeşil var mı {green:.2f}, kutu "
              f"{box['left']:.2f}-{box['right']:.2f} × {box['top']:.2f}-{box['bottom']:.2f} "
              f"({'doğru yerde' if inside else 'YANLIŞ yerde'}) · {dec.latency * 1000:.0f} ms, {dec.engine}")
    ok = inside and red >= 0.7 and green <= 0.3
    return {"status": "ok" if ok else "warn", "detail": detail}
