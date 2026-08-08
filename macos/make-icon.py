#!/usr/bin/env python3
"""Render the Share-It app icon with zero dependencies.

Draws a coral squircle tile with a white "share" glyph (three nodes linked),
writes a 1024px master PNG, and (on macOS) builds AppIcon.icns via sips+iconutil.
Pure stdlib: analytic 1px anti-aliasing, hand-rolled PNG encoder. Reproducible.
"""
import math
import os
import struct
import subprocess
import sys
import zlib

N = 1024
CX = CY = N / 2.0

# --- brand palette (Claude coral) ---
TOP = (0xF0, 0x93, 0x63)      # warm coral, top
BOT = (0xC2, 0x54, 0x33)      # deep terracotta, bottom
WHITE = (0xFF, 0xFF, 0xFF)


def _len(x, y):
    return math.hypot(x, y)


def sd_roundrect(px, py, half, r):
    qx = abs(px - CX) - (half - r)
    qy = abs(py - CY) - (half - r)
    return min(max(qx, qy), 0.0) + _len(max(qx, 0.0), max(qy, 0.0)) - r


def sd_circle(px, py, cx, cy, r):
    return _len(px - cx, py - cy) - r


def sd_segment(px, py, ax, ay, bx, by, half):
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return _len(px - (ax + t * dx), py - (ay + t * dy)) - half


def cov(d):
    """Signed distance (px) -> coverage in [0,1] with ~1px smooth edge."""
    return max(0.0, min(1.0, 0.5 - d))


# glyph geometry — three nodes: one left, two right, linked
NODE_R = 88.0
LINK_W = 46.0
LEFT = (372.0, CY)
TOPR = (686.0, 330.0)
BOTR = (686.0, 694.0)


def glyph_cov(px, py):
    d = min(
        sd_circle(px, py, *LEFT, NODE_R),
        sd_circle(px, py, *TOPR, NODE_R),
        sd_circle(px, py, *BOTR, NODE_R),
        sd_segment(px, py, LEFT[0], LEFT[1], TOPR[0], TOPR[1], LINK_W / 2),
        sd_segment(px, py, LEFT[0], LEFT[1], BOTR[0], BOTR[1], LINK_W / 2),
    )
    return cov(d)


def render():
    half = N / 2.0 - 40.0          # 40px transparent margin
    radius = 232.0                 # squircle-ish corner
    rows = bytearray()
    for y in range(N):
        py = y + 0.5
        rows.append(0)             # PNG filter byte: none
        g = py / N                 # vertical gradient factor
        br = int(TOP[0] + (BOT[0] - TOP[0]) * g)
        bg = int(TOP[1] + (BOT[1] - TOP[1]) * g)
        bb = int(TOP[2] + (BOT[2] - TOP[2]) * g)
        for x in range(N):
            px = x + 0.5
            tile = cov(sd_roundrect(px, py, half, radius))
            if tile <= 0.0:
                rows += b"\x00\x00\x00\x00"
                continue
            gc = glyph_cov(px, py)
            r = int(br + (WHITE[0] - br) * gc)
            gg = int(bg + (WHITE[1] - bg) * gc)
            b = int(bb + (WHITE[2] - bb) * gc)
            a = int(255 * tile)
            rows += bytes((r, gg, b, a))
    return rows


def write_png(path, raw):
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", N, N, 8, 6, 0, 0, 0)  # 8-bit RGBA
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", ihdr))
        f.write(chunk(b"IDAT", zlib.compress(bytes(raw), 9)))
        f.write(chunk(b"IEND", b""))


def build_icns(master, out_dir):
    iconset = os.path.join(out_dir, "AppIcon.iconset")
    os.makedirs(iconset, exist_ok=True)
    for size in (16, 32, 128, 256, 512):
        for scale, name in ((1, f"icon_{size}x{size}.png"),
                            (2, f"icon_{size}x{size}@2x.png")):
            px = size * scale
            subprocess.run(["sips", "-z", str(px), str(px), master,
                            "--out", os.path.join(iconset, name)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    icns = os.path.join(out_dir, "AppIcon.icns")
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", icns], check=True)
    return icns


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    master = os.path.join(here, "AppIcon-1024.png")
    print("rendering 1024px master…")
    write_png(master, render())
    print("wrote", master)
    if sys.platform == "darwin":
        icns = build_icns(master, here)
        print("wrote", icns)
    else:
        print("skipping .icns (not macOS)")
