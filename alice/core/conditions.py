"""Akış koşulları: bir öğe bildirime değer mi?

Ucuzdan pahalıya sırayla bakılır, ilk tutmayan koşul öğeyi eler:

    [feed.when]
    any    = ["alice", "@everyone"]   # metinde en az biri geçmeli
    all    = ["dcs", "güncelleme"]    # hepsi geçmeli
    none   = ["bot", "reklam"]        # hiçbiri geçmemeli
    regex  = "\\b(acil|hemen)\\b"      # düzenli ifade (büyük/küçük harf duyarsız)
    fields = { "author.username" = ["ahmet", "ayse"], gameextrainfo = "*" }   # "*" = boş değil
    min    = { "statistics.viewCount" = 1000 }   # sayısal alt sınır
    max    = { "playtime_2weeks" = 60 }
    ask    = "Bu mesaj bana bir şey soruyor ya da acil bir şey mi?"   # anlamına göre: System One / hızlı model

Metin araması, şablonla üretilen başlık/gövde ile öğenin bütün metin alanları üzerinde yapılır ve
Türkçe büyük/küçük harfe dikkat eder ("İSTANBUL" ile "istanbul" eşleşir, "I" -> "ı").

`ask` en sona bırakıldı: bir model çağrısı olduğu için sadece diğer koşulları geçen öğe için sorulur.
Karar motoru yoksa ya da hata verirse öğe geçer (kurallardaki gibi: kaçırılan uyarı, fazladan
uyarıdan daha kötü).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


def tr_lower(s: str) -> str:
    """Türkçe küçük harf: 'I' -> 'ı', 'İ' -> 'i' (str.lower ikisini de yanlış yapıyor)."""
    return s.replace("I", "ı").replace("İ", "i").lower()


def _list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return [str(v)]


@dataclass
class Condition:
    any: list[str] = field(default_factory=list)
    all: list[str] = field(default_factory=list)
    none: list[str] = field(default_factory=list)
    regex: str = ""
    fields: dict[str, list[str]] = field(default_factory=dict)
    min: dict[str, float] = field(default_factory=dict)
    max: dict[str, float] = field(default_factory=dict)
    ask: str = ""

    def empty(self) -> bool:
        return not (self.any or self.all or self.none or self.regex or self.fields or self.min or self.max
                    or self.ask)

    def summary(self) -> str:
        parts = []
        if self.any:
            parts.append("biri: " + ", ".join(self.any))
        if self.all:
            parts.append("hepsi: " + ", ".join(self.all))
        if self.none:
            parts.append("hiçbiri: " + ", ".join(self.none))
        if self.regex:
            parts.append(f"regex: {self.regex}")
        for k, v in self.fields.items():
            parts.append(f"{k} = {' | '.join(v)}")
        for k, v in self.min.items():
            parts.append(f"{k} ≥ {v:g}")
        for k, v in self.max.items():
            parts.append(f"{k} ≤ {v:g}")
        if self.ask:
            parts.append(f"soru: {self.ask}")
        return "; ".join(parts) or "koşul yok"


def parse_condition(raw: Any, where: str = "when") -> Condition:
    """TOML tablosundan koşul. Bozuk alan ValueError (akış yüklenirken anlaşılır hataya çevrilir)."""
    if raw in (None, {}):
        return Condition()
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: tablo olmalı")
    known = {"any", "all", "none", "regex", "fields", "min", "max", "ask"}
    extra = set(raw) - known
    if extra:
        raise ValueError(f"{where}: bilinmeyen alan: {', '.join(sorted(extra))} (olanlar: {', '.join(sorted(known))})")
    regex = str(raw.get("regex", ""))
    if regex:
        try:
            re.compile(regex)
        except re.error as e:
            raise ValueError(f"{where}.regex geçersiz: {e}") from e

    def nums(name: str) -> dict[str, float]:
        tab = raw.get(name) or {}
        if not isinstance(tab, dict):
            raise ValueError(f"{where}.{name}: tablo olmalı, ör. {{ \"alan\" = 10 }}")
        try:
            return {str(k): float(v) for k, v in tab.items()}
        except (TypeError, ValueError) as e:
            raise ValueError(f"{where}.{name}: değerler sayı olmalı") from e

    fields_raw = raw.get("fields") or {}
    if not isinstance(fields_raw, dict):
        raise ValueError(f"{where}.fields: tablo olmalı")
    return Condition(any=_list(raw.get("any")), all=_list(raw.get("all")), none=_list(raw.get("none")),
                     regex=regex, fields={str(k): _list(v) for k, v in fields_raw.items()},
                     min=nums("min"), max=nums("max"), ask=str(raw.get("ask", "")))


def _leaves(data: Any, out: list[str], depth: int = 0) -> None:
    if depth > 6 or len(out) > 400:
        return
    if isinstance(data, str):
        out.append(data)
    elif isinstance(data, dict):
        for v in data.values():
            _leaves(v, out, depth + 1)
    elif isinstance(data, list):
        for v in data[:50]:
            _leaves(v, out, depth + 1)


def haystack(item: Any, *rendered: str) -> str:
    """Aranacak metin: şablondan çıkan başlık/gövde + öğenin bütün metin alanları, Türkçe küçük harf."""
    parts = [r for r in rendered if r]
    _leaves(item, parts)
    return tr_lower("\n".join(parts))


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def check(cond: Condition, item: Any, text: str, ask: Any = None) -> tuple[bool, str]:
    """(geçti mi, sebep). `text`: haystack() çıktısı. `ask(soru, metin) -> bool`: anlam sorusu için."""
    from alice.connect.runner import pick

    if cond.any:
        if not any(tr_lower(w) in text for w in cond.any):
            return False, "hiçbiri geçmiyor: " + ", ".join(cond.any)
    for w in cond.all:
        if tr_lower(w) not in text:
            return False, f"geçmiyor: {w}"
    for w in cond.none:
        if tr_lower(w) in text:
            return False, f"istenmeyen kelime: {w}"
    if cond.regex and not re.search(cond.regex, text, re.I):
        return False, f"regex tutmadı: {cond.regex}"
    for key, wanted in cond.fields.items():
        v = pick(item, key) if isinstance(item, (dict, list)) else None
        sv = "" if v is None else str(v)
        if wanted == ["*"]:
            if not sv.strip():
                return False, f"{key} boş"
        elif tr_lower(sv) not in {tr_lower(w) for w in wanted}:
            return False, f"{key} = {sv or '(boş)'} (istenen: {' | '.join(wanted)})"
    for key, lo in cond.min.items():
        n = _num(pick(item, key) if isinstance(item, (dict, list)) else None)
        if n is None or n < lo:
            return False, f"{key} = {n if n is not None else '?'} < {lo:g}"
    for key, hi in cond.max.items():
        n = _num(pick(item, key) if isinstance(item, (dict, list)) else None)
        if n is None or n > hi:
            return False, f"{key} = {n if n is not None else '?'} > {hi:g}"
    if cond.ask:
        if ask is None:
            return True, "soru soruldu sayılmadı (karar motoru yok)"
        try:
            ok = bool(ask(cond.ask, text))
        except Exception:  # noqa: BLE001 - karar motoru bozuksa öğeyi kaçırma
            return True, "karar motoru cevap vermedi, geçirildi"
        if not ok:
            return False, f"soru 'hayır' dedi: {cond.ask}"
    return True, "geçti"
