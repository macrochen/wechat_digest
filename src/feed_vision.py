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
# 单行标题带的逻辑高度范围（像素）。用来把「常看的号」头像（约 30+px）、
# 「余下 N 篇 / 时间」这类 4~5px 小字噪点，与真正的标题（约 10~14px）区分开。
# 全屏匹配与就近匹配都只用这个高度区间的带。
MIN_TITLE_LOGICAL_HEIGHT = 6
MAX_TITLE_LOGICAL_HEIGHT = 22

# 「该内容已被发布者删除」空页：整页近白 + 居中一个黄色圆形感叹号图标。
# 实测颜色（2026-11-07 截图采样）：白底 ~248，图标黄 ~(245,200,20)。
# 判据必须**两个条件同时成立**：纯白底的文章（公告、纯文字）白占比同样很高，
# 但它没有那块黄色图标；反过来图片文章可能有黄色，但不是白底。
WHITE_MIN = 235
YELLOW_R_MIN = 200
YELLOW_G_MIN = 150
YELLOW_G_MAX = 235
YELLOW_B_MAX = 90


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
                     min_rows: int = MIN_BAND_ROWS,
                     x0: int | None = None, x1: int | None = None) -> list[Band]:
    """在图像行 [y0, y1) 内找深色文字带，中心换算回逻辑 y。

    默认扫信息流左侧文字区（FEED_LEFT..FEED_RIGHT）；弹窗菜单等别的区域
    可以把 x0/x1 传成**图像像素**列范围。
    """
    if x0 is None:
        x0 = int(FEED_LEFT / img.sx)
    if x1 is None:
        x1 = int(FEED_RIGHT / img.sx)
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


def _prefer_strong(bands: list[Band], min_peak: int) -> list[Band]:
    """优先只留「够黑够粗」的标题带，噪声带只在没别的选项时才用。

    实测真标题峰值 88~169；「余下 N 篇 >」「时间」这类小字带峰值只有
    ~20。两者都落在标题高度区间里，光靠高度分不开，所以再加一道峰值门槛。
    全都没超门槛时退回原列表（比如界面换配色），不做硬失败。
    """
    if min_peak <= 0:
        return bands
    strong = [b for b in bands if b.peak >= min_peak]
    return strong or bands


def _choose_band(bands: list[Band], y: float,
                 search_down: float, search_up: float) -> tuple[Band | None, str]:
    """从候选带里挑目标：优先「已在标题行」，否则向下优先、再向上。

    来源名在标题上方，误点几乎都是 y 偏小，所以优先向下找。
    按「深色像素峰值」选而不是按距离选：卡片里「余下 N 篇 >」之类的小字
    偶尔也会形成弱带，离得更近但不是标题。
    """
    for band in bands:
        if abs(band.center - y) <= ON_TITLE_TOLERANCE:
            return band, "on_title"
    below = [b for b in bands if b.center > y and b.center - y <= search_down]
    if below:
        return max(below, key=lambda b: (b.peak, -(b.center - y))), "moved_down"
    above = [b for b in bands if b.center < y and y - b.center <= search_up]
    if above:
        return max(above, key=lambda b: (b.peak, -(y - b.center))), "moved_up"
    return None, "no_title"


