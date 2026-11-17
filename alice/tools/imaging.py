from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image


def image_to_png_b64(img: Image.Image, max_width: int) -> tuple[str, float]:
    """Resize image to max_width, return base64 PNG and scale ratio (original/resized)."""
    scale = 1.0
    if img.width > max_width:
        scale = img.width / max_width
        img = img.resize((max_width, round(img.height / scale)), Image.LANCZOS)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii"), scale


def file_to_png_b64(path: Path, max_width: int) -> str:
    with Image.open(path) as img:
        return image_to_png_b64(img, max_width)[0]
