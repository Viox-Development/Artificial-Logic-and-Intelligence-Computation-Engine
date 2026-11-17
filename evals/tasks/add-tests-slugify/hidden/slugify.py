"""Turn titles into URL slugs."""

import re
import unicodedata


def slugify(text: str, max_len: int = 50) -> str:
    """Make a URL slug from `text`.

    Rules:
    - the result is lower case;
    - accents are removed and other non-ASCII characters are dropped ("Café" -> "cafe");
    - every run of characters that are not ASCII letters or digits becomes a single "-";
    - there is no "-" at the start or the end;
    - the result is at most `max_len` characters long, and cutting it never leaves a "-" at the end.
    """
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    if len(text) > max_len:
        text = text[:max_len].rstrip("-")
    return text
