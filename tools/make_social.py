#!/usr/bin/env python3
"""Draw torrent-box's social preview (1280x640, for GitHub's repository settings): the start
page's mark and wordmark with a line about it, on its dark background. No image libraries: the
mark is drawn from the same shapes as dashboard.py's logo, the letters straight from a
TrueType font's outlines.

    python3 tools/make_social.py [--font /path/to/Font-Bold.ttf] [--font-regular /path/to/Font.ttf]

It writes .github/social-preview.png. Upload it under the repository's Settings, General,
Social preview.
"""

import argparse
import math
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".github" / "social-preview.png"
W, H = 1280, 640
SS = 4                                  # supersampling per axis, for filled shapes
BG = (0x0E, 0x10, 0x17)                 # the start page's dark --bg
B1, B2 = (0x4F, 0x5B, 0xD5), (0xA1, 0x4F, 0xD6)   # dashboard.BRAND
FG = (0xE7, 0xE9, 0xF2)                 # --fg
MUTED = (0x9A, 0xA1, 0xB5)              # --muted
FONTS = "/usr/share/fonts/truetype/dejavu"
LINE = "Torrent box in one install"
POINTS = "Transmission · nCore RSS · Jellyfin · the *arrs · Samba · Tailscale · a start page"


def mix(a, b, t):
    t = min(1.0, max(0.0, t))
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


# -- TrueType outlines --------------------------------------------------------------------

class Font:
    """Just enough of a TrueType font to draw Latin text: cmap (format 4), glyf, hmtx."""

    def __init__(self, path):
        self.data = data = Path(path).read_bytes()
        count = struct.unpack_from(">H", data, 4)[0]
        self.tables = {}
        for i in range(count):
            tag, _, offset, length = struct.unpack_from(">4sIII", data, 12 + 16 * i)
            self.tables[tag.decode("latin-1")] = (offset, length)
        head = self.tables["head"][0]
        self.units = struct.unpack_from(">H", data, head + 18)[0]
        self.long_loca = struct.unpack_from(">h", data, head + 50)[0] == 1
        self.metrics = struct.unpack_from(">H", data, self.tables["hhea"][0] + 34)[0]
        self.cmap = self.read_cmap()

    def read_cmap(self):
        data, base = self.data, self.tables["cmap"][0]
        n = struct.unpack_from(">H", data, base + 2)[0]
        for i in range(n):
            platform, encoding, offset = struct.unpack_from(">HHI", data, base + 4 + 8 * i)
            sub = base + offset
            if (platform, encoding) in ((3, 1), (0, 3)) and struct.unpack_from(">H", data, sub)[0] == 4:
                segs = struct.unpack_from(">H", data, sub + 6)[0] // 2
                ends = struct.unpack_from(f">{segs}H", data, sub + 14)
                starts = struct.unpack_from(f">{segs}H", data, sub + 16 + 2 * segs)
                deltas = struct.unpack_from(f">{segs}h", data, sub + 16 + 4 * segs)
                range_at = sub + 16 + 6 * segs
                ranges = struct.unpack_from(f">{segs}H", data, range_at)
                return segs, ends, starts, deltas, ranges, range_at
        raise ValueError("no Unicode cmap")

    def glyph_id(self, ch):
        segs, ends, starts, deltas, ranges, range_at = self.cmap
        c = ord(ch)
        for i in range(segs):
            if c <= ends[i]:
                if c < starts[i]:
                    return 0
                if not ranges[i]:
                    return (c + deltas[i]) & 0xFFFF
                at = range_at + 2 * i + ranges[i] + 2 * (c - starts[i])
                g = struct.unpack_from(">H", self.data, at)[0]
                return (g + deltas[i]) & 0xFFFF if g else 0
        return 0

    def advance(self, gid):
        hmtx = self.tables["hmtx"][0]
        return struct.unpack_from(">H", self.data, hmtx + 4 * min(gid, self.metrics - 1))[0]

    def glyph_at(self, gid):
        loca = self.tables["loca"][0]
        if self.long_loca:
            a, b = struct.unpack_from(">II", self.data, loca + 4 * gid)
        else:
            a, b = (2 * x for x in struct.unpack_from(">HH", self.data, loca + 2 * gid))
        return (self.tables["glyf"][0] + a) if b > a else None

    def contours(self, gid, dx=0, dy=0):
        """The glyph's contours as lists of (x, y, on_curve), in font units."""
        at = self.glyph_at(gid)
        if at is None:
            return []
        data = self.data
        n = struct.unpack_from(">h", data, at)[0]
        if n < 0:  # composite: its parts, moved
            out, p = [], at + 10
            while True:
                flags, part = struct.unpack_from(">HH", data, p)
                p += 4
                if flags & 1:
                    x, y = struct.unpack_from(">hh", data, p)
                    p += 4
                else:
                    x, y = struct.unpack_from(">bb", data, p)
                    p += 2
                p += 2 if flags & 0x08 else 4 if flags & 0x40 else 8 if flags & 0x80 else 0
                out += self.contours(part, dx + x, dy + y)
                if not flags & 0x20:
                    return out
        ends = struct.unpack_from(f">{n}H", data, at + 10)
        p = at + 10 + 2 * n
        p += 2 + struct.unpack_from(">H", data, p)[0]
        count = ends[-1] + 1 if n else 0
        flags = []
        while len(flags) < count:
            f = data[p]
            p += 1
            flags.append(f)
            if f & 8:
                flags += [f] * data[p]
                p += 1
        coords = []
        for short, same in ((2, 16), (4, 32)):
            v, vals = 0, []
            for f in flags:
                if f & short:
                    d = data[p]
                    p += 1
                    v += d if f & same else -d
                elif not f & same:
                    v += struct.unpack_from(">h", data, p)[0]
                    p += 2
                vals.append(v)
            coords.append(vals)
        pts = [(x + dx, y + dy, bool(f & 1)) for x, y, f in zip(coords[0], coords[1], flags)]
        out, start = [], 0
        for end in ends:
            out.append(pts[start:end + 1])
            start = end + 1
        return out


