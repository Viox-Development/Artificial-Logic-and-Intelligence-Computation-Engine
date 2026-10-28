from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from alice.config import Config
    from alice.decider import Decider
    from alice.providers.router import Router

# Risk sınıfları izin sistemi tarafından kullanılır.
READ, WRITE, EXEC, COMPUTER, NETWORK, DANGER = "read", "write", "exec", "computer", "network", "danger"
# Dışa dönük eylem: başka birine giden mesaj, gönderi, e-posta. Dosya düzenlemenin tersine geri alınamaz;
# bu yüzden auto modda da sorulur ([agent] confirm_outward = false ile kapatılabilir).
SEND = "send"


@dataclass
class ToolResult:
    text: str
    images: list[str] = field(default_factory=list)  # base64 PNG
    is_error: bool = False
    diff: str = ""  # arayüzde gösterilecek unified diff (modele gitmez)


class ToolError(Exception):
    """Araç içinde kullanıcıya/modele anlamlı bir hata mesajı döndürmek için."""


@dataclass
class ToolContext:
    cwd: Path
    config: "Config"
    router: "Router | None" = None
    decider: "Decider | None" = None
    state: dict[str, Any] = field(default_factory=dict)  # araçlar arası paylaşılan durum (tarayıcı, süreçler, todo)
    emit: Callable[[str], None] = lambda s: None

    def cancelled(self) -> bool:
        """Kullanıcı Durdur'a bastı mı? Uzun döngüsü olan araçlar bunu ara ara kontrol etmeli."""
        agent = self.state.get("agent")
        cancel = getattr(agent, "cancel", None)
        return bool(cancel is not None and cancel.is_set())

    def resolve(self, path: str) -> Path:
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.cwd / p
        return p.resolve()


class DesktopLock:
    """Ekran, fare/klavye ve kullanıcının tarayıcısı tek: aynı anda tek sohbet kullanır. Alan sohbet turu bitene kadar
    tutar, yarım kalan bir ekran işinin arasına başka sohbet girmesin. Sahip ToolContext'tir (alt ajanlar aynı bağlamı
    paylaşır, kendi sohbetlerini bekletmezler).

    Sahip değişince önceki sahibin tarayıcı bağlantısı bırakılır: tarayıcı köprüsü tek istemciye hizmet verir, eski
    bağlantı sessizce ölü kalır ve sonraki çağrısı takılırdı."""

    def __init__(self) -> None:
        import threading

        self._cond = threading.Condition()
        self.owner: ToolContext | None = None
        self.last: ToolContext | None = None

    def held_by(self, ctx: ToolContext) -> bool:
        return self.owner is ctx

    def acquire(self, ctx: ToolContext, cancelled: Callable[[], bool], on_wait: Callable[[], None] | None = None) -> bool:
        """Sıra gelene kadar bekle; durdurulursa False."""
        with self._cond:
            told = False
            while self.owner is not None and self.owner is not ctx:
                if cancelled():
                    return False
                if not told and on_wait is not None:
                    on_wait()
                    told = True
                self._cond.wait(0.25)
            prev, self.owner, self.last = self.last, ctx, ctx
        if prev is not None and prev is not ctx:
            b = prev.state.get("browser")
            if b is not None and hasattr(b, "abort"):
                b.abort("handoff")
        return True

    def release(self, ctx: ToolContext) -> None:
        with self._cond:
            if self.owner is ctx:
                self.owner = None
                self._cond.notify_all()


class Tool:
    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    risk: str = READ
    # True: yan etkisi olmayan, thread'e bağlı nesne kullanmayan araç; art arda gelirse paralel çalışır.
    parallel_safe: bool = False
    # True: sonucu dışarıdan gelen içerik (web sayfası, mesaj, e-posta, MCP). Modele "güvenilmeyen veri" diye
    # işaretlenmiş gider; o turda komut çalıştırma ve dışarı mesaj göndermek auto modda da onaya bağlanır.
    untrusted: bool = False

    def risk_for(self, args: dict[str, Any]) -> str:
        return self.risk

    def summary(self, args: dict[str, Any]) -> str:
        """Ekranda gösterilecek kısa özet."""
        s = json.dumps(args, ensure_ascii=False)
        return s if len(s) < 160 else s[:157] + "..."

    def run(self, ctx: ToolContext, **args: Any) -> ToolResult:
        raise NotImplementedError

    def spec(self) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


def check_args(tool: Tool, args: dict[str, Any]) -> str:
    """Argümanları aracın şemasına göre kabaca denetle (zorunlu alanlar, enum). Sorun varsa modelin kendini
    düzeltebileceği bir hata metni, yoksa "" döner. Claude Code ve Codex de hatalı çağrıyı aracı çalıştırmadan,
    neyin eksik/yanlış olduğunu ve geçerli değerleri söyleyerek geri çevirir."""
    schema = tool.parameters if isinstance(tool.parameters, dict) else {}
    props = schema.get("properties") or {}
    missing = [k for k in schema.get("required") or [] if args.get(k) is None]  # "" geçerli: new_string="" siler
    if missing:
        return f"{tool.name}: eksik zorunlu parametre: {', '.join(missing)}. {_param_hint(props, missing)}"
    for k, v in args.items():
        enum = (props.get(k) or {}).get("enum")
        if enum and isinstance(v, str) and v not in enum:
            return f"{tool.name}: {k}='{v}' geçersiz. Geçerli değerler: {', '.join(map(str, enum))}."
    return ""


def unknown_params(tool: Tool, args: dict[str, Any]) -> str:
    """Şemada olmayan parametreler için hata metni (TypeError'ı anlaşılır kılmak için)."""
    props = (tool.parameters or {}).get("properties") or {}
    extra = [k for k in args if k not in props]
    if not extra or not props:
        return ""
    return (f"{tool.name}: bilinmeyen parametre: {', '.join(extra)}. Geçerli parametreler: "
            f"{', '.join(props)}.")


def _param_hint(props: dict[str, Any], names: list[str]) -> str:
    hints = [f"{n}: {props[n]['description']}" for n in names if (props.get(n) or {}).get("description")]
    return " ".join(hints)


def obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


def make_diff(path: str, before: str, after: str, context: int = 3, max_lines: int = 400) -> str:
    import difflib

    lines = list(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                      fromfile=f"a/{path}", tofile=f"b/{path}", n=context, lineterm=""))
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... ({len(lines) - max_lines} satır daha)"]
    return "\n".join(lines)


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    return f"{text[:head]}\n\n... [{len(text) - limit} karakter kesildi] ...\n\n{text[-tail:]}"
