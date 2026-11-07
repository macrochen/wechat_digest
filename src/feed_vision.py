#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""点击卡片前的「标题行校准」——纯标准库，无 OCR、无辅助功能权限。

问题背景
--------
微信 macOS「公众号」信息流的一张卡片是两行结构：

    第一行  圆形头像 + 公众号名    小字、浅灰，约在标题上方 30px
    第二行  加粗文章标题          深色、字号大

点到第一行会打开**公众号主页**（不是文章），那里「…」菜单没有
「复制链接」，grab 读到空剪贴板 → 退出码 3 让人介入。坐标由读图给出，
读图误差、或微信不断往信息流顶部插入新文章导致的列表漂移，都会让
落点上移到来源名那一行。

可利用的像素事实（2026-11-07 实测 out/capture.png，1280x832 逻辑坐标）
---------------------------------------------------------------------
在 x=520..880 的文字区逐行统计「RGB 三通道都 < 120」的像素数：

    公众号名行（y≈150/280/406/551/680）   0 ~ 1
    加粗标题行（y≈184/313/442/586/715）  65 ~ 169

两者差了一个数量级，而且不依赖字体、不依赖具体文字内容。所以本模块
不做任何文字识别，只在**点击之前**重新截一张图，把给定的 y 吸附到
附近那条深色文字带的中心。

边界
----
* 只改 y，不改 x；不改 config 里的任何 ui 坐标，不写台账。
* 找不到明确标题带、截图/解码失败时一律返回原坐标（降级为旧行为），
  绝不因为校准本身让抓取失败。
* 这是「点击前校准」，不是「失败后自动重试」：复制不到链接仍然立刻
  退出码 3 停下，等人介入，符合 PRD 决策 7。
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

# 探测窗口：只扫信息流左侧文字区，避开左侧会话列表与右侧缩略图
FEED_LEFT = 500
FEED_RIGHT = 880
# 深色判定：公众号名是浅灰（约 140~180），加粗标题近黑（约 30~70）
DARK_THRESHOLD = 120
# 半分辨率探测图里，一行至少要有这么多暗像素才算标题带
MIN_DARK_PER_ROW = 10
# 标题带至少要连续这么多行，防噪点
MIN_BAND_ROWS = 2
# 给定 y 与标题带中心相差不超过这个数，就认为已经落在标题上，不动
ON_TITLE_TOLERANCE = 8


@dataclass
class Image:
    """解码后的像素。rows 每行只保留到 max_col，省内存与解码时间。"""
    w: int
    h: int
    ch: int
    rows: list[bytearray]
    sx: float = 1.0          # 逻辑 x / 图像 x
    sy: float = 1.0          # 逻辑 y / 图像 y


@dataclass(frozen=True)
class Calibration:
    given_y: float
    used_y: float
    status: str              # on_title / moved_down / moved_up / no_title / error
    detail: str

    @property
    def moved(self) -> bool:
        return self.status in ("moved_down", "moved_up")

    def as_dict(self) -> dict:
        return {"given_y": round(self.given_y, 1),
                "used_y": round(self.used_y, 1),
                "status": self.status,
                "detail": self.detail}


