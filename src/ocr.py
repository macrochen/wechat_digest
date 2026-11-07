#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地文字识别（macOS Vision 框架）——纯本地、不联网、不申请任何授权。

为什么要它
----------
这个项目原先全靠像素判据（标题带的高度/峰值、36x36 蓝方块），一条 OCR 都
没用过，好处是零依赖、确定性高。但「按号抓取」有个绕不开的需求：**在左栏
一堆公众号里按名字找到指定的那个**。名字是文字，几何判据完全无能为力——
除非每次让用户自己点，那就失去全自动的意义。

所以这里只破一次例，而且把影响面压到最小：

* 只用系统自带的 Vision（`VNRecognizeTextRequest`，zh-Hans），不引第三方库、
  不联网、不出本机；
* 复用 `winpos` 已经验证过的做法：把 Swift 源码写成常量、用 `swiftc` 编成
  小工具、**二进制名带源码哈希**（改了源码自动重编，不会「以为生效其实没
  有」）、编译不了就明确报错让上层降级；
* 识别结果只用于「认字」，**不参与点击坐标的计算**：点哪里仍然由像素判据
  （标题带）决定，OCR 只负责回答「这个号在哪一行」「右栏顶部是不是我要的号」。

实测（2026-11-08，本机微信列表模式截图）
---------------------------------------
公众号名、文章标题识别置信度普遍 1.0，「EarlETF」「猫笔刀」「复利先生」
全中；时间分隔行「昨天 08:31」「星期三 07:31」也稳定可读——分组靠它。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import project_path

# 输出协议：第一行 "SIZE\t<宽>\t<高>"，之后每行
# "x\ty\t宽\t高\t置信度\t文字"，坐标是**图像像素**、原点左上。
_SWIFT = r"""
import Foundation
import Vision
import ImageIO
import CoreGraphics

let args = CommandLine.arguments
guard args.count >= 2 else {
    FileHandle.standardError.write("usage: wdocr <png> [minConf]\n".data(using: .utf8)!)
    exit(2)
}
let minConf = args.count >= 3 ? (Float(args[2]) ?? 0) : 0
guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: args[1]) as CFURL, nil),
      let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else { exit(3) }
let req = VNRecognizeTextRequest()
req.recognitionLevel = .accurate
req.recognitionLanguages = ["zh-Hans", "en-US"]
req.usesLanguageCorrection = false
let handler = VNImageRequestHandler(cgImage: img, options: [:])
do { try handler.perform([req]) } catch { exit(4) }
let w = Double(img.width), h = Double(img.height)
print("SIZE\t\(img.width)\t\(img.height)")
for obs in (req.results ?? []) {
    guard let c = obs.topCandidates(1).first else { continue }
    if c.confidence < minConf { continue }
    let bb = obs.boundingBox
    let x = bb.origin.x * w
    let y = (1 - bb.origin.y - bb.size.height) * h
    print(String(format: "%.1f\t%.1f\t%.1f\t%.1f\t%.3f\t%@",
                 x, y, bb.size.width * w, bb.size.height * h, c.confidence, c.string))
}
"""

_VER = hashlib.sha256(_SWIFT.encode("utf-8")).hexdigest()[:12]
_BIN = project_path("out", ".bin", f"wdocr-{_VER}")
_SRC = project_path("out", ".bin", f"wdocr-{_VER}.swift")


class OcrError(RuntimeError):
    """认不出字或工具不可用。上层要么降级、要么按「需要人介入」停下。"""


@dataclass(frozen=True)
class Text:
    """一段识别出来的文字（逻辑坐标，原点左上，可用于点击）。"""
    text: str
    x: float
    y: float
    w: float
    h: float
    conf: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    def as_dict(self) -> dict:
        return {"text": self.text, "x": round(self.x, 1), "y": round(self.y, 1),
                "w": round(self.w, 1), "h": round(self.h, 1),
                "conf": round(self.conf, 3)}


def _compile() -> Path | None:
    """编译并缓存识别工具；编译不了返回 None（调用方负责报错/降级）。"""
    if _BIN.exists() and os.access(_BIN, os.X_OK):
        return _BIN
    if not shutil.which("swiftc"):
        return None
    try:
        _SRC.parent.mkdir(parents=True, exist_ok=True)
        _SRC.write_text(_SWIFT, encoding="utf-8")
        p = subprocess.run(["swiftc", "-O", "-o", str(_BIN), str(_SRC)],
                           capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0 or not _BIN.exists():
        return None
    return _BIN


def available() -> bool:
    """OCR 能不能用（供 doctor 与设置页展示；不抛异常）。"""
    try:
        return _compile() is not None
    except Exception:                                  # noqa: BLE001
        return False


def _png_size(path: str | Path) -> tuple[int, int] | None:
    """只读 IHDR 拿像素尺寸（不解码像素）。"""
    try:
        blob = Path(path).read_bytes()[:33]
    except OSError:
        return None
    if len(blob) < 24 or blob[:8] != b"\x89PNG\r\n\x1a\n" or blob[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", blob[16:24])


def recognize(image_path: str | Path, *,
              logical_width: int, logical_height: int,
              min_confidence: float = 0.0) -> list[Text]:
    """识别一张截图里的所有文字，坐标换算成 1280x832 逻辑坐标。

    任何失败（工具编不出来 / 图片读不了 / Vision 报错）都抛 `OcrError`，
    **绝不返回空列表冒充「没有文字」**——那会让调用方把「识别失败」当成
    「这个号不存在」，进而盲点。空列表只在真的一个字都没认出来时出现。
    """
    binary = _compile()
    if binary is None:
        raise OcrError(
            "本地文字识别工具编译失败（需要 swiftc）。\n"
            "  装 Xcode Command Line Tools 后重跑：xcode-select --install\n"
            "  或者先不用「按号抓取」，走信息流那套。")
    try:
        p = subprocess.run([str(binary), str(image_path), str(min_confidence)],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise OcrError(f"识别工具调用失败：{exc}") from exc
    if p.returncode != 0:
        raise OcrError(f"识别工具返回 {p.returncode}：{(p.stderr or '').strip()[:160]}")

    img_w = img_h = None
    rows: list[tuple[float, float, float, float, float, str]] = []
    for line in (p.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0] == "SIZE":
            img_w, img_h = int(parts[1]), int(parts[2])
            continue
        if len(parts) < 6:
            continue
        try:
            x, y, w, h, conf = (float(parts[0]), float(parts[1]), float(parts[2]),
                                float(parts[3]), float(parts[4]))
        except ValueError:
            continue
        text = "\t".join(parts[5:]).strip()
        if text:
            rows.append((x, y, w, h, conf, text))

    if not img_w or not img_h:
        size = _png_size(image_path)
        if not size:
            raise OcrError("读不出截图尺寸")
        img_w, img_h = size
    sx = logical_width / img_w
    sy = logical_height / img_h
    return [Text(text=t, x=x * sx, y=y * sy, w=w * sx, h=h * sy, conf=c)
            for x, y, w, h, c, t in rows]


def first_line(texts: list[Text], *, x0: float, x1: float,
               y0: float, y1: float) -> Text | None:
    """在矩形区域里取最靠上的一行文字（找不到返回 None）。"""
    inside = [t for t in texts
              if t.cx >= x0 and t.cx <= x1 and t.cy >= y0 and t.cy <= y1]
    return min(inside, key=lambda t: t.y) if inside else None
