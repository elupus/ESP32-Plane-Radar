#!/usr/bin/env python3
"""Bake a TrueType font into a VLW smooth-font file for LovyanGFX/TFT_eSPI.

The VLW format (24-byte header + one 28-byte metrics entry per glyph +
concatenated 8-bit coverage bitmaps, all big-endian) is reverse-engineered
from LovyanGFX's loader (lgfx_fonts.cpp: VLWfont::loadFont/drawChar), not
from a spec doc, so it matches exactly what this project's loader expects.

Usage:
  .venv/bin/python scripts/gen_vlw_font.py \
      --font "/System/Library/Fonts/Supplemental/Verdana Bold.ttf" \
      --y-advance 14 \
      --out data/ui_font.vlw
"""

import argparse
import string
import struct

from PIL import Image, ImageDraw, ImageFont

CHARSET = "".join(
    c for c in (string.digits + string.ascii_uppercase + string.ascii_lowercase +
                string.punctuation) if c != " "
)


def native_y_advance(font_path: str, px_size: int) -> int:
    """ascent + descent, in pixels — this is what LovyanGFX's fontHeight()
    measures for a VLW font (VLWfont::getDefaultMetric: height = yAdvance =
    maxAscent + maxDescent), not a capital letter's cap height. Matching
    *this* value is what makes the runtime auto-sizing search land on
    scale == 1.0 (no resampling of the baked glyph bitmaps).
    """
    font = ImageFont.truetype(font_path, px_size)
    ascent, descent = font.getmetrics()
    return ascent + descent


def pick_px_size(font_path: str, target_y_advance: int) -> int:
    """Search for the pixel size whose native yAdvance is closest to target."""
    best_size, best_diff = target_y_advance, None
    for px_size in range(max(4, target_y_advance - 8), target_y_advance + 8):
        diff = abs(native_y_advance(font_path, px_size) - target_y_advance)
        if best_diff is None or diff < best_diff:
            best_size, best_diff = px_size, diff
    return best_size


def render_glyph(font: ImageFont.FreeTypeFont, char: str, px_size: int):
    """Returns (width, height, dx, dy, x_advance, coverage_bytes) or None."""
    margin = px_size * 2
    baseline = px_size * 2
    canvas = Image.new("L", (px_size * 4 + margin, px_size * 4), 0)
    draw = ImageDraw.Draw(canvas)
    pen_x = margin // 2
    draw.text((pen_x, baseline), char, font=font, fill=255, anchor="ls")
    bbox = canvas.getbbox()
    x_advance = round(draw.textlength(char, font=font))
    if bbox is None:
        return 0, 0, 0, 0, x_advance, b""
    left, top, right, bottom = bbox
    width, height = right - left, bottom - top
    dx, dy = left - pen_x, baseline - top
    cropped = canvas.crop(bbox)
    return width, height, dx, dy, x_advance, cropped.tobytes()


def build_vlw(font_path: str, px_size: int, charset: str) -> bytes:
    font = ImageFont.truetype(font_path, px_size)
    ascent, descent = font.getmetrics()

    glyphs = []
    for char in sorted(set(charset)):
        width, height, dx, dy, x_advance, bitmap = render_glyph(font, char, px_size)
        glyphs.append({
            "unicode": ord(char),
            "width": width,
            "height": height,
            "x_advance": x_advance,
            "dx": dx,
            "dy": dy,
            "bitmap": bitmap,
        })

    header = struct.pack(">6i", len(glyphs), 1, px_size, 0, ascent, descent)

    table = b"".join(
        struct.pack(">7i", g["unicode"], g["height"], g["width"], g["x_advance"],
                    g["dy"], g["dx"], 0)
        for g in glyphs
    )

    bitmaps = b"".join(g["bitmap"] for g in glyphs)

    return header + table + bitmaps


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", required=True, help="Path to a .ttf/.otf file")
    parser.add_argument("--y-advance", type=int, required=True,
                        help="Target fontHeight()/yAdvance (ascent+descent), in "
                             "pixels — the value that must match a label's "
                             "target height for the runtime auto-sizer to pick "
                             "scale 1.0 (no resampling)")
    parser.add_argument("--out", required=True, help="Output .vlw path")
    parser.add_argument("--px-size", type=int,
                        help="Skip the search and bake at this exact pixel size")
    args = parser.parse_args()

    px_size = args.px_size or pick_px_size(args.font, args.y_advance)
    actual = native_y_advance(args.font, px_size)
    print(f"baking at px_size={px_size} (native yAdvance {actual}px, "
          f"target {args.y_advance}px)")

    data = build_vlw(args.font, px_size, CHARSET)
    with open(args.out, "wb") as f:
        f.write(data)
    print(f"wrote {args.out} ({len(data)} bytes, {len(set(CHARSET))} glyphs)")


if __name__ == "__main__":
    main()
