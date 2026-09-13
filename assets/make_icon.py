"""Regenerate ``assets/icon.ico`` with no third-party dependencies.

The mark is a gauge ring on a dark rounded square: a terracotta arc filled to
~70%, which is the same idea the overlay's progress bar draws.  Everything is
rasterised here by hand (4x supersampled) and written as PNG-compressed ICO
entries, so the repo needs neither Pillow nor a committed binary toolchain.

Usage::

    python assets/make_icon.py            # writes assets/icon.ico
    python assets/make_icon.py out.ico    # writes somewhere else
"""

from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path
from typing import List, Sequence, Tuple

SIZES: Sequence[int] = (16, 24, 32, 48, 64, 128, 256)
SUPERSAMPLE = 4

BACKGROUND = (21, 23, 25)
TRACK = (47, 52, 56)
ACCENT = (217, 119, 87)
FILL_FRACTION = 0.70

CORNER_RADIUS = 0.20        # fraction of the icon edge
RING_OUTER = 0.375          # fraction of the icon edge, from centre
RING_INNER = 0.255
GAP_DEGREES = 70.0          # opening at the bottom of the gauge

RGBA = Tuple[int, int, int, int]


def _rounded_square_alpha(x: float, y: float) -> float:
    """1 inside the rounded square, 0 outside (hard edge; AA comes from SSAA)."""
    r = CORNER_RADIUS
    if x < 0.0 or x > 1.0 or y < 0.0 or y > 1.0:
        return 0.0
    cx = min(max(x, r), 1.0 - r)
    cy = min(max(y, r), 1.0 - r)
    dx, dy = x - cx, y - cy
    return 1.0 if (dx * dx + dy * dy) <= r * r else 0.0


def _gauge_angle(x: float, y: float) -> float:
    """Angle in degrees around the centre, 0 at the top, growing clockwise."""
    dx = x - 0.5
    dy = y - 0.5
    angle = math.degrees(math.atan2(dx, -dy))
    return angle + 360.0 if angle < 0.0 else angle


def _sample(x: float, y: float) -> RGBA:
    if _rounded_square_alpha(x, y) <= 0.0:
        return (0, 0, 0, 0)

    dx, dy = x - 0.5, y - 0.5
    distance = math.hypot(dx, dy)
    if RING_INNER <= distance <= RING_OUTER:
        angle = _gauge_angle(x, y)
        sweep = 360.0 - GAP_DEGREES
        start = GAP_DEGREES / 2.0
        # Position along the gauge, measured clockwise from the left of the gap.
        along = angle - start
        if along < 0.0:
            along += 360.0
        if along <= sweep:
            colour = ACCENT if along <= sweep * FILL_FRACTION else TRACK
            return (colour[0], colour[1], colour[2], 255)

    return (BACKGROUND[0], BACKGROUND[1], BACKGROUND[2], 255)


def render(size: int) -> bytes:
    """Render one icon size as raw RGBA bytes, supersampled then box-filtered."""
    scale = SUPERSAMPLE
    big = size * scale
    step = 1.0 / big
    rows: List[bytearray] = []

    hi: List[List[RGBA]] = []
    for j in range(big):
        y = (j + 0.5) * step
        hi.append([_sample((i + 0.5) * step, y) for i in range(big)])

    for j in range(size):
        row = bytearray()
        for i in range(size):
            r = g = b = a = 0
            for sj in range(scale):
                source = hi[j * scale + sj]
                for si in range(scale):
                    pr, pg, pb, pa = source[i * scale + si]
                    # Premultiply so transparent pixels do not darken the edge.
                    r += pr * pa
                    g += pg * pa
                    b += pb * pa
                    a += pa
            if a == 0:
                row += b"\x00\x00\x00\x00"
                continue
            row += bytes((r // a, g // a, b // a, a // (scale * scale)))
        rows.append(row)

    return b"".join(rows)


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def to_png(size: int, rgba: bytes) -> bytes:
    """Minimal 8-bit RGBA PNG encoder (filter type 0 on every scanline)."""
    stride = size * 4
    raw = b"".join(
        b"\x00" + rgba[row * stride : (row + 1) * stride] for row in range(size)
    )
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )


def to_ico(images: Sequence[Tuple[int, bytes]]) -> bytes:
    """Pack PNG payloads into an ICO container (PNG entries need Vista or newer)."""
    count = len(images)
    directory = b""
    blobs = b""
    offset = 6 + 16 * count
    for size, png in images:
        directory += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,   # 0 means 256
            0 if size >= 256 else size,
            0,                            # palette colours
            0,                            # reserved
            1,                            # colour planes
            32,                           # bits per pixel
            len(png),
            offset,
        )
        blobs += png
        offset += len(png)
    return struct.pack("<HHH", 0, 1, count) + directory + blobs


def main(argv: Sequence[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent / "icon.ico"
    images = []
    for size in SIZES:
        images.append((size, to_png(size, render(size))))
        print(f"  rendered {size}x{size}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(to_ico(images))
    print(f"wrote {target} ({target.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
