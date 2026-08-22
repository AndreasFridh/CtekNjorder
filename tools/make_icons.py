#!/usr/bin/env python3
"""
Turn the product photo into the add-on's icon and logo.

Home Assistant shows `icon.png` in the add-on store list and `logo.png` on the
add-on's own page; without them the add-on gets a generic placeholder.

The interesting part is cutting the product out of its backdrop. The obvious
approach - make white pixels transparent - destroys the artwork, because the
charger is black with a *white* logo printed on it, and the shot has a soft
grey shadow that is neither. So the subject is found by its silhouette instead:
the dark body is detected, then each row and column is filled between its first
and last dark pixel. Requiring both fills keeps the gap between the two cables
open, which a row fill alone would bridge.

    python tools/make_icons.py

Re-run after replacing branding/source.webp.
"""
from __future__ import annotations

import os

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BRANDING = os.path.join(ROOT, "branding")
ADDON = os.path.join(ROOT, "ctek_njord_sim")

ICON_SIZE = 256
LOGO_SIZE = (500, 200)
# Luminance below this counts as "part of the product". The photo separates
# cleanly: the body sits under 60, its anti-aliased edge runs to ~130, and the
# drop shadow occupies 150-240 against a 250-ish backdrop. Picking the gap
# keeps the whole charger and leaves the shadow behind - at 200 the shadow came
# through as a grey halo around the icon.
DARK_BELOW = 130


def find_source() -> str | None:
    for name in ("source.webp", "source.png", "source.jpg"):
        path = os.path.join(BRANDING, name)
        if os.path.exists(path):
            return path
    return None


def cut_out(img: Image.Image) -> Image.Image:
    """Replace the backdrop with transparency, keeping the product intact."""
    rgb = np.asarray(img.convert("RGB")).astype(np.int16)
    lum = rgb.mean(axis=2)
    dark = lum < DARK_BELOW
    h, w = dark.shape

    rows = np.arange(w)[None, :]
    has_row = dark.any(1)
    first = np.where(has_row, dark.argmax(1), w)
    last = np.where(has_row, w - 1 - dark[:, ::-1].argmax(1), -1)
    row_fill = (rows >= first[:, None]) & (rows <= last[:, None])

    cols = np.arange(h)[:, None]
    has_col = dark.any(0)
    top = np.where(has_col, dark.argmax(0), h)
    bottom = np.where(has_col, h - 1 - dark[::-1, :].argmax(0), -1)
    col_fill = (cols >= top[None, :]) & (cols <= bottom[None, :])

    # Both, not either: a row across the two cables would otherwise bridge the
    # daylight between them.
    solid = row_fill & col_fill

    alpha = Image.fromarray((solid * 255).astype(np.uint8), "L")
    alpha = alpha.filter(ImageFilter.GaussianBlur(0.6))   # soften the cut edge
    out = img.convert("RGBA")
    out.putalpha(alpha)
    return out.crop(out.getbbox())


def fit(img: Image.Image, size: tuple[int, int], pad: float) -> Image.Image:
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    inner = (max(1, int(size[0] * (1 - 2 * pad))), max(1, int(size[1] * (1 - 2 * pad))))
    art = img.copy()
    art.thumbnail(inner, Image.LANCZOS)
    canvas.alpha_composite(art, ((size[0] - art.width) // 2, (size[1] - art.height) // 2))
    return canvas


def on_light_plate(art: Image.Image, size: int) -> Image.Image:
    """
    Put the cut-out on a pale rounded plate.

    The charger is almost black. Left transparent, the icon would be close to
    invisible against Home Assistant's dark theme - which is the theme most
    people use. The plate costs nothing on a light theme and makes the icon
    legible on a dark one.
    """
    plate = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(plate)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.22),
                        fill=(244, 245, 247, 255))
    plate.alpha_composite(fit(art, (size, size), pad=0.10))
    return plate


def build_logo(art: Image.Image, size: tuple[int, int]) -> Image.Image:
    """
    Charger on the left, name on the right.

    The product is portrait and the logo canvas is landscape, so fitting the
    photo alone leaves most of the width empty and the charger shrunk to a
    thumbnail. Pairing it with the name uses the space and says what the add-on
    is. Same pale plate as the icon, for the same reason: black-on-transparent
    disappears against a dark theme.
    """
    w, h = size
    plate = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(plate)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=int(h * 0.16),
                        fill=(244, 245, 247, 255))

    art_h = int(h * 0.82)
    scaled = art.copy()
    scaled.thumbnail((int(w * 0.30), art_h), Image.LANCZOS)
    plate.alpha_composite(scaled, (int(h * 0.16), (h - scaled.height) // 2))

    try:
        big = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", int(h * 0.24))
        small = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", int(h * 0.135))
    except OSError:
        big = small = ImageFont.load_default()

    x = int(h * 0.16) + scaled.width + int(w * 0.06)
    d.text((x, h * 0.33), "Njord", font=big, fill=(24, 26, 30))
    d.text((x, h * 0.56), "Load Balancer", font=small, fill=(108, 116, 126))
    return plate


def main() -> None:
    os.makedirs(BRANDING, exist_ok=True)
    source = find_source()
    if not source:
        raise SystemExit(
            f"No artwork found. Put the product image at "
            f"{os.path.join('branding', 'source.webp')} and re-run."
        )

    src = Image.open(source)
    print(f"source {os.path.relpath(source, ROOT)}  {src.width}x{src.height} {src.mode}")

    art = cut_out(src)
    print(f"  subject cut to {art.width}x{art.height}")

    transparent = os.path.join(BRANDING, "njord.png")
    art.save(transparent)
    print(f"  wrote {os.path.relpath(transparent, ROOT)} (full size, transparent)")

    outputs = {
        os.path.join(ADDON, "icon.png"): on_light_plate(art, ICON_SIZE),
        os.path.join(ADDON, "logo.png"): build_logo(art, LOGO_SIZE),
    }
    for path, img in outputs.items():
        img.save(path)
        print(f"  wrote {os.path.relpath(path, ROOT)} {img.width}x{img.height}"
              f" ({os.path.getsize(path)} bytes)")


if __name__ == "__main__":
    main()
