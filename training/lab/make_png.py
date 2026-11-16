#!/usr/bin/env python3
"""生成培训用的自制 PNG 素材（仅使用 Python 标准库，不依赖 Pillow）。

输出两份 16x16 RGBA PNG，图案由固定公式逐像素计算，任何机器上重跑
得到字节级一致的文件（用 sha256 摘要核对，见 README 的固定内容表）。
另生成一份内容损坏文件 bad-not-png.bin，用于演示入口校验拒绝。
"""
import hashlib
import struct
import sys
import zlib
from pathlib import Path

W = H = 16


def png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def write_png(path: Path, pixels: list[list[tuple[int, int, int, int]]]) -> None:
    """pixels: 16 行，每行 16 个 RGBA 元组。PNG 采用每行前置 filter byte 0。"""
    raw = bytearray()
    for row in pixels:
        raw.append(0)
        for r, g, b, a in row:
            raw.extend((r, g, b, a))
    ihdr = struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0)  # 8-bit RGBA
    data = (b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", ihdr)
            + png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + png_chunk(b"IEND", b""))
    path.write_bytes(data)


def asset_a() -> list[list[tuple[int, int, int, int]]]:
    """蓝底（#145A70）+ 白色对角带 + 右下透明象限，代号 A。"""
    rows = []
    for y in range(H):
        row = []
        for x in range(W):
            if x >= 8 and y >= 8:
                row.append((0, 0, 0, 0))                      # 透明象限
            elif abs(x - y) <= 1:
                row.append((255, 255, 255, 255))              # 白色对角带
            else:
                row.append((0x14, 0x5A, 0x70, 255))           # 蓝色底
        rows.append(row)
    return rows


def asset_b() -> list[list[tuple[int, int, int, int]]]:
    """橙底（#E87E4F）+ 青色反对角带 + 左下半透明区，代号 B（与 A 内容不同）。"""
    rows = []
    for y in range(H):
        row = []
        for x in range(W):
            base = (0xE8, 0x7E, 0x4F)
            alpha = 128 if (x < 8 and y >= 8) else 255       # 半透明象限
            if abs(x + y - 15) <= 1:
                row.append((0x32, 0xA5, 0xA0, alpha))        # 青色反对角带
            else:
                row.append((base[0], base[1], base[2], alpha))
        rows.append(row)
    return rows


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "assets"
    out.mkdir(parents=True, exist_ok=True)
    plan = {
        "asset-a.png": asset_a(),
        "asset-b.png": asset_b(),
    }
    manifest = {}
    for name, pixels in plan.items():
        target = out / name
        write_png(target, pixels)
        data = target.read_bytes()
        manifest[name] = {
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "dimensions": f"{W}x{H}",
            "colorType": "RGBA 8-bit",
        }
    # 非 PNG 字节，用于证明 PUT /assets 入口先做 PNG 解码校验
    bad = out / "bad-not-png.bin"
    bad.write_bytes(b"this is definitely not a png file\n" * 8)
    manifest["bad-not-png.bin"] = {
        "bytes": bad.stat().st_size,
        "sha256": hashlib.sha256(bad.read_bytes()).hexdigest(),
        "dimensions": "n/a",
        "colorType": "n/a (plain text, must be rejected)",
    }
    for name, meta in manifest.items():
        print(f"{name:18s} {meta['bytes']:6d} bytes  sha256={meta['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
