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
import subprocess
import time
from pathlib import Path

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


def screen_locked():
    """返回 True/False/None（None = 无法判定，不误报）。"""
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