def calibrate(image_path: str | Path, y: float, *,
              logical_width: int, logical_height: int,
              search_down: float = 45, search_up: float = 20,
              threshold: int = DARK_THRESHOLD,
              min_dark: int = MIN_DARK_PER_ROW) -> Calibration:
    """把给定 y 吸附到附近的加粗标题行；任何异常都降级为「保持原坐标」。

    image_path 是探测截图（通常与逻辑分辨率不同，由 sx/sy 换算）。
    只在给定 y 附近的小窗里找，找不到就保持原坐标（旧行为）。
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
        band, how = _choose_band(bands, y, search_down, search_up)
        if how == "on_title":
            return Calibration(y, y, "on_title",
                               f"已在标题行（y≈{band.center:.0f}）")
        if how == "moved_down":
            return Calibration(y, band.center, "moved_down",
                               f"下移到深色标题行 y≈{band.center:.0f}"
                               f"（峰值 {band.peak}）")
        if how == "moved_up":
            return Calibration(y, band.center, "moved_up",
                               f"上移到深色标题行 y≈{band.center:.0f}"
                               f"（峰值 {band.peak}）")
        return Calibration(y, y, "no_title", "附近没有可用标题带，保持原坐标")
    except Exception as exc:                       # 降级，绝不让校准拖垮抓取
        return Calibration(y, y, "error", f"校准不可用（{type(exc).__name__}: {exc}），保持原坐标")


def _band_xrange(img: Image, band: Band, threshold: int,
                 x_lo: int, x_hi: int) -> tuple[float, float]:
    """一条标题带的横向文字范围（逻辑坐标）。"""
    y0 = max(0, int(band.y0 / img.sy))
    y1 = min(img.h - 1, int(band.y1 / img.sy))
    xs: list[int] = []
    for y in range(y0, y1 + 1):
        row = img.rows[y]
        for x in range(x_lo, min(x_hi, img.w)):
            i = x * img.ch
            if img.ch >= 3:
                if row[i] < threshold and row[i + 1] < threshold \
                        and row[i + 2] < threshold:
                    xs.append(x)
            elif row[i] < threshold:
                xs.append(x)
    if not xs:
        return (FEED_LEFT, FEED_RIGHT)
    return (min(xs) * img.sx, max(xs) * img.sx)


def _band_fingerprint(img: Image, band: Band, threshold: int,
                      x_lo: int, x_hi: int, cell: int = 6) -> str:
    """标题行的像素指纹：按 cell 宽切格子统计暗像素，二值化后打包成十六进制。

    用途是**跨屏认同一篇文章**：滚动后同一篇文章换了 y，但文字像素分布不变，
    指纹也就不变。这样既能避免重复点同一张卡片，也能发现「这一屏跟上一屏
    完全没重叠」的漏抓风险。
    """
    y0 = max(0, int(band.y0 / img.sy))
    y1 = min(img.h - 1, int(band.y1 / img.sy))
    if y1 <= y0:
        y1 = min(img.h - 1, y0 + 1)
    rows = y1 - y0 + 1
    need = max(1, int(rows * 0.5))
    bits: list[int] = []
    x = x_lo
    while x < min(x_hi, img.w):
        xe = min(x + cell, x_hi, img.w)
        cnt = 0
        for yy in range(y0, y1 + 1):
            cnt += _dark_count(img, yy, x, xe, threshold)
        bits.append(1 if cnt >= need else 0)
        x = xe
    # 每 4 位一个十六进制字符，末尾补 0
    while len(bits) % 4:
        bits.append(0)
    out = []
    for i in range(0, len(bits), 4):
        v = (bits[i] << 3) | (bits[i + 1] << 2) | (bits[i + 2] << 1) | bits[i + 3]
        out.append("0123456789abcdef"[v])
    return "".join(out)


def fps_close(a: str, b: str, max_diff: int = 4) -> bool:
    """两个指纹是否指向同一行文字（按十六进制字符算汉明距离）。"""
    if not a or not b or len(a) != len(b):
        return False
    diff = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            diff += 1
            if diff > max_diff:
                return False
    return True


def find_card_titles(image_path: str | Path, *,
                     logical_width: int, logical_height: int,
                     threshold: int = DARK_THRESHOLD,
                     min_dark: int = MIN_DARK_PER_ROW,
                     min_logical_height: float = MIN_TITLE_LOGICAL_HEIGHT,
                     max_logical_height: float = MAX_TITLE_LOGICAL_HEIGHT,
                     min_peak: int = 55,
                     min_y: float = 200,
                     merge_gap: float = 30,
                     weak_min_peak: int = 26,
                     weak_min_gap: float = 90,
                     left_align_tol: float = 14,
                     ) -> list["CardTitle"]:
    """找信息流里每张卡片的标题落点（含横向范围），供自动点击使用。

    与 `find_all_title_bands` 的区别：这里要的是「能点的位置」，所以

    * 用 `min_peak` 只留真正加粗的标题行——卡片里「余下 N 篇」「时间」
      这类小字峰值只有 18~37，标题是 100+，差一个数量级；
    * 用 `min_y` 跳过顶部「常看的号」头像行；
    * 同一张卡片的两行标题（间距约 21px）合并成一条，`merge_gap` 取 30
      远小于卡片间距（约 128px），不会把相邻两张卡片并成一张；
    * 额外算出标题文字的横向范围，点击落在**文字中间**而不是固定 x。

    短标题补回（2026-11-08 新增）
    ----------------------------
    纯峰值门槛会漏掉「大佬们开始调仓了。」这种八九个字的短标题：暗像素
    总量随字数线性下降，短标题峰值只有 20~40，被当成小字噪声扔掉，整张
    卡片就永远点不到（用户反馈的「漏点」）。所以再加一轮**弱带回收**：

    * 峰值落在 [weak_min_peak, min_peak) 之间；
    * 距最近的强标题 ≥ weak_min_gap（同一张卡片里的小字总在强标题上下
      40px 内，跨卡片才到 90px 以上）；
    * 左边缘与强标题的左边缘对齐（卡片标题一律左对齐，「常看的号」头像
      下面的名字是居中排在头像下面的，对不齐，正好被这一条筛掉）。
    """
    img = decode_png(image_path)
    img.sx = logical_width / img.w
    img.sy = logical_height / img.h
    bands = find_title_bands(img, 0, img.h, threshold=threshold,
                            min_dark=min_dark)
    x_lo, x_hi = int(FEED_LEFT / img.sx), int(FEED_RIGHT / img.sx)
    in_range = [b for b in bands
                if min_logical_height <= (b.y1 - b.y0) <= max_logical_height
                and b.center >= min_y]
    strong = [b for b in in_range if b.peak >= min_peak]

    weak: list[Band] = []
    if weak_min_peak > 0:
        strong_x0 = [_band_xrange(img, b, threshold, x_lo, x_hi)[0]
                     for b in strong]
        left_edge = (sum(strong_x0) / len(strong_x0)) if strong_x0 else None
        for b in in_range:
            if b.peak >= min_peak or b.peak < weak_min_peak:
                continue
            if left_edge is not None:
                bx0 = _band_xrange(img, b, threshold, x_lo, x_hi)[0]
                # 「常看的号」头像下面的名字比卡片标题窄得多，且起点偏右
                if abs(bx0 - left_edge) > left_align_tol:
                    continue
            if strong and min(abs(b.center - s.center) for s in strong) \
                    < weak_min_gap:
                continue
            weak.append(b)

    keep = sorted(strong + weak, key=lambda b: b.center)

    groups: list[list[Band]] = []
    for b in keep:
        if groups and b.center - groups[-1][-1].center <= merge_gap:
            groups[-1].append(b)
        else:
            groups.append([b])

    cards: list["CardTitle"] = []
    for grp in groups:
        top = grp[0]
        first, last = grp[0], grp[-1]
        cx0, cx1 = _band_xrange(
            img, Band(center=top.center, peak=max(b.peak for b in grp),
                      y0=first.y0, y1=last.y1),
            threshold, x_lo, x_hi)
        cards.append(CardTitle(
            x=(cx0 + cx1) / 2, y=top.center, peak=max(b.peak for b in grp),
            lines=len(grp), x0=cx0, x1=cx1,
            fp=_band_fingerprint(img, first, threshold, x_lo, x_hi),
        ))
    return cards


# 左侧会话列表里「公众号」入口是一个实心蓝色圆角方块（约 36×36），
# 其余蓝色像素都是零碎小字/小图标（实测 <15px），用「尺寸 + 填充度」即可
# 唯一确定它，不需要 OCR、也不需要读图。
BLUE_MIN_SIDE = 24
BLUE_MAX_SIDE = 58
BLUE_MIN_ROW = 16


def _is_icon_blue(r: int, g: int, b: int) -> bool:
    return b > 140 and b - r > 50 and b - g > 30


@dataclass(frozen=True)
class IconBlock:
    """左侧列表里的一个实心色块（用来定位「公众号」入口）。"""
    x: float
    y: float
    w: float
    h: float
    fill: int

    def as_dict(self) -> dict:
        return {"x": round(self.x, 1), "y": round(self.y, 1),
                "w": round(self.w, 1), "h": round(self.h, 1),
                "fill": self.fill}


@dataclass(frozen=True)
class CardTitle:
    """一张文章卡片的可点位置（逻辑坐标）。"""
    x: float
    y: float
    peak: int
    lines: int
    x0: float
    x1: float
    fp: str = ""        # 标题行像素指纹，跨屏认同一篇文章用

    def as_dict(self) -> dict:
        return {"x": round(self.x, 1), "y": round(self.y, 1),
                "peak": self.peak, "lines": self.lines,
                "text_x": [round(self.x0, 1), round(self.x1, 1)]}


def find_icon_blocks(image_path: str | Path, *,
                     logical_width: int, logical_height: int,
                     x0: int = 20, x1: int = 340,
                     y0: int = 60, y1: int | None = None,
                     min_side: float = BLUE_MIN_SIDE,
                     max_side: float = BLUE_MAX_SIDE,
                     min_row: int = BLUE_MIN_ROW) -> list[IconBlock]:
    """在左侧会话列表里找实心蓝色方块，按「填充度」从高到低返回。

    只用于导航（点开「公众号」信息流），不参与抓取，也绝不写台账。
    y1 默认停在程序坞上方，避免把 Dock 里的应用图标当成列表条目。
    """
    img = decode_png(image_path)
    sx = logical_width / img.w
    sy = logical_height / img.h
    px0, px1 = int(x0 / sx), min(int(x1 / sx), img.w)
    py0 = max(0, int(y0 / sy))
    py1 = min(img.h, int((logical_height - 90 if y1 is None else y1) / sy))

    rows: dict[int, tuple[int, int, int]] = {}
    for y in range(py0, py1):
        row = img.rows[y]
        n = 0
        lo = hi = 0
        for x in range(px0, px1):
            i = x * img.ch
            if img.ch >= 3:
                hit = _is_icon_blue(row[i], row[i + 1], row[i + 2])
            else:
                hit = row[i] > 140
            if hit:
                if not n:
                    lo = x
                hi = x
                n += 1
        if n >= min_row:
            rows[y] = (n, lo, hi)

    blocks: list[list[float]] = []
    cur: list[float] | None = None
    for y in sorted(rows):
        n, lo, hi = rows[y]
        if cur is not None and y - cur[1] <= 3:
            cur[1] = y
            cur[2] = max(cur[2], n)
            cur[3] = min(cur[3], lo)
            cur[4] = max(cur[4], hi)
        else:
            if cur is not None:
                blocks.append(cur)
            cur = [y, y, n, lo, hi]
    if cur is not None:
        blocks.append(cur)

    out: list[IconBlock] = []
    for y_start, y_end, fill, lo, hi in blocks:
        w, h = (hi - lo + 1) * sx, (y_end - y_start + 1) * sy
        if not (min_side <= w <= max_side and min_side <= h <= max_side):
            continue
        if not (0.6 <= w / h <= 1.8):
            continue
        out.append(IconBlock(x=(lo + hi) / 2 * sx,
                             y=(y_start + y_end) / 2 * sy,
                             w=w, h=h, fill=int(fill)))
    out.sort(key=lambda b: (b.fill, min(b.w, b.h)), reverse=True)
    return out


@dataclass(frozen=True)
class MenuItem:
    """弹窗菜单里的一行可点文字（逻辑坐标）。"""
    y: float
    x: float
    peak: int

    def as_dict(self) -> dict:
        return {"x": round(self.x, 1), "y": round(self.y, 1), "peak": self.peak}


def find_menu_items(image_path: str | Path, *,
                    x0: float, x1: float, y0: float, y1: float,
                    logical_width: int, logical_height: int,
                    threshold: int = DARK_THRESHOLD,
                    min_dark: int = 6,
                    gap: float = 30,
                    min_h: float = 6, max_h: float = 22
                    ) -> list[list[MenuItem]]:
    """把弹出菜单按「分隔线」分组成一簇一簇的菜单项。

    为什么要分组：微信文章「…」菜单的内容会变（比如多出/少掉「添加浮窗」），
    写死像素坐标必然失效。但分组结构稳定——「复制链接」始终是**第二组
    第二项**（第一组是转发/分享/收藏，第三组是浏览记录/下载）。
    找不到就返回空列表，由调用方决定报错还是降级。
    """
    img = decode_png(image_path)
    img.sx = logical_width / img.w
    img.sy = logical_height / img.h
    px0, px1 = int(x0 / img.sx), int(x1 / img.sx)
    bands = find_title_bands(img, int(y0 / img.sy), int(y1 / img.sy),
                            threshold=threshold, min_dark=min_dark,
                            x0=px0, x1=px1)
    bands = sorted(bands, key=lambda b: b.center)
    bands = [b for b in bands if min_h <= (b.y1 - b.y0) <= max_h]

    items: list[MenuItem] = []
    for b in bands:
        rows = range(max(0, int(b.y0 / img.sy)),
                     min(img.h, int(b.y1 / img.sy) + 1))
        xs: list[int] = []
        for y in rows:
            row = img.rows[y]
            for x in range(px0, min(px1, img.w)):
                i = x * img.ch
                if row[i] < threshold and row[i + 1] < threshold \
                        and row[i + 2] < threshold:
                    xs.append(x)
        xs.sort()
        # 菜单项左对齐：取最左一段文字的中间，避免点到右侧的快捷键文字
        left = xs[0] if xs else px0
        right = left + 24
        cx = (left + min(right, xs[-1] if xs else left)) / 2 * img.sx
        items.append(MenuItem(y=b.center, x=cx, peak=b.peak))

    groups: list[list[MenuItem]] = []
    for it in items:
        if groups and it.y - groups[-1][-1].y <= gap:
            groups[-1].append(it)
        else:
            groups.append([it])
    return groups


def looks_like_deleted_page(image_path: str | Path, *,
                            logical_width: int, logical_height: int,
                            rect: tuple[float, float, float, float] | None = None,
                            min_white_ratio: float = 0.70,
                            min_yellow: int = 800,
                            max_yellow: int = 40000,
                            min_icon: float = 28,
                            max_icon: float = 180,
                            step: int = 2) -> dict:
    """这张截图是不是微信「该内容已被发布者删除」这类空错误页。

    为什么需要它（2026-11-08 用户反馈）
    ----------------------------------
    文章被发布者删除后，点开卡片仍会打开一个**正常的文章浮窗**，但页面只有
    一行提示，顶部「…」菜单里**没有「复制链接」**。旧代码识别不到菜单项时会
    退回配置里写死的 copy_link 坐标盲点，结果剪贴板为空 → 判成界面异常 →
    连续两张就整轮停下，看起来就是「卡死」。

    判据（纯像素，不做 OCR）
    ----------------------
    这类页面有个很稳的组合特征：**近白底 + 一块黄色圆形图标**。

      * 白底：实测 RGB ≈ (248,248,248)，正文页哪怕全是文字也有大量深色像素、
        有图片的页面更不可能 70% 以上近白；
      * 黄色块：实测 ≈ (245,200,20)，宽高各约 70 逻辑 px、接近正方形。

    两个条件都满足才判命中，所以「纯文字白底文章」和「带黄色配图的文章」
    都不会被误判。真正误判的代价也只是少抓一篇（会记进 skipped），
    而漏判的代价是整轮停住——所以门槛偏宽。

    rect 是文章窗口的逻辑坐标 (x, y, w, h)：只在窗口内统计，避开左侧主窗口
    的信息流和其它窗口的干扰。不给就退回屏幕中央偏左的默认区域。
    """
    img = decode_png(image_path)
    img.sx = logical_width / img.w
    img.sy = logical_height / img.h
    if rect is None:
        lx0, ly0 = 0.0, 0.10 * logical_height
        lx1, ly1 = 0.72 * logical_width, 0.95 * logical_height
    else:
        x, y, w, h = rect
        lx0, ly0, lx1, ly1 = x + 8, y + 8, x + w - 8, y + h - 8
    px0 = max(0, int(lx0 / img.sx))
    px1 = min(img.w, int(lx1 / img.sx))
    py0 = max(0, int(ly0 / img.sy))
    py1 = min(img.h, int(ly1 / img.sy))
    if px1 <= px0 or py1 <= py0:
        return {"deleted": False, "white_ratio": 0.0, "yellow": 0,
                "icon": None, "note": "扫描区域为空"}

    total = white = yellow = 0
    ymin = ymax = xmin = xmax = None
    for y in range(py0, py1, step):
        row = img.rows[y]
        for x in range(px0, px1, step):
            i = x * img.ch
            r, g, b = row[i], row[i + 1], row[i + 2]
            total += 1
            if r >= WHITE_MIN and g >= WHITE_MIN and b >= WHITE_MIN:
                white += 1
            elif (r >= YELLOW_R_MIN and YELLOW_G_MIN <= g <= YELLOW_G_MAX
                  and b <= YELLOW_B_MAX):
                yellow += 1
                xmin = x if xmin is None else min(xmin, x)
                xmax = x if xmax is None else max(xmax, x)
                ymin = y if ymin is None else min(ymin, y)
                ymax = y if ymax is None else max(ymax, y)
    if not total:
        return {"deleted": False, "white_ratio": 0.0, "yellow": 0,
                "icon": None, "note": "扫描区域为空"}

    ratio = white / total
    # 采样是隔 step 取的点，按 step² 折算回真实像素数，阈值才和分辨率无关
    yellow_px = yellow * step * step
    icon = None
    if ymin is not None:
        icon = ((xmax - xmin + step) * img.sx, (ymax - ymin + step) * img.sy)
    shape_ok = False
    if icon:
        iw, ih = icon
        long_side, short_side = max(iw, ih), max(1.0, min(iw, ih))
        shape_ok = (min_icon <= long_side <= max_icon
                    and 0.55 <= short_side / long_side <= 1.8)
    deleted = bool(ratio >= min_white_ratio
                   and min_yellow <= yellow_px <= max_yellow
                   and shape_ok)
    return {"deleted": deleted,
            "white_ratio": round(ratio, 3),
            "yellow": int(yellow_px),
            "icon": None if icon is None else [round(icon[0], 1),
                                               round(icon[1], 1)]}


def find_all_title_bands(image_path: str | Path, *,
                         logical_width: int, logical_height: int,
                         threshold: int = DARK_THRESHOLD,
                         min_dark: int = MIN_DARK_PER_ROW,
                         min_rows: int = MIN_BAND_ROWS,
                         min_logical_height: float = MIN_TITLE_LOGICAL_HEIGHT,
                         max_logical_height: float = MAX_TITLE_LOGICAL_HEIGHT
                         ) -> list[Band]:
    """全屏找所有「标题高度」的深色带，排除头像等大块深色区域。

    读图误差会让视觉模型给出的 y 系统性偏移，且偏移量逐张累积（卡片间距
    被压缩）。单靠小窗校准救不回来，所以需要拿到整屏所有标题带，供
    `snap` 按「离给定 y 最近且没点过」顺序匹配。
    """
    img = decode_png(image_path)
    img.sx = logical_width / img.w
    img.sy = logical_height / img.h
    raw = find_title_bands(img, 0, img.h, threshold=threshold,
                           min_dark=min_dark, min_rows=min_rows)
    return [b for b in raw
            if min_logical_height <= (b.y1 - b.y0) <= max_logical_height]


def nearest_unused_band(bands: list[Band], y: float,
                        used_centers: list[float] | tuple = (),
                        *, min_sep: float = 24,
                        max_distance: float = 240) -> Band | None:
    """挑离 y 最近、且没被点过的标题带；超出 max_distance 视为没有。"""
    cand = [b for b in bands
            if all(abs(b.center - c) > min_sep for c in used_centers)]
    if not cand:
        return None
    best = min(cand, key=lambda b: abs(b.center - y))
    if abs(best.center - y) > max_distance:
        return None
    return best


def snap(image_path: str | Path, y: float, *,
         logical_width: int, logical_height: int,
         search_down: float = 45, search_up: float = 20,
         used_centers: list[float] | tuple = (),
         threshold: int = DARK_THRESHOLD,
         min_dark: int = MIN_DARK_PER_ROW,
         min_sep: float = 24, max_distance: float = 240,
         min_logical_height: float = MIN_TITLE_LOGICAL_HEIGHT,
         max_logical_height: float = MAX_TITLE_LOGICAL_HEIGHT,
         min_peak: int = 0) -> Calibration:
    """点击前校准的增强版：小窗精校准 → 全屏最近未点过的标题带。

    与 `calibrate` 的区别：小窗找不到、或找到的那条标题「已经被前面的
    坐标点过」时，改为全屏识别所有标题带，选离 y 最近且没点过的一条。
    这样能吃掉视觉模型把卡片间距压缩造成的**累积**偏移（实测 5 张卡片
    误差从 15px 一路涨到 178px），同时靠 `used_centers` 保证不会重复点
    同一张卡片。任何异常一律降级为保持原坐标。
    """
    try:
        img = decode_png(image_path)
    except Exception as exc:
        return Calibration(y, y, "error",
                           f"校准不可用（{type(exc).__name__}: {exc}），保持原坐标")
    img.sx = logical_width / img.w
    img.sy = logical_height / img.h
    used = list(used_centers or ())

    def _is_used(center: float) -> bool:
        return any(abs(center - c) <= min_sep for c in used)

    # 1) 先在给定 y 的小窗里精校准（保留原有行为）
    y0 = max(0, int((y - search_up) / img.sy))
    y1 = min(img.h, int((y + search_down) / img.sy) + 1)
    # 小窗也要按高度过滤：否则「余下 N 篇 / 时间」这类 4~5px 弱带
    # 会比真正的标题更靠近给定 y，把落点带偏。
    small = [b for b in find_title_bands(img, y0, y1, threshold=threshold,
                                         min_dark=min_dark)
             if min_logical_height <= (b.y1 - b.y0) <= max_logical_height]
    small = _prefer_strong(small, min_peak)
    band, how = _choose_band(small, y, search_down, search_up)
    if band is not None and not _is_used(band.center):
        if how == "on_title":
            # 吸附到真实标题带中心（而不是原 y），让 used_centers 更准
            return Calibration(y, band.center, "on_title",
                               f"已在标题行（y≈{band.center:.0f}）")
        word = "下移" if how == "moved_down" else "上移"
        return Calibration(y, band.center, how,
                           f"{word}到深色标题行 y≈{band.center:.0f}"
                           f"（峰值 {band.peak}）")

    # 2) 小窗没找到、或那条标题已经点过：全屏找最近的未点标题带
    raw = find_title_bands(img, 0, img.h, threshold=threshold,
                           min_dark=min_dark)
    all_bands = _prefer_strong(
        [b for b in raw
         if min_logical_height <= (b.y1 - b.y0) <= max_logical_height], min_peak)
    best = nearest_unused_band(all_bands, y, used,
                               min_sep=min_sep, max_distance=max_distance)
    if best is None:
        if band is not None:      # 小窗找到了，但这条已经点过
            return Calibration(y, y, "no_title",
                               "附近标题行都已点过，按原坐标点击")
        return Calibration(y, y, "no_title", "附近没有深色标题带，保持原坐标")
    if abs(best.center - y) <= ON_TITLE_TOLERANCE:
        return Calibration(y, best.center, "on_title",
                           f"全屏匹配已在标题行（y≈{best.center:.0f}）")
    status = "moved_down" if best.center > y else "moved_up"
    word = "下移" if best.center > y else "上移"
    return Calibration(y, best.center, status,
                       f"全屏匹配{word}到标题行 y≈{best.center:.0f}"
                       f"（峰值 {best.peak}，跳过已点过的）")
