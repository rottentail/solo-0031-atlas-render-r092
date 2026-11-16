#!/usr/bin/env python3
"""把培训样本拼成一张对照图（contact sheet），纯标准库实现 PNG 解码/编码。

输入：
  assets/asset-a.png、assets/asset-b.png（16x16 RGBA 源素材，放大显示）
  evidence/steps/02-baseline-render.png      退役前、实例 A 的基准渲染
  evidence/steps/04-render-after-retire.png  退役后用同一旧快照的渲染
  evidence/steps/05-render-instance-b.png    交换包导入实例 B 后的渲染
输出：
  evidence/contact-sheet.png
三份渲染若逐像素一致，下方对照条为全绿并标注 changed=0（与 /v1/compare 结果互证）。
"""
import struct
import sys
import zlib
from pathlib import Path

TRAINING = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------- PNG 解码
def read_png(path: Path):
    """支持本培训用到的 8-bit RGB(colortype 2) 与 RGBA(colortype 6)。"""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} 不是 PNG")
    pos, width, height, colortype = 8, 0, 0, 0
    idat = b""
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height, bd, colortype, *_ = struct.unpack(">IIBBBBB", chunk)
            if bd != 8 or colortype not in (2, 6):
                raise ValueError("只支持 8-bit RGB/RGBA")
        elif tag == b"IDAT":
            idat += chunk
        elif tag == b"IEND":
            break
    channels = {2: 3, 6: 4}[colortype]
    stride = width * channels
    raw = zlib.decompress(idat)
    rows = []
    prev = bytearray(stride)
    i = 0
    for _ in range(height):
        ftype = raw[i]
        i += 1
        line = bytearray(raw[i:i + stride])
        i += stride
        for x in range(stride):
            a = line[x - channels] if x >= channels else 0
            b = prev[x]
            c = prev[x - channels] if x >= channels else 0
            if ftype == 1:
                line[x] = (line[x] + a) & 255
            elif ftype == 2:
                line[x] = (line[x] + b) & 255
            elif ftype == 3:
                line[x] = (line[x] + ((a + b) >> 1)) & 255
            elif ftype == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 255
            elif ftype != 0:
                raise ValueError(f"不支持的 filter {ftype}")
        rows.append(bytes(line))
        prev = line
    pixels = [[(0, 0, 0, 0)] * width for _ in range(height)]
    for y, line in enumerate(rows):
        for x in range(width):
            off = x * channels
            px = line[off:off + channels]
            pixels[y][x] = (px[0], px[1], px[2],
                            px[3] if channels == 4 else 255)
    return pixels


# ---------------------------------------------------------------- PNG 编码
def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + tag + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))


def write_png(path: Path, pixels):
    height, width = len(pixels), len(pixels[0])
    raw = bytearray()
    for row in pixels:
        raw.append(0)
        for r, g, b, a in row:
            raw.extend((r, g, b, a))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
                     + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
                     + _chunk(b"IEND", b""))


# ---------------------------------------------------------------- 5x7 点阵字
FONT = {
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01111", "10000", "10000", "10000", "10000", "10000", "01111"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "G": ["01111", "10000", "10000", "10111", "10001", "10001", "01111"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "J": ["00111", "00010", "00010", "00010", "00010", "10010", "01100"],
    "Z": ["11111", "00001", "00010", "00100", "01000", "10000", "11111"],
    "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    "0": ["01110", "10011", "10101", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    ":": ["00000", "00100", "00000", "00000", "00000", "00100", "00000"],
    ".": ["00000", "00000", "00000", "00000", "00000", "00110", "00110"],
    "=": ["00000", "00000", "11111", "00000", "11111", "00000", "00000"],
    "/": ["00001", "00010", "00010", "00100", "01000", "01000", "10000"],
    "(": ["00010", "00100", "01000", "01000", "01000", "00100", "00010"],
    ")": ["01000", "00100", "00010", "00010", "00010", "00100", "01000"],
    " ": ["00000"] * 7,
}
INK = (32, 38, 46, 255)
GREEN = (34, 139, 87, 255)
RED = (200, 60, 60, 255)
BG = (245, 241, 232, 255)
GRID = (210, 205, 195, 255)


