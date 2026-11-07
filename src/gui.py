#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""macOS GUI 原子操作：截屏 + 合成鼠标/键盘事件 + 剪贴板。

为什么走 CGEventPost：本机没给「辅助功能」授权，System Events 报 -10004，
但上一轮实测 ApplicationServices 的 CGEventPost 能绕过它真正移动鼠标、点击。
本模块不做任何视觉识别——坐标由 Hy4 读截图后传入，识别失败就报错让人介入。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import re
import struct
import subprocess
import time
from pathlib import Path

from . import winpos

_LIB = ctypes.util.find_library("ApplicationServices")
if not _LIB:
    raise RuntimeError("找不到 ApplicationServices，本模块只能在 macOS 运行")
_AS = ctypes.CDLL(_LIB)


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


_AS.CGEventCreateMouseEvent.restype = ctypes.c_void_p
_AS.CGEventCreateMouseEvent.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, _CGPoint, ctypes.c_uint32,
]
_AS.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
_AS.CGEventCreateKeyboardEvent.argtypes = [
    ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool,
]
_AS.CGEventSetFlags.restype = None
_AS.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
_AS.CGEventCreateScrollWheelEvent.restype = ctypes.c_void_p
_AS.CGEventCreateScrollWheelEvent.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32,
]
_AS.CGEventPost.restype = None
_AS.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
_AS.CFRelease.restype = None
_AS.CFRelease.argtypes = [ctypes.c_void_p]

# kCGEvent 常量
_MOUSE_MOVED, _LEFT_DOWN, _LEFT_UP, _RIGHT_DOWN, _RIGHT_UP = 5, 1, 2, 3, 4
_KEY_DOWN, _KEY_UP = 10, 11
_CMD_FLAG = 0x100000  # kCGEventFlagMaskCommand
_KVK_COMMAND, _KVK_C, _KVK_W, _KVK_V, _KVK_ESC = 55, 8, 13, 9, 53


def _post(event) -> None:
    try:
        _AS.CGEventPost(0, event)  # 0 = kCGHIDEventTap
    finally:
        _AS.CFRelease(event)


def move(x: float, y: float) -> None:
    p = _CGPoint(float(x), float(y))
    _post(_AS.CGEventCreateMouseEvent(None, _MOUSE_MOVED, p, 0))


def click(x: float, y: float, right: bool = False, delay: float = 0.06) -> None:
    """左键/右键单击。坐标是 1280x832 逻辑坐标。"""
    p = _CGPoint(float(x), float(y))
    button = 1 if right else 0  # kCGMouseButtonRight / Left
    down, up = (_RIGHT_DOWN, _RIGHT_UP) if right else (_LEFT_DOWN, _LEFT_UP)
    move(x, y)
    time.sleep(0.05)
    _post(_AS.CGEventCreateMouseEvent(None, down, p, button))
    time.sleep(delay)
    _post(_AS.CGEventCreateMouseEvent(None, up, p, button))


def key(code: int, flags: int = 0) -> None:
    for down in (True, False):
        ev = _AS.CGEventCreateKeyboardEvent(None, code, down)
        if flags:
            _AS.CGEventSetFlags(ev, flags)
        _post(ev)
        time.sleep(0.05)


def cmd_key(code: int) -> None:
    """Command+<key>。显式按下/抬起修饰键，比只设 flags 稳（上一轮实测）。"""
    down = _AS.CGEventCreateKeyboardEvent(None, _KVK_COMMAND, True)
    _post(down)
    time.sleep(0.08)
    key(code, _CMD_FLAG)
    up = _AS.CGEventCreateKeyboardEvent(None, _KVK_COMMAND, False)
    _post(up)


def press_escape() -> None:
    key(_KVK_ESC)


def scroll(x: float, y: float, amount: int = -4) -> None:
    """在 (x,y) 处滚滚轮。amount 为负向下，绝对值 = 滚动行数。

    用 CGEventCreateScrollWheelEvent（kCGScrollEventUnitLine=1, wheelCount=1）。
    滚完由 Hy4 重新截图定位，脚本不猜卡片位置。
    """
    move(x, y)
    time.sleep(0.1)
    step = -1 if amount < 0 else 1
    for _ in range(max(1, min(abs(int(amount)), 40))):
        ev = _AS.CGEventCreateScrollWheelEvent(None, 1, 1, step)
        _post(ev)
        time.sleep(0.04)


