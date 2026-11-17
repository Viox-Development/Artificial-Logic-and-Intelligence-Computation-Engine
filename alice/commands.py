"""Slash commands: built-ins + custom commands written by the user.

A custom command is a Markdown file. ~/.alice/commands/ (valid everywhere) or
the project folder's .alice/commands/ (.claude/commands/ is also read, for Claude Code compatibility).

  ~/.alice/commands/review.md:
      ---
      description: Review changes
      ---
      Review the recent changes in this project and list the bugs. Focus: $ARGUMENTS

Typing `/review performans` sends the file body to ALICE as a normal message, with $ARGUMENTS replaced by
"performans" (Turkish for "performance").
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from alice.config import alice_home

BUILTINS: dict[str, str] = {
    "help": "Komutları göster",
    "clear": "Konuşmayı sıfırla (yeni sohbet)",
    "compact": "Context'i şimdi özetle",
    "btw": "İş sürerken yan soru sor: işi bölmez, konuşmaya yazılmaz (/btw <soru>)",
    "mode": "İzin modu: plan | ask | auto | yolo",
    "plan": "Plan moduna geç (sadece okur, plan yazar)",
    "model": "Rol zincirleri / modeli sabitle",
    "think": "Düşünme seviyesi: off | low | medium | high | default (rol verilebilir: /think brain off)",
    "rewind": "Bir önceki mesaja geri sar (dosyalar dahil)",
    "usage": "Bugünkü istek ve token kullanımı",
    "cwd": "Çalışma klasörünü göster/değiştir",
    "tools": "Araç listesi",
    "mcp": "Bağlı MCP sunucuları ve araçları",
    "init": "Bu proje için ALICE.md talimat dosyası oluştur",
    "memory": "Kalıcı hafızayı göster",
    "kurul": "Bir konuyu inceleme kuruluna sor: eleştirmen, savunucu, kontrolcü ve uzmanlar tartışıp oylar",
    "review": "Değişiklikleri (git diff ya da verilen PR/dal) hata ve risk açısından incele; /review derin: ayrı "
              "inceleyici ajanlar (performans, test, doküman da olur)",
    "security-review": "Değişikliklerde güvenlik açığı ara (sır sızıntısı, enjeksiyon, yetki, güvensiz komut)",
}

# If the user has a custom command with the same name, theirs wins (old ~/.alice/commands/review.md files keep working).
OVERRIDABLE = {"review", "security-review"}

REVIEW_PROMPT = (
    "Kod incelemesi yap. Kapsam: {target}. Önce değişikliği bul: git aracıyla status ve diff'e bak (kapsam bir PR ya da "
    "dalsa ona göre); değişen dosyaların ilgili kısımlarını oku. Sadece GERÇEK sorunları raporla: hatalı mantık, kenar "
    "durumlar, yarış durumları, kırılan çağrılar, eksik hata yönetimi, testin kaçırdığı davranış. Her bulgu için: "
    "dosya:satır, ne yanlış, hangi girdiyle bozulur, önerilen düzeltme. Önem sırasına diz (kritik, önemli, küçük). Stil "
    "zevklerini ve emin olmadığın tahminleri ayrı bir 'belki' başlığına koy. Değişiklik YAPMA; sadece raporla."
)
# /review deep|derin|performans|test ...: run the code_review tool with that focus, using independent agent(s).
DEEP_WORDS = {"deep": "deep", "derin": "deep", "performance": "performance", "performans": "performance",
              "tests": "tests", "test": "tests", "docs": "docs", "doküman": "docs", "dokuman": "docs"}
TOOL_REVIEW_PROMPT = (
    "code_review aracını focus={focus} ile çalıştır. Kapsam: {target} (bir PR numarası geçiyorsa target=pr ve number "
    "ver). Bulguları önem sırasıyla bana göster; değişiklik YAPMA, düzeltmeyi ben isteyince yap."
)
SECURITY_PROMPT = (
    "Güvenlik incelemesi yap. Kapsam: {target}. git aracıyla değişikliği bul ve değişen kodu oku. Ara: koda/commit'e "
    "girmiş anahtar ve şifreler, komut/SQL/yol enjeksiyonu, güvenilmeyen girdinin shell'e ya da eval'e gitmesi, yetki "
    "kontrolü eksikliği, güvensiz deserializasyon, TLS doğrulamasını kapatma, hassas verinin loglanması, dışarıdan "
    "gelen içeriğin talimat gibi işlenmesi. Her bulgu için: dosya:satır, saldırı senaryosu (kim, hangi girdiyle, ne "
    "elde eder), önem (kritik/yüksek/orta/düşük), düzeltme. Bulgu yoksa açıkça 'bulgu yok' de. Değişiklik YAPMA."
)


def builtin_prompt(text: str) -> str | None:
    """Built-in commands sent to the model as a message (/init, /kurul, /review, /security-review); else None."""
    name, args = _split(text.strip())
    name = name.lower()
    if name == "init":
        return INIT_PROMPT
    if name == "kurul":
        return council_prompt(args)
    if name == "review":
        words = args.split()
        focus = next((DEEP_WORDS[w.lower()] for w in words if w.lower() in DEEP_WORDS), "")
        if focus:
            rest = " ".join(w for w in words if w.lower() not in DEEP_WORDS)
            return TOOL_REVIEW_PROMPT.format(focus=focus, target=rest or "yerel değişiklikler (target=local)")
    if name in ("review", "security-review"):
        target = args or "çalışma klasöründeki commit'lenmemiş değişiklikler, yoksa son commit"
        return (REVIEW_PROMPT if name == "review" else SECURITY_PROMPT).format(target=target)
    return None

INIT_PROMPT = (
    "Bu klasördeki projeyi incele (yapı, diller, build/test komutları, önemli dosyalar, kod stili) ve proje kökünde "
    "ALICE.md adında kısa ve yoğun bir talimat dosyası oluştur. Gelecekte bu projede çalışacak bir ajana lazım olacak "
    "her şeyi yaz: nasıl çalıştırılır, nasıl test edilir, klasör yapısı, dikkat edilecek kurallar. Varsa README'yi ve "
    "mevcut ALICE.md/CLAUDE.md/AGENTS.md dosyalarını temel al, tekrar etme."
)


def council_prompt(args: str) -> str:
    """/kurul <topic>: the message that asks the review council about the topic."""
    topic = args.strip() or "Bu konuşmada en son konuştuğumuz konu ya da değişiklik"
    return (f"Şunu inceleme kuruluna sor (council aracı; konu kritikse critical=true): {topic}\n\nKurulun kararını, "
            "doğrulanmış bulguları ve kimin neyi neden savunduğunu bana kısaca özetle.")


@dataclass
class CustomCommand:
    name: str
    description: str
    body: str
    path: Path

    def expand(self, args: str) -> str:
        if "$ARGUMENTS" in self.body:
            return self.body.replace("$ARGUMENTS", args)
        return self.body + (f"\n\n{args}" if args else "")


def _parse(path: Path) -> CustomCommand:
    text = path.read_text(encoding="utf-8", errors="replace")
    desc = ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                k, _, v = line.partition(":")
                if k.strip() == "description":
                    desc = v.strip()
            text = text[end + 4:]
    body = text.strip()
    if not desc:
        desc = body.splitlines()[0][:80] if body else ""
    return CustomCommand(path.stem.lower(), desc, body, path)


def load_custom(cwd: Path) -> dict[str, CustomCommand]:
    out: dict[str, CustomCommand] = {}
    # Later entries overwrite earlier ones: project commands take priority over personal commands.
    for d in (alice_home() / "commands", cwd / ".claude" / "commands", cwd / ".alice" / "commands"):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            try:
                cmd = _parse(f)
            except OSError:
                continue
            if cmd.name not in BUILTINS or cmd.name in OVERRIDABLE:
                out[cmd.name] = cmd
    return out


def catalog(cwd: Path) -> list[dict[str, str]]:
    custom = load_custom(cwd)
    items = [{"name": n, "description": d, "kind": "builtin"} for n, d in BUILTINS.items() if n not in custom]
    items += [{"name": c.name, "description": c.description, "kind": "custom"} for c in custom.values()]
    return items


def expand_custom(text: str, cwd: Path) -> str | None:
    """If '/name args' is a custom command, return the expanded message; otherwise None."""
    if not text.startswith("/"):
        return None
    name, args = _split(text)
    cmd = load_custom(cwd).get(name.lower())
    return cmd.expand(args) if cmd else None


def _split(text: str) -> tuple[str, str]:
    """'/name args' -> (name, args). The name ends at the first space or at the end of the line:
    "/kurul\\nkonu" (Shift+Enter). It used to split only at a space, so the name became "kurul\\nkonu" and
    "bilinmeyen komut" ("unknown command") was reported."""
    parts = text[1:].split(None, 1)
    return (parts[0] if parts else ""), (parts[1].strip() if len(parts) > 1 else "")