def outline(contour, steps=8):
    """A TrueType contour (quadratic, with implied on-curve points) as a polygon."""
    pts = list(contour)
    if not pts:
        return []
    if not pts[0][2]:  # start on an on-curve point
        k = next((i for i, p in enumerate(pts) if p[2]), None)
        if k is None:
            a, b = pts[0], pts[1]
            pts.insert(0, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, True))
        else:
            pts = pts[k:] + pts[:k]
    poly, prev, ctrl = [pts[0][:2]], pts[0], None
    for p in pts[1:] + pts[:1]:
        if p[2]:
            if ctrl:
                poly += quad(prev, ctrl, p, steps)
            else:
                poly.append(p[:2])
            prev, ctrl = p, None
        elif ctrl:
            mid = ((ctrl[0] + p[0]) / 2, (ctrl[1] + p[1]) / 2, True)
            poly += quad(prev, ctrl, mid, steps)
            prev, ctrl = mid, p
        else:
            ctrl = p
    return poly


def quad(a, c, b, steps):
    return [((1 - t) ** 2 * a[0] + 2 * (1 - t) * t * c[0] + t * t * b[0],
             (1 - t) ** 2 * a[1] + 2 * (1 - t) * t * c[1] + t * t * b[1])
            for t in (i / steps for i in range(1, steps + 1))]


def text_shape(font, text, x, baseline, px, spacing=0.0):
    """The polygons of TEXT at PX pixels, from (x, baseline); and its width."""
    s = px / font.units
    polys, pen = [], 0.0
    for ch in text:
        gid = font.glyph_id(ch)
        for c in font.contours(gid):
            polys.append([(x + (pen + gx) * s, baseline - gy * s) for gx, gy in outline(c)])
        pen += font.advance(gid) + spacing * font.units
    return polys, pen * s


def text_width(font, text, px, spacing=0.0):
    return sum(font.advance(font.glyph_id(ch)) + spacing * font.units for ch in text) * px / font.units


# -- painting -----------------------------------------------------------------------------

