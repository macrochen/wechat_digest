#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从本机微信「公众号」信息流取原文链接。

分工：Hy4 读截图给出文章卡片坐标 → 本模块按坐标批量
「点开卡片 → … 菜单 → 复制链接 → 关窗口」。

**人介入兜底**（PRD 决策 7）：只要复制出来的不是 mp.weixin.qq.com 链接、
或前后两篇拿到同一个链接（说明窗口没关掉 / 菜单位置变了），
立刻报错退出，不改任何 state，等人核对 config/settings.json 的 ui 坐标。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

from . import gui
from .config import project_path
from .state import load_json

_URL_RE = re.compile(r"https?://mp\.weixin\.qq\.com/s[^\s\"'<>]*")


class GrabAborted(RuntimeError):
    """需要人介入的界面异常。退出码 3。"""


def preflight() -> None:
    if sys.platform != "darwin":
        raise GrabAborted("只能在 macOS 上抓微信客户端")
    if not gui.wechat_running():
        raise GrabAborted("微信没运行。请先启动并登录微信，打开「公众号」信息流。")
    locked = gui.screen_locked()
    if locked is True:
        raise GrabAborted("屏幕已锁定，截屏和点击都不可靠。请解锁后手动重跑。")


def capture(cfg: dict, out: str | None = None) -> dict:
    """激活微信并截一张逻辑坐标图，交给 Hy4 读卡片位置。"""
    preflight()
    gui.activate_wechat()
    ui = cfg["ui"]
    path = out or str(project_path("out", "capture.png"))
    gui.screenshot(path, ui["logical_width"], ui["logical_height"])
    return {"image": str(Path(path).resolve()),
            "logical_size": [ui["logical_width"], ui["logical_height"]],
            "note": "请读图给出本屏可见文章卡片的标题中心坐标（x,y；多个用 ; 分隔）"}


def parse_coords(raw: str) -> list[tuple[float, float]]:
    coords = []
    for chunk in (raw or "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*", chunk)
        if not m:
            raise GrabAborted(f"坐标格式不对：{chunk!r}，应形如 560,275;560,400")
        coords.append((float(m.group(1)), float(m.group(2))))
    if not coords:
        raise GrabAborted("没给任何卡片坐标")
    return coords


def _abort(coords, index, reason, obtained):
    """失败时把已拿到的链接留个临时文件，方便人接着干；不写任何台账。"""
    partial = project_path("out", "_raw_urls_partial.json")
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_text(json.dumps({"obtained": obtained,
                                   "aborted_at_index": index,
                                   "reason": reason},
                                  ensure_ascii=False, indent=2),
                       encoding="utf-8")
    raise GrabAborted(
        f"第 {index} 篇界面操作失败：{reason}\n"
        f"  已取到的链接留在 {partial}（未写入任何台账）\n"
        f"  请截图核对 config/settings.json 的 ui.more_button / "
        f"ui.copy_link / ui.close_window 坐标，改好后重跑。\n"
        f"  本次坐标：{coords}"
    )


def grab(cfg: dict, coords: list[tuple[float, float]], limit: int,
         dry_run: bool = False, append: bool = False) -> dict:
    preflight()
    ui = cfg["ui"]
    more = tuple(ui["more_button"])
    copy = tuple(ui["copy_link"])
    close = tuple(ui["close_window"])

    if dry_run:
        return {"dry_run": True, "coords": coords,
                "ui": {"more": more, "copy": copy, "close": close},
                "would_click": len(coords) * 4}

    gui.activate_wechat()
    obtained: list[str] = []
    for i, (x, y) in enumerate(coords, 1):
        if len(obtained) >= limit:
            break
        gui.clipboard_clear()
        gui.click(x, y)                       # 点开文章
        _sleep(ui["open_wait"])
        gui.click(*more)                      # 右上角「…」
        _sleep(ui["menu_wait"])
        gui.click(*copy)                      # 「复制链接」
        _sleep(ui["click_wait"] + 0.2)
        url = gui.clipboard_read()

        m = _URL_RE.search(url)
        if not m:
            _abort(coords, i,
                   f"剪贴板里不是公众号链接（得到 {url[:80]!r}）。"
                   "多半是「…」菜单没弹出或「复制链接」位置变了",
                   obtained)
        link = m.group(0)
        if link in obtained:
            _abort(coords, i,
                   f"又拿到同一篇 {link}，说明上一篇窗口没关掉或列表没滚动",
                   obtained)

        gui.click(*close)                     # 关文章窗口，回到信息流
        _sleep(ui["close_wait"])
        obtained.append(link)
        gui.clipboard_clear()

    out = project_path("out", "_raw_urls.json")
    prev = load_json(out, {}) if append else {}
    prev_urls = (prev.get("urls") or []) if isinstance(prev, dict) else []
    # 一屏放不下 20 篇时会分多轮抓，--append 把各屏链接合并去重
    merged = list(dict.fromkeys(list(prev_urls) + obtained))
    payload = {"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "count": len(merged), "new_this_round": len(obtained),
               "urls": merged}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    return {"count": len(merged), "new_this_round": len(obtained),
            "file": str(out), "urls": merged}


def _sleep(seconds) -> None:
    import time
    time.sleep(float(seconds))
