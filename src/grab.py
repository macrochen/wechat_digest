#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从本机微信「公众号」信息流取原文链接。

分工：Hy4 读截图给出文章卡片坐标 → 本模块按坐标批量
「点开卡片 → … 菜单 → 复制链接 → 关窗口」。

**点错公众号名的修复**：卡片第一行是浅灰小字的公众号名，第二行才是
深色加粗标题。每篇点击前会重新截一张图，用 feed_vision 把 y 吸附到
深色标题行，避免读图误差 / 信息流漂移把落点带到来源名上（那会打开
公众号主页，菜单里没有「复制链接」）。校准只改 y、不改任何配置，
失败时降级为原坐标，不做失败后自动重试。

**人介入兜底**（PRD 决策 7）：只要复制出来的不是 mp.weixin.qq.com 链接、
或前后两篇拿到同一个链接（说明窗口没关掉 / 菜单位置变了），
立刻报错退出，不改任何 state，留一份现场截图，等人核对
config/settings.json 的 ui 坐标。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

from . import feed_vision, gui
from .config import project_path
from .state import load_json

_URL_RE = re.compile(r"https?://mp\.weixin\.qq\.com/s[^\s\"'<>]*")
_PROBE_PATH = Path("/tmp/wd_feed_probe.png")


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


def _abort(coords, index, reason, obtained, *, cfg=None, cal=None,
           used_y=None):
    """失败时留已取链接 + 现场截图，方便人接着干；不写任何台账、不自动重试。"""
    partial = project_path("out", "_raw_urls_partial.json")
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_text(json.dumps({"obtained": obtained,
                                   "aborted_at_index": index,
                                   "reason": reason},
                                  ensure_ascii=False, indent=2),
                       encoding="utf-8")
    shot = None
    if cfg is not None:
        try:
            shot = project_path(
                "out", f"grab-fail-{datetime.now():%Y%m%d-%H%M%S}.png")
            gui.screenshot(shot, cfg["ui"]["logical_width"],
                           cfg["ui"]["logical_height"])
        except Exception:                      # 留证失败不能盖掉原始错误
            shot = None
    lines = [f"第 {index} 篇界面操作失败：{reason}",
             f"  已取到的链接留在 {partial}（未写入任何台账）"]
    if cal is not None:
        lines.append(f"  本轮点击：给定 y={cal.given_y:g} → 实际 y={used_y:g}"
                     f"（标题行校准：{cal.status}，{cal.detail}）")
    if shot is not None:
        lines.append(f"  现场截图：{shot}")
    lines += [
        "  若打开的是公众号主页而不是文章，多半是点到了卡片第一行的小字"
        "来源名；请先手动 Cmd+W 关掉该窗口，对照现场截图重新给坐标。",
        "  若打开的是文章但没复制到链接，请核对 config/settings.json 的 "
        "ui.more_button / ui.copy_link / ui.close_window 坐标。",
        f"  本次坐标：{coords}",
    ]
    raise GrabAborted("\n".join(lines))


def _probe_title_y(cfg: dict, y: float) -> feed_vision.Calibration:
    """点击前重新截图，把 y 吸附到深色加粗标题行。

    探测图用逻辑分辨率的一半：像素量降到 1/4，实测单次解码约 0.16 秒；
    坐标换算由 feed_vision 按实际图像尺寸完成。任何异常都降级为原坐标。
    """
    ui = cfg["ui"]
    if not ui.get("title_probe", 1):
        return feed_vision.Calibration(y, y, "disabled", "标题行校准已关闭")
    try:
        gui.screenshot(_PROBE_PATH, max(1, ui["logical_width"] // 2),
                       max(1, ui["logical_height"] // 2))
    except Exception as exc:
        return feed_vision.Calibration(
            y, y, "error", f"探测截图失败（{type(exc).__name__}: {exc}），保持原坐标")
    return feed_vision.calibrate(
        _PROBE_PATH, y,
        logical_width=ui["logical_width"],
        logical_height=ui["logical_height"],
        search_down=ui.get("title_search_down", 45),
        search_up=ui.get("title_search_up", 20),
        threshold=ui.get("title_dark_threshold", 120),
        min_dark=ui.get("title_min_dark", 10),
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
                "title_probe": bool(ui.get("title_probe", 1)),
                "would_click": len(coords) * 4}

    gui.activate_wechat()
    obtained: list[str] = []
    calibrations: list[dict] = []
    for i, (x, y) in enumerate(coords, 1):
        if len(obtained) >= limit:
            break

        # 点之前先对齐标题行：给定坐标偏高会点中浅灰的公众号名，
        # 打开公众号主页而不是文章。每篇都重新截屏，顺带抵消列表漂移。
        cal = _probe_title_y(cfg, y)
        used_y = cal.used_y
        calibrations.append({"index": i, "x": x, **cal.as_dict()})
        if cal.moved:
            print(f"  · 第 {i} 篇：标题行校准 y {y:g} → {used_y:g}"
                  f"（{cal.detail}）", flush=True)
        elif cal.status in ("no_title", "error", "disabled"):
            print(f"  · 第 {i} 篇：{cal.detail}，按原坐标点击", flush=True)

        gui.clipboard_clear()
        gui.click(x, used_y)                  # 点开文章（只点加粗标题行）
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
                   "可能是点到了公众号名打开主页、或「…」菜单没弹出 / "
                   "「复制链接」位置变了",
                   obtained, cfg=cfg, cal=cal, used_y=used_y)
        link = m.group(0)
        if link in obtained:
            _abort(coords, i,
                   f"又拿到同一篇 {link}，说明上一篇窗口没关掉或列表没滚动",
                   obtained, cfg=cfg, cal=cal, used_y=used_y)

        # 微信把公众号文章开成同一个多标签窗口，点左上角红点常被吞掉
        # （窗口未被激活 / 菜单模态），实测 Cmd+W 能可靠关掉整窗回到信息流。
        # 13 = kVK_ANSI_W；这是本机微信界面行为变化后的修正，不做视觉识别。
        gui.cmd_key(13)
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
            "file": str(out), "urls": merged,
            "calibrations": calibrations}


def _sleep(seconds) -> None:
    import time
    time.sleep(float(seconds))
