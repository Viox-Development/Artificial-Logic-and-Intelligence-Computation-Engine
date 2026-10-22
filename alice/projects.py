"""Kullanıcının projeleri: bilgisayardaki repo/Unity/Python/Node projelerini bul, kısa kartlarını çıkar.

ALICE "Unity projemi aç", "FPS oyununda en son ne yapmıştık", "ALICE reposunda kaç commit var" dendiğinde hangi
klasörden bahsedildiğini bilmiyordu. Bu modül ev klasöründeki bilinen yerleri (Masaüstü, Belgeler, GitHub,
source/repos, Unity...) ve Windows'ta diğer sürücülerin üst klasörlerini sığ bir şekilde tarar; .git, Unity
(Assets + ProjectSettings), pyproject/package.json/.sln gibi işaretlerden projeyi tanır. Kartlar
~/.alice/projects.json'da durur, 12 saatte bir arka planda yenilenir. En son çalışılanlar sistem istemine
girer; ayrıntı `projects` aracıyla.

config.toml:
  [projects]
  paths = ["D:/Oyunlar/Unity"]   # ek kök klasörler
  depth = 3                      # köklerin altında kaç seviye inilir
  enabled = true
"""

from __future__ import annotations

import json
import os
import re
import string
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from alice.config import alice_home

REFRESH = 12 * 3600
SKIP = {"node_modules", ".venv", "venv", "env", "__pycache__", "library", "temp", "obj", "bin", "build", "dist",
        "appdata", ".cache", "$recycle.bin", "windows", "program files", "program files (x86)", "programdata",
        "site-packages", ".git", ".idea", ".vs", ".vscode", "logs", "packages", "steamapps", "system volume information",
        "recovery", "perflogs", "onedrive - personal", ".gradle", ".m2", ".npm", ".nuget", "go", "anaconda3",
        "miniconda3", ".rustup", ".cargo", ".local", "snap"}
HOME_ROOTS = ["Desktop", "Masaüstü", "Documents", "Belgeler", "Projects", "projects", "Projeler", "source/repos",
              "repos", "dev", "code", "src", "GitHub", "Unity", "Unity Projects", "Godot", "OneDrive/Desktop",
              "OneDrive/Documents", "OneDrive/Belgeler", "Downloads"]
MARKERS = [("unity", ("Assets", "ProjectSettings")), ("godot", ("project.godot",)), ("python", ("pyproject.toml",)),
           ("python", ("setup.py",)), ("node", ("package.json",)), ("rust", ("Cargo.toml",)), ("go", ("go.mod",)),
           ("java", ("pom.xml",)), ("java", ("build.gradle",)), ("cpp", ("CMakeLists.txt",))]
LANG = {".py": "Python", ".cs": "C#", ".js": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
        ".jsx": "JavaScript", ".java": "Java", ".cpp": "C++", ".cc": "C++", ".c": "C", ".h": "C/C++", ".hpp": "C++",
        ".rs": "Rust", ".go": "Go", ".lua": "Lua", ".gd": "GDScript", ".html": "HTML", ".css": "CSS",
        ".kt": "Kotlin", ".swift": "Swift", ".php": "PHP", ".rb": "Ruby", ".shader": "Shader", ".hlsl": "Shader"}

_lock = threading.Lock()
_refreshing = threading.Event()


@dataclass
class Project:
    name: str
    path: str
    kind: str = "other"
    languages: list[str] = field(default_factory=list)
    remote: str = ""
    branch: str = ""
    last_commit: str = ""
    active: float = 0.0  # son çalışma zamanı (son commit ya da en yeni dosya)
    readme: str = ""

    def line(self, now: float | None = None) -> str:
        from alice.memory import _ago

        langs = "/".join(self.languages[:3])
        bits = [self.kind if self.kind != "other" else "", langs]
        meta = ", ".join(b for b in bits if b)
        when = f", son çalışma {_ago(self.active, now)}" if self.active else ""
        gist = f": {self.readme[:120]}" if self.readme else ""
        return f"{self.name} ({meta}{when}) {self.path}{gist}"


