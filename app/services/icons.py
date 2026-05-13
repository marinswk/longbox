"""PNG rasterisation of the app's SVG icon.

Firefox on Android (and several other PWA install paths) won't accept
SVG icons in the manifest — they need fixed-pixel PNGs at specific
sizes. Pillow can't render arbitrary SVG, but our icon is two
primitives (a rounded square + an "L" path), so we redraw it in
ImageDraw at any requested size.

Outputs are cached in-process: the first request at each size pays
the render cost (~5ms for 512), subsequent requests at the same size
hit a dict.
"""

from __future__ import annotations

import io
from threading import Lock

from PIL import Image, ImageDraw


_CRAWL_YELLOW = (255, 232, 31, 255)   # #FFE81F — primary accent
_BLACK = (0, 0, 0, 255)
_TRANSPARENT = (0, 0, 0, 0)

# In-process cache so we render each (variant, size) pair at most once.
_cache: dict[tuple[str, int], bytes] = {}
_cache_lock = Lock()


def _render_any(size: int) -> bytes:
    """`purpose=any` variant — rounded yellow square + black border
    + black "L" centred in the same shape the app's display headings
    use. Matches `app/static/icons/icon.svg` at 512x512 viewBox."""
    # Scale every coordinate from the 512-unit SVG viewBox.
    s = size / 512.0
    img = Image.new("RGBA", (size, size), _TRANSPARENT)
    draw = ImageDraw.Draw(img)

    # Rounded square — leave 20-unit margin on each side.
    margin = int(20 * s)
    radius = int(92 * s)
    stroke = max(1, int(32 * s))
    draw.rounded_rectangle(
        (margin, margin, size - margin - 1, size - margin - 1),
        radius=radius,
        fill=_CRAWL_YELLOW,
        outline=_BLACK,
        width=stroke,
    )

    # "L" — closed polygon, same six vertices as the SVG path.
    pts = [
        (154, 144), (218, 144), (218, 328),
        (368, 328), (368, 392), (154, 392),
    ]
    draw.polygon([(int(x * s), int(y * s)) for (x, y) in pts], fill=_BLACK)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _render_maskable(size: int) -> bytes:
    """`purpose=maskable` variant — full-bleed yellow background so
    Android adaptive icons can apply any shape mask without revealing
    transparent corners. The "L" lives inside the central 80% safe
    zone. Matches `app/static/icons/maskable.svg`."""
    s = size / 512.0
    img = Image.new("RGBA", (size, size), _CRAWL_YELLOW)
    draw = ImageDraw.Draw(img)
    pts = [
        (180, 160), (236, 160), (236, 322),
        (368, 322), (368, 378), (180, 378),
    ]
    draw.polygon([(int(x * s), int(y * s)) for (x, y) in pts], fill=_BLACK)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_icon(variant: str, size: int) -> bytes:
    """Return PNG bytes for `variant` ('any' or 'maskable') at the
    given pixel `size`. Cached after first render."""
    variant = variant if variant in ("any", "maskable") else "any"
    size = max(16, min(1024, int(size)))   # clamp to a sane range
    key = (variant, size)
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    png = _render_any(size) if variant == "any" else _render_maskable(size)
    with _cache_lock:
        _cache[key] = png
    return png