def decode_png(path: str | Path, *, max_col: int | None = None,
               sx: float = 1.0, sy: float = 1.0) -> Image:
    """读 8-bit 非隔行 PNG（screencapture + sips 的产物）。

    只重建 max_col 之前的列：PNG 的 Sub/Up/Average/Paeth 滤波器只依赖
    左方与上方已解码像素，截断右半边不会破坏左半边的正确性。
    """
    blob = Path(path).read_bytes()
    if blob[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG 文件")
    pos, idat = 8, bytearray()
    w = h = bd = ct = interlace = None
    while pos + 8 <= len(blob):
        length = struct.unpack(">I", blob[pos:pos + 4])[0]
        typ = blob[pos + 4:pos + 8]
        data = blob[pos + 8:pos + 8 + length]
        if typ == b"IHDR":
            w, h, bd, ct, _comp, _filt, interlace = struct.unpack(">IIBBBBB", data)
        elif typ == b"IDAT":
            idat += data
        elif typ == b"IEND":
            break
        pos += 12 + length
    if w is None or h is None:
        raise ValueError("PNG 缺 IHDR")
    if bd != 8 or ct not in (0, 2, 4, 6) or interlace != 0:
        raise ValueError(f"暂不支持的 PNG 格式：bitdepth={bd} colortype={ct} "
                         f"interlace={interlace}")
    ch = {0: 1, 2: 3, 4: 2, 6: 4}[ct]
    stride = w * ch
    need = min(stride, (max_col or w) * ch)
    raw = zlib.decompress(bytes(idat))
    rows: list[bytearray] = []
    prev = bytearray(need)
    p = 0
    for _y in range(h):
        f = raw[p]
        p += 1
        line = raw[p:p + stride]
        p += stride
        if len(line) < need:
            raise ValueError("PNG 数据被截断")
        cur = bytearray(line[:need])
        if f == 1:                                   # Sub
            for i in range(ch, need):
                cur[i] = (cur[i] + cur[i - ch]) & 255
        elif f == 2:                                 # Up
            for i in range(need):
                cur[i] = (cur[i] + prev[i]) & 255
        elif f == 3:                                 # Average
            for i in range(need):
                a = cur[i - ch] if i >= ch else 0
                cur[i] = (cur[i] + ((a + prev[i]) >> 1)) & 255
        elif f == 4:                                 # Paeth
            for i in range(need):
                a = cur[i - ch] if i >= ch else 0
                b = prev[i]
                c = prev[i - ch] if i >= ch else 0
                pp = a + b - c
                pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                cur[i] = (cur[i] + pr) & 255
        elif f != 0:
            raise ValueError(f"不认识的 PNG 滤波器：{f}")
        rows.append(cur)
        prev = cur
    return Image(w=w, h=h, ch=ch, rows=rows, sx=sx, sy=sy)


def _dark_count(img: Image, y: int, x0: int, x1: int,
                threshold: int = DARK_THRESHOLD) -> int:
    row = img.rows[y]
    n = 0
    for x in range(x0, min(x1, img.w)):
        i = x * img.ch
        if img.ch >= 3:
            if row[i] < threshold and row[i + 1] < threshold \
                    and row[i + 2] < threshold:
                n += 1
        elif row[i] < threshold:
            n += 1
    return n


@dataclass(frozen=True)
class Band:
    center: float            # 逻辑坐标
    peak: int
    y0: float
    y1: float


def find_title_bands(img: Image, y0: int, y1: int, *,
                     threshold: int = DARK_THRESHOLD,
                     min_dark: int = MIN_DARK_PER_ROW,
                     min_rows: int = MIN_BAND_ROWS) -> list[Band]:
    """在图像行 [y0, y1) 内找深色文字带，中心换算回逻辑 y。"""
    x0, x1 = int(FEED_LEFT / img.sx), int(FEED_RIGHT / img.sx)
    counts = [_dark_count(img, y, x0, x1, threshold)
              for y in range(max(0, y0), min(img.h, y1))]
    bands: list[Band] = []
    start = None
    for off, count in enumerate(counts + [0]):
        y = max(0, y0) + off
        if count >= min_dark:
            if start is None:
                start = y
        elif start is not None:
            if y - start >= min_rows:
                seg = counts[start - max(0, y0):y - max(0, y0)]
                bands.append(Band(
                    center=(start + y - 1) / 2 * img.sy,
                    peak=max(seg),
                    y0=start * img.sy, y1=(y - 1) * img.sy,
                ))
            start = None
    return bands


def calibrate(image_path: str | Path, y: float, *,
              logical_width: int, logical_height: int,
              search_down: float = 45, search_up: float = 20,
              threshold: int = DARK_THRESHOLD,
              min_dark: int = MIN_DARK_PER_ROW) -> Calibration:
    """把给定 y 吸附到附近的加粗标题行；任何异常都降级为「保持原坐标」。

    image_path 是探测截图（通常与逻辑分辨率不同，由 sx/sy 换算）。
    """
    try:
        img = decode_png(image_path)
        # 用实际图像尺寸换算：探测图一般是逻辑分辨率的一半
        img.sx = logical_width / img.w
        img.sy = logical_height / img.h
        y0 = max(0, int((y - search_up) / img.sy))
        y1 = min(img.h, int((y + search_down) / img.sy) + 1)
        bands = find_title_bands(img, y0, y1, threshold=threshold,
                                 min_dark=min_dark)
        if not bands:
            return Calibration(y, y, "no_title",
                               "附近没有深色标题带，保持原坐标")
        for band in bands:
            if abs(band.center - y) <= ON_TITLE_TOLERANCE:
                return Calibration(y, y, "on_title",
                                   f"已在标题行（y≈{band.center:.0f}）")
        # 来源名在标题上方，误点几乎都是 y 偏小，所以优先向下找。
        # 按「深色像素峰值」选而不是按距离选：卡片里「余下 N 篇 >」之类
        # 的小字偶尔也会形成弱带，离得更近但不是标题。
        below = [b for b in bands if b.center > y
                 and b.center - y <= search_down]
        if below:
            best = max(below, key=lambda b: (b.peak, -(b.center - y)))
            return Calibration(y, best.center, "moved_down",
                               f"下移到深色标题行 y≈{best.center:.0f}"
                               f"（峰值 {best.peak}）")
        above = [b for b in bands if b.center < y
                 and y - b.center <= search_up]
        if above:
            best = max(above, key=lambda b: (b.peak, -(y - b.center)))
            return Calibration(y, best.center, "moved_up",
                               f"上移到深色标题行 y≈{best.center:.0f}"
                               f"（峰值 {best.peak}）")
        return Calibration(y, y, "no_title", "附近没有可用标题带，保持原坐标")
    except Exception as exc:                       # 降级，绝不让校准拖垮抓取
        return Calibration(y, y, "error", f"校准不可用（{type(exc).__name__}: {exc}），保持原坐标")
