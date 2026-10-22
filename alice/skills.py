"""Skills ve özel alt ajanlar (Claude Code'daki skills ve .claude/agents gibi).

Skill: bir klasör ve içinde SKILL.md. Sistem istemine sadece adı ve tek satırlık açıklaması girer; iş ona uyunca
model `skill` aracıyla tamamını okur (yanındaki betik ve dosyaların yollarıyla). Böylece "DCS görev editöründe
tetik nasıl kurulur", "Unity build al" gibi bilgiler kod yazmadan ve istemi şişirmeden öğretilir.

  ~/.alice/skills/unity-build/SKILL.md
  ---
  name: unity-build
  description: Unity projesinden Windows build'i almak (komut satırından, editörü açmadan)
  ---
  1. Unity.exe'yi `-batchmode -quit -projectPath ... -buildWindows64Player ...` ile çalıştır...

Alt ajan: tek bir markdown dosyası. Gövdesi ajanın sistem istemi; ön bilgide araç listesi ve model.

  ~/.alice/agents/inceleyici.md
  ---
  name: inceleyici
  description: Kod değişikliklerini hata ve risk açısından inceler, değişiklik yapmaz
  tools: read_file, grep, glob, list_dir, git
  model: brain            # rol (brain/fast/vision) ya da "sağlayıcı:model"
  max_steps: 30
  ---
  Sen titiz bir kod inceleyicisin...

Yerler (sonra gelen öncekini ezer): ~/.alice/..., <proje>/.claude/..., <proje>/.alice/...
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from alice.config import alice_home

MAX_LISTED = 40


def front_matter(text: str) -> tuple[dict[str, str], str]:
    meta: dict[str, str] = {}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                k, sep, v = line.partition(":")
                if sep and k.strip():
                    meta[k.strip().lower()] = v.split(" #", 1)[0].strip().strip('"').strip("'")
            text = text[end + 4:]
    return meta, text.strip()


def _dirs(cwd: Path | None, kind: str) -> list[Path]:
    out = [alice_home() / kind]
    if cwd is not None:
        out += [Path(cwd) / ".claude" / kind, Path(cwd) / ".alice" / kind]
    return out


# ------------------------------------------------------------------------------------------- skills
@dataclass
class Skill:
    name: str
    description: str
    path: Path  # SKILL.md

    def body(self) -> str:
        return front_matter(self.path.read_text(encoding="utf-8", errors="replace"))[1]

    def files(self, limit: int = 30) -> list[str]:
        root = self.path.parent
        out = []
        for p in sorted(root.rglob("*")):
            if p.is_file() and p != self.path and not any(part.startswith(".") for part in p.relative_to(root).parts):
                out.append(str(p.relative_to(root)).replace("\\", "/"))
            if len(out) >= limit:
                break
        return out


def load_skills(cwd: Path | None = None) -> dict[str, Skill]:
    out: dict[str, Skill] = {}
    for d in _dirs(cwd, "skills"):
        if not d.is_dir():
            continue
        for md in sorted(d.glob("*/SKILL.md")):
            try:
                meta, body = front_matter(md.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            name = (meta.get("name") or md.parent.name).strip().lower()
            desc = meta.get("description") or (body.splitlines()[0][:120] if body else "")
            out[name] = Skill(name, desc, md)
    return out


def skills_prompt(cwd: Path | None = None) -> str:
    skills = load_skills(cwd)
    if not skills:
        return ""
    lines = [f"- {s.name}: {s.description}" for s in list(skills.values())[:MAX_LISTED]]
    return ("# Skills\nThe user taught you these skills (instructions + files). When a task matches one, call the "
            "`skill` tool with its name FIRST and follow what it says:\n" + "\n".join(lines))


# ------------------------------------------------------------------------------------------- alt ajanlar
@dataclass
class AgentDef:
    name: str
    description: str
    prompt: str
    tools: list[str] = field(default_factory=list)  # boş: hepsi
    model: str = ""  # boş: ana ajanla aynı
    max_steps: int = 40
    path: Path | None = None


def load_agents(cwd: Path | None = None) -> dict[str, AgentDef]:
    out: dict[str, AgentDef] = {}
    for d in _dirs(cwd, "agents"):
        if not d.is_dir():
            continue
        for md in sorted(d.glob("*.md")):
            try:
                meta, body = front_matter(md.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            name = (meta.get("name") or md.stem).strip().lower()
            tools = [t.strip() for t in meta.get("tools", "").replace(";", ",").split(",") if t.strip()]
            try:
                steps = int(meta.get("max_steps", "40") or 40)
            except ValueError:
                steps = 40
            desc = meta.get("description") or (body.splitlines()[0][:120] if body else "")
            out[name] = AgentDef(name, desc, body, tools, meta.get("model", ""), steps, md)
    return out


def agents_note(cwd: Path | None = None) -> str:
    agents = load_agents(cwd)
    if not agents:
        return ""
    return ("Custom subagents (task tool, agent=<name>): "
            + "; ".join(f"{a.name}: {a.description}" for a in list(agents.values())[:20]))


def spawn(parent, adef: AgentDef):
    """Ana ajandan, tanımdaki istem/araç/modelle bir alt ajan üret."""
    from alice.agent import Agent

    tools = list(parent.tools.values())
    if adef.tools:
        wanted = set(adef.tools)
        tools = [t for t in tools if t.name in wanted or any(t.name.startswith(w.rstrip("*")) for w in wanted
                                                              if w.endswith("*"))]
    system = (f"{adef.prompt}\n\n# Environment\n- Working directory: {parent.ctx.cwd}\n"
              "You are a subagent: do the task you are given and reply with the result; the parent agent reads it.")
    child = Agent(parent.config, parent.router, tools, parent.ctx, parent.permissions, parent.ui,
                  getattr(parent, "decider", None), system_prompt=system, is_child=True)
    if adef.tools:
        child.tool_specs = lambda: [t.spec() for t in tools]  # seçilen araçlar grup filtresinden bağımsız görünsün
    model = adef.model.strip()
    if model and ":" in model:
        role = f"agent:{adef.name}"
        try:
            fallback = parent.router.chain(parent.role)
        except Exception:  # noqa: BLE001
            fallback = []
        parent.router.role_overrides[role] = [model] + [r for r in fallback if r != model]
        child.role = role
    elif model:
        child.role = model
    else:
        child.role = parent.role
    return child