# ------------------------------------------------------------------------------------------- tarama
def roots(cfg=None) -> list[tuple[Path, int]]:
    """(kök, derinlik). Ev klasöründeki bilinen yerler + config'teki yollar + Windows'ta diğer sürücüler."""
    o = (cfg.data.get("projects", {}) if cfg is not None else {}) or {}
    depth = int(o.get("depth", 3))
    home = Path.home()
    out: list[tuple[Path, int]] = [(Path(os.path.expandvars(os.path.expanduser(str(p)))), depth)
                                   for p in o.get("paths", []) or []]
    out += [(home / r, depth) for r in HOME_ROOTS]
    if sys.platform == "win32" and o.get("other_drives", True):
        sysdrive = os.environ.get("SystemDrive", "C:").upper()
        for letter in string.ascii_uppercase[2:]:
            d = Path(f"{letter}:/")
            if f"{letter}:" != sysdrive and d.exists():
                out.append((d, 2))
    seen, uniq = set(), []
    for p, dep in out:
        try:
            key = str(p.resolve()).lower()
        except OSError:
            continue
        if key not in seen and p.is_dir():
            seen.add(key)
            uniq.append((p, dep))
    return uniq


def detect(path: Path) -> str | None:
    """Klasör bir proje kökü mü? Türünü döndür (git deposu ama tanınmayan tür: 'repo')."""
    try:
        names = set(os.listdir(path))
    except OSError:
        return None
    for kind, needed in MARKERS:
        if all(n in names for n in needed):
            return kind
    if any(n.endswith((".sln", ".csproj")) for n in names):
        return "dotnet"
    if ".git" in names:
        return "repo"
    return None


def scan(cfg=None, limit: int = 300, deadline: float = 20.0) -> list[Project]:
    t0 = time.monotonic()
    found: dict[str, Project] = {}

    def walk(p: Path, left: int) -> None:
        if len(found) >= limit or time.monotonic() - t0 > deadline:
            return
        kind = detect(p)
        if kind:
            key = str(p).lower()
            if key not in found:
                found[key] = card(p, kind)
            return  # proje içinde alt proje aranmaz
        if left <= 0:
            return
        try:
            subs = sorted((e for e in os.scandir(p) if e.is_dir(follow_symlinks=False)), key=lambda e: e.name)
        except OSError:
            return
        for e in subs:
            if e.name.lower() in SKIP or e.name.startswith((".", "$")):
                continue
            walk(Path(e.path), left - 1)

    for root, depth in roots(cfg):
        walk(root, depth)
    projects = sorted(found.values(), key=lambda pr: pr.active, reverse=True)
    return projects


def card(path: Path, kind: str) -> Project:
    pr = Project(name=path.name, path=str(path), kind=kind)
    git = path / ".git"
    if git.is_dir():
        try:
            head = (git / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
            pr.branch = head.rsplit("/", 1)[-1] if head.startswith("ref:") else head[:7]
        except OSError:
            pass
        try:
            m = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)',
                          (git / "config").read_text(encoding="utf-8", errors="replace"))
            pr.remote = _clean_remote(m.group(1)) if m else ""
        except OSError:
            pass
        last = commits(path, 1)
        if last:
            pr.active, pr.last_commit = last[0]
    pr.languages = languages(path)
    pr.readme = readme(path)
    if not pr.active:
        pr.active = newest_mtime(path)
    if kind == "unity":
        try:
            ver = (path / "ProjectSettings" / "ProjectVersion.txt").read_text(encoding="utf-8", errors="replace")
            m = re.search(r"m_EditorVersion:\s*(\S+)", ver)
            if m:
                pr.kind = f"unity {m.group(1)}"
        except OSError:
            pass
    return pr


def _clean_remote(url: str) -> str:
    url = re.sub(r"https://[^@/]+@", "https://", url)  # URL içine gömülü token kart dosyasına yazılmasın
    m = re.search(r"github\.com[:/]+([\w.-]+/[\w.-]+?)(?:\.git)?$", url)
    return m.group(1) if m else url


def commits(path: Path, n: int = 5) -> list[tuple[float, str]]:
    """Son n commit (zaman, mesaj), .git/logs/HEAD'den: git kurulu olmasa da çalışır."""
    try:
        with open(path / ".git" / "logs" / "HEAD", "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 16000))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        m = re.match(r"\S+ \S+ .*? (\d{9,11}) [+-]\d{4}\t(.*)$", line)
        if not m:
            continue
        msg = m.group(2)
        if msg.startswith(("checkout:", "reset:", "pull", "rebase", "merge")) and "commit" not in msg:
            continue
        out.append((float(m.group(1)), re.sub(r"^commit( \(\w+\))?: ", "", msg)[:160]))
        if len(out) >= n:
            break
    return out


def languages(path: Path, cap: int = 3000) -> list[str]:
    counts: dict[str, int] = {}
    seen = 0
    for dirpath, dirnames, files in os.walk(path):
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP and not d.startswith(".")]
        for f in files:
            lang = LANG.get(os.path.splitext(f)[1].lower())
            if lang:
                counts[lang] = counts.get(lang, 0) + 1
            seen += 1
        if seen > cap:
            break
    return [k for k, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:4]