def draw_text(canvas, text, x, y, color=INK, scale=2):
    for ch in text.upper():
        glyph = FONT.get(ch, FONT[" "])
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit == "1":
                    for sy in range(scale):
                        for sx in range(scale):
                            px, py = x + gx * scale + sx, y + gy * scale + sy
                            if 0 <= py < len(canvas) and 0 <= px < len(canvas[0]):
                                canvas[py][px] = color
        x += 6 * scale


def paste(canvas, sprite, x, y, zoom=1):
    """最近邻放大粘贴；透明像素与背景做 source-over（与服务语义一致）。"""
    for sy, row in enumerate(sprite):
        for sx, (r, g, b, a) in enumerate(row):
            for zy in range(zoom):
                for zx in range(zoom):
                    px, py = x + sx * zoom + zx, y + sy * zoom + zy
                    if a == 255:
                        canvas[py][px] = (r, g, b, 255)
                    elif a > 0:
                        br, bg2, bb, _ = canvas[py][px]
                        canvas[py][px] = (
                            (r * a + br * (255 - a)) // 255,
                            (g * a + bg2 * (255 - a)) // 255,
                            (b * a + bb * (255 - a)) // 255, 255)


def frame(canvas, x, y, w, h, color=GRID):
    for px in range(x, x + w):
        canvas[y][px] = canvas[y + h - 1][px] = color
    for py in range(y, y + h):
        canvas[py][x] = canvas[py][x + w - 1] = color


def main() -> int:
    steps = TRAINING / "evidence" / "steps"
    asset_a = read_png(TRAINING / "assets" / "asset-a.png")
    asset_b = read_png(TRAINING / "assets" / "asset-b.png")
    r1 = read_png(steps / "02-baseline-render.png")
    r2 = read_png(steps / "04-render-after-retire.png")
    r3 = read_png(steps / "05-render-instance-b.png")

    pad, zoom_asset, render_w, render_h = 24, 10, 320, 240
    row1_h = 16 * zoom_asset
    width = pad * 4 + 16 * zoom_asset * 2 + render_w * 3 + 56
    height = 40 + row1_h + 46 + render_h + 120
    canvas = [[BG for _ in range(width)] for _ in range(height)]

    draw_text(canvas, "SOURCE ASSETS (16X16, ZOOM 10X)", pad, 12, scale=2)
    x = pad
    for name, sprite in (("ASSET-A (IN USE)", asset_a), ("ASSET-B (REJECTED)", asset_b)):
        frame(canvas, x - 2, 36, 16 * zoom_asset + 4, row1_h + 4)
        paste(canvas, sprite, x, 38, zoom=zoom_asset)
        draw_text(canvas, name, x, 38 + row1_h + 8, scale=2)
        x += 16 * zoom_asset + pad * 2

    draw_text(canvas,
              "RENDERS 320X240  (LEFT=BASELINE  MID=AFTER RETIRE  RIGHT=INSTANCE B)",
              pad, 38 + row1_h + 40, scale=2)
    ry = 38 + row1_h + 64
    x = pad
    titles = ["02 BASELINE", "04 AFTER RETIRE", "05 INSTANCE B"]
    renders = [r1, r2, r3]
    for title, sprite in zip(titles, renders):
        frame(canvas, x - 2, ry - 2, render_w + 4, render_h + 4)
        paste(canvas, sprite, x, ry)
        draw_text(canvas, title, x, ry + render_h + 8, scale=2)
        x += render_w + pad

    # 逐像素差异对照：以基准为左，分别与另两份比较，有差异点红，无差异全绿
    diff_y = ry + render_h + 34
    draw_text(canvas, "PIXEL DIFF VS BASELINE:", pad, diff_y, scale=2)
    bar_x = pad + 300
    for idx, sprite in enumerate((r2, r3)):
        changed = sum(
            1 for y in range(render_h) for x0 in range(render_w)
            if sprite[y][x0][:3] != r1[y][x0][:3])
        color = GREEN if changed == 0 else RED
        label = f"({idx + 1}) CHANGED={changed}"
        draw_text(canvas, label, bar_x + idx * 260, diff_y, color=color, scale=2)

    out = TRAINING / "evidence" / "contact-sheet.png"
    write_png(out, canvas)
    print(f"对照图已写出: {out} ({width}x{height})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
