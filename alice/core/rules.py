"""Rules: "tell me if this metric stays like this for a long time" ("şu metrik uzun süre şöyle olursa haber ver").

The decision chain, from cheapest to most expensive:
  1. Rule: has the metric stayed past the threshold for the required time? (no AI involved)
  2. If confirm is set: the situation is ambiguous, so Jev (System One) is asked "haber vermeye değer mi?"
     ("is it worth reporting?")
  3. If action = "agent": ALICE itself wakes up, does the research in the prompt, and reports the result

Rules live in ~/.alice/rules.json. The user does not write rules by hand; they say "nabzım uzun süre yüksek
kalırsa beni uyar" (meaning "tell me if my heart rate stays high for a long time"), and `parse_rule` turns that
into a rule with the fast model.
"""

from __future__ import annotations

import fnmatch
import operator
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from alice.config import alice_home
from alice.i18n import _
from alice.core.notify import Notification, Notifier
from alice.core.store import Store

OPS: dict[str, Callable[[float, float], bool]] = {
    ">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le, "==": operator.eq, "!=": operator.ne}

# For rules without a duration (duration=0): skip evaluation if the last measurement is older than this.
FRESH = 120.0


@dataclass
class Rule:
    metric: str                       # key or glob: "pc.gpu.temp", "pc.disk.*.percent"
    op: str
    threshold: float
    message: str                      # placeholders: {value}, {key}, {match}, {threshold}
    duration: float = 0.0             # seconds; the condition must hold continuously for this long
    cooldown: float = 1800.0          # how long to wait before alerting again for the same key
    severity: str = "warn"            # info | warn | critical
    topic: str = "pc"
    modes: list[str] = field(default_factory=list)  # empty: every mode; if set, only in these modes
    confirm: str = ""                 # if set, a yes/no question for Jev (System One)
    action: str = "notify"            # notify | agent
    prompt: str = ""                  # if action=agent: the task given to ALICE
    description: str = ""             # the user's own words
    enabled: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def __post_init__(self) -> None:
        if self.op not in OPS:
            raise ValueError(_("invalid operator: {op}", op=self.op))
        if self.action not in ("notify", "agent"):
            raise ValueError(_("invalid action: {action}", action=self.action))
        if self.action == "agent" and not self.prompt:
            raise ValueError(_("action=agent needs prompt"))
        self.threshold = float(self.threshold)
        self.duration = float(self.duration)
        self.cooldown = float(self.cooldown)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Rule":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})

    def summary(self) -> str:
        dur = _(" for {duration}", duration=fmt_duration(self.duration)) if self.duration else ""
        modes = f" [{', '.join(self.modes)}]" if self.modes else ""
        extra = _(" +decider check") if self.confirm else ""
        act = _(" -> ALICE looks into it") if self.action == "agent" else ""
        off = "" if self.enabled else _(" (off)")
        return f"{self.id}: {self.metric} {self.op} {self.threshold:g}{dur}{modes}{extra}{act} ({self.severity}){off}"


def fmt_duration(sec: float) -> str:
    if sec >= 3600 and sec % 3600 == 0:
        return _("{n} h", n=f"{sec / 3600:g}")
    if sec >= 60:
        return _("{n} min", n=f"{sec / 60:g}")
    return _("{secs} s", secs=f"{sec:g}")


DEFAULT_RULES = [
    Rule("pc.gpu.temp", ">", 85, "The graphics card is at {value:.0f}°C and has been very hot for a few minutes.", duration=180,
         description="tell me if the GPU stays very hot for a long time", id="gpu-hot"),
    Rule("pc.cpu.temp", ">", 90, "The processor is at {value:.0f}°C and has been very hot for a few minutes.", duration=180,
         description="tell me if the CPU stays very hot for a long time", id="cpu-hot"),
    Rule("pc.mem.percent", ">", 92, "Memory is {value:.0f}% full.", duration=300,
         description="tell me if RAM stays full for a long time", id="mem-full"),
    Rule("pc.disk.*.percent", ">", 95, "Disk {match} is {value:.0f}% full.", cooldown=6 * 3600,
         description="tell me if a disk is about to fill up", id="disk-full"),
    Rule("pc.disk.*.free_gb", "<", 10, "Only {value:.1f} GB free left on disk {match}. Shall I look at what's taking up space?",
         cooldown=6 * 3600, description="tell me if a disk has less than 10 GB free", id="disk-low"),
    Rule("pc.proc.*.mem_mb", ">", 8000, "{match} is using {value:.0f} MB of memory and it isn't going down.", duration=600,
         cooldown=3 * 3600, description="tell me if a program keeps bloating its memory", id="proc-bloat"),
    Rule("watch.hr", ">", 125, "Your heart rate is {value:.0f} and has been high for a while. Are you okay?", duration=600, cooldown=2 * 3600,
         topic="health", modes=["normal", "dev", "quiet"],
         confirm="The user is likely sitting at the computer (not exercising). Is this sustained high heart rate "
                 "worth a gentle check-in?",
         description="warn me if my heart rate stays high for a long time", id="hr-high"),
]

# The defaults are stored in English and shown in the interface language with _(). rules.json files written before
# that hold the Turkish text; it is turned into the English key on load so it follows the language too.
OLD_DEFAULT_TEXT = {
    "Ekran kartı {value:.0f}°C, birkaç dakikadır çok sıcak.": "The graphics card is at {value:.0f}°C and has been very hot for a few minutes.",
    "GPU uzun süre çok ısınırsa söyle": "tell me if the GPU stays very hot for a long time",
    "İşlemci {value:.0f}°C, birkaç dakikadır çok sıcak.": "The processor is at {value:.0f}°C and has been very hot for a few minutes.",
    "CPU uzun süre çok ısınırsa söyle": "tell me if the CPU stays very hot for a long time",
    "Bellek %{value:.0f} dolu.": "Memory is {value:.0f}% full.",
    "RAM uzun süre dolu kalırsa söyle": "tell me if RAM stays full for a long time",
    "{match} diski %{value:.0f} dolu.": "Disk {match} is {value:.0f}% full.",
    "disk dolmak üzereyse söyle": "tell me if a disk is about to fill up",
    "{match} diskinde {value:.1f} GB boş yer kaldı. Ne yer kaplıyor bakayım mı?": "Only {value:.1f} GB free left on disk {match}. Shall I look at what's taking up space?",
    "bir diskte 10 GB'tan az yer kalırsa söyle": "tell me if a disk has less than 10 GB free",
    "{match} {value:.0f} MB bellek kullanıyor ve düşmüyor.": "{match} is using {value:.0f} MB of memory and it isn't going down.",
    "bir program belleği şişirirse söyle": "tell me if a program keeps bloating its memory",
    "Nabzın {value:.0f}, bir süredir yüksek. İyi misin?": "Your heart rate is {value:.0f} and has been high for a while. Are you okay?",
    "nabzım uzun süre yüksek kalırsa beni uyar": "warn me if my heart rate stays high for a long time",
}

# Defaults added after rules.json was first written: added on load if they are missing.
ADDED_LATER = {"disk-low"}


class RuleSet:
    def __init__(self, path: Path | None = None):
        self.path = path or alice_home() / "rules.json"
        self.rules: list[Rule] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.rules = [Rule.from_dict(asdict(r)) for r in DEFAULT_RULES]
            return
        from alice.core.schedule import read_json_list

        data = read_json_list(self.path)
        if data is None:  # corrupt file: keep what we have so the core doesn't crash
            return
        rules = []
        for d in data:
            try:
                if isinstance(d, dict):
                    d = {**d, **{k: OLD_DEFAULT_TEXT[v] for k, v in d.items() if k in ("message", "description")
                                 and isinstance(v, str) and v in OLD_DEFAULT_TEXT}}
                rules.append(Rule.from_dict(d))
            except (TypeError, ValueError, AttributeError):
                continue
        # A default rule added later should also reach an old rules.json; if the user deleted it, it must not come back.
        have = {r.id for r in rules}
        removed = self._removed()
        # If none of the defaults are present, the user deleted them all: don't impose the new one either.
        had_defaults = bool(have & {r.id for r in DEFAULT_RULES if r.id not in ADDED_LATER})
        added = [Rule.from_dict(asdict(r)) for r in DEFAULT_RULES
                 if had_defaults and r.id in ADDED_LATER and r.id not in have and r.id not in removed]
        self.rules = rules + added
        if added:
            self.save()

    def _removed_path(self) -> Path:
        return self.path.with_name(self.path.stem + "-removed.json")

    def _removed(self) -> set[str]:
        from alice.core.schedule import read_json_list

        return {str(x) for x in (read_json_list(self._removed_path()) or [])}

    def save(self) -> None:
        from alice.core.schedule import write_json_atomic

        write_json_atomic(self.path, [asdict(r) for r in self.rules])

    def add(self, rule: Rule) -> Rule:
        self.rules = [r for r in self.rules if r.id != rule.id] + [rule]
        self.save()
        return rule

    def get(self, rule_id: str) -> Rule | None:
        return next((r for r in self.rules if r.id == rule_id), None)

    def remove(self, rule_id: str) -> bool:
        before = len(self.rules)
        self.rules = [r for r in self.rules if r.id != rule_id]
        if len(self.rules) != before:
            self.save()
            if rule_id in ADDED_LATER:
                from alice.core.schedule import write_json_atomic

                write_json_atomic(self._removed_path(), sorted(self._removed() | {rule_id}))
            return True
        return False

    def set_enabled(self, rule_id: str, enabled: bool) -> bool:
        r = self.get(rule_id)
        if r is None:
            return False
        r.enabled = enabled
        self.save()
        return True


@dataclass
class Firing:
    rule: Rule
    key: str
    value: float
    text: str


class RuleEngine:
    """Evaluates rules against the data in the store and reports the ones that fire."""

    def __init__(self, rules: RuleSet, store: Store, notifier: Notifier, decider=None,
                 run_agent: Callable[[str], str] | None = None, mode: Callable[[], str] = lambda: "normal"):
        self.rules = rules
        self.store = store
        self.notifier = notifier
        self.decider = decider
        self.run_agent = run_agent
        self.sync_agent = False  # for tests: run the agent action on the same thread
        self.mode = mode
        self.last_fired: dict[tuple[str, str], float] = {}

    def check(self, rule: Rule, key: str, now: float) -> float | None:
        """Return the last value if the condition holds, or None if it does not."""
        cmp = OPS[rule.op]
        if rule.duration <= 0:
            last = self.store.last(key)
            if not last or last[1] is None or now - last[0] > FRESH:
                return None
            return last[1] if cmp(last[1], rule.threshold) else None
        pts = self.store.series(key, since=now - rule.duration, until=now)
        if not pts or any(not cmp(v, rule.threshold) for _ts, v in pts):
            return None
        # If the measurements don't cover most of the window (service just started, data gap),
        # the condition is not yet counted as held for "a long time"; the last measurement must also be fresh.
        if pts[0][0] > now - rule.duration * 0.8 or now - pts[-1][0] > FRESH:
            return None
        return pts[-1][1]

    def evaluate(self, now: float | None = None) -> list[Firing]:
        now = time.time() if now is None else now
        mode = self.mode()
        fired: list[Firing] = []
        all_keys: list[str] | None = None
        for rule in self.rules.rules:
            if not rule.enabled or (rule.modes and mode not in rule.modes):
                continue
            if any(c in rule.metric for c in "*?["):
                if all_keys is None:
                    all_keys = self.store.keys(since=now - max(3600.0, FRESH))
                keys = [k for k in all_keys if fnmatch.fnmatchcase(k, rule.metric)]
            else:
                keys = [rule.metric]
            for key in keys:
                if now - self.last_fired.get((rule.id, key), -1e18) < rule.cooldown:
                    continue
                try:
                    value = self.check(rule, key, now)
                    if value is None:
                        continue
                    text = self._format(rule, key, value)
                    if rule.confirm and not self._confirmed(rule, key, value, text):
                        continue
                    self.last_fired[(rule.id, key)] = now
                    fired.append(Firing(rule, key, value, text))
                    self._act(fired[-1])
                except Exception as e:  # noqa: BLE001 - one broken rule must not stop the core turn or the other rules
                    self.last_fired[(rule.id, key)] = now  # don't repeat the same error every turn
                    print(f"rule error ({rule.id}, {key}): {e!r}")
        return fired

    @staticmethod
    def _format(rule: Rule, key: str, value: float) -> str:
        match = ""
        if "*" in rule.metric:
            pre, _star, post = rule.metric.partition("*")
            match = key[len(pre):len(key) - len(post) if post else None]
        try:
            return _(rule.message).format(value=value, key=key, match=match, threshold=rule.threshold)
        except (KeyError, IndexError, ValueError):
            return f"{rule.message} ({key} = {value:g})"

    def _confirmed(self, rule: Rule, key: str, value: float, text: str) -> bool:
        if self.decider is None:
            return True
        from alice.decider import DeciderError, Noul

        recent = self.store.series(key, since=time.time() - 3600)[-12:]
        state = (f"Mode: {self.mode()}. Metric {key} = {value:g} (rule: {rule.op} {rule.threshold:g}). "
                 f"Recent values: {', '.join(f'{v:g}' for _ts, v in recent)}. Alert text: {text}. "
                 f"User's own words: {rule.description}")
        try:
            d = self.decider.decide(state, {"notify": Noul(rule.confirm)})
            return float(d["notify"].value) >= 0.5
        except (DeciderError, Exception):  # noqa: BLE001 - no decider, or a broken answer: the rule decides alone
            return True

    def _act(self, f: Firing) -> None:
        r = f.rule
        run_agent = self.run_agent
        if r.action == "agent" and run_agent is not None:
            # An agent turn can take minutes: don't make the core turn (monitoring, tasks, status file) wait.
            def work() -> None:
                prompt = f"{r.prompt}\n\n(This job was triggered by this alert: {f.text})"
                try:
                    body = run_agent(prompt)
                except Exception as e:  # noqa: BLE001 - even if the agent crashes, the alert must not be lost
                    body = _("{text}\n(research failed: {error})", text=f.text, error=e)
                try:
                    self.notifier.send(Notification("ALICE", (body or f.text)[:500], r.severity, r.topic))
                except Exception:  # noqa: BLE001
                    pass

            if self.sync_agent:
                work()
            else:
                threading.Thread(target=work, daemon=True, name="alice-rule-agent").start()
        else:
            self.notifier.send(Notification("ALICE", f.text, r.severity, r.topic))


# --------------------------------------------------------- natural language -> rule
PARSE_PROMPT = """Convert the user's request into a monitoring rule for a background assistant.
Available metric keys (glob * allowed): {keys}
Common keys: pc.cpu.percent, pc.cpu.temp, pc.gpu.temp, pc.gpu.util, pc.gpu.mem_percent, pc.mem.percent,
pc.disk.<drive>.percent, pc.proc.<name>.mem_mb, pc.proc.<name>.cpu, watch.hr (heart rate), watch.stress,
dcs.fuel.kg, dcs.fuel.min_left, dcs.g, dcs.chaff, dcs.flare.

Reply ONLY with JSON:
{{"metric": "<key or glob>", "op": ">|>=|<|<=|==|!=", "threshold": <number>,
  "duration": <seconds the condition must hold, 0 if instant>, "cooldown": <seconds before repeating, default 1800>,
  "severity": "info|warn|critical", "topic": "pc|flight|health|other",
  "modes": [<subset of "normal","dev","flight","quiet"; empty = always>],
  "message": "<short Turkish alert, may use {{value}}, {{match}}>",
  "confirm": "<optional yes/no question for a fast classifier when the rule alone could false-alarm, else empty>",
  "action": "notify|agent", "prompt": "<if agent: what ALICE should investigate, in Turkish>"}}
User request: {text}"""


def parse_rule(text: str, router, store: Store | None = None) -> Rule:
    from alice.decider import extract_json

    keys = ", ".join(store.keys(since=time.time() - 86400)[:80]) if store else "(no data yet)"
    msgs = [{"role": "user", "content": PARSE_PROMPT.format(keys=keys or "(no data yet)", text=text)}]
    res = router.chat("fast", msgs, temperature=0, max_tokens=800)
    data = extract_json(res.text)
    if not isinstance(data, dict):
        raise ValueError(_("the model returned no rule: {text}", text=res.text[:200]))
    data["description"] = text
    data.pop("id", None)
    return Rule.from_dict(data)