def readme(path: Path) -> str:
    for name in ("README.md", "readme.md", "README.txt", "README", "Readme.md"):
        try:
            text = (path / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for para in re.split(r"\n\s*\n", text):
            p = " ".join(line.strip() for line in para.splitlines() if not line.strip().startswith(("#", "!", "[", "<",
                                                                                                    "```", "|")))
            p = re.sub(r"[*_`]", "", p).strip()
            if len(p) >= 25:
                return p[:300]
    return ""


def newest_mtime(path: Path, cap: int = 400) -> float:
    best, n = 0.0, 0
    for dirpath, dirnames, files in os.walk(path):
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP and not d.startswith(".")]
        for f in files:
            try:
                best = max(best, os.path.getmtime(os.path.join(dirpath, f)))
            except OSError:
                pass
            n += 1
        if n > cap:
            break
    return best


# ------------------------------------------------------------------------------------------- önbellek
def cache_path() -> Path:
    return alice_home() / "projects.json"


def load() -> tuple[float, list[Project]]:
    try:
        data = json.loads(cache_path().read_text(encoding="utf-8"))
        fields = set(Project.__dataclass_fields__)
        return float(data.get("scanned", 0)), [Project(**{k: v for k, v in p.items() if k in fields})
                                               for p in data.get("projects", [])]
    except (OSError, ValueError, TypeError):
        return 0.0, []


def save(projects: list[Project]) -> None:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"scanned": time.time(), "projects": [asdict(p) for p in projects]},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def enabled(cfg=None) -> bool:
    return bool(((cfg.data.get("projects", {}) if cfg is not None else {}) or {}).get("enabled", True))


def refresh(cfg=None) -> list[Project]:
    with _lock:
        projects = scan(cfg)
        save(projects)
        return projects


def get(cfg=None, background: bool = True) -> list[Project]:
    """Önbellekteki projeler; eskiyse arka planda yenilenir (ilk açılışta da beklemeden boş döner)."""
    if not enabled(cfg):
        return []
    scanned, projects = load()
    if time.time() - scanned > REFRESH and not _refreshing.is_set():
        if background:
            _refreshing.set()

            def run() -> None:
                try:
                    refresh(cfg)
                except Exception:  # noqa: BLE001 - tarama ipucu; hata ALICE'i etkilemesin
                    pass
                finally:
                    _refreshing.clear()

            threading.Thread(target=run, daemon=True, name="alice-projects").start()
        else:
            projects = refresh(cfg)
    return projects


def find(query: str, projects: list[Project]) -> list[Project]:
    from alice.memory import fold

    q = fold(query).strip()
    if not q:
        return []
    words = [w for w in re.findall(r"[a-z0-9]+", q) if len(w) >= 2]
    scored = []
    for p in projects:
        hay = fold(f"{p.name} {p.path} {p.kind} {' '.join(p.languages)} {p.remote} {p.readme}")
        name = fold(p.name)
        score = (5 if q == name else 0) + (3 if q in name else 0) + sum(1 for w in words if w in hay)
        if score:
            scored.append((score, p.active, p))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [p for *_, p in scored]


def brief(cfg=None, n: int = 8) -> str:
    """Sistem istemine giren kısa liste: en son çalışılan n proje."""
    projects = get(cfg)[:n]
    if not projects:
        return ""
    return ("# User's projects\nProjects found on this computer, most recently active first. When the user says "
            "\"projem\", \"oyunum\", \"repo\" or names one, it is probably one of these; use the projects tool for "
            "details (recent commits, files).\n" + "\n".join(f"- {p.line()}" for p in projects))


def names(cfg=None, n: int = 20) -> list[str]:
    """Ses tanıma ipucu için proje adları."""
    try:
        return [p.name for p in get(cfg)[:n]]
    except Exception:  # noqa: BLE001
        return []


def info(p: Project) -> str:
    lines = [p.line()]
    if p.remote:
        lines.append(f"GitHub/uzak: {p.remote}" + (f" (dal {p.branch})" if p.branch else ""))
    recent = commits(Path(p.path), 8)
    if recent:
        from datetime import datetime

        lines.append("Son commitler:")
        lines += [f"  {datetime.fromtimestamp(ts):%d.%m.%Y %H:%M} {msg}" for ts, msg in recent]
    try:
        entries = sorted(os.listdir(p.path))
        shown = [e + ("/" if os.path.isdir(os.path.join(p.path, e)) else "") for e in entries
                 if not e.startswith(".")][:40]
        lines.append("Klasör: " + ", ".join(shown))
    except OSError:
        pass
    if p.readme:
        lines.append(f"README: {p.readme}")
    return "\n".join(lines)
