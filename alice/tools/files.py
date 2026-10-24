from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from alice.tools.shell import NO_WINDOW
from alice.tools.base import WRITE, Tool, ToolContext, ToolError, ToolResult, make_diff, obj

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
             "dist", "build", ".idea", ".vs", "bin", "obj", "Library", "Temp", ".gradle", "AppData",
             "site-packages"}
# Dosya araması sınırları: ev klasörü gibi büyük bir yerde aranınca ajan dakikalarca takılmasın.
SEARCH_SECONDS = 20.0
SEARCH_MAX_FILES = 20000
SEARCH_MAX_BYTES = 2_000_000


def _skip_dir(name: str) -> bool:
    return name in SKIP_DIRS or name.startswith(".")


def glob_regex(pattern: str) -> "re.Pattern[str]":
    """Glob desenini (ör. '**/*.py', 'src/**/*.cs', '*.txt') göreli yola uyan regex'e çevir.
    '**/' sıfır ya da daha fazla klasör, '*' ve '?' tek klasör adı içinde eşleşir. Windows'ta büyük/küçük harf duyarsız."""
    pat = pattern.replace("\\", "/")
    while pat.startswith("./"):
        pat = pat[2:]
    out, i = "", 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pat.startswith("**", i):
            out += ".*"
            i += 2
        elif pat[i] == "*":
            out += "[^/]*"
            i += 1
        elif pat[i] == "?":
            out += "[^/]"
            i += 1
        elif pat[i] == "[":
            j = pat.find("]", i + 1)
            if j == -1:
                out += re.escape(pat[i])
                i += 1
            else:
                body = pat[i + 1:j]
                out += "[" + ("^" + body[1:] if body.startswith("!") else body) + "]"
                i = j + 1
        else:
            out += re.escape(pat[i])
            i += 1
    return re.compile(out + r"\Z", re.IGNORECASE if os.name == "nt" else 0)


def _limit_note(reason: str) -> str:
    return (f"[ARAMA YARIDA KESİLDİ: {reason}. Sonuç eksik; 'eşleşme yok' aranan şeyin olmadığı anlamına GELMEZ. "
            "Daha dar bir klasör (path) ya da dosya filtresi (glob) vererek tekrar ara.]\n")


def _with_note(body: str, note: str) -> ToolResult:
    # Kesilen aramada notu başa koy ki zayıf modeller "eşleşme yok"u kesin sonuç sanmasın.
    return ToolResult(note + body, is_error=bool(note))


def _is_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return b"\0" in f.read(4096)
    except OSError:
        return True


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8", "utf-8-sig", "cp1254", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def snapshot(ctx: ToolContext, p: Path) -> None:
    """Geri sarma için: bu turda dosyaya ilk dokunulmadan önceki halini sakla."""
    cp = ctx.state.get("checkpoint")
    if cp is None:
        return
    key = str(p)
    if key not in cp:
        try:
            cp[key] = p.read_bytes() if p.exists() else None
        except OSError:
            pass


def _page_range(pages: str | None, total: int) -> list[int]:
    if not pages:
        return list(range(total))
    out: list[int] = []
    for part in str(pages).replace(" ", "").split(","):
        a, _, b = part.partition("-")
        try:
            lo, hi = int(a), int(b or a)
        except ValueError as e:
            raise ToolError(f"pages anlaşılamadı: {pages!r} (ör. '3' ya da '1-5')") from e
        out += [i - 1 for i in range(max(lo, 1), min(hi, total) + 1)]
    if not out:
        raise ToolError(f"pages={pages!r} aralık dışında; PDF {total} sayfa")
    return out


