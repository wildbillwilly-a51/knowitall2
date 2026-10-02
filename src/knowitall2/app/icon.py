"""The app icon as a Windows .ico file, drawn in pure Python.

The same design as ``static/icon.svg``: three connected nodes on an indigo
rounded square, in a 64-unit grid. Each size is drawn with supersampling for
smooth edges and stored as a PNG inside the icon file.
"""

from __future__ import annotations

import math
import struct
import zlib

SIZES = (16, 24, 32, 48, 64, 256)
_ACCENT = (0x4A, 0x55, 0xC8)
_WHITE = (0xFF, 0xFF, 0xFF)
_NODES = ((32.0, 20.0), (20.0, 43.0), (45.0, 40.0))
_EDGES = ((0, 1), (1, 2), (2, 0))


def ico_bytes() -> bytes:
    images = [_png(size, _draw(size, samples=2 if size >= 128 else 4)) for size in SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries = b""
    for size, data in zip(SIZES, images):
        dimension = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return header + entries + b"".join(images)


def _draw(size: int, *, samples: int) -> bytearray:
    pixels = bytearray(size * size * 4)
    scale = 64.0 / size
    for y in range(size):
        for x in range(size):
            red = green = blue = alpha = 0.0
            for j in range(samples):
                for i in range(samples):
                    u = (x + (i + 0.5) / samples) * scale
                    v = (y + (j + 0.5) / samples) * scale
                    color, opacity = _shade(u, v)
                    red += color[0] * opacity
                    green += color[1] * opacity
                    blue += color[2] * opacity
                    alpha += opacity
            count = samples * samples
            index = (y * size + x) * 4
            if alpha:
                pixels[index:index + 4] = bytes((
                    round(red / alpha), round(green / alpha), round(blue / alpha), round(255 * alpha / count),
                ))
    return pixels


def _shade(u: float, v: float) -> tuple[tuple[int, int, int], float]:
    """The color and opacity of one point of the design."""

    if not _in_rounded_square(u, v, radius=15.0):
        return _WHITE, 0.0
    color = _ACCENT
    if math.dist((u, v), _NODES[0]) <= 3.0:
        return _ACCENT, 1.0
    if any(math.dist((u, v), node) <= 7.0 for node in _NODES):
        return _WHITE, 1.0
    if any(_segment_distance((u, v), _NODES[a], _NODES[b]) <= 2.0 for a, b in _EDGES):
        color = tuple(round(0.9 * white + 0.1 * accent) for white, accent in zip(_WHITE, _ACCENT))
    return color, 1.0


def _in_rounded_square(u: float, v: float, *, radius: float) -> bool:
    if not (0.0 <= u <= 64.0 and 0.0 <= v <= 64.0):
        return False
    cx = min(max(u, radius), 64.0 - radius)
    cy = min(max(v, radius), 64.0 - radius)
    return math.dist((u, v), (cx, cy)) <= radius


def _segment_distance(point, start, end) -> float:
    (px, py), (ax, ay), (bx, by) = point, start, end
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.dist((px, py), (ax + t * dx, ay + t * dy))


def _png(size: int, pixels: bytearray) -> bytes:
    rows = b"".join(b"\x00" + bytes(pixels[y * size * 4:(y + 1) * size * 4]) for y in range(size))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))
