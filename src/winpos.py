#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""窗口几何与层级查询（macOS CoreGraphics，无辅助功能权限）。

为什么需要它
------------
GUI 抓取全部用「绝对屏幕坐标」。但微信主窗口会被别的窗口挡住（比如
WorkBuddy 的窗口），这时点击会落到遮挡者身上——本轮就是这样误开了
「我」聊天窗、又开出空白文章标签。进程存活（pgrep）完全看不出来。

这里编译一个十几行的 Swift 小工具（首次约 5 秒，之后复用缓存二进制），
读 `CGWindowListCopyWindowInfo`：窗口边界、层级、前后顺序。
有了它就能回答三个问题：

1. 微信主窗口在不在屏幕上、多大、在哪；
2. 它前面还压着哪些窗口（= 点击会被谁吃掉）；
3. 窗口可见区域里哪个点可以安全点击（点到标题栏只抬升窗口，不触发内容）。

拿不到 swiftc 或调用失败时一律降级为「未知」（返回 None），绝不因此让
抓取流程崩溃。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from .config import project_path

# 微信窗口的普通层号上限。主窗口是 layer 0，公众号文章窗口是 layer 3
# （Chromium 浮动窗）；再高的层是输入法候选框、系统 HUD 之类，不能算。
MAX_WECHAT_LAYER = 3

_SWIFT = r"""
import Foundation
import CoreGraphics

var opts: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
if CommandLine.arguments.count > 1 && CommandLine.arguments[1] == "--all" {
    opts = [.optionAll]
}

// --display：主显示器的**逻辑**尺寸（CGDisplayBounds，单位 point，与
// CGEvent 坐标同一空间）、物理像素尺寸，以及当前活动显示器数量。
// 抓取依赖「截图像素空间 == CGEvent 坐标空间 == 1280x832」，这里就是
// 校验这件事的唯一可信来源。
if CommandLine.arguments.count > 1 && CommandLine.arguments[1] == "--display" {
    let id = CGMainDisplayID()
    let b = CGDisplayBounds(id)
    let pw = CGDisplayPixelsWide(id)
    let ph = CGDisplayPixelsHigh(id)
    var count: UInt32 = 0
    CGGetActiveDisplayList(0, nil, &count)
    let scale = b.width > 0 ? Double(pw) / b.width : 0.0
    print("\(b.origin.x)\t\(b.origin.y)\t\(b.width)\t\(b.height)\t"
          + "\(pw)\t\(ph)\t\(scale)\t\(count)")
    exit(0)
}

guard let list = CGWindowListCopyWindowInfo(opts, kCGNullWindowID) as? [[String: Any]] else {
    exit(2)
}
for (i, w) in list.enumerated() {
    let owner = w[kCGWindowOwnerName as String] as? String ?? ""
    let layer = (w[kCGWindowLayer as String] as? NSNumber)?.intValue ?? 0
    let num = (w[kCGWindowNumber as String] as? NSNumber)?.intValue ?? 0
    let onscreen = (w[kCGWindowIsOnscreen as String] as? NSNumber)?.boolValue ?? false
    let b = w[kCGWindowBounds as String] as? [String: Any] ?? [:]
    func val(_ k: String) -> Double {
        return ((b[k] as? NSNumber)?.doubleValue) ?? 0
    }
    print("\(i)\t\(num)\t\(layer)\t\(onscreen ? 1 : 0)\t"
          + "\(val("X"))\t\(val("Y"))\t\(val("Width"))\t\(val("Height"))\t\(owner)")
}
"""

_VER = hashlib.sha256(_SWIFT.encode("utf-8")).hexdigest()[:12]
# 二进制名带上源码哈希：改了上面的 swift 就自动编译新版本，不会复用旧缓存
# 而「看起来生效了其实没有」——这是加 --display 时踩过的坑。
_BIN = project_path("out", ".bin", f"wdwin-{_VER}")
_SRC = project_path("out", ".bin", f"wdwin-{_VER}.swift")
_cached_ok: bool | None = None


