"""生成站群监控应用图标（stdlib，无 Pillow 依赖）。

输出：
    * site_group_monitor/web/static/sgm.ico
    * site_group_monitor/web/static/favicon.ico
    * site_group_monitor/web/static/sgm-256.png
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "site_group_monitor" / "web" / "static"

PRIMARY = (47, 111, 237, 255)       # #2f6fed
PRIMARY_DARK = (24, 74, 186, 255)
WHITE = (255, 255, 255, 255)
OK = (46, 200, 120, 255)
PANEL = (245, 248, 255, 255)
BAR = (180, 198, 232, 255)


def _blend(dst: list[int], x: int, y: int, w: int, h: int, color: tuple[int, int, int, int], cover: float) -> None:
    if cover <= 0 or not (0 <= x < w and 0 <= y < h):
        return
    i = (y * w + x) * 4
    a = color[3] / 255.0 * min(1.0, cover)
    inv = 1.0 - a
    dst[i] = int(dst[i] * inv + color[0] * a)
    dst[i + 1] = int(dst[i + 1] * inv + color[1] * a)
    dst[i + 2] = int(dst[i + 2] * inv + color[2] * a)
    dst[i + 3] = min(255, int(dst[i + 3] + (255 - dst[i + 3]) * a))


def _cover_from_sd(sd: float) -> float:
    return max(0.0, min(1.0, 0.5 - sd))


def _fill_round_rect(
    px: list[int], w: int, h: int,
    x0: float, y0: float, x1: float, y1: float, r: float,
    color: tuple[int, int, int, int],
) -> None:
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    hx = max(0.0, (x1 - x0) / 2.0)
    hy = max(0.0, (y1 - y0) / 2.0)
    radius = min(r, hx, hy)
    for y in range(max(0, int(y0) - 2), min(h, int(y1) + 3)):
        for x in range(max(0, int(x0) - 2), min(w, int(x1) + 3)):
            dx = abs(x + 0.5 - cx) - (hx - radius)
            dy = abs(y + 0.5 - cy) - (hy - radius)
            ox = max(dx, 0.0)
            oy = max(dy, 0.0)
            sd = (ox * ox + oy * oy) ** 0.5 + min(max(dx, dy), 0.0) - radius
            _blend(px, x, y, w, h, color, _cover_from_sd(sd))


def _fill_circle(px: list[int], w: int, h: int, cx: float, cy: float, r: float, color: tuple[int, int, int, int]) -> None:
    for y in range(max(0, int(cy - r - 2)), min(h, int(cy + r + 3))):
        for x in range(max(0, int(cx - r - 2)), min(w, int(cx + r + 3))):
            sd = ((x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2) ** 0.5 - r
            _blend(px, x, y, w, h, color, _cover_from_sd(sd))


def render(size: int) -> bytes:
    """绘制指定边长的 RGBA 图标。"""
    s = size
    px = [0] * (s * s * 4)
    # 外圆角底板
    pad = s * 0.06
    _fill_round_rect(px, s, s, pad, pad, s - pad, s - pad, s * 0.22, PRIMARY_DARK)
    _fill_round_rect(px, s, s, pad + s * 0.02, pad + s * 0.02, s - pad - s * 0.02, s - pad - s * 0.04, s * 0.20, PRIMARY)
    # 浏览器窗口
    wx0, wy0 = s * 0.22, s * 0.24
    wx1, wy1 = s * 0.78, s * 0.72
    _fill_round_rect(px, s, s, wx0, wy0, wx1, wy1, s * 0.06, WHITE)
    _fill_round_rect(px, s, s, wx0, wy0, wx1, wy0 + s * 0.10, s * 0.05, PANEL)
    # 窗口三点
    r_dot = max(1.2, s * 0.018)
    for i, col in enumerate(((255, 95, 86, 255), (255, 189, 46, 255), (39, 201, 63, 255))):
        _fill_circle(px, s, s, wx0 + s * 0.06 + i * s * 0.055, wy0 + s * 0.05, r_dot, col)
    # 三条站点行 + 状态点
    for i in range(3):
        y = wy0 + s * 0.20 + i * s * 0.12
        _fill_round_rect(px, s, s, wx0 + s * 0.08, y, wx1 - s * 0.16, y + s * 0.055, s * 0.02, BAR)
        _fill_circle(px, s, s, wx1 - s * 0.10, y + s * 0.028, s * 0.028, OK)
    # 右下角监控徽章
    _fill_circle(px, s, s, s * 0.74, s * 0.74, s * 0.13, WHITE)
    _fill_circle(px, s, s, s * 0.74, s * 0.74, s * 0.10, OK)
    return bytes(px)


def rgba_to_png(width: int, height: int, rgba: bytes) -> bytes:
    """把 RGBA 像素编码成 PNG。"""
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)
        raw.extend(rgba[y * stride:(y + 1) * stride])

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def downsample(src: bytes, src_size: int, dst_size: int) -> bytes:
    """盒状滤波缩小。"""
    scale = src_size / dst_size
    out = bytearray(dst_size * dst_size * 4)
    for y in range(dst_size):
        for x in range(dst_size):
            x0 = int(x * scale)
            y0 = int(y * scale)
            x1 = int((x + 1) * scale)
            y1 = int((y + 1) * scale)
            tot = [0, 0, 0, 0]
            count = 0
            for yy in range(y0, max(y0 + 1, y1)):
                for xx in range(x0, max(x0 + 1, x1)):
                    i = (yy * src_size + xx) * 4
                    tot[0] += src[i]
                    tot[1] += src[i + 1]
                    tot[2] += src[i + 2]
                    tot[3] += src[i + 3]
                    count += 1
            o = (y * dst_size + x) * 4
            out[o] = tot[0] // count
            out[o + 1] = tot[1] // count
            out[o + 2] = tot[2] // count
            out[o + 3] = tot[3] // count
    return bytes(out)


def write_ico(path: Path, pngs: list[tuple[int, bytes]]) -> None:
    """写入含多尺寸 PNG 帧的 ICO。"""
    count = len(pngs)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    entries = b""
    payload = b""
    for size, png in pngs:
        w = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(png), offset)
        payload += png
        offset += len(png)
    path.write_bytes(header + entries + payload)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    master = render(256)
    png_256 = rgba_to_png(256, 256, master)
    (OUT_DIR / "sgm-256.png").write_bytes(png_256)
    frames: list[tuple[int, bytes]] = []
    for size in (16, 24, 32, 48, 64, 128, 256):
        pixels = master if size == 256 else downsample(master, 256, size)
        frames.append((size, rgba_to_png(size, size, pixels)))
    write_ico(OUT_DIR / "sgm.ico", frames)
    write_ico(OUT_DIR / "favicon.ico", frames[:4])
    print(f"wrote {OUT_DIR / 'sgm.ico'} ({(OUT_DIR / 'sgm.ico').stat().st_size} bytes)")


if __name__ == "__main__":
    main()
