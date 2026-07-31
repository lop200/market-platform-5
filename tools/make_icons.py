"""Generate the PWA icons as PNGs, with no image library involved.

Pillow is not a dependency of this project and an icon is not worth adding
one for: a PNG is a zlib stream of rows, and the mark is a few shapes. Run
this only when the artwork changes; the output is committed.

    python tools/make_icons.py
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

BACKGROUND = (8, 13, 22)      # --bg
GREEN = (64, 212, 154)        # --green
BLUE = (107, 184, 255)        # --blue


def _blend(base, colour, alpha):
    return tuple(int(round(b + (c - b) * alpha)) for b, c in zip(base, colour))


def _rounded_mask(size, radius, x, y):
    """Coverage of a rounded square at a pixel, antialiased at the corners."""
    cx = min(max(x, radius), size - radius)
    cy = min(max(y, radius), size - radius)
    distance = math.hypot(x - cx, y - cy)
    return max(0.0, min(1.0, radius - distance + 0.5))


def _line_coverage(x, y, points, width):
    """Distance-based coverage of a polyline, so the stroke is smooth."""
    best = float("inf")
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        dx, dy = x2 - x1, y2 - y1
        span = dx * dx + dy * dy
        t = 0.0 if span == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / span))
        best = min(best, math.hypot(x - (x1 + t * dx), y - (y1 + t * dy)))
    return max(0.0, min(1.0, width / 2 - best + 0.5))


def render(size: int, *, maskable: bool) -> bytes:
    # A maskable icon may be cropped to a circle, so the mark is drawn smaller
    # and the background covers the whole square.
    inset = size * (0.24 if maskable else 0.16)
    radius = size if maskable else size * 0.22
    stroke = size * 0.075
    span = size - inset * 2
    # A rising line with one pullback: the shape the scanner looks for.
    shape = [(0.0, 0.74), (0.26, 0.50), (0.45, 0.62), (0.72, 0.24), (1.0, 0.34)]
    points = [(inset + px * span, inset + py * span) for px, py in shape]
    peak = points[3]

    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            centre = _rounded_mask(size, radius, x + 0.5, y + 0.5)
            pixel = BACKGROUND
            alpha = centre
            if centre > 0:
                line = _line_coverage(x + 0.5, y + 0.5, points, stroke)
                if line > 0:
                    # Fade the stroke from green to blue along its length.
                    ratio = max(0.0, min(1.0, (x - inset) / span))
                    pixel = _blend(pixel, _blend(GREEN, BLUE, ratio), line)
                dot = max(
                    0.0, min(1.0, stroke * 0.95 - math.hypot(x + 0.5 - peak[0], y + 0.5 - peak[1]) + 0.5)
                )
                if dot > 0:
                    pixel = _blend(pixel, BLUE, dot)
            row += bytes((*pixel, int(round(alpha * 255))))
        rows.append(bytes(row))

    raw = zlib.compress(b"".join(rows), 9)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", raw)
        + chunk(b"IEND", b"")
    )


def main() -> None:
    STATIC.mkdir(parents=True, exist_ok=True)
    for name, size, maskable in (
        ("icon-192.png", 192, False),
        ("icon-512.png", 512, False),
        ("icon-maskable-512.png", 512, True),
    ):
        target = STATIC / name
        target.write_bytes(render(size, maskable=maskable))
        print(f"{name}: {target.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
