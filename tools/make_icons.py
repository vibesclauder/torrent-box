#!/usr/bin/env python3
"""Draw the start page's app icons, for installing it as an app on a phone: the mark from
make_social.py (dashboard.py's logo) as PNGs, no image libraries needed.

    python3 tools/make_icons.py

It writes icons/*.png, which setup.sh installs next to the start page: the rounded tile for the
web manifest, a full-bleed one Android masks to its launcher's shape (the arrow stays inside
its safe circle), and iOS's home-screen icon (full-bleed too: iOS rounds the corners itself).
"""

import struct
import zlib

import make_social as ms

OUT = ms.ROOT / "icons"
ICONS = {  # file -> (size, rounded corners)
    "icon-192.png": (192, True),
    "icon-512.png": (512, True),
    "icon-maskable-512.png": (512, False),
    "icon-180.png": (180, False),
}


def draw(size, rounded):
    """The mark filling a SIZE square: RGB rows, and the alpha of each pixel."""
    canvas = [[list(ms.mix(ms.B1, ms.B2, (x + y) / (2 * size))) for x in range(size)] for y in range(size)]
    ms.mark(canvas, 0, 0, size)
    if not rounded:
        return canvas, None
    k = size / 48
    return canvas, ms.coverage([[(x * k, y * k) for x, y in ms.rounded_rect(0, 0, 48, 48, 12)]])


def png_rgba(canvas, alpha):
    size = len(canvas)
    raw = bytearray()
    for y, row in enumerate(canvas):
        raw.append(0)
        for x, px in enumerate(row):
            a = 1.0 if alpha is None else min(1.0, alpha.get((x, y), 0.0))
            raw += bytes(max(0, min(255, round(v))) for v in px) + bytes([round(a * 255)])

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def main():
    OUT.mkdir(exist_ok=True)
    for name, (size, rounded) in ICONS.items():
        (OUT / name).write_bytes(png_rgba(*draw(size, rounded)))
        print(f"wrote {OUT / name}")


if __name__ == "__main__":
    main()
