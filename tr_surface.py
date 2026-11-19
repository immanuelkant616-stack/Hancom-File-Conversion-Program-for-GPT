"""Small, procedural clay surfaces for the Tk interface."""

from base64 import b64encode
from functools import lru_cache
from math import hypot
from struct import pack
import tkinter as tk
import zlib


def clay_photo(master, width, height, color, *, outline, radius=22):
    """Return a flat rounded surface with uniform matte grain."""
    width, height = int(width), int(height)
    if width < 1 or height < 1:
        raise ValueError("Surface dimensions must be positive.")

    def rgb(value):
        return tuple((channel + 128) // 257 for channel in master.winfo_rgb(value))

    png = _clay_png(width, height, rgb(color), rgb(outline), float(radius))
    return tk.PhotoImage(master=master, data=b64encode(png), format="png")


def _distance(x, y, radius):
    if x > 0 and y > 0:
        return hypot(x, y) - radius
    return max(x, y) - radius


def _byte(value):
    return min(255, max(0, round(value)))


def _png_chunk(kind, data):
    return pack(">I", len(data)) + kind + data + pack(">I", zlib.crc32(kind + data))


@lru_cache(maxsize=20)
def _clay_png(width, height, color, outline, radius):
    scale = min(1, width / 40, height / 44)
    center_x = width / 2
    center_y = height / 2
    half_width = center_x - 7 * scale
    half_height = center_y - 5 * scale
    radius = max(0, min(radius, half_width, half_height))
    horizontal = [abs(x + 0.5 - center_x) - half_width + radius for x in range(width)]
    grain_x = [x * 374761393 for x in range(width)]
    tones = [bytes(_byte(channel + shade) for channel in color) for shade in range(-4, 5)]
    stroke = bytes(outline)
    stroke_width = 2 * scale
    stride = width * 4 + 1
    scanlines = bytearray(stride * height)
    for y in range(height):
        py = y + 0.5
        qy = abs(py - center_y) - half_height + radius
        grain_y = y * 668265263
        for x, qx in enumerate(horizontal):
            coverage = 0.5 - _distance(qx, qy, radius)
            cover = min(1, coverage)
            if cover <= 0:
                continue
            offset = y * stride + 1 + x * 4
            face_cover = max(0, min(1, coverage - stroke_width))
            pixel = stroke
            if face_cover:
                key = grain_x[x] + grain_y
                mixed = (key ^ (key >> 13)) * 1274126177
                grain = (mixed ^ (mixed >> 16)) & 255
                face = tones[grain * len(tones) // 256]
                if face_cover == 1:
                    pixel = face
                else:
                    pixel = bytes(round(edge + (fill - edge) * face_cover)
                                  for edge, fill in zip(stroke, face))
            scanlines[offset:offset + 3] = pixel
            scanlines[offset + 3] = round(cover * 255)

    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", pack(">2I5B", width, height, 8, 6, 0, 0, 0))
            + _png_chunk(b"IDAT", zlib.compress(scanlines, 4))
            + _png_chunk(b"IEND", b""))