def read_pdf(p: Path, pages: str | None, max_chars: int) -> str:
    """PDF'ten metin: pypdf varsa onunla, yoksa pdftotext (poppler) ile."""
    try:
        from pypdf import PdfReader
    except ImportError:
        PdfReader = None
    if PdfReader is not None:
        try:
            reader = PdfReader(str(p))
            total = len(reader.pages)
            parts = [f"--- sayfa {i + 1}/{total} ---\n{(reader.pages[i].extract_text() or '').strip()}"
                     for i in _page_range(pages, total)]
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001 - bozuk/şifreli PDF
            raise ToolError(f"PDF okunamadı: {type(e).__name__}: {e}") from e
        text = "\n\n".join(parts)
    elif shutil.which("pdftotext"):
        cmd = ["pdftotext", "-layout"]
        if pages:
            a, _, b = str(pages).partition("-")
            cmd += ["-f", a.strip(), "-l", (b or a).strip()]
        r = subprocess.run(cmd + [str(p), "-"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=60, creationflags=NO_WINDOW)
        if r.returncode != 0:
            raise ToolError(f"pdftotext hatası: {r.stderr.strip()[:300]}")
        text = r.stdout
    else:
        raise ToolError("PDF okumak için pypdf gerekli: run_command ile 'pip install pypdf' çalıştır, sonra tekrar dene")
    if not text.strip() or not re.sub(r"--- sayfa \d+/\d+ ---", "", text).strip():
        return f"{p}: PDF'te metin yok (taranmış görüntü olabilir; sayfaları görmek için ekran/görme araçlarını kullan)"
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... [kesildi; pages ile daha küçük aralık iste]"
    return text


def render_notebook(p: Path) -> str:
    """Jupyter defterini hücre hücre okunur metne çevir (kaynak + metin çıktıları)."""
    import json

    try:
        nb = json.loads(_read_text(p))
    except ValueError as e:
        raise ToolError(f"ipynb okunamadı: {e}") from e
    out: list[str] = []
    for i, cell in enumerate(nb.get("cells") or [], 1):
        src = cell.get("source") or ""
        src = "".join(src) if isinstance(src, list) else str(src)
        out.append(f"# %% hücre {i} [{cell.get('cell_type', '?')}]")
        out += src.splitlines()
        for o in cell.get("outputs") or []:
            txt = o.get("text") or (o.get("data") or {}).get("text/plain") or o.get("evalue") or ""
            txt = "".join(txt) if isinstance(txt, list) else str(txt)
            if txt.strip():
                out.append("# çıktı:")
                out += [f"# {ln}" for ln in txt.splitlines()[:30]]
    return "\n".join(out)


class ReadFile(Tool):
    name = "read_file"
    parallel_safe = True
    description = ("Bir dosyanın içeriğini satır numaralarıyla okur; png/jpg görseller görüntü olarak, PDF metin olarak, "
                   ".ipynb hücreleri ve çıktılarıyla döner.\n"
                   "- Düzenlemeden önce dosyayı bununla oku; edit_file'a buradan birebir kopyala (satır numarası hariç).\n"
                   "- Büyük dosyada önce grep ile yeri bul, sonra offset/limit ile o bölümü oku.\n"
                   "- Klasör için list_dir, dosya aramak için glob kullan.")
    parameters = obj({
        "path": {"type": "string", "description": "Dosya yolu; mutlak ya da çalışma klasörüne göre, ör. 'src/main.py'."},
        "offset": {"type": "integer", "description": "Okumaya başlanacak satır (1'den başlar). Varsayılan 1."},
        "limit": {"type": "integer", "description": "Okunacak en fazla satır. Varsayılan 2000."},
        "pages": {"type": "string", "description": "Sadece PDF: okunacak sayfalar, ör. '3' ya da '1-5'. Varsayılan: "
                  "hepsi (büyük PDF'te sayfa ver)."},
    }, ["path"])

    def summary(self, args: dict[str, Any]) -> str:
        extra = f" (satır {args['offset']}+)" if args.get("offset") else ""
        return f"{args.get('path', '')}{extra}"

    def run(self, ctx: ToolContext, path: str, offset: int = 1, limit: int = 2000, pages: str | None = None) -> ToolResult:
        p = ctx.resolve(path)
        if not p.exists():
            raise ToolError(f"dosya yok: {p}")
        if p.is_dir():
            raise ToolError(f"{p} bir klasör, list_dir kullan")
        if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"):
            from alice.tools.imaging import file_to_png_b64
            return ToolResult(f"görüntü: {p}", images=[file_to_png_b64(p, ctx.config.agent.get("screenshot_max_width", 1366))])
        if p.suffix.lower() == ".pdf":
            return ToolResult(read_pdf(p, pages, int(ctx.config.agent.get("tool_output_chars", 12000)) * 4))
        if _is_binary(p):
            raise ToolError(f"{p} binary bir dosya ({p.stat().st_size} byte)")
        text = render_notebook(p) if p.suffix.lower() == ".ipynb" else _read_text(p)
        lines = text.splitlines()
        start = max(offset, 1)
        chunk = lines[start - 1:start - 1 + limit]
        body = "\n".join(f"{i:>6}\t{line[:2000]}" for i, line in enumerate(chunk, start))
        more = len(lines) - (start - 1 + len(chunk))
        if more > 0:
            body += f"\n... ({more} satır daha, offset={start + len(chunk)} ile devam et)"
        return ToolResult(body or "(boş dosya)")


class WriteFile(Tool):
    name = "write_file"
    description = ("Yeni dosya oluşturur ya da var olan dosyanın TAMAMINI verilen içerikle değiştirir; eksik klasörler "
                   "açılır.\n"
                   "- Var olan dosyada küçük değişiklik için bunu değil edit_file/multi_edit kullan; tüm dosyayı yeniden "
                   "yazmak hem yavaş hem riskli.\n"
                   "- Var olan bir dosyanın üzerine yazmadan önce read_file ile oku.")
    parameters = obj({
        "path": {"type": "string", "description": "Yazılacak dosyanın yolu; mutlak ya da çalışma klasörüne göre."},
        "content": {"type": "string", "description": "Dosyanın yeni içeriğinin tamamı."},
    }, ["path", "content"])
    risk = WRITE

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('path')} ({len(args.get('content', ''))} karakter)"

    def run(self, ctx: ToolContext, path: str, content: str) -> ToolResult:
        p = ctx.resolve(path)
        snapshot(ctx, p)
        p.parent.mkdir(parents=True, exist_ok=True)
        existed = p.exists()
        before = _read_text(p) if existed and not _is_binary(p) else ""
        p.write_text(content, encoding="utf-8", newline="")
        return ToolResult(f"{'güncellendi' if existed else 'oluşturuldu'}: {p} ({len(content.splitlines())} satır)",
                          diff=make_diff(path, before, content))


def _apply_edit(text: str, old: str, new: str, replace_all: bool, where: str = "") -> tuple[str, int]:
    """Bir değişikliği bellekteki metne uygula. Bulunamazsa ya da belirsizse ToolError."""
    if old == new:
        raise ToolError(f"{where}old_string ve new_string aynı")
    if not old:
        raise ToolError(f"{where}old_string boş olamaz")
    count = text.count(old)
    if count == 0 and "\r\n" in text:
        # CRLF dosyalarda modelin LF göndermesi çok yaygın.
        old_crlf = old.replace("\r\n", "\n").replace("\n", "\r\n")
        if old_crlf in text:
            old, new = old_crlf, new.replace("\r\n", "\n").replace("\n", "\r\n")
            count = text.count(old)
    if count == 0:
        raise ToolError(f"{where}old_string dosyada bulunamadı. Dosyayı read_file ile tekrar oku ve birebir kopyala.")
    if count > 1 and not replace_all:
        raise ToolError(f"{where}old_string {count} kez geçiyor; bağlam ekle ya da replace_all=true kullan")
    if replace_all:
        return text.replace(old, new), count
    return text.replace(old, new, 1), 1


class EditFile(Tool):
    name = "edit_file"
    description = ("Var olan bir dosyada birebir metin değiştirir. old_string dosyada tam olarak (girinti ve boşluklar dahil) ve "
                   "tek bir kez geçmeli; birden fazla geçiyorsa daha fazla bağlam ekle ya da replace_all=true ver. "
                   "Aynı dosyada birden fazla değişiklik için multi_edit kullan.")
    parameters = obj({
        "path": {"type": "string", "description": "Düzenlenecek dosyanın yolu."},
        "old_string": {"type": "string", "description": "Değiştirilecek metin; read_file çıktısından birebir, satır "
                       "numarası olmadan. Tek olması için gerekirse önceki/sonraki satırları da ekle."},
        "new_string": {"type": "string", "description": "Yerine yazılacak metin; old_string'den farklı olmalı."},
        "replace_all": {"type": "boolean", "description": "true: old_string'in geçtiği her yeri değiştir (ör. yeniden "
                        "adlandırma). Varsayılan false."},
    }, ["path", "old_string", "new_string"])
    risk = WRITE

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("path"))

    def run(self, ctx: ToolContext, path: str, old_string: str, new_string: str, replace_all: bool = False) -> ToolResult:
        p = ctx.resolve(path)
        if not p.is_file():
            raise ToolError(f"dosya yok: {p}")
        before = _read_text(p)
        text, n = _apply_edit(before, old_string, new_string, replace_all)
        snapshot(ctx, p)
        p.write_text(text, encoding="utf-8", newline="")
        return ToolResult(f"düzenlendi: {p} ({n} değişiklik)",
                          diff=make_diff(path, before.replace("\r\n", "\n"), text.replace("\r\n", "\n")))


class MultiEdit(Tool):
    name = "multi_edit"
    description = ("Tek dosyada birden fazla değişikliği tek seferde yap. edits sırayla uygulanır (her biri bir öncekinin "
                   "sonucuna); biri bile başarısız olursa dosyaya HİÇBİR şey yazılmaz. Her edit edit_file kurallarına "
                   "uyar: old_string birebir ve tek olmalı ya da replace_all=true. Yeni dosya için write_file kullan.")
    parameters = obj({
        "path": {"type": "string", "description": "Düzenlenecek dosyanın yolu."},
        "edits": {"type": "array", "minItems": 1, "description": "Sırayla uygulanacak değişiklikler.", "items": obj({
            "old_string": {"type": "string", "description": "Değiştirilecek metin (birebir)."},
            "new_string": {"type": "string", "description": "Yerine yazılacak metin."},
            "replace_all": {"type": "boolean", "description": "Her geçtiği yeri değiştir. Varsayılan false."},
        }, ["old_string", "new_string"])},
    }, ["path", "edits"])
    risk = WRITE

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('path')} ({len(args.get('edits') or [])} değişiklik)"

    def run(self, ctx: ToolContext, path: str, edits: list[dict[str, Any]]) -> ToolResult:
        p = ctx.resolve(path)
        if not p.is_file():
            raise ToolError(f"dosya yok: {p}")
        if not edits:
            raise ToolError("edits boş")
        before = _read_text(p)
        text = before
        total = 0
        for i, e in enumerate(edits, 1):
            if not isinstance(e, dict) or "old_string" not in e or "new_string" not in e:
                raise ToolError(f"edit {i}: old_string ve new_string gerekli")
            text, n = _apply_edit(text, str(e["old_string"]), str(e["new_string"]), bool(e.get("replace_all")),
                                  where=f"edit {i}/{len(edits)}: ")
            total += n
        snapshot(ctx, p)
        p.write_text(text, encoding="utf-8", newline="")
        return ToolResult(f"düzenlendi: {p} ({len(edits)} edit, {total} değişiklik)",
                          diff=make_diff(path, before.replace("\r\n", "\n"), text.replace("\r\n", "\n")))


# Kullanıcının "İndirilenler", "Masaüstü" dediği klasörler. Model bunları bulmak için onlarca PowerShell komutu
# deniyordu (voice.log: bir dakikada 40+ run_command); adı doğrudan klasöre çevrilir.
KNOWN_FOLDERS = {
    "downloads": ["Downloads"], "indirilenler": ["Downloads"], "i̇ndirilenler": ["Downloads"],
    "desktop": ["OneDrive/Desktop", "OneDrive/Masaüstü", "Desktop"], "masaüstü": ["OneDrive/Desktop",
                                                                                "OneDrive/Masaüstü", "Desktop"],
    "documents": ["OneDrive/Documents", "OneDrive/Belgeler", "Documents"],
    "belgeler": ["OneDrive/Documents", "OneDrive/Belgeler", "Documents"],
    "pictures": ["OneDrive/Pictures", "Pictures"], "resimler": ["OneDrive/Pictures", "Pictures"],
    "videos": ["Videos"], "videolar": ["Videos"], "music": ["Music"], "müzik": ["Music"],
}


def known_folder(path: str) -> Path | None:
    """'İndirilenler', 'downloads', 'Masaüstü' -> kullanıcının o klasörü (OneDrive'a taşınmışsa orası)."""
    key = path.strip().strip("/\\").lower()
    for cand in KNOWN_FOLDERS.get(key, []):
        p = Path.home() / cand
        if p.is_dir():
            return p
    return None


def _when(ts: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


class ListDir(Tool):
    name = "list_dir"
    parallel_safe = True
    description = ("Bir klasördeki dosya ve alt klasörleri boyut ve türleriyle listeler (alt klasörlerin içine inmez). "
                   "'İndirilenler', 'Masaüstü', 'Belgeler' gibi adları doğrudan path olarak ver. 'Son indirdiğim dosya' "
                   "gibi sorular için sort=newest (en yeni önce, tarihle). Klasör içeriği için run_command kullanma. "
                   "Desene göre dosya aramak için glob, içerik aramak için grep kullan.")
    parameters = obj({
        "path": {"type": "string", "description": "Klasör yolu ya da 'İndirilenler', 'Masaüstü', 'Belgeler'. "
                 "Varsayılan: çalışma klasörü."},
        "sort": {"type": "string", "enum": ["name", "newest"], "description": "name (varsayılan) ya da newest: "
                 "en son değişen önce, tarihleriyle."},
        "limit": {"type": "integer", "description": "En fazla kaç öğe (varsayılan 500; newest için 30)."},
    })

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("path") or ".")

    def run(self, ctx: ToolContext, path: str = ".", sort: str = "name", limit: int | None = None) -> ToolResult:
        p = known_folder(path) or ctx.resolve(path)
        if not p.is_dir():
            raise ToolError(f"klasör yok: {p}")
        newest = sort == "newest"
        limit = max(1, min(int(limit or (30 if newest else 500)), 500))

        def mtime(x: Path) -> float:
            try:
                return x.stat().st_mtime
            except OSError:
                return 0.0

        entries = list(p.iterdir())
        entries.sort(key=(lambda x: -mtime(x)) if newest else (lambda x: (not x.is_dir(), x.name.lower())))
        rows = []
        for e in entries[:limit]:
            try:
                when = f", {_when(e.stat().st_mtime)}" if newest else ""
                rows.append(f"{e.name}/{'  (' + when[2:] + ')' if when else ''}" if e.is_dir()
                            else f"{e.name}  ({e.stat().st_size} B{when})")
            except OSError:
                rows.append(f"{e.name}  (erişilemedi)")
        more = f"\n... ve {len(entries) - limit} öğe daha" if len(entries) > limit else ""
        return ToolResult(f"{p}\n" + "\n".join(rows) + more if rows else f"{p} boş")


class Glob(Tool):
    name = "glob"
    parallel_safe = True
    description = ("Adı desene uyan dosya ve klasörleri bulur; en son değiştirilen önce, en fazla 300 sonuç. Dosyanın "
                   "İÇİNDE metin aramak için grep kullan. Kurulu bir programı bulmak için bunu değil windows action=find "
                   "kullan. Kullanıcı klasörü ya da disk kökü gibi büyük yerlerde '**' arama yavaştır; dar bir path ver.")
    parameters = obj({
        "pattern": {"type": "string", "description": "Glob deseni, path'e göre: '**/*.py' (her derinlik), '*.txt' "
                    "(sadece o klasör), 'src/**/test_*.py'."},
        "path": {"type": "string", "description": "Aramanın başlayacağı klasör. Varsayılan: çalışma klasörü."},
    }, ["pattern"])

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("pattern", ""))

    def run(self, ctx: ToolContext, pattern: str, path: str = ".") -> ToolResult:
        import time

        base = ctx.resolve(path)
        rx = glob_regex(pattern)
        # "**" yoksa desen derinliği sınırlı: daha derine inmeye gerek yok.
        max_depth = None if "**" in pattern else pattern.replace("\\", "/").strip("/").count("/")
        hits: list[Path] = []
        note = ""
        deadline = time.monotonic() + SEARCH_SECONDS
        # Path.glob sadece eşleşmeleri verir: az eşleşmede süre/durdurma hiç kontrol edilmiyor ve atlanacak
        # klasörlere (AppData, node_modules) yine de iniyordu. Kendimiz yürüyoruz, klasör başına kontrol ediyoruz.
        for root, dirs, files in os.walk(base):
            if ctx.cancelled():
                note = _limit_note("kullanıcı durdurdu")
                break
            if time.monotonic() > deadline:
                note = _limit_note(f"{SEARCH_SECONDS:.0f} sn doldu")
                break
            rel_root = os.path.relpath(root, base).replace("\\", "/")
            rel_root = "" if rel_root == "." else rel_root + "/"
            depth = rel_root.count("/")
            dirs[:] = [d for d in dirs if not _skip_dir(d)] if max_depth is None or depth < max_depth else []
            for name in [*files, *dirs]:
                if rx.match(rel_root + name):
                    hits.append(Path(root) / name)
            if len(hits) >= 2000:
                break
        hits.sort(key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True)
        out = [str(h.relative_to(base)) for h in hits[:300]]
        return _with_note("\n".join(out) if out else "eşleşme yok", note)


class Grep(Tool):
    name = "grep"
    parallel_safe = True
    description = ("Dosyaların içinde düzenli ifade (regex) arar (ripgrep varsa onu kullanır). Kodda/metinde bir şeyin "
                   "nerede geçtiğini bulmak için run_command'da grep/findstr yerine bunu kullan; dosya ADI için glob.\n"
                   "- output_mode: content 'dosya:satır:metin' satırları (varsayılan), files_with_matches sadece dosya "
                   "adları (önce hangi dosyalar diye bakmak için en ucuzu), count dosya başına eşleşme sayısı.\n"
                   "- context ile eşleşmenin etrafındaki satırları da gör; multiline ile satırlar arası desen ara.\n"
                   "- Çok sonuç varsa offset ile sonraki sayfaya geç.")
    parameters = obj({
        "pattern": {"type": "string", "description": "Regex, ör. 'def \\w+_test' ya da 'TODO'. Özel karakterleri "
                    "(. ( [ {) birebir aramak için kaçır."},
        "path": {"type": "string", "description": "Aranacak klasör ya da dosya. Varsayılan: çalışma klasörü."},
        "glob": {"type": "string", "description": "Sadece bu desene uyan dosyalar, ör. '*.py' ya da '*.{ts,tsx}'."},
        "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"],
                        "description": "content (satırlar, varsayılan), files_with_matches (dosya adları), count."},
        "context": {"type": "integer", "description": "content modunda her eşleşmenin önünde ve arkasında gösterilecek "
                    "satır sayısı. Varsayılan 0."},
        "ignore_case": {"type": "boolean", "description": "Büyük/küçük harf duyarsız ara. Varsayılan false."},
        "multiline": {"type": "boolean", "description": "true: desen satır sonlarını aşabilir ('.' satır sonunu da "
                      "kapsar). Varsayılan false."},
        "max_results": {"type": "integer", "description": "En fazla sonuç satırı. Varsayılan 200."},
        "offset": {"type": "integer", "description": "İlk bu kadar sonuç satırını atla (sayfalama). Varsayılan 0."},
    }, ["pattern"])

    def summary(self, args: dict[str, Any]) -> str:
        where = f" ({args['glob']})" if args.get("glob") else ""
        mode = f" [{args['output_mode']}]" if args.get("output_mode") not in (None, "content") else ""
        return f"{args.get('pattern', '')}{where}{mode}"

    def run(self, ctx: ToolContext, pattern: str, path: str = ".", glob: str | None = None,
            output_mode: str = "content", context: int = 0, ignore_case: bool = False, multiline: bool = False,
            max_results: int = 200, offset: int = 0) -> ToolResult:
        base = ctx.resolve(path)
        if not base.exists():
            raise ToolError(f"yol yok: {base}")
        context = max(0, min(int(context or 0), 20))
        max_results = max(1, int(max_results or 200))
        offset = max(0, int(offset or 0))
        multiline = multiline or "\\n" in pattern  # satır sonu arayan desen tek satırda asla eşleşmez
        rg = shutil.which("rg")
        if rg:
            lines, note = self._rg(rg, ctx, base, pattern, glob, output_mode, context, ignore_case, multiline)
        else:
            lines, note = self._py(ctx, base, pattern, glob, output_mode, context, ignore_case, multiline,
                                   offset + max_results)
        total = len(lines)
        page = lines[offset:offset + max_results]
        body = "\n".join(page) if page else ("eşleşme yok" if not offset else f"bu sayfada sonuç yok ({total} satır var)")
        if total > offset + max_results:
            body += f"\n... (daha fazla sonuç var; offset={offset + max_results} ile devam et ya da aramayı daralt)"
        return _with_note(body, note)

    @staticmethod
    def _rg(rg: str, ctx: ToolContext, base: Path, pattern: str, glob: str | None, mode: str, context: int,
            ignore_case: bool, multiline: bool) -> tuple[list[str], str]:
        cmd = [rg, "--no-heading", "--color", "never", "--max-columns", "300"]
        if mode == "files_with_matches":
            cmd.append("-l")
        elif mode == "count":
            cmd.append("-c")
        else:
            cmd += ["-n", "-m", "100"]
            if context:
                cmd += ["-C", str(context)]
        if ignore_case:
            cmd.append("-i")
        if multiline:
            cmd += ["-U", "--multiline-dotall"]
            # rg dosyayı ham okur: CRLF dosyada '\n' deseni '\r' yüzünden tutmasın diye '\r?\n' yap.
            pattern = re.sub(r"(?<!\\)\\n", r"\\r?\\n", pattern)
        if glob:
            cmd += ["-g", glob]
        # Göreli yollar: hem okunaklı hem modele daha az token.
        cmd += ["-e", pattern, "." if base.is_dir() else str(base)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                               cwd=base if base.is_dir() else None, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            return [], _limit_note("60 sn doldu")
        if r.returncode == 2 and not r.stdout.strip():
            # rg hatası (desteklenmeyen regex, olmayan yol): "eşleşme yok" diye yutulursa model yanlış sonuca varıyor.
            raise ToolError(f"rg hatası: {(r.stderr or '').strip()[:300]}")
        lines = [ln[2:] if ln.startswith("./") or ln.startswith(".\\") else ln for ln in r.stdout.splitlines()]
        return lines, ""

    def _py(self, ctx: ToolContext, base: Path, pattern: str, glob: str | None, mode: str, context: int,
            ignore_case: bool, multiline: bool, enough: int) -> tuple[list[str], str]:
        import time

        flags = (re.I if ignore_case else 0) | ((re.S | re.M) if multiline else 0)
        try:
            rx = re.compile(pattern, flags)
        except re.error as e:
            raise ToolError(f"geçersiz regex: {e}") from e
        out: list[str] = []
        note = ""
        deadline = time.monotonic() + SEARCH_SECONDS
        files = [base] if base.is_file() else self._walk(base)
        for n, f in enumerate(files):
            if n % 200 == 0:
                if ctx.cancelled():
                    note = _limit_note("kullanıcı durdurdu")
                    break
                if time.monotonic() > deadline:
                    note = _limit_note(f"{SEARCH_SECONDS:.0f} sn doldu")
                    break
            if n >= SEARCH_MAX_FILES:
                note = _limit_note(f"{SEARCH_MAX_FILES} dosyaya bakıldı")
                break
            if glob and not _glob_match(f.name, glob):
                continue
            try:
                if f.stat().st_size > SEARCH_MAX_BYTES or _is_binary(f):
                    continue
                text = _read_text(f).replace("\r\n", "\n")  # CRLF: çok satırlı "\\n" deseni Windows dosyasında da tutsun
            except OSError:
                continue
            shown = str(f.relative_to(base) if base.is_dir() else f)
            lines = text.splitlines()
            if multiline:
                starts = sorted({text.count("\n", 0, m.start()) for m in rx.finditer(text)})
            else:
                starts = [i for i, line in enumerate(lines) if rx.search(line)]
            if not starts:
                continue
            if mode == "files_with_matches":
                out.append(shown)
            elif mode == "count":
                out.append(f"{shown}:{len(starts)}")
            else:
                shown_lines: set[int] = set()
                prev = -2
                for i in starts:
                    for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                        if j in shown_lines:
                            continue
                        if context and prev >= 0 and j > prev + 1:
                            out.append("--")
                        sep = ":" if j == i or (multiline and j in starts) else "-"
                        out.append(f"{shown}{sep}{j + 1}{sep}{lines[j][:300]}")
                        shown_lines.add(j)
                        prev = j
            if len(out) >= enough:
                break
        return out, note

    @staticmethod
    def _walk(base: Path):
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not _skip_dir(d)]
            for name in files:
                yield Path(root) / name


