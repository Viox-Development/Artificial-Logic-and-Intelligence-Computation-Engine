"""Voice-commanded AI wingmen in DCS.

  parse(text) -> turns a Turkish command such as "İkinci, sağdaki SAM'i bastır" into a Command (None if not understood)
  send(cmd)   -> sends the command to the DCS hook (alice_wingmen_hook.lua) as a single UDP line; never blocks

Line format: ALICE1|fiil (verb)|sıra (index)|hedef (target)|yön (direction)|grup (group). The hook accepts only these
fixed fields and builds the mission Lua itself; text from the network is never run as Lua. Index: the player counts as
leader (1); "İkinci" (second) = the wingman closest to the player (or the first one in the [sims] dcs_wingmen list),
"Üçüncü" (third) = the next one; "Birinci" also picks the first one; 0 = all. If there is only one wingman, it is that
one whatever the index.
"""

from __future__ import annotations

import re
import socket
from dataclasses import dataclass
from typing import Any

PORT = 41231
VERBS = ("attack", "rejoin", "hold", "rtb")
TARGETS = ("sam", "air", "ground", "ship", "any")
DIRECTIONS = ("right", "left", "front", "behind", "any")
_GROUP_OK = re.compile(r"[\w \-]{0,40}", re.ASCII)

ORDINALS = ["hepsi", "Birinci", "İkinci", "Üçüncü", "Dördüncü", "Beşinci", "Altıncı", "Yedinci", "Sekizinci",
            "Dokuzuncu"]
VERB_TR = {"attack": "saldır", "rejoin": "yanıma gel", "hold": "bulunduğun yerde bekle", "rtb": "üsse dön"}
TARGET_TR = {"sam": "SAM", "air": "düşman uçağı", "ground": "kara hedefi", "ship": "gemi", "any": "en yakın düşman"}
DIR_TR = {"right": "sağdaki", "left": "soldaki", "front": "öndeki", "behind": "arkadaki", "any": ""}


@dataclass
class Command:
    verb: str
    index: int = 2  # 2 = first wingman (the player is leader = 1), 0 = all
    target: str = "any"
    direction: str = "any"
    group: str = ""

    def line(self) -> str:
        """Validated UDP line. Raises ValueError if a field is invalid."""
        if self.verb not in VERBS:
            raise ValueError(f"bilinmeyen komut: {self.verb}")
        if self.target not in TARGETS or self.direction not in DIRECTIONS:
            raise ValueError("geçersiz hedef ya da yön")
        if not isinstance(self.index, int) or not 0 <= self.index <= 9:
            raise ValueError("sıra 0-9 arası olmalı")
        if not _GROUP_OK.fullmatch(self.group):
            raise ValueError("grup adı sadece harf, rakam, boşluk, - ve _ içerebilir (en çok 40)")
        return f"ALICE1|{self.verb}|{self.index}|{self.target}|{self.direction}|{self.group}"

    def describe(self) -> str:
        who = ORDINALS[self.index] if self.index < len(ORDINALS) else f"{self.index}."
        who = self.group or ("tüm kanat adamları" if self.index == 0 else who)
        if self.verb == "attack":
            what = " ".join(w for w in (DIR_TR[self.direction], TARGET_TR[self.target]) if w)
            return f"{who}: {what} -> saldır"
        return f"{who}: {VERB_TR[self.verb]}"


def _norm(text: str) -> str:
    return text.replace("İ", "i").replace("I", "ı").lower().replace("’", "'")


_ORD_WORDS = [(r"birinci", 1), (r"ikinci", 2), (r"üçüncü", 3), (r"dördüncü", 4), (r"beşinci", 5)]
_CARD_WORDS = [(r"bir", 1), (r"iki", 2), (r"üç", 3), (r"dört", 4), (r"beş", 5)]
_ALL = re.compile(r"\b(hepsi|hepiniz|herkes|tüm kanat|bütün kanat|tümü|tamamı)")

_VERB_RE = [
    ("rtb", r"eve dön|üsse dön|üsse git|geri dön|üsse geri|rtb\b|inişe geç|eve git"),
    ("rejoin", r"katıl|toplan|yanıma gel|yanıma dön|formasyon|kanadıma|rejoin|beni takip|bana katıl"),
    ("hold", r"bekle|tur at|orbit|daire çiz|\bhold\b|olduğun yerde"),
    ("attack", r"bastır|saldır|\bvur\b|vurun|vursun|yok et|imha et|angaje|engage|attack|ateş aç"),
]
_TARGET_RE = [
    ("sam", r"\bsam\b|hava savunma|uçaksavar|\baaa\b|füze rampa|\bsead\b"),
    ("air", r"uça[kğ]|bandit|bogey|savaşçı|\bmig\b|helikopter"),
    ("ship", r"gemi|fırkateyn|destroyer|muhrip|\bbot\b"),
    ("ground", r"tank|zırhlı|\bkara\b|kara hedef|konvoy|araç|topçu|\bbirlik"),
]
_DIR_RE = [
    ("right", r"\bsağ(?!l)\w*"),
    ("left", r"\bsol(?!o)\w*"),
    ("front", r"\bön(?!ce)\w*|\bileri\w*|\bkarşı\w*"),
    ("behind", r"\barka\w*|\bgeri(de|deki)\b|\bkuyru\w*"),
]


def _index(t: str) -> int | None:
    if _ALL.search(t):
        return 0
    for word, n in _ORD_WORDS:
        if re.search(rf"\b{word}", t):
            return n
    # Number words and digits count only at the start or with "numara" ("bir SAM'i vur", "hit a SAM", is not an index)
    for word, n in _CARD_WORDS:
        if re.match(rf"\s*{word}\b", t) or re.search(rf"\b{word}\s+numara", t):
            return n
    m = re.match(r"\s*([1-9])\b", t) or re.search(r"\b([1-9])\s*(?:numara|\.)", t)
    return int(m.group(1)) if m else None


def parse(text: str) -> Command | None:
    """Convert a Turkish command into a Command. None if no verb is found."""
    t = _norm(text or "")
    verb = next((v for v, rx in _VERB_RE if re.search(rx, t)), None)
    if verb is None:
        return None
    idx = _index(t)
    target, direction = "any", "any"
    if verb == "attack":
        target = next((k for k, rx in _TARGET_RE if re.search(rx, t)), "any")
        direction = next((k for k, rx in _DIR_RE if re.search(rx, t)), "any")
    return Command(verb, 2 if idx is None else idx, target, direction)


def settings(sims_cfg: dict[str, Any] | None) -> tuple[int, list[str]]:
    """[sims] -> (hook port, fixed wingman group names)."""
    cfg = sims_cfg or {}
    try:
        port = int(cfg.get("dcs_wingmen_port", PORT))
    except (TypeError, ValueError):
        port = PORT
    names = cfg.get("dcs_wingmen") or []
    if isinstance(names, str):
        names = [names]
    return port, [str(n) for n in names if str(n).strip()][:9]


def send(cmd: Command, port: int = PORT, host: str = "127.0.0.1") -> bool:
    """Send the command to the hook. UDP: does not block even if DCS is closed; False if it could not be sent."""
    data = cmd.line().encode("ascii", errors="strict")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setblocking(False)
            sock.sendto(data, (host, port))
        return True
    except OSError:
        return False