def coverage(polys):
    """Per-pixel coverage (even-odd) of polygons in canvas pixels: {(x, y): 0..1}."""
    pts = [p for poly in polys for p in poly]
    x0, x1 = max(0, int(min(p[0] for p in pts))), min(W - 1, int(max(p[0] for p in pts)) + 1)
    y0, y1 = max(0, int(min(p[1] for p in pts))), min(H - 1, int(max(p[1] for p in pts)) + 1)
    edges = []
    for poly in polys:
        for (ax, ay), (bx, by) in zip(poly, poly[1:] + poly[:1]):
            if ay != by:
                edges.append((ax * SS, ay * SS, bx * SS, by * SS))
    cov, unit = {}, 1 / (SS * SS)
    for sy in range(y0 * SS, (y1 + 1) * SS):
        y = sy + 0.5
        xs = sorted(ax + (y - ay) * (bx - ax) / (by - ay)
                    for ax, ay, bx, by in edges if (ay <= y < by) or (by <= y < ay))
        py = sy // SS
        for a, b in zip(xs[::2], xs[1::2]):
            for sx in range(max(0, math.ceil(a - 0.5)), min(W * SS - 1, math.floor(b - 0.5)) + 1):
                key = (sx // SS, py)
                cov[key] = cov.get(key, 0) + unit
    return cov


def stroke_coverage(lines, width):
    """Per-pixel coverage of polylines stroked WIDTH wide, with round caps and joins."""
    half, cov = width / 2, {}
    segs = [(a, b) for line in lines for a, b in zip(line, line[1:])]
    pts = [p for line in lines for p in line]
    x0, x1 = int(min(p[0] for p in pts) - half - 2), int(max(p[0] for p in pts) + half + 2)
    y0, y1 = int(min(p[1] for p in pts) - half - 2), int(max(p[1] for p in pts) + half + 2)
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            px, py, d = x + 0.5, y + 0.5, 1e9
            for (ax, ay), (bx, by) in segs:
                dx, dy = bx - ax, by - ay
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy or 1)))
                d = min(d, math.hypot(px - ax - t * dx, py - ay - t * dy))
            c = max(0.0, min(1.0, half + 0.5 - d))
            if c:
                cov[(x, y)] = c
    return cov


def paint(canvas, cov, color):
    """COLOR: an (r, g, b), or a function of (x, y) giving one."""
    for (x, y), c in cov.items():
        c = min(1.0, c)
        col = color(x, y) if callable(color) else color
        px = canvas[y][x]
        canvas[y][x] = [px[i] + (col[i] - px[i]) * c for i in range(3)]


def background():
    """The start page's dark background, with its brand glow from the top left."""
    rows = []
    for y in range(H):
        row = []
        for x in range(W):
            d = math.hypot((x - 300) / 900, (y - 200) / 520)
            row.append(list(mix(BG, B1, max(0.0, 1 - d) ** 2 * 0.32)))
        rows.append(row)
    return rows


def rounded_rect(x, y, w, h, r, steps=12):
    pts = []
    for cx, cy, a0 in ((x + w - r, y + r, -90), (x + w - r, y + h - r, 0), (x + r, y + h - r, 90), (x + r, y + r, 180)):
        pts += [(cx + r * math.cos(math.radians(a0 + 90 * i / steps)),
                 cy + r * math.sin(math.radians(a0 + 90 * i / steps))) for i in range(steps + 1)]
    return pts


def arc(cx, cy, r, a0, a1, steps=8):
    return [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / steps)),
             cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / steps))) for i in range(steps + 1)]


def mark(canvas, left, top, size):
    """dashboard.logo_svg: a 48-unit tile in the brand gradient, a tray and a down arrow."""
    k = size / 48

    def at(points):
        return [(left + x * k, top + y * k) for x, y in points]
    paint(canvas, coverage([at(rounded_rect(0, 0, 48, 48, 12))]),
          lambda x, y: mix(B1, B2, ((x - left) + (y - top)) / (2 * size)))
    tray = [(13, 24), (13, 33)] + arc(16, 33, 3, 180, 90) + [(32, 36)] + arc(32, 33, 3, 90, 0) + [(35, 24)]
    arrow = [(24, 11), (24, 27)]
    head = [(17.5, 20.5), (24, 27), (30.5, 20.5)]
    paint(canvas, stroke_coverage([at(tray), at(arrow), at(head)], 3.2 * k), (255, 255, 255))


def png(canvas):
    raw = bytearray()
    for row in canvas:
        raw.append(0)
        for px in row:
            raw += bytes(max(0, min(255, round(v))) for v in px)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--font", default=f"{FONTS}/DejaVuSans-Bold.ttf")
    ap.add_argument("--font-regular", default=f"{FONTS}/DejaVuSans.ttf")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    bold, regular = Font(args.font), Font(args.font_regular)
    canvas = background()
    mark(canvas, 120, 180, 280)
    left, base, px, spacing = 470, 305, 118, -0.03
    first, width = text_shape(bold, "torrent", left, base, px, spacing)
    paint(canvas, coverage(first), FG)
    second, width2 = text_shape(bold, "box", left + width, base, px, spacing)
    span = (left + width, left + width + width2)
    paint(canvas, coverage(second), lambda x, y: mix(B1, B2, (x - span[0]) / (span[1] - span[0])))
    for text, y, size, color in ((LINE, 405, 34, FG), (POINTS, 451, 22, MUTED)):
        while text_width(regular, text, size) > W - left - 50:
            size -= 1
        paint(canvas, coverage(text_shape(regular, text, left + 2, y, size)[0]), color)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(png(canvas))
    print("wrote", out)


if __name__ == "__main__":
    main()
