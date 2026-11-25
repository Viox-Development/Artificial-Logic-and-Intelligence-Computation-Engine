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

from alice.i18n import spoken

PORT = 41231
VERBS = ("attack", "rejoin", "hold", "rtb")
TARGETS = ("sam", "air", "ground", "ship", "any")
DIRECTIONS = ("right", "left", "front", "behind", "any")
_GROUP_OK = re.compile(r"[\w \-]{0,40}", re.ASCII)

# Spoken confirmation of an order, in the speech language (spoken()).
ORDINALS = ["all", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"]
VERB_TR = {"attack": "attack", "rejoin": "rejoin on me", "hold": "hold where you are", "rtb": "return to base"}
TARGET_TR = {"sam": "SAM", "air": "enemy aircraft", "ground": "ground target", "ship": "ship", "any": "nearest enemy"}
DIR_TR = {"right": "on the right", "left": "on the left", "front": "in front of you", "behind": "behind", "any": ""}


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
            raise ValueError(f"unknown command: {self.verb}")
        if self.target not in TARGETS or self.direction not in DIRECTIONS:
            raise ValueError("invalid target or direction")
        if not isinstance(self.index, int) or not 0 <= self.index <= 9:
            raise ValueError("the index must be 0-9")
        if not _GROUP_OK.fullmatch(self.group):
            raise ValueError("the group name may only contain letters, digits, spaces, - and _ (at most 40)")
        return f"ALICE1|{self.verb}|{self.index}|{self.target}|{self.direction}|{self.group}"

    def describe(self) -> str:
        who = spoken(ORDINALS[self.index]) if self.index < len(ORDINALS) else f"{self.index}."
        who = self.group or (spoken("all wingmen") if self.index == 0 else who)
        if self.verb == "attack":
            what = spoken("{target} {direction}", target=spoken(TARGET_TR[self.target]),
                          direction=spoken(DIR_TR[self.direction]) if DIR_TR[self.direction] else "").strip()
            return spoken("{who}: {what} -> attack", who=who, what=what)
        return f"{who}: {spoken(VERB_TR[self.verb])}"


def _norm(text: str) -> str:
    return text.replace("İ", "i").replace("I", "ı").lower().replace("’", "'")


_ORD_WORDS = [(r"birinci", 1), (r"ikinci", 2), (r"üçüncü", 3), (r"dördüncü", 4), (r"beşinci", 5)]
_CARD_WORDS = [(r"bir", 1), (r"iki", 2), (r"üç", 3), (r"dört", 4), (r"beş", 5),
               (r"one", 1), (r"two", 2), (r"three", 3), (r"four", 4), (r"five", 5)]  # English callsign numbers
_ALL = re.compile(r"\b(hepsi|hepiniz|herkes|tüm kanat|bütün kanat|tümü|tamamı|all\b|everyone|everybody|whole flight)")

_VERB_RE = [
    ("rtb", r"eve dön|üsse dön|üsse git|geri dön|üsse geri|rtb\b|inişe geç|eve git|return to base|go home|"
            r"head home"),
    ("rejoin", r"katıl|toplan|yanıma gel|yanıma dön|formasyon|kanadıma|rejoin|beni takip|bana katıl|form up|"
               r"join on me|follow me|\bon me\b"),
    ("hold", r"bekle|tur at|orbit|daire çiz|\bhold\b|olduğun yerde|\bcircle\b|stay there|stay where"),
    ("attack", r"bastır|saldır|\bvur\b|vurun|vursun|yok et|imha et|angaje|engage|attack|ateş aç|suppress|"
               r"take out|destroy|\bhit\b|\bkill\b"),
]
_TARGET_RE = [
    ("sam", r"\bsam\b|hava savunma|uçaksavar|\baaa\b|füze rampa|\bsead\b|air defen[cs]e|triple a"),
    ("air", r"uça[kğ]|bandit|bogey|savaşçı|\bmig\b|helikopter|aircraft|plane|fighter|helicopter"),
    ("ship", r"gemi|fırkateyn|destroyer|muhrip|\bbot\b|\bships?\b|frigate|\bboats?\b|vessel"),
    ("ground", r"tank|zırhlı|\bkara\b|kara hedef|konvoy|araç|topçu|\bbirlik|armou?r|ground|convoy|vehicle|"
               r"artillery|troops"),
]
_DIR_RE = [
    ("right", r"\bsağ(?!l)\w*|\bright\b"),
    ("left", r"\bsol(?!o)\w*|\bleft\b"),
    ("front", r"\bön(?!ce)\w*|\bileri\w*|\bkarşı\w*|\bahead\b|\bin front\b"),
    ("behind", r"\barka\w*|\bgeri(de|deki)\b|\bkuyru\w*|\bbehind\b|\brear\b"),
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
    """Convert a Turkish or English command into a Command. None if no verb is found."""
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