# ---------------------------------------------------------------- 系统状态

def wechat_running() -> bool:
    try:
        p = subprocess.run(["pgrep", "-fl", "WeChat"], capture_output=True,
                           text=True, timeout=5)
        return p.returncode == 0 and bool(p.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def activate_wechat(wait: float = 1.2) -> None:
    subprocess.run(["open", "-a", "WeChat"], check=False, capture_output=True)
    time.sleep(wait)


def frontmost_app() -> str | None:
    """当前前台 App 的显示名（读 lsappinfo，不需要辅助功能权限）。

    用来判断「菜单栏现在归谁」：只有微信是前台 App 时，点菜单栏才是
    安全的（否则点到别人的菜单上，可能触发奇怪的动作）。判不出来返回 None。
    """
    try:
        p = subprocess.run(["lsappinfo", "front"], capture_output=True,
                           text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    asn = (p.stdout or "").strip()
    if not asn:
        return None
    try:
        q = subprocess.run(["lsappinfo", "info", "-only", "name", asn],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r'"LSDisplayName"="([^"]+)"', q.stdout or "")
    return m.group(1) if m else None


_WECHAT_NAMES = {"微信", "wechat"}


def is_wechat_app(name: str | None) -> bool:
    """这个名字是不是微信。

    别用 `== "微信"` 精确比较：微信的 CFBundleDisplayName 是 `WeChat`，
    而窗口所有者名（CGWindowOwnerName）是「微信」，`lsappinfo` 在不同
    系统语言下可能给其中任意一个。只认中文名会「永远认为微信不在前台」，
    于是反复点标题栏、最后误报失败。

    但也**不能用包含匹配**：那样「企业微信 / WeChatWork」会被当成微信，
    点击就跑到另一个 App 上去了。所以是规范化后的**精确**比较。
    """
    if not name:
        return False
    return name.strip().casefold() in _WECHAT_NAMES


def screen_locked():
    """返回 True/False/None（None = 无法判定，不误报）。

    两条判据：ioreg 的 CGSSessionScreenIsLocked，以及前台 App 是否是
    loginwindow（锁屏 / 登录窗口时它一定在最前，这是最靠谱的信号）。
    """
    if frontmost_app() == "loginwindow":
        return True
    try:
        p = subprocess.run(["ioreg", "-n", "Root", "-d1", "-w0"],
                           capture_output=True, text=True, timeout=5)
        m = re.search(r"CGSSessionScreenIsLocked\s*=\s*(\w+)", p.stdout or "")
        if not m:
            return None
        return m.group(1).lower() in ("yes", "true", "1")
    except (OSError, subprocess.SubprocessError):
        return None


def screenshot(out_path: str | Path, width: int = 1280,
               height: int = 832) -> Path:
    """全屏截图并缩到逻辑坐标，使截图像素与 CGEvent 坐标 1:1。"""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    raw = Path("/tmp/wd_screen_raw.png")
    p = subprocess.run(["screencapture", "-x", str(raw)],
                       capture_output=True, text=True)
    if p.returncode != 0 or not raw.exists():
        raise RuntimeError(
            "截屏失败：没有「屏幕录制」权限，或屏幕已锁定。"
            + (p.stderr or "").strip()[:160]
        )
    s = subprocess.run(
        ["sips", "-z", str(height), str(width), str(raw), "--out", str(out)],
        capture_output=True, text=True,
    )
    if s.returncode != 0 or not out.exists():
        raise RuntimeError("sips 缩放截图失败：" + (s.stderr or "").strip()[:160])
    return out


def png_size(path: str | Path) -> tuple[int, int] | None:
    """只读 PNG 的 IHDR 拿像素尺寸，不解码像素（读前 33 字节即可）。"""
    try:
        blob = Path(path).read_bytes()[:33]
    except OSError:
        return None
    if len(blob) < 24 or blob[:8] != b"\x89PNG\r\n\x1a\n" or blob[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", blob[16:24])


def screen_geometry(raw_out: str | Path = "/tmp/wd_geo_raw.png") -> dict:
    """采集一次「真实屏幕几何」：主显示器逻辑尺寸 + 全屏截图的原始像素尺寸。

    为什么要真的截一张图：`screencapture -x` 会把**所有**显示器拼成一张
    （外接屏 / iPad 随航都算）。只看 CGDisplayBounds 发现不了这种拼合，
    而拼合后 `sips -z 832 1280` 会把整块拼图压扁，一切像素判据失准——
    而且不报错，只是静默点错卡片。

    截屏失败（没权限 / 屏幕锁定）时 raw 返回 None、error 带原因，交给
    调用方决定；本函数不抛异常。
    """
    display = winpos.main_display()
    raw_size: tuple[int, int] | None = None
    error: str | None = None
    out = Path(raw_out)
    try:
        p = subprocess.run(["screencapture", "-x", str(out)],
                           capture_output=True, text=True)
        if p.returncode == 0 and out.exists():
            raw_size = png_size(out)
        else:
            error = (p.stderr or "").strip()[:160] or "screencapture 返回非 0"
    except (OSError, subprocess.SubprocessError) as exc:
        error = str(exc)[:160]
    return {"display": display, "raw": raw_size, "error": error}


def judge_geometry(*, expected_w: int, expected_h: int,
                   display: dict | None = None,
                   raw_size: tuple[int, int] | None = None,
                   tolerance: float = 0.01) -> tuple[bool, list[dict]]:
    """判断当前屏幕是否还满足「截图空间 == CGEvent 空间 == 期望尺寸」。

    纯函数，不碰系统，便于单测。取不到的信息一律按「无法判定」跳过，
    绝不因为查不到就报错——诊断信息缺失不该拦住正常抓取。

    两条判据（任一条命中即不通过）：

    1. **逻辑尺寸**必须等于期望值。CGEvent 点击用的是逻辑点坐标，屏幕换成
       别的分辨率后，同一组数字会落到别处——这是最致命的一条。
    2. **截图宽高比**必须与期望一致（容差 tolerance）。`sips -z` 是强制
       拉伸而非等比缩放，比例一偏，1280x832 归一化空间里的所有像素阈值
       （标题带 11~12px、36x36 图标、x=520..880 的暗像素数）全部失准。
       这一条同时覆盖 `screencapture` 把扩展屏拼进同一张图的情况——左右
       或上下拼接必然改变宽高比。

    刻意**不**用 `CGDisplayPixelsWide` 校验 backing scale：本机实测
    CGMainDisplayID 报 1280x832 / scale 1.0，而 `screencapture` 实际拍出
    2560x1664（缩放分辨率模式下它返回的是逻辑值，不是 backing store）。
    拿它当基准只会稳定误报。比例一致就足够——2x 等比缩放正是项目一直在
    用的正常路径。
    """
    issues: list[dict] = []

    if display:
        dw, dh = float(display.get("w") or 0), float(display.get("h") or 0)
        if dw > 0 and dh > 0:
            if abs(dw - expected_w) > 1 or abs(dh - expected_h) > 1:
                issues.append({
                    "code": "logical_mismatch",
                    "message": (
                        f"主显示器逻辑尺寸是 {dw:g}x{dh:g}，不是 "
                        f"{expected_w}x{expected_h}。点击坐标按 "
                        f"{expected_w}x{expected_h} 发送，会整体偏移。"),
                })

    if raw_size:
        rw, rh = raw_size
        if expected_w > 0 and expected_h > 0 and rh > 0:
            want = expected_w / expected_h
            got = rw / rh
            if abs(got - want) / want > tolerance:
                extra = ""
                n = int((display or {}).get("displays") or 0)
                if n > 1:
                    extra = (f"当前有 {n} 块活动显示器，`screencapture` 会把"
                             f"扩展屏一起拼成一张图，这是最常见的原因；"
                             f"接第二块屏或 iPad 随航后请重新校准坐标。")
                issues.append({
                    "code": "aspect_mismatch",
                    "message": (
                        f"全屏截图是 {rw}x{rh}（宽高比 {got:.4f}），与 "
                        f"{expected_w}x{expected_h}（{want:.4f}）不一致，"
                        f"缩放后画面会被拉扁，像素判据不可信。{extra}"),
                })

    return (not issues), issues


def clipboard_read() -> str:
    try:
        p = subprocess.run(["pbpaste"], capture_output=True, timeout=5)
        return p.stdout.decode("utf-8", "ignore").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def clipboard_clear() -> None:
    try:
        subprocess.run(["pbcopy"], input=b"", capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass
