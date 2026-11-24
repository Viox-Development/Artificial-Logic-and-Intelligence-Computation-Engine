"""Turn a web page into clean, compact Markdown for a model, without third-party parsers.

The ideas come from crawl4ai (Apache-2.0), reimplemented on the standard library:

* prune():         crawl4ai's PruningContentFilter. Every block gets a score from text density, link density, tag
                   weight, class/id hints and text length; low scoring blocks (menus, footers, cookie bars, share
                   rows, sidebars) are dropped. This replaces "keep <main> if there is one".
* to_markdown():   headings, lists, tables, code blocks and quotes survive; links become `text⟨3⟩` with one numbered
                   reference list at the end ("markdown with citations"), so a page full of links costs a fraction
                   of the tokens and the model can still follow "link 3".
* bm25_select():   crawl4ai's BM25ContentFilter. The page is split into chunks and only the ones relevant to a
                   question are kept, with matches in headings counting more. web_fetch(prompt=...) sends those to
                   the small model instead of the first 60k characters.
* detect_block():  crawl4ai's antibot detector: tells a real page from a 403/429, a Cloudflare/Akamai/DataDome
                   challenge, a captcha wall or an empty JavaScript shell, so web_fetch knows to escalate.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urldefrag, urlparse

# ----------------------------------------------------------------------------------------------- DOM tree
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
# Never content. Removed before anything else (crawl4ai's excluded tags, plus form controls and media).
DROP = {"script", "style", "noscript", "template", "svg", "canvas", "iframe", "object", "embed", "head", "form",
        "button", "select", "input", "textarea", "dialog", "picture", "video", "audio", "map"}
# Structural chrome: dropped unless it is the only thing on the page (some tiny sites put everything in <header>).
CHROME = {"nav", "footer", "aside"}
BLOCK = {"p", "div", "section", "article", "main", "header", "footer", "aside", "nav", "ul", "ol", "li", "dl", "dt",
         "dd", "table", "thead", "tbody", "tfoot", "tr", "td", "th", "blockquote", "pre", "figure", "figcaption",
         "h1", "h2", "h3", "h4", "h5", "h6", "hr", "address", "details", "summary", "body", "html"}
# Elements whose end tag closes an open element of the same kind (HTML's implied end tags), so sloppy markup like
# <p>a<p>b or <li>a<li>b still nests correctly.
AUTOCLOSE = {"p": {"p"}, "li": {"li"}, "dt": {"dt", "dd"}, "dd": {"dt", "dd"}, "tr": {"tr"}, "td": {"td", "th"},
             "th": {"td", "th"}, "option": {"option"}}
# A block-level start tag closes an open <p> (HTML parsing rules).
CLOSES_P = BLOCK - {"li", "dt", "dd", "td", "th", "tr", "thead", "tbody", "tfoot", "body", "html"}


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node | str"] = field(default_factory=list)
    parent: "Node | None" = None

    def text(self) -> str:
        out: list[str] = []
        stack: list[Node | str] = [self]
        while stack:
            n = stack.pop()
            if isinstance(n, str):
                out.append(n)
            else:
                stack.extend(reversed(n.children))
        return "".join(out)

    def link_text_len(self) -> int:
        total = 0
        stack: list[Node | str] = [self]
        while stack:
            n = stack.pop()
            if isinstance(n, str):
                continue
            if n.tag == "a":
                total += len(_squash(n.text()))
            else:
                stack.extend(n.children)
        return total

    def iter(self):
        stack: list[Node | str] = [self]
        while stack:
            n = stack.pop()
            if isinstance(n, Node):
                yield n
                stack.extend(reversed(n.children))


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.cur = self.root
        self.title = ""
        self._in_title = False
        self.meta: dict[str, str] = {}

    def _open(self, tag: str) -> bool:
        n = self.cur
        while n is not None:
            if n.tag == tag:
                return True
            n = n.parent
        return False

    def _close_to(self, tag: str) -> None:
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key in ("description", "og:description", "og:title", "robots"):
                self.meta[key] = a.get("content", "")
        if tag in AUTOCLOSE and self.cur.tag in AUTOCLOSE[tag]:
            self.cur = self.cur.parent or self.root
        if tag in CLOSES_P and self.cur.tag == "p":
            self.cur = self.cur.parent or self.root
        node = Node(tag, a, parent=self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and self.cur.tag == tag:
            self.cur = self.cur.parent or self.root

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in VOID:
            return
        if self._open(tag):
            self._close_to(tag)

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if data:
            self.cur.children.append(data)


def parse(html: str) -> tuple[Node, str, dict[str, str]]:
    b = _TreeBuilder()
    try:
        b.feed(html)
        b.close()
    except Exception:  # noqa: BLE001 - HTMLParser is lenient; anything odd still leaves a usable partial tree
        pass
    return b.root, _squash(b.title), b.meta


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


# ------------------------------------------------------------------------------------------------ pruning
# crawl4ai PruningContentFilter defaults.
NEGATIVE = re.compile(r"nav|footer|header|sidebar|ads|comment|promo|advert|social|share|cookie|consent|banner|"
                      r"newsletter|subscribe|popup|modal|breadcrumb|related|recommend|widget|menu|toolbar|skip-link",
                      re.I)
POSITIVE = re.compile(r"article|content|main|post|entry|story|body|text|markdown|prose|docs?|readme", re.I)
TAG_WEIGHTS = {"div": 0.5, "p": 1.0, "article": 1.5, "section": 1.0, "span": 0.3, "li": 0.5, "ul": 0.5, "ol": 0.5,
               "h1": 1.2, "h2": 1.1, "h3": 1.0, "h4": 0.9, "h5": 0.8, "h6": 0.7, "pre": 1.5, "code": 1.5,
               "table": 1.2, "blockquote": 1.2, "main": 1.5, "td": 0.6, "th": 0.6, "dl": 0.8}
W_TEXT, W_LINK, W_TAG, W_CLASS, W_LEN = 0.4, 0.2, 0.2, 0.1, 0.1
THRESHOLD = 0.48
MIN_WORDS = 2
# Never pruned: dropping them loses the meaning (a code block or table has little "prose" but is the answer).
KEEP = {"pre", "code", "table", "h1", "h2", "h3", "blockquote"}


def _hint(n: Node) -> str:
    return f"{n.attrs.get('class', '')} {n.attrs.get('id', '')} {n.attrs.get('role', '')} {n.attrs.get('aria-label', '')}"


def _card_link_len(n: Node) -> int:
    """Link text, except links that wrap a whole card (a heading plus a summary inside one <a>, the news/blog-index
    pattern): those are content with a link, not navigation."""
    total = 0
    stack: list[Node | str] = [n]
    while stack:
        x = stack.pop()
        if isinstance(x, str):
            continue
        if x.tag == "a":
            t = _squash(x.text())
            heading = any(d.tag in ("h1", "h2", "h3", "h4") for d in x.iter())
            if not (heading and len(t.split()) >= 8):
                total += len(t)
        else:
            stack.extend(x.children)
    return total


def _score(n: Node, text: str, html_len: int) -> float:
    tl = len(text)
    link = _card_link_len(n)
    text_density = tl / html_len if html_len else 0.0
    link_density = 1 - (link / tl if tl else 1)
    tag = TAG_WEIGHTS.get(n.tag, 0.5)
    hint = _hint(n)
    cls = 0.5
    if NEGATIVE.search(hint):
        cls -= 0.5
    if POSITIVE.search(hint):
        cls += 0.5
    length = math.log(tl + 1)
    return (W_TEXT * min(text_density, 1.0) + W_LINK * link_density + W_TAG * tag + W_CLASS * cls
            + W_LEN * min(length / 10, 1.0)) / (W_TEXT + W_LINK + W_TAG + W_CLASS + W_LEN)


def _approx_html_len(n: Node) -> int:
    """Rough serialized size: text plus ~ 2*(tag + attrs) per element. Only the ratio matters."""
    size = 0
    for el in n.iter():
        size += 2 * len(el.tag) + 5 + sum(len(k) + len(v) + 4 for k, v in el.attrs.items())
    return size + len(n.text())


def prune(root: Node, threshold: float = THRESHOLD) -> Node:
    """Drop non-content subtrees in place (crawl4ai pruning) and return the root."""
    # Hard removals first.
    for el in list(root.iter()):
        if el.parent is None:
            continue
        hidden = "hidden" in el.attrs or el.attrs.get("aria-hidden") == "true" or \
            re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", el.attrs.get("style", ""), re.I)
        if el.tag in DROP or hidden or el.attrs.get("role") in ("navigation", "banner", "contentinfo", "dialog"):
            el.parent.children = [c for c in el.parent.children if c is not el]
    full = _squash(root.text())
    # Structural chrome, unless removing it would leave almost nothing (some sites put the article in <header>).
    for el in [e for e in root.iter() if e.tag in CHROME and e.parent is not None]:
        if len(full) - len(_squash(el.text())) > 200:
            el.parent.children = [c for c in el.parent.children if c is not el]
    _prune_children(root, threshold)
    return root


# Parts of a structure that survived as a whole: judging items one by one dropped short list items or table cells
# that happen to hold a link ("Second point with a link"), leaving half a list.
WHOLE = {"ul", "ol", "dl", "table", "thead", "tbody", "tfoot", "tr"}


def _prune_children(n: Node, threshold: float) -> None:
    if n.tag in WHOLE:
        return
    for child in list(n.children):
        if isinstance(child, str) or child.tag not in BLOCK or child.tag in KEEP:
            if isinstance(child, Node) and child.tag not in KEEP:
                _prune_children(child, threshold)
            continue
        text = _squash(child.text())
        words = len(text.split())
        if words < MIN_WORDS and not any(d.tag in KEEP or d.tag == "img" for d in child.iter()):
            n.children = [c for c in n.children if c is not child]
            continue
        score = _score(child, text, _approx_html_len(child))
        if score < threshold and not any(d.tag in KEEP for d in child.iter()) \
                and not _has_good_part(child, threshold):
            n.children = [c for c in n.children if c is not child]
            continue
        _prune_children(child, threshold)


def _has_good_part(n: Node, threshold: float) -> bool:
    """Nested wrapper <div>s (div > div > a > div > h2) score low on their own: lots of markup, little text of their
    own. Removing the wrapper used to throw away the article card inside it. Keep it when a descendant block with
    real text (a heading, a paragraph) scores well on its own; that descendant is judged again on the way down."""
    for d in n.iter():
        if d is n or d.tag not in ("p", "h1", "h2", "h3", "h4", "article", "blockquote", "li", "dd", "td"):
            continue
        t = _squash(d.text())
        if len(t.split()) >= 6 and not NEGATIVE.search(_hint(d)) and _score(d, t, _approx_html_len(d)) >= threshold:
            return True
    return False


# ----------------------------------------------------------------------------------------------- markdown
@dataclass
class Page:
    title: str
    markdown: str
    links: list[tuple[str, str]]  # (url, anchor text), numbered from 1 in order of first use
    description: str = ""
    raw_text_len: int = 0  # visible text before pruning (block detection, "is it a JS shell")


class _MD:
    def __init__(self, base: str) -> None:
        self.base = base
        self.out: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._index: dict[str, int] = {}

    def link(self, href: str, text: str) -> str:
        href = (href or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
            return text
        url = urldefrag(urljoin(self.base, href))[0]
        if not url.startswith(("http://", "https://")):
            return text
        i = self._index.get(url)
        if i is None:
            self.links.append((url, _squash(text)[:80]))
            i = self._index[url] = len(self.links)
        return f"{text}⟨{i}⟩" if text else f"⟨{i}⟩"

    def inline(self, n: Node | str) -> str:
        if isinstance(n, str):
            return re.sub(r"\s+", " ", n)
        t = n.tag
        inner = "".join(self.inline(c) for c in n.children)
        if t == "br":
            return "\n"
        if t == "img":
            alt = _squash(n.attrs.get("alt", ""))
            return f"[image: {alt}]" if alt else ""
        if t == "a":
            return self.link(n.attrs.get("href", ""), _squash(inner))
        if t in ("strong", "b") and inner.strip():
            return f"**{inner.strip()}**"
        if t in ("em", "i") and inner.strip():
            return f"*{inner.strip()}*"
        if t == "code" and inner.strip():
            return f"`{inner.strip()}`"
        if t in BLOCK:  # a block inside inline context (sloppy markup): keep it on its own line
            return "\n" + self.block_text(n) + "\n"
        return inner

    def block_text(self, n: Node) -> str:
        sub = _MD(self.base)
        sub.links, sub._index = self.links, self._index
        sub.render(n.children)
        return "\n".join(sub.out).strip()

    def render(self, nodes: list[Node | str], depth: int = 0) -> None:
        buf: list[str] = []

        def flush() -> None:
            s = _squash("".join(buf)) if "\n" not in "".join(buf) else \
                "\n".join(_squash(x) for x in "".join(buf).split("\n"))
            if s.strip():
                self.out.append(s.strip())
            buf.clear()

        for n in nodes:
            if isinstance(n, str) or n.tag not in BLOCK:
                buf.append(self.inline(n))
                continue
            flush()
            t = n.tag
            if t in ("h1", "h2", "h3", "h4", "h5", "h6"):
                # Sphinx/MkDocs permalink anchors ("¶", "#", "🔗") are noise in a heading.
                text = _squash("".join(self.inline(c) for c in n.children))
                text = re.sub(r"\s*(?:¶|🔗|#)(?:⟨\d+⟩)?\s*$", "", text).strip()
                if text:
                    self.out.append(f"\n{'#' * int(t[1])} {text}")
            elif t == "pre":
                code = n.text().strip("\n")
                lang = ""
                for el in n.iter():
                    m = re.search(r"(?:language|lang)-([\w+#-]+)", el.attrs.get("class", ""))
                    if m:
                        lang = m.group(1)
                        break
                if code.strip():
                    self.out.append(f"```{lang}\n{code}\n```")
            elif t in ("ul", "ol"):
                self._list(n, ordered=t == "ol", depth=depth)
            elif t == "table":
                self._table(n)
            elif t == "blockquote":
                inner = self.block_text(n)
                if inner:
                    self.out.append("\n".join("> " + line for line in inner.splitlines()))
            elif t == "hr":
                self.out.append("---")
            else:
                self.render(n.children, depth)
        flush()

    def _list(self, n: Node, ordered: bool, depth: int) -> None:
        i = 0
        for li in n.children:
            if not isinstance(li, Node) or li.tag != "li":
                continue
            i += 1
            nested = [c for c in li.children if isinstance(c, Node) and c.tag in ("ul", "ol")]
            rest = [c for c in li.children if c not in nested]
            text = _squash("".join(self.inline(c) if (isinstance(c, str) or c.tag not in BLOCK)
                                   else " " + self.block_text(c) + " " for c in rest))
            if text:
                self.out.append(f"{'  ' * depth}{f'{i}.' if ordered else '-'} {text}")
            for sub in nested:
                self._list(sub, ordered=sub.tag == "ol", depth=depth + 1)

    def _table(self, n: Node) -> None:
        rows: list[list[str]] = []
        head = False
        for tr in (e for e in n.iter() if e.tag == "tr"):
            cells = [c for c in tr.children if isinstance(c, Node) and c.tag in ("td", "th")]
            if not cells:
                continue
            if not rows and all(c.tag == "th" for c in cells):
                head = True
            rows.append([_squash("".join(self.inline(x) for x in c.children)).replace("|", "\\|") for c in cells])
        if not rows:
            return
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        # Drop columns that are empty in every body row, or exact copies of the previous column (GitHub renders the
        # file name twice, one copy for mobile).
        cols = list(range(width))
        drop = set()
        for c in cols:
            body = [r[c] for r in rows[1:]] if len(rows) > 1 else [r[c] for r in rows]
            if not any(x.strip() for x in body):
                drop.add(c)
            elif c and all(r[c] == r[c - 1] for r in rows):
                drop.add(c)
        if drop and len(drop) < width:
            rows = [[x for i, x in enumerate(r) if i not in drop] for r in rows]
            width = len(rows[0])
        if len(rows) == 1 or width == 1:  # layout table, not data: plain lines
            self.out.extend(" ".join(r).strip() for r in rows if any(r))
            return
        if not head:
            rows.insert(0, [""] * width)
        self.out.append("\n".join(["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
                                  + ["| " + " | ".join(r) + " |" for r in rows[1:]]))


# Short lines that are pure UI chrome (login prompts, cookie buttons, empty link stubs). Dropped as whole lines only,
# so prose that merely mentions "sign up" stays.
UI_LINE = re.compile(r"^(?:(?:log ?in|sign ?(?:in|up)|register|subscribe|accept(?: all)?(?: cookies)?|reject all|"
                     r"manage (?:cookies|preferences)|skip to (?:main )?content|menu|close|share|tweet|"
                     r"giriş yap|kayıt ol|üye ol|çerezleri kabul et)(?:⟨\d+⟩)?\s*)+$|^(?:⟨\d+⟩\s*)+$", re.I)


def _tidy(markdown: str) -> str:
    out: list[str] = []
    prev = ""
    fence = False
    for line in markdown.split("\n"):
        s = line.strip()
        if s.startswith("```"):
            fence = not fence
            out.append(line)
            prev = s
            continue
        if fence:  # code is code: no UI-line or duplicate filtering inside a block
            out.append(line)
            continue
        if s and UI_LINE.match(s):
            continue
        if s and s == prev:  # the same line twice in a row (mobile + desktop copies of a header)
            continue
        out.append(line)
        if s:
            prev = s
    # A short heading followed directly by the next heading of the same or a higher level has no content (the
    # pruner removed it, e.g. a "Stars" / "Forks" / "Resources" sidebar): drop it. A long headline in a news index
    # (## Headline text...) must stay even when the next item is another headline.
    lines = "\n".join(out).split("\n")
    level = lambda s: len(s) - len(s.lstrip("#")) if re.match(r"#{1,6} ", s) else 0  # noqa: E731
    keep: list[str] = []
    fence = False
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("```"):
            fence = not fence
        lv = 0 if fence else level(s)
        if lv:
            heading_text = s.lstrip("#").strip()
            # Only short, label-like headings (<= 4 words, < 35 chars) count as "empty section headers".
            if len(heading_text.split()) <= 4 and len(heading_text) < 35:
                nxt = next((x.strip() for x in lines[i + 1:] if x.strip()), "")
                nlv = level(nxt) if not nxt.startswith("```") else 0
                if not nxt or (nlv and nlv <= lv):
                    continue
        keep.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip()


def to_markdown(html: str, base_url: str = "", fit: bool = True) -> Page:
    """HTML -> Page. fit=True prunes boilerplate first (crawl4ai's fit_markdown); fit=False keeps everything."""
    root, title, meta = parse(html)
    body = next((e for e in root.iter() if e.tag == "body"), root)
    raw_len = len(_squash(_visible_text(body)))
    if fit:
        prune(body)
        # Pruning can be wrong on unusual layouts: if it removed nearly everything, keep the unpruned page instead.
        if len(_squash(body.text())) < min(200, raw_len * 0.2):
            root, title, meta = parse(html)
            body = next((e for e in root.iter() if e.tag == "body"), root)
            for el in list(body.iter()):
                if el.tag in DROP and el.parent is not None:
                    el.parent.children = [c for c in el.parent.children if c is not el]
    md = _MD(base_url)
    md.render(body.children)
    text = "\n\n".join(md.out)
    text = _tidy(text) if fit else re.sub(r"\n{3,}", "\n\n", text).strip()
    return Page(title=title or meta.get("og:title", ""), markdown=text, links=md.links,
                description=meta.get("description") or meta.get("og:description", ""), raw_text_len=raw_len)


def _visible_text(n: Node) -> str:
    out: list[str] = []
    stack: list[Node | str] = [n]
    while stack:
        x = stack.pop()
        if isinstance(x, str):
            out.append(x)
        elif x.tag not in DROP:
            stack.extend(reversed(x.children))
    return " ".join(out)


def references(links: list[tuple[str, str]], used: set[int] | None = None, limit: int = 120) -> str:
    """The numbered link list appended to the markdown. used: only the numbers that survived (after BM25)."""
    rows = [f"⟨{i}⟩ {url}" + (f" — {text}" if text else "")
            for i, (url, text) in enumerate(links, 1) if used is None or i in used]
    if not rows:
        return ""
    extra = len(rows) - limit
    rows = rows[:limit] + ([f"... {extra} more links"] if extra > 0 else [])
    return "\n\n## Links\n" + "\n".join(rows)


def used_links(text: str) -> set[int]:
    return {int(m) for m in re.findall(r"⟨(\d+)⟩", text)}


def strip_link_marks(text: str) -> str:
    return re.sub(r"⟨\d+⟩", "", text)


# ------------------------------------------------------------------------------------------------- BM25
_WORD = re.compile(r"[\w\u00c0-\u024f\u0400-\u04ff]+", re.U)
STOP = set("""a an and are as at be by for from has have in is it its of on or that the this to was were will with
you your how what why when where which who do does can i we they ve bir bu şu ve ile için de da mi mı mu mü ne nasıl
neden ki gibi olan olarak daha çok en""".split())
HEADING_BOOST = 5.0  # crawl4ai: h1 5x; we apply it to every heading level
CODE_BOOST = 2.0


def _tokens(s: str) -> list[str]:
    out = []
    for w in _WORD.findall(s.lower()):
        if len(w) < 2 or w in STOP:
            continue
        # A light stem so "installing"/"installation"/"install" and Turkish suffixes still meet. Deliberately crude:
        # without nltk, prefix matching on the first 5 letters catches most inflections.
        out.append(w[:5] if len(w) > 5 else w)
    return out


def chunks(markdown: str, size: int = 900) -> list[tuple[str, str]]:
    """Split markdown into (heading path, text) chunks at headings and paragraph boundaries."""
    out: list[tuple[str, str]] = []
    heads: list[str] = []
    cur: list[str] = []
    cur_len = 0

    def emit() -> None:
        nonlocal cur, cur_len
        body = "\n\n".join(cur).strip()
        if body:
            out.append((" > ".join(h for h in heads if h), body))
        cur, cur_len = [], 0

    # Paragraphs, but a fenced code block stays one piece (its "# comment" lines are not headings).
    paras: list[str] = []
    fence: list[str] | None = None
    for p in re.split(r"\n{2,}", markdown):
        if fence is not None:
            fence.append(p)
            if p.count("```") % 2:
                paras.append("\n\n".join(fence))
                fence = None
        elif p.count("```") % 2:
            fence = [p]
        else:
            paras.append(p)
    if fence:
        paras.append("\n\n".join(fence))
    for para in paras:
        m = None if para.lstrip().startswith("```") else re.match(r"^\s*(#{1,6})\s+(.*)", para)
        if m:
            emit()
            level = len(m.group(1))
            # Headings may skip levels (h1 then h3): pad so the path stays aligned with the level.
            heads = (heads + [""] * level)[:level - 1] + [m.group(2).strip()]
            continue
        if cur_len + len(para) > size and cur:
            emit()
        cur.append(para)
        cur_len += len(para)
    emit()
    return out


def bm25_select(markdown: str, query: str, budget: int = 12000, k1: float = 1.2, b: float = 0.75) -> str:
    """Keep the chunks most relevant to query (BM25 over heading+text, headings boosted), in page order, up to budget
    characters. Falls back to the head of the page when nothing matches."""
    parts = chunks(markdown)
    q = _tokens(query)
    if not parts or not q:
        return markdown[:budget]
    docs = []
    heads = []
    for head, body in parts:
        toks = _tokens(body)
        weighted = Counter(toks)
        for code in re.findall(r"`([^`]+)`|```.*?\n(.*?)```", body, re.S):
            for t in _tokens(" ".join(code)):
                weighted[t] += CODE_BOOST - 1
        docs.append((weighted, max(len(toks), 1)))
        # The nearest heading is what the chunk is about; parent headings ("Coroutines and tasks") are shared by
        # the whole page and would boost everything equally, so only the last one counts.
        heads.append(set(_tokens(head.rsplit(" > ", 1)[-1])))
    n = len(docs)
    avgdl = sum(d[1] for d in docs) / n
    df = Counter(t for d, _ in docs for t in set(d))
    qs = set(q)
    idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in qs}
    scores = []
    for i, (tf, dl) in enumerate(docs):
        s = 0.0
        for t in qs:
            f = tf.get(t, 0)
            if f:
                s += idf[t] * f * (k1 + 1) / (f + k1 * (1 - b + b * dl / avgdl))
            if t in heads[i]:
                # crawl4ai weighs heading text 5x; added on top of the body score so a section titled after the
                # question outranks long passages that merely repeat one of its words.
                s += HEADING_BOOST * idf[t]
        # Coverage: a chunk matching more of the question's distinct terms beats one repeating a single term.
        hit = sum(1 for t in qs if t in tf or t in heads[i]) / len(qs)
        scores.append((s * (0.5 + hit), i))
    best = [i for s, i in sorted(scores, reverse=True) if s > 0]
    if not best:
        return markdown[:budget]
    keep: list[int] = []
    used = 0
    for i in best:
        size = len(parts[i][1]) + len(parts[i][0]) + 8
        if used + size > budget and keep:
            continue
        keep.append(i)
        used += size
        if used >= budget:
            break
    out = []
    for i in sorted(keep):
        head, body = parts[i]
        out.append((f"### {head}\n\n" if head else "") + body)
    skipped = n - len(keep)
    note = f"\n\n[{len(keep)}/{n} sections relevant to the question shown; {skipped} sections skipped]" if skipped else ""
    return "\n\n---\n\n".join(out) + note


def relevance(text: str, query: str) -> float:
    """0..1: share of the query's terms that appear in text (link/anchor scoring for the crawler)."""
    q = set(_tokens(query))
    if not q:
        return 0.0
    return len(q & set(_tokens(text))) / len(q)


# ------------------------------------------------------------------------------------- block detection
# crawl4ai antibot_detector: tier 1 is decisive on its own, tier 2 only on small pages (big real pages mention
# "captcha" or "access denied" in passing).
TIER1 = [re.compile(p, re.I) for p in (
    r"<title>\s*Just a moment\.\.\.", r"cf-browser-verification", r"cf_chl_opt", r"challenge-platform",
    r"<title>\s*Attention Required! \| Cloudflare", r"Checking your browser before accessing",
    r"_Incapsula_Resource", r"Request unsuccessful\. Incapsula incident",
    r"px-captcha", r"perimeterx", r"captcha-delivery\.com", r"geo\.captcha-delivery", r"datadome",
    r"<title>\s*Access Denied\s*</title>.*Reference #", r"akamai.*(?:access denied|reference #)",
    r"sec-if-cpt-container", r"Pardon Our Interruption", r"window\._cf_chl",
    r"kasada", r"KPSDK", r"<title>\s*Robot Check\s*</title>", r"api-services-support@amazon\.com",
    r"Please respect our robot policy", r"bot-traffic@wikimedia\.org",
)]
TIER2 = [re.compile(p, re.I) for p in (
    r"access denied", r"forbidden", r"are you a robot", r"verify you are (?:a )?human", r"unusual traffic",
    r"enable javascript and cookies to continue", r"please enable javascript", r"captcha", r"blocked",
    r"too many requests", r"rate limit", r"bot detection", r"security check",
)]
JS_SHELL = re.compile(r'<div[^>]+id=["\'](?:root|app|__next|__nuxt|svelte|main-app)["\'][^>]*>\s*</div>', re.I)