def _compile() -> Path | None:
    """编译并缓存查询工具；编译不了就返回 None（功能降级）。"""
    global _cached_ok
    if _BIN.exists() and os.access(_BIN, os.X_OK):
        _cached_ok = True
        return _BIN
    _cached_ok = False
    if not shutil.which("swiftc"):
        return None
    try:
        _SRC.parent.mkdir(parents=True, exist_ok=True)
        _SRC.write_text(_SWIFT, encoding="utf-8")
        p = subprocess.run(["swiftc", "-O", "-o", str(_BIN), str(_SRC)],
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0 or not _BIN.exists():
        return None
    _cached_ok = True
    return _BIN


def windows(include_offscreen: bool = False) -> list[dict] | None:
    """按**前到后**顺序返回窗口；取不到信息时返回 None（未知，不误报）。"""
    binary = _compile()
    if binary is None:
        return None
    args = [str(binary)] + (["--all"] if include_offscreen else [])
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    out: list[dict] = []
    for line in (p.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 9:
            continue
        try:
            out.append({
                "index": int(parts[0]), "id": int(parts[1]),
                "layer": int(parts[2]), "onscreen": parts[3] == "1",
                "x": float(parts[4]), "y": float(parts[5]),
                "w": float(parts[6]), "h": float(parts[7]),
                "owner": "\t".join(parts[8:]),
            })
        except ValueError:
            continue
    return out


def main_display() -> dict | None:
    """主显示器几何：逻辑点尺寸、物理像素尺寸、backing scale、屏数。

    取不到（没有 swiftc / 调用失败）返回 None，调用方按「无法判定」处理，
    **不误报**——和本模块其它查询一个口径。
    """
    binary = _compile()
    if binary is None:
        return None
    try:
        p = subprocess.run([str(binary), "--display"], capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    parts = (p.stdout or "").strip().split("\t")
    if len(parts) < 8:
        return None
    try:
        x, y, w, h, pw, ph, scale, count = parts[:8]
        return {
            "x": float(x), "y": float(y), "w": float(w), "h": float(h),
            "px": int(float(pw)), "py": int(float(ph)),
            "scale": float(scale), "displays": int(float(count)),
        }
    except ValueError:
        return None


def find_wechat(ws: list[dict] | None) -> dict | None:
    """微信主窗口 = 所有者含「微信/WeChat」、layer 0、面积最大的那个。"""
    if not ws:
        return None
    cand = [w for w in ws
            if w["layer"] == 0 and w["onscreen"]
            and ("微信" in w["owner"] or "WeChat" in w["owner"])
            and w["w"] >= 600 and w["h"] >= 400]
    if not cand:
        return None
    return max(cand, key=lambda w: w["w"] * w["h"])


def front_wechat(ws: list[dict] | None, *, min_w: float = 500,
                 min_h: float = 400) -> dict | None:
    """最前面的那个「大」微信窗口（文章窗口打开时就是它）。

    **不能只看 layer == 0**：本机微信（2026-11）的公众号文章窗口是
    **layer 3 的浮动窗口**（Chromium 内核，896x508 或 1206x569），主窗口
    才是 layer 0。早先这里过滤 layer==0，于是文章窗口开着时也判成「前面
    没有文章窗口」——关窗逻辑形同虚设，界面上一直卡着旧文章，下一次点击
    落在文章正文上，剪贴板当然是空的。

    层号大的画在上面，所以先按 layer 降序、再按窗口列表顺序（前到后）取。

    min_w 从 800 降到 500（2026-11-07）：点中卡片非标题区域会开出**公众号
    主页浮窗**（宽约 600 逻辑 px），800 的阈值把它漏掉，article_window_open
    就误判「没有浮窗」，close_article 拒绝关窗、grab 也发现不了「点了但没开
    文章」。500 仍能排除 372 宽的窄条窗口（主窗口被压窄时的形态）。
    """
    if not ws:
        return None
    cand = [w for w in ws
            if 0 <= w["layer"] <= MAX_WECHAT_LAYER
            and w["onscreen"]
            and ("微信" in w["owner"] or "WeChat" in w["owner"])
            and w["w"] >= min_w and w["h"] >= min_h]
    if not cand:
        return None
    return min(cand, key=lambda w: (-w["layer"], w["index"]))


def _area(w: dict) -> float:
    return max(0.0, w["w"]) * max(0.0, w["h"])


def _intersect(a: dict, b: dict) -> float:
    dx = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
    dy = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
    if dx <= 0 or dy <= 0:
        return 0.0
    return dx * dy


def blockers(main: dict, ws: list[dict] | None) -> list[dict]:
    """压在微信主窗口前面、且与之有重叠的**其它程序**窗口（会吃掉点击）。

    微信自己的窗口（文章标签页、公众号小窗等）不算遮挡：点击它们仍然是
    点给微信，那正是我们要的。
    """
    if not ws:
        return []
    return [w for w in ws
            if w["index"] < main["index"] and w["layer"] == 0 and w["onscreen"]
            and w["id"] != main["id"] and _intersect(main, w) > 0
            and "微信" not in w["owner"] and "WeChat" not in w["owner"]]


def is_front(main: dict, ws: list[dict] | None) -> bool:
    """微信主窗口是否基本没被挡住（遮挡面积 < 5%）。"""
    if ws is None:                    # 未知：当作没问题，交给后续校验
        return True
    covered = sum(_intersect(main, w) for w in blockers(main, ws))
    return covered < 0.05 * max(1.0, _area(main))


def free_point(main: dict, ws: list[dict] | None,
               *, step: int = 24) -> tuple[float, float] | None:
    """在微信窗口里找一个**没有被上层窗口遮住**的点。

    优先取标题栏那一条（y 在窗口顶部 8~22px）：那里点下去只是抬升窗口，
    不会误触会话 / 文章卡片。

    x 候选**限制在窗口中央带**（0.4~0.6 倍宽，2026-11-07 收紧）：微信是
    无边框自定义窗口，「标题栏」其实是页面顶栏，左右两端全是功能控件
    （搜索框、列表切换、通知、右上加号…）。早先的 0.1/0.8/0.9 倍候选点
    会撞上这些控件——一旦撞上就是一次真实的界面操作（切视图 / 弹菜单），
    抓取流程直接错乱。中央带实测在聊天窗口和公众号聚合页都是空白。
    """
    block = blockers(main, ws) if ws else []
    if not block:
        return (main["x"] + main["w"] / 2, main["y"] + 12)

    def covered(x: float, y: float) -> bool:
        return any(w["x"] <= x <= w["x"] + w["w"]
                   and w["y"] <= y <= w["y"] + w["h"] for w in block)

    strip_top = main["y"] + 12
    for frac in (0.5, 0.45, 0.55, 0.4, 0.6):
        x = main["x"] + main["w"] * frac
        if not covered(x, strip_top):
            return (x, strip_top)
    y = main["y"] + 30
    x_lo = main["x"] + main["w"] * 0.25   # 内容区扫描同样限制在中央带，
    x_hi = main["x"] + main["w"] * 0.75   # 避开左侧列表和右侧功能按钮列
    while y < main["y"] + main["h"] - 10:
        x = x_lo
        while x < x_hi:
            if not covered(x, y):
                return (x, y)
            x += step
        y += step
    return None
