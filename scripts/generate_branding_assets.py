"""Generate Sleep Classifier branding assets using only the Python stdlib.

This script produces three PNG files that ship with the repository:

- ``sleep_classifier/icon.png``        128×128 — HA add-on store icon
- ``sleep_classifier/logo.png``        250×100 — wordmark logo (icon + text)
- ``assets/screenshots/dashboard-tonight.png``
                                       1200×675 — Lovelace 4-view placeholder

Design notes (keep aligned with `requirements.md` §1 and `design.md` §3.1):

- 主色为 HA UI 蓝 ``#03a9f4``。图标为「半月 + 床」简笔。
- Logo 左侧为缩小版图标，右侧为 ``SLEEP CLASSIFIER`` wordmark。
- 截图为 4-view dashboard 的占位 mockup，宽度 ≥ 1200 px。

The script intentionally has **zero** runtime dependencies. It uses only
``zlib`` and ``struct`` from the standard library so it can run inside the
add-on container as well as on CI without pulling in Pillow.

Usage::

    python scripts/generate_branding_assets.py            # writes to repo root
    python scripts/generate_branding_assets.py path/to/repo
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path
from typing import Iterable, Sequence, Tuple


# ---------------------------------------------------------------------------
# Color palette (RGBA tuples). Aligned with HA primary blue ``#03a9f4``.
# ---------------------------------------------------------------------------

RGBA = Tuple[int, int, int, int]

HA_BLUE: RGBA = (0x03, 0xA9, 0xF4, 0xFF)
HA_BLUE_DARK: RGBA = (0x01, 0x6B, 0xA8, 0xFF)
HA_BLUE_LIGHT: RGBA = (0x4F, 0xC3, 0xF7, 0xFF)
NAVY: RGBA = (0x10, 0x1B, 0x2E, 0xFF)
WHITE: RGBA = (0xFF, 0xFF, 0xFF, 0xFF)
OFF_WHITE: RGBA = (0xF5, 0xF7, 0xFA, 0xFF)
GREY_300: RGBA = (0xD0, 0xD7, 0xDE, 0xFF)
GREY_500: RGBA = (0x8B, 0x95, 0xA1, 0xFF)
GREY_700: RGBA = (0x4A, 0x52, 0x5C, 0xFF)
TEAL: RGBA = (0x26, 0xC6, 0xDA, 0xFF)
AMBER: RGBA = (0xFF, 0xB7, 0x4D, 0xFF)
TRANSPARENT: RGBA = (0, 0, 0, 0)


# ---------------------------------------------------------------------------
# Minimal 5×7 bitmap font covering the characters we actually render.
#
# Each glyph is 7 rows of 5 columns. A space ``" "`` means pixel off, anything
# else (we use ``"X"``) means pixel on. Characters not in this map are
# rendered as blank glyphs (used as fallback for unsupported punctuation).
# ---------------------------------------------------------------------------

_FONT_5X7: dict[str, Tuple[str, ...]] = {
    " ": ("     ", "     ", "     ", "     ", "     ", "     ", "     "),
    "-": ("     ", "     ", "     ", "XXXXX", "     ", "     ", "     "),
    "_": ("     ", "     ", "     ", "     ", "     ", "     ", "XXXXX"),
    ".": ("     ", "     ", "     ", "     ", "     ", " XX  ", " XX  "),
    "/": ("    X", "    X", "   X ", "  X  ", " X   ", "X    ", "X    "),
    "&": (" XX  ", "X  X ", "X X  ", " X   ", "X X X", "X  X ", " XX X"),
    "A": (" XXX ", "X   X", "X   X", "XXXXX", "X   X", "X   X", "X   X"),
    "B": ("XXXX ", "X   X", "X   X", "XXXX ", "X   X", "X   X", "XXXX "),
    "C": (" XXXX", "X    ", "X    ", "X    ", "X    ", "X    ", " XXXX"),
    "D": ("XXXX ", "X   X", "X   X", "X   X", "X   X", "X   X", "XXXX "),
    "E": ("XXXXX", "X    ", "X    ", "XXXX ", "X    ", "X    ", "XXXXX"),
    "F": ("XXXXX", "X    ", "X    ", "XXXX ", "X    ", "X    ", "X    "),
    "G": (" XXXX", "X    ", "X    ", "X  XX", "X   X", "X   X", " XXX "),
    "H": ("X   X", "X   X", "X   X", "XXXXX", "X   X", "X   X", "X   X"),
    "I": ("XXXXX", "  X  ", "  X  ", "  X  ", "  X  ", "  X  ", "XXXXX"),
    "J": ("  XXX", "   X ", "   X ", "   X ", "   X ", "X  X ", " XX  "),
    "K": ("X   X", "X  X ", "X X  ", "XX   ", "X X  ", "X  X ", "X   X"),
    "L": ("X    ", "X    ", "X    ", "X    ", "X    ", "X    ", "XXXXX"),
    "M": ("X   X", "XX XX", "X X X", "X   X", "X   X", "X   X", "X   X"),
    "N": ("X   X", "XX  X", "X X X", "X X X", "X  XX", "X   X", "X   X"),
    "O": (" XXX ", "X   X", "X   X", "X   X", "X   X", "X   X", " XXX "),
    "P": ("XXXX ", "X   X", "X   X", "XXXX ", "X    ", "X    ", "X    "),
    "Q": (" XXX ", "X   X", "X   X", "X   X", "X X X", "X  X ", " XX X"),
    "R": ("XXXX ", "X   X", "X   X", "XXXX ", "X X  ", "X  X ", "X   X"),
    "S": (" XXXX", "X    ", "X    ", " XXX ", "    X", "    X", "XXXX "),
    "T": ("XXXXX", "  X  ", "  X  ", "  X  ", "  X  ", "  X  ", "  X  "),
    "U": ("X   X", "X   X", "X   X", "X   X", "X   X", "X   X", " XXX "),
    "V": ("X   X", "X   X", "X   X", "X   X", "X   X", " X X ", "  X  "),
    "W": ("X   X", "X   X", "X   X", "X X X", "X X X", "X X X", " X X "),
    "X": ("X   X", "X   X", " X X ", "  X  ", " X X ", "X   X", "X   X"),
    "Y": ("X   X", "X   X", "X   X", " X X ", "  X  ", "  X  ", "  X  "),
    "Z": ("XXXXX", "    X", "   X ", "  X  ", " X   ", "X    ", "XXXXX"),
    "0": (" XXX ", "X   X", "X  XX", "X X X", "XX  X", "X   X", " XXX "),
    "1": ("  X  ", " XX  ", "  X  ", "  X  ", "  X  ", "  X  ", " XXX "),
    "2": (" XXX ", "X   X", "    X", "   X ", "  X  ", " X   ", "XXXXX"),
    "3": ("XXXX ", "    X", "    X", " XXX ", "    X", "    X", "XXXX "),
    "4": ("X   X", "X   X", "X   X", "XXXXX", "    X", "    X", "    X"),
    "5": ("XXXXX", "X    ", "X    ", "XXXX ", "    X", "    X", "XXXX "),
    "6": (" XXX ", "X    ", "X    ", "XXXX ", "X   X", "X   X", " XXX "),
    "7": ("XXXXX", "    X", "   X ", "  X  ", " X   ", " X   ", " X   "),
    "8": (" XXX ", "X   X", "X   X", " XXX ", "X   X", "X   X", " XXX "),
    "9": (" XXX ", "X   X", "X   X", " XXXX", "    X", "    X", " XXX "),
}

# Width / height of a single (unscaled) glyph cell.
_GLYPH_W: int = 5
_GLYPH_H: int = 7


# ---------------------------------------------------------------------------
# Canvas — a flat ``bytearray`` of width × height × RGBA bytes. We expose a
# few primitive drawing helpers (set_pixel / fill_rect / fill_circle /
# fill_ring_arc / draw_text) and serialize to PNG via ``write_png``.
# ---------------------------------------------------------------------------


class Canvas:
    """In-memory RGBA raster with stdlib-only PNG serialization."""

    __slots__ = ("width", "height", "_pixels")

    def __init__(self, width: int, height: int, fill: RGBA = TRANSPARENT) -> None:
        if width <= 0 or height <= 0:
            raise ValueError(f"canvas dimensions must be positive: {width}x{height}")
        self.width = width
        self.height = height
        self._pixels = bytearray(bytes(fill) * (width * height))

    # -- low-level access --------------------------------------------------

    def _index(self, x: int, y: int) -> int:
        return (y * self.width + x) * 4

    def set_pixel(self, x: int, y: int, color: RGBA) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            i = self._index(x, y)
            self._pixels[i : i + 4] = bytes(color)

    # -- primitives --------------------------------------------------------

    def fill_rect(self, x: int, y: int, w: int, h: int, color: RGBA) -> None:
        x0 = max(0, x)
        y0 = max(0, y)
        x1 = min(self.width, x + w)
        y1 = min(self.height, y + h)
        if x1 <= x0 or y1 <= y0:
            return
        row = bytes(color) * (x1 - x0)
        for yy in range(y0, y1):
            i = self._index(x0, yy)
            self._pixels[i : i + len(row)] = row

    def fill_circle(self, cx: int, cy: int, r: int, color: RGBA) -> None:
        if r <= 0:
            return
        r2 = r * r
        x0 = max(0, cx - r)
        x1 = min(self.width - 1, cx + r)
        y0 = max(0, cy - r)
        y1 = min(self.height - 1, cy + r)
        for yy in range(y0, y1 + 1):
            dy = yy - cy
            for xx in range(x0, x1 + 1):
                dx = xx - cx
                if dx * dx + dy * dy <= r2:
                    i = self._index(xx, yy)
                    self._pixels[i : i + 4] = bytes(color)

    def fill_rounded_rect(
        self, x: int, y: int, w: int, h: int, radius: int, color: RGBA
    ) -> None:
        radius = max(0, min(radius, w // 2, h // 2))
        if radius == 0:
            self.fill_rect(x, y, w, h, color)
            return
        # Center band (full width).
        self.fill_rect(x, y + radius, w, h - 2 * radius, color)
        # Top / bottom bands minus the corner squares.
        self.fill_rect(x + radius, y, w - 2 * radius, radius, color)
        self.fill_rect(x + radius, y + h - radius, w - 2 * radius, radius, color)
        # Four corner quarter-circles.
        self.fill_circle(x + radius, y + radius, radius, color)
        self.fill_circle(x + w - radius - 1, y + radius, radius, color)
        self.fill_circle(x + radius, y + h - radius - 1, radius, color)
        self.fill_circle(x + w - radius - 1, y + h - radius - 1, radius, color)

    # -- text --------------------------------------------------------------

    def draw_text(
        self,
        x: int,
        y: int,
        text: str,
        color: RGBA,
        scale: int = 2,
        spacing: int = 1,
    ) -> int:
        """Render ``text`` and return the x advance just past the last glyph."""
        if scale <= 0:
            raise ValueError("scale must be a positive integer")
        cursor = x
        for ch in text.upper():
            glyph = _FONT_5X7.get(ch, _FONT_5X7[" "])
            for row_idx, row in enumerate(glyph):
                for col_idx, cell in enumerate(row):
                    if cell != " ":
                        self.fill_rect(
                            cursor + col_idx * scale,
                            y + row_idx * scale,
                            scale,
                            scale,
                            color,
                        )
            cursor += (_GLYPH_W + spacing) * scale
        return cursor

    def text_width(self, text: str, scale: int = 2, spacing: int = 1) -> int:
        if not text:
            return 0
        return len(text) * (_GLYPH_W + spacing) * scale - spacing * scale


# ---------------------------------------------------------------------------
# PNG serialization (RFC 2083 / W3C PNG spec). 8-bit RGBA, no interlace.
# ---------------------------------------------------------------------------

_PNG_SIGNATURE: bytes = b"\x89PNG\r\n\x1a\n"


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def write_png(path: Path, canvas: Canvas) -> None:
    """Serialize ``canvas`` to a PNG file at ``path``."""
    width, height = canvas.width, canvas.height

    # Prepend a single zero filter byte to each scanline (filter type 0 = None).
    stride = width * 4
    raw = bytearray()
    pixels = canvas._pixels  # noqa: SLF001 — internal access by design
    for row in range(height):
        raw.append(0)
        offset = row * stride
        raw.extend(pixels[offset : offset + stride])

    ihdr = struct.pack(
        ">IIBBBBB",
        width,
        height,
        8,  # bit depth
        6,  # color type: RGBA
        0,  # compression
        0,  # filter
        0,  # interlace
    )
    idat = zlib.compress(bytes(raw), 9)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        _PNG_SIGNATURE
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------------------
# Asset 1 — icon.png (128×128). 「半月 + 床」简笔 over HA blue background.
# ---------------------------------------------------------------------------


def _draw_crescent_moon(
    canvas: Canvas,
    cx: int,
    cy: int,
    r: int,
    color: RGBA,
    background: RGBA,
    offset: Tuple[int, int] = (-6, -4),
) -> None:
    """Fill a crescent shape: full moon disc minus an offset background disc."""
    canvas.fill_circle(cx, cy, r, color)
    canvas.fill_circle(cx + offset[0], cy + offset[1], r, background)


def _draw_bed(
    canvas: Canvas,
    x: int,
    y: int,
    width: int,
    height: int,
    body: RGBA,
    sheet: RGBA,
    pillow: RGBA,
) -> None:
    """Draw a stylized bed silhouette (mattress + headboard + pillow)."""
    headboard_w = max(6, width // 9)
    mattress_h = max(8, height // 2)
    mattress_y = y + height - mattress_h
    # Headboard at left rises above the mattress.
    canvas.fill_rounded_rect(
        x, y, headboard_w, height, radius=3, color=body
    )
    # Mattress body (full width including headboard footprint).
    canvas.fill_rounded_rect(
        x, mattress_y, width, mattress_h, radius=4, color=body
    )
    # Sheet stripe along the front of the mattress.
    sheet_h = max(3, mattress_h // 4)
    canvas.fill_rect(
        x + 2,
        mattress_y + mattress_h - sheet_h - 2,
        width - 4,
        sheet_h,
        sheet,
    )
    # Pillow sitting on top of the mattress, flush to the headboard.
    pillow_w = max(10, (width - headboard_w) // 4)
    pillow_h = max(5, mattress_h // 2)
    canvas.fill_rounded_rect(
        x + headboard_w + 4,
        mattress_y - pillow_h + 2,
        pillow_w,
        pillow_h,
        radius=2,
        color=pillow,
    )


def build_icon() -> Canvas:
    canvas = Canvas(128, 128, fill=HA_BLUE)

    # Inner rounded panel for visual depth without breaking the square.
    canvas.fill_rounded_rect(0, 0, 128, 128, radius=18, color=HA_BLUE)

    # Soft halo ring around the moon (lighter blue).
    canvas.fill_circle(78, 44, 30, HA_BLUE_LIGHT)
    canvas.fill_circle(78, 44, 28, HA_BLUE)

    # Crescent moon — white disc minus offset HA-blue disc.
    _draw_crescent_moon(
        canvas,
        cx=80,
        cy=44,
        r=24,
        color=WHITE,
        background=HA_BLUE,
        offset=(-9, -5),
    )

    # Bed silhouette across the bottom third.
    _draw_bed(
        canvas,
        x=14,
        y=72,
        width=100,
        height=42,
        body=WHITE,
        sheet=HA_BLUE_LIGHT,
        pillow=OFF_WHITE,
    )

    # Subtle floor line under the bed for grounding.
    canvas.fill_rect(8, 116, 112, 2, HA_BLUE_DARK)

    return canvas


# ---------------------------------------------------------------------------
# Asset 2 — logo.png (250×100). Mini icon on the left + wordmark on the right.
# ---------------------------------------------------------------------------


def build_logo() -> Canvas:
    canvas = Canvas(250, 100, fill=WHITE)

    # Left badge: HA-blue rounded square holding a miniature moon + bed mark.
    badge_x, badge_y, badge_size = 8, 10, 80
    canvas.fill_rounded_rect(
        badge_x, badge_y, badge_size, badge_size, radius=12, color=HA_BLUE
    )

    # Mini crescent moon inside the badge.
    moon_cx = badge_x + 50
    moon_cy = badge_y + 28
    _draw_crescent_moon(
        canvas,
        cx=moon_cx,
        cy=moon_cy,
        r=14,
        color=WHITE,
        background=HA_BLUE,
        offset=(-5, -3),
    )

    # Mini bed silhouette inside the badge.
    _draw_bed(
        canvas,
        x=badge_x + 8,
        y=badge_y + 46,
        width=64,
        height=26,
        body=WHITE,
        sheet=HA_BLUE_LIGHT,
        pillow=OFF_WHITE,
    )

    # Wordmark: SLEEP / CLASSIFIER stacked on two lines for readability.
    text_x = badge_x + badge_size + 12
    canvas.draw_text(text_x, 20, "SLEEP", color=NAVY, scale=3, spacing=1)
    canvas.draw_text(text_x, 56, "CLASSIFIER", color=HA_BLUE_DARK, scale=2, spacing=1)

    # Decorative underline accent.
    canvas.fill_rect(text_x, 84, 130, 3, HA_BLUE)

    return canvas


# ---------------------------------------------------------------------------
# Asset 3 — assets/screenshots/dashboard-tonight.png (1200×675). Placeholder
# Lovelace 4-view dashboard mockup. Layout: header bar + 2×2 card grid.
# ---------------------------------------------------------------------------


def _draw_card(
    canvas: Canvas,
    x: int,
    y: int,
    w: int,
    h: int,
    title: str,
    accent: RGBA,
    body_lines: Sequence[str],
) -> None:
    canvas.fill_rounded_rect(x, y, w, h, radius=12, color=WHITE)
    # Top accent bar.
    canvas.fill_rect(x, y, w, 6, accent)
    # Card title.
    canvas.draw_text(x + 24, y + 24, title, color=NAVY, scale=3, spacing=1)
    # Body lines rendered as smaller text blocks.
    for idx, line in enumerate(body_lines):
        canvas.draw_text(
            x + 24,
            y + 70 + idx * 28,
            line,
            color=GREY_700,
            scale=2,
            spacing=1,
        )
    # Faux chart area on the right half of the card.
    chart_x = x + w - 220
    chart_y = y + 70
    canvas.fill_rounded_rect(
        chart_x, chart_y, 196, h - 90, radius=8, color=OFF_WHITE
    )
    # Stylized bars inside the chart area.
    bar_widths = [20, 28, 36, 44, 36, 28, 20]
    bar_x = chart_x + 12
    bar_baseline = chart_y + h - 110
    for idx, bar_h in enumerate(bar_widths):
        canvas.fill_rounded_rect(
            bar_x + idx * 26,
            bar_baseline - bar_h * 2,
            18,
            bar_h * 2,
            radius=3,
            color=accent,
        )


def build_screenshot() -> Canvas:
    width, height = 1200, 675
    canvas = Canvas(width, height, fill=OFF_WHITE)

    # Header bar.
    header_h = 88
    canvas.fill_rect(0, 0, width, header_h, HA_BLUE)
    canvas.fill_rect(0, header_h, width, 4, HA_BLUE_DARK)

    # Header title + subtitle.
    canvas.draw_text(36, 22, "SLEEP CLASSIFIER", color=WHITE, scale=4, spacing=1)
    canvas.draw_text(
        36, 58, "TONIGHT - 4 VIEW DASHBOARD", color=HA_BLUE_LIGHT, scale=2, spacing=1
    )

    # Right-side header chip ("V2.1.0 PREVIEW").
    chip_text = "V2.1.0 PREVIEW"
    chip_w = canvas.text_width(chip_text, scale=2) + 32
    canvas.fill_rounded_rect(
        width - chip_w - 36, 28, chip_w, 32, radius=10, color=HA_BLUE_DARK
    )
    canvas.draw_text(
        width - chip_w - 20, 36, chip_text, color=WHITE, scale=2, spacing=1
    )

    # 2×2 grid of cards beneath the header.
    margin = 36
    gap = 32
    grid_top = header_h + margin
    card_w = (width - 2 * margin - gap) // 2
    card_h = (height - grid_top - margin - gap) // 2

    cards = [
        (
            "TONIGHT",
            HA_BLUE,
            ("BEDTIME 23 12", "WAKE 07 03", "SCORE 82 100"),
        ),
        (
            "STAGE",
            TEAL,
            ("CURRENT DEEP", "REM 1H 14M", "AWAKE 0H 06M"),
        ),
        (
            "LEARNING",
            AMBER,
            ("MIDPOINT 22 4 C", "DELTA STAGE 0 9 C", "SAMPLES 27 NIGHTS"),
        ),
        (
            "DIAGNOSTICS",
            GREY_500,
            ("DRY RUN ON", "HA WS CONNECTED", "TELEMETRY OFF"),
        ),
    ]

    positions = [
        (margin, grid_top),
        (margin + card_w + gap, grid_top),
        (margin, grid_top + card_h + gap),
        (margin + card_w + gap, grid_top + card_h + gap),
    ]

    for (title, accent, body), (cx, cy) in zip(cards, positions):
        _draw_card(canvas, cx, cy, card_w, card_h, title, accent, body)

    # Footer hairline.
    canvas.fill_rect(0, height - 4, width, 4, GREY_300)

    return canvas


# ---------------------------------------------------------------------------
# CLI driver.
# ---------------------------------------------------------------------------


def _targets(repo_root: Path) -> Tuple[Tuple[Path, Canvas], ...]:
    return (
        (repo_root / "sleep_classifier" / "icon.png", build_icon()),
        (repo_root / "sleep_classifier" / "logo.png", build_logo()),
        (
            repo_root / "assets" / "screenshots" / "dashboard-tonight.png",
            build_screenshot(),
        ),
    )


def main(argv: Iterable[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    repo_root = Path(args[0]).resolve() if args else Path.cwd()

    for path, canvas in _targets(repo_root):
        write_png(path, canvas)
        print(
            f"generate_branding_assets: wrote {path.relative_to(repo_root)} "
            f"({canvas.width}x{canvas.height})"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI passthrough
    raise SystemExit(main())