def _glob_match(name: str, pattern: str) -> bool:
    """fnmatch + '*.{ts,tsx}' gibi süslü parantezli seçenekler (rg -g ile aynı davranış)."""
    m = re.search(r"\{([^{}]*)\}", pattern)
    if m:
        return any(_glob_match(name, pattern[:m.start()] + alt + pattern[m.end():]) for alt in m.group(1).split(","))
    return fnmatch.fnmatch(name, pattern)


class FileOps(Tool):
    name = "file_ops"
    description = ("Dosya ve klasör yönetimi: taşı/yeniden adlandır (move), kopyala (copy), sil (delete), klasör aç "
                   "(mkdir). run_command'da move/copy/del/rm yerine bunu kullan: Windows/Linux farkı yok ve silinenler "
                   "kalıcı silinmez, ALICE çöp klasörüne taşınır (geri alınabilir).\n"
                   "- Birden fazla dosya için paths listesi ver; destination o zaman bir klasör olmalı.\n"
                   "- Hedef varsa üzerine yazmaz; overwrite=true ver.")
    parameters = obj({
        "action": {"type": "string", "enum": ["move", "copy", "delete", "mkdir"], "description": "Yapılacak işlem."},
        "path": {"type": "string", "description": "Kaynak dosya/klasör (mkdir'de açılacak klasör)."},
        "paths": {"type": "array", "items": {"type": "string", "description": "Kaynak yol."},
                  "description": "Birden fazla kaynak (path yerine)."},
        "destination": {"type": "string", "description": "move/copy hedefi: yeni yol ya da içine konacak klasör."},
        "overwrite": {"type": "boolean", "description": "Hedef varsa üzerine yaz. Varsayılan false."},
    }, ["action"])
    risk = WRITE

    def risk_for(self, args: dict[str, Any]) -> str:
        from alice.permissions import is_system_path
        from alice.tools.base import DANGER

        n = len(args.get("paths") or []) + (1 if args.get("path") else 0)
        if args.get("action") == "delete" and n > 20:
            return DANGER
        for key in ("destination", *(["paths"] if args.get("paths") else [])):
            vals = args.get(key) or []
            for v in vals if isinstance(vals, list) else [vals]:
                try:
                    if is_system_path(Path(str(v)).expanduser().resolve()):
                        return DANGER
                except (OSError, ValueError):
                    return DANGER
        return WRITE

    def summary(self, args: dict[str, Any]) -> str:
        src = args.get("path") or ", ".join(map(str, args.get("paths") or []))
        dest = f" -> {args['destination']}" if args.get("destination") else ""
        return f"{args.get('action')} {src}{dest}"[:160]

    def run(self, ctx: ToolContext, action: str, path: str | None = None, paths: list[str] | None = None,
            destination: str | None = None, overwrite: bool = False) -> ToolResult:
        sources = [ctx.resolve(x) for x in ([path] if path else []) + list(paths or [])]
        if not sources:
            raise ToolError("path ya da paths gerekli")
        if action == "mkdir":
            for s in sources:
                s.mkdir(parents=True, exist_ok=True)
            return ToolResult("klasör hazır: " + ", ".join(map(str, sources)))
        missing = [str(s) for s in sources if not s.exists()]
        if missing:
            raise ToolError(f"yok: {', '.join(missing)}")
        if action == "delete":
            return ToolResult("çöpe taşındı (geri almak için oradan taşı):\n" +
                              "\n".join(f"{s} -> {self._trash(ctx, s)}" for s in sources))
        if not destination:
            raise ToolError(f"{action} için destination gerekli")
        dest = ctx.resolve(destination)
        into_dir = dest.is_dir() or len(sources) > 1 or destination.endswith(("/", "\\"))
        if len(sources) > 1 and dest.exists() and not dest.is_dir():
            raise ToolError(f"birden fazla kaynak için destination bir klasör olmalı: {dest}")
        plan = [(s, dest / s.name if into_dir else dest) for s in sources]
        for s, d in plan:
            if d == s or s in d.parents:
                raise ToolError(f"{s} kendi içine {action} edilemez")
            if d.exists() and not overwrite:
                raise ToolError(f"hedef zaten var: {d} (üzerine yazmak için overwrite=true)")
        done = []
        for s, d in plan:
            d.parent.mkdir(parents=True, exist_ok=True)
            if d.exists():
                self._trash(ctx, d)
            if action == "move":
                if s.is_file():
                    snapshot(ctx, s)
                shutil.move(str(s), str(d))
            elif action == "copy":
                snapshot(ctx, d)
                if s.is_dir():
                    shutil.copytree(s, d)
                else:
                    shutil.copy2(s, d)
            done.append(f"{s} -> {d}")
        return ToolResult(f"{'taşındı' if action == 'move' else 'kopyalandı'}:\n" + "\n".join(done))

    @staticmethod
    def _trash(ctx: ToolContext, p: Path) -> Path:
        import time

        from alice.config import alice_home

        if p.is_file():
            snapshot(ctx, p)
        box = alice_home() / "trash" / time.strftime("%Y%m%d-%H%M%S")
        box.mkdir(parents=True, exist_ok=True)
        target = box / p.name
        k = 1
        while target.exists():
            target = box / f"{p.stem}-{k}{p.suffix}"
            k += 1
        shutil.move(str(p), str(target))
        return target