def detect_block(status: int, html: str, text_len: int | None = None) -> str:
    """Why this response is not the real page, or "" if it looks fine. text_len: visible text length if known."""
    size = len(html)
    head = html[:20000]
    # A big page with plenty of text that merely carries a challenge script (Cloudflare leaves its
    # "challenge-platform" loader on pages it already let through) is the real page, not a block.
    real = status < 400 and (text_len or 0) > 3000
    for rx in TIER1:
        if rx.search(head) and not real:
            return f"bot protection ({rx.pattern[:40]})"
    if status == 429:
        return "HTTP 429 (too many requests)"
    if status in (403, 503) and size and not html.lstrip().startswith(("{", "[", "<?xml")):
        return f"HTTP {status} block page"
    if status >= 400 and size < 10_000 and not html.lstrip().startswith(("{", "[", "<?xml")):
        for rx in TIER2:
            if rx.search(html):
                return f"HTTP {status}: {rx.pattern}"
    if status < 400:
        visible = text_len if text_len is not None else len(_squash(re.sub(r"<[^>]+>", " ", re.sub(
            r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I))))
        if size < 100:
            return "almost empty response"
        if size < 50_000 and visible < 200:
            for rx in TIER2:
                if rx.search(html):
                    return f"block page ({rx.pattern})"
        # A JavaScript app shell or an interstitial: markup/script but almost no text (an empty mount point, or a
        # page like Reddit's that only runs a script and resubmits a form).
        if visible < 300 and (JS_SHELL.search(html) or size > 5_000 or "<script" in head.lower()):
            return "page loaded with JavaScript (no text)"
    return ""


# ------------------------------------------------------------------------------------------- crawler bits
def page_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Every <a href> on the page (before pruning, so navigation is still there for the crawler): (url, text)."""
    root, _, _ = parse(html)
    seen: set[str] = set()
    out = []
    for el in root.iter():
        if el.tag != "a":
            continue
        href = el.attrs.get("href", "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
            continue
        url = urldefrag(urljoin(base_url, href))[0]
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        out.append((url, _squash(el.text())[:120]))
    return out


SKIP_EXT = re.compile(r"\.(?:png|jpe?g|gif|webp|svg|ico|css|js|mjs|zip|gz|tgz|rar|7z|exe|msi|dmg|deb|rpm|apk|mp[34]|"
                      r"avi|mov|webm|woff2?|ttf|eot|xml|rss)(?:\?|$)", re.I)


def same_site(url: str, start: str) -> bool:
    a, b = urlparse(url).hostname or "", urlparse(start).hostname or ""
    strip = lambda h: h[4:] if h.startswith("www.") else h  # noqa: E731
    return strip(a) == strip(b)