class NotebookEdit(Tool):
    name = "notebook_edit"
    description = ("Jupyter defterinde (.ipynb) tek hücreyi değiştirir, araya yeni hücre ekler ya da siler; dosyanın geri "
                   "kalanına ve diğer hücrelerin çıktılarına dokunmaz. Önce read_file ile oku (hücre numaraları orada). "
                   "Değiştirilen kod hücresinin eski çıktısı silinir.")
    parameters = obj({
        "path": {"type": "string", "description": ".ipynb dosyasının yolu."},
        "cell": {"type": "integer", "description": "Hücre numarası (0'dan başlar). insert'te yeni hücre bu numaraya girer "
                 "(hücre sayısı = sona ekle)."},
        "mode": {"type": "string", "enum": ["replace", "insert", "delete"], "description": "replace: hücrenin içeriğini "
                 "değiştir | insert: yeni hücre ekle | delete: hücreyi sil. Varsayılan replace."},
        "source": {"type": "string", "description": "Hücrenin yeni içeriği (replace/insert)."},
        "cell_type": {"type": "string", "enum": ["code", "markdown"], "description": "insert için hücre türü; replace'te "
                      "verilirse tür de değişir. Varsayılan code."},
    }, ["path", "cell"])
    risk = WRITE

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('path')} hücre {args.get('cell')} ({args.get('mode') or 'replace'})"

    def run(self, ctx: ToolContext, path: str, cell: int, mode: str = "replace", source: str = "",
            cell_type: str = "") -> ToolResult:
        import json as _json

        p = ctx.resolve(path)
        if p.suffix.lower() != ".ipynb" or not p.is_file():
            raise ToolError(f"Jupyter defteri bulunamadı: {p}")
        before = _read_text(p)
        try:
            nb = _json.loads(before)
        except ValueError as e:
            raise ToolError(f"defter okunamadı (bozuk JSON): {e}") from e
        cells = nb.get("cells")
        if not isinstance(cells, list):
            raise ToolError("defterde hücre listesi yok")
        n = len(cells)
        lines = source.splitlines(keepends=True)
        if mode == "insert":
            if not 0 <= cell <= n:
                raise ToolError(f"insert için hücre 0..{n} arasında olmalı")
            kind = cell_type or "code"
            new_cell: dict[str, Any] = {"cell_type": kind, "metadata": {}, "source": lines}
            if kind == "code":
                new_cell.update(execution_count=None, outputs=[])
            cells.insert(cell, new_cell)
            done = f"hücre {cell} eklendi ({kind})"
        elif not 0 <= cell < n:
            raise ToolError(f"hücre {cell} yok (defterde {n} hücre var, 0..{n - 1})")
        elif mode == "delete":
            cells.pop(cell)
            done = f"hücre {cell} silindi"
        elif mode == "replace":
            c = cells[cell]
            c["source"] = lines
            if cell_type and cell_type != c.get("cell_type"):
                c["cell_type"] = cell_type
                if cell_type == "markdown":
                    c.pop("outputs", None)
                    c.pop("execution_count", None)
            if c.get("cell_type") == "code":
                c["outputs"], c["execution_count"] = [], None
            done = f"hücre {cell} değiştirildi"
        else:
            raise ToolError(f"bilinmeyen mode: {mode}")
        snapshot(ctx, p)
        after = _json.dumps(nb, ensure_ascii=False, indent=1) + "\n"
        p.write_text(after, encoding="utf-8", newline="")
        return ToolResult(f"{done}: {p} (toplam {len(cells)} hücre)", diff=make_diff(path, before, after))


TOOLS = [ReadFile(), WriteFile(), EditFile(), MultiEdit(), ListDir(), Glob(), Grep(), FileOps(), NotebookEdit()]
