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

**跳过没有链接的卡片**（2026-11-08）：文章被发布者删除后仍会打开一个正常
浮窗，但页面只有一句「该内容已被发布者删除」，「…」菜单里**没有**
「复制链接」。这种是内容问题、不是界面坏了，判成 `_CardSkipped`：关窗、
换下一张，不累加「连续失败」。识别靠 `feed_vision.looks_like_deleted_page`
（近白底 + 黄色感叹号图标），并且**绝不**在认不出菜单时盲点写死坐标。
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
_MENU_PROBE = Path("/tmp/wd_menu_probe.png")
_PAGE_PROBE = Path("/tmp/wd_page_probe.png")


class GrabAborted(RuntimeError):
    """需要人介入的界面异常。退出码 3。"""


class _CardFailed(Exception):
    """单张卡片没拿到链接（广告卡片、点错位置、菜单没弹出来）。"""

    def __init__(self, reason: str, cal=None, used_y=None):
        super().__init__(reason)
        self.reason = reason
        self.cal = cal
        self.used_y = used_y


class _CardSkipped(_CardFailed):
    """这张卡片本身没有可复制的链接（文章被删 / 被屏蔽），跳过继续。

    与父类的区别只在**后果**：这是内容问题而不是界面问题，所以不计入
    「连续失败」、不留现场截图、不停轮。调用方必须先 catch 本类再 catch
    父类。`kind` 进 skipped 台账，方便事后看这批里有多少篇是被删的。
    """

    def __init__(self, reason: str, cal=None, used_y=None, *,
                 kind: str = "deleted", detail: dict | None = None):
        super().__init__(reason, cal, used_y)
        self.kind = kind
        self.detail = detail or {}


def _check_geometry(ui: dict) -> None:
    """抓取前确认屏幕仍在「1280x832 逻辑坐标 + 等比截图」这个前提上。

    为什么必须挡在点击之前：这套抓取靠固定像素阈值（标题带 11~12px、
    36x36 的「公众号」蓝方块、x=520..880 的暗像素数）在 1280x832 的
    归一化空间里找目标。分辨率变了、宽高比变了、多了一块扩展屏被
    `screencapture` 拼进来，这些判据不会报错——只会**静默点到别处**，
    表现为「点了但剪贴板是空的」或「开错浮窗」，排查起来极费时间。

    外接显示器 / iPad 随航 / 远程桌面接入都可能触发，所以每次 GUI 抓取
    前都查一遍（一次截图 + 一次 CGWindowList，约 150ms）。查不到信息时
    放行，交给后续真正的截图报错，不在这里冒充失败。
    """
    expected_w = int(ui.get("logical_width", 1280))
    expected_h = int(ui.get("logical_height", 832))
    tolerance = float(ui.get("geometry_tolerance", 0.01))

    geo = gui.screen_geometry()
    ok, issues = gui.judge_geometry(
        expected_w=expected_w, expected_h=expected_h,
        display=geo["display"], raw_size=geo["raw"], tolerance=tolerance,
    )
    if ok:
        return

    disp = geo["display"]
    seen = ("主显示器 "
            + (f"{disp['w']:g}x{disp['h']:g}、{int(disp.get('displays') or 0)} 块屏"
               if disp else "未知")
            + f"；全屏截图 {geo['raw'] or '未知'}")
    if geo["error"]:
        seen += f"（截屏失败：{geo['error']}）"

    lines = ["屏幕几何与坐标系不匹配，继续点下去会静默点错位置，已停止。"]
    for it in issues:
        lines.append(f"  · {it['message']}")
    lines.append(f"  · 实测：{seen}")
    lines.append(
        "处理办法（任选一）："
        f"① 把显示器逻辑分辨率改回 {expected_w}x{expected_h}（系统设置 > 显示器）；"
        "② 断开外接屏 / iPad 随航后重跑；"
        "③ 若新分辨率才是长期常态，就按新尺寸重新校准 ui.* 坐标与像素阈值。"
        "只想先跑通：参数设置页把「屏幕几何自检」关掉。")
    raise GrabAborted("\n".join(lines))


def preflight(cfg: dict | None = None) -> None:
    if sys.platform != "darwin":
        raise GrabAborted("只能在 macOS 上抓微信客户端")
    if not gui.wechat_running():
        raise GrabAborted("微信没运行。请先启动并登录微信，打开「公众号」信息流。")
    locked = gui.screen_locked()
    if locked is True:
        raise GrabAborted("屏幕已锁定，截屏和点击都不可靠。请解锁后手动重跑。")
    if cfg is not None:
        ui = cfg.get("ui") or {}
        if int(ui.get("geometry_check", 1)):
            _check_geometry(ui)


def capture(cfg: dict, out: str | None = None) -> dict:
    """激活微信并截一张逻辑坐标图，交给 Hy4 读卡片位置。"""
    preflight(cfg)
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


def _probe_title_y(cfg: dict, y: float,
                   used_centers=None) -> feed_vision.Calibration:
    """点击前重新截图，把 y 吸附到深色加粗标题行。

    探测图用**全逻辑分辨率**（1280x832）。早先用半分辨率省解码时间，但
    缩放会把细标题行的深色像素平均掉（峰值从 60~80 掉到 ~15），导致部分
    标题带漏检、匹配错位，所以退回全分辨率（单次解码约 0.7 秒，相对每篇
    5 秒以上的开窗/关窗等待可接受）。

    used_centers 是本轮已经点过的标题行中心：视觉模型读图会把卡片间距
    压缩，误差逐张累积，只靠小窗校准救不回来。带上已点过的中心后，校准
    会在全屏范围内挑「离给定 y 最近、且没点过」的标题带，既抵消累积偏移，
    又保证不会重复点同一张卡片。
    """
    ui = cfg["ui"]
    if not ui.get("title_probe", 1):
        return feed_vision.Calibration(y, y, "disabled", "标题行校准已关闭")
    try:
        gui.screenshot(_PROBE_PATH, ui["logical_width"], ui["logical_height"])
    except Exception as exc:
        return feed_vision.Calibration(
            y, y, "error", f"探测截图失败（{type(exc).__name__}: {exc}），保持原坐标")
    return feed_vision.snap(
        _PROBE_PATH, y,
        logical_width=ui["logical_width"],
        logical_height=ui["logical_height"],
        search_down=ui.get("title_search_down", 45),
        search_up=ui.get("title_search_up", 20),
        used_centers=used_centers or (),
        threshold=ui.get("title_dark_threshold", 120),
        min_dark=ui.get("title_min_dark", 10),
        min_sep=ui.get("title_min_sep", 24),
        max_distance=ui.get("title_global_max_distance", 240),
        min_logical_height=ui.get("title_min_height", 6),
        max_logical_height=ui.get("title_max_height", 22),
        min_peak=ui.get("title_min_peak", 45),
    )


def _guard_title(cfg: dict, x: float, y: float, used_centers) -> float:
    """点击前验证 (x, y) 附近**确实有标题带**，返回要点击的 y。

    背景（2026-11-07 事故）：点中卡片非标题区域（卡片空白、时间戳行、
    「常看的号」头像）会打开公众号主页浮窗 / 号详情，把「公众号」聚合页
    切成「号列表」形态（一行一个号、只显示最新一篇），整轮抓取报废。

    所以点击目标必须落在标题带上：给定点附近 ±tol px 找不到标题带就
    **拒绝点击**。与旧 `title_probe` 的区别：旧逻辑会全局吸附到远处标题带
    （吸附到缩略图/图片上，正是把 y 从 237 拉到 278 的元凶）；新守卫只在
    给定点附近**验证**，绝不挪点，找不到就失败停轮。

    used_centers 是本轮已点过的标题行中心：附近有带但已被点过时同样拒绝，
    防止视觉模型读图误差把同一张卡片点两遍。
    """
    ui = cfg["ui"]
    if not ui.get("title_guard", 1):
        return y
    try:
        gui.screenshot(_PROBE_PATH, ui["logical_width"], ui["logical_height"])
    except Exception as exc:
        # 截不了图就没法验证。保持原坐标放行：点击后还有「文章窗口是否
        # 真的打开」这一道兜底，不会因此盲目盲点。
        print(f"  · 标题带守卫截图失败（{type(exc).__name__}: {exc}），"
              "按原坐标点击", flush=True)
        return y
    cards = feed_vision.find_card_titles(
        _PROBE_PATH,
        logical_width=ui["logical_width"],
        logical_height=ui["logical_height"],
        threshold=ui.get("title_dark_threshold", 120),
        min_dark=ui.get("title_min_dark", 10),
        min_logical_height=ui.get("title_min_height", 6),
        max_logical_height=ui.get("title_max_height", 22),
        min_peak=ui.get("title_min_peak", 45),
        min_y=ui.get("title_guard_min_y", 100),
        weak_min_peak=ui.get("card_weak_min_peak", 26),
        weak_min_gap=ui.get("card_weak_min_gap", 90),
        left_align_tol=ui.get("card_left_align_tol", 14),
    )
    tol = float(ui.get("title_guard_tol", 15))
    near = [c for c in cards
            if abs(c.y - y) <= tol
            and (c.x0 - 40) <= x <= (c.x1 + 40)
            and all(abs(c.y - u) >= 12 for u in used_centers)]
    if not near:
        bands = "; ".join(f"y≈{c.y:.0f} x[{c.x0:.0f}-{c.x1:.0f}]"
                          for c in cards[:8]) or "（整屏没有标题带）"
        raise _CardFailed(
            f"点击点 ({x:.0f},{y:.0f}) 附近 ±{tol:.0f}px 内没有未点过的标题行，"
            "拒绝盲点 —— 点中卡片空白 / 时间戳 / 头像会打开公众号主页，"
            "把聚合页切成号列表形态。\n"
            f"  当前识别到的标题带：{bands}")
    return min(near, key=lambda c: abs(c.y - y)).y


def _article_window_state(cfg: dict) -> bool | None:
    """最前面的微信窗口是不是文章 / 主页浮窗。拿不到窗口信息时返回 None。"""
    try:
        from . import nav
        return nav.article_window_open(cfg)
    except Exception:
        return None


def _focus(cfg: dict) -> None:
    """每次点击前确认微信窗口在前（WorkBuddy 会自己弹到最前把微信盖住）。

    ensure_front 抛 GrabAborted（主窗口不在屏幕上 / 被完全盖住抬不起来）
    时必须直接传播：吞掉它会让点击白白发出去，最后只能以「点击后文章
    窗口没打开」这种间接原因停轮，人看不出真正的问题。
    """
    try:
        from . import nav
        nav.ensure_focus(cfg)
    except Exception as exc:
        from .grab import GrabAborted   # 本模块定义，延迟引用仅为可读性
        if isinstance(exc, GrabAborted):
            raise
        # 拿不到窗口信息就按原样点，别让检查本身失败


def _front_window() -> dict | None:
    """最前面的微信窗口（文章浮窗打开时就是它）。拿不到窗口信息返回 None。"""
    try:
        from . import winpos
        return winpos.front_wechat(winpos.windows())
    except Exception:
        return None


def _more_point(win: dict | None) -> tuple[float, float] | None:
    """「…」按钮位置：按当前文章窗口的实际几何算，而不是写死屏幕坐标。

    实测文章窗口是独立的浮动浏览器窗口（896x508 / 1206x569 两种尺寸都见过），
    「…」固定在标题栏右端、距窗口右边约 30px、距窗口顶部约 20px。
    窗口被拖动 / 大小变化都不会影响这个相对位置。
    """
    if not win:
        return None
    return (win["x"] + win["w"] - 30, win["y"] + 20)


def _article_more_point() -> tuple[float, float] | None:
    """保留旧入口（按屏幕实时几何算「…」位置）。"""
    return _more_point(_front_window())
def _menu_groups(cfg: dict, more: tuple[float, float]
                 ) -> list[list[feed_vision.MenuItem]]:
    """截图并按「分隔线」把「…」菜单的文字行分组。

    菜单内容会随文章变化（比如多出「添加浮窗」），写死像素坐标早晚失效。
    但分组结构稳定：「复制链接」在第 2 组（第 1 组是转发/分享/收藏，
    第 3 组是浏览记录/下载），且是组内第 2 项。

    截图另外落到 `_MENU_PROBE`：识别不到「复制链接」时要用它判「文章是不是
    被删了」，省一次截屏（这类页面菜单开着时，中间的黄色图标照样看得见）。
    """
    ui = cfg["ui"]
    gui.screenshot(_MENU_PROBE, ui["logical_width"], ui["logical_height"])
    return feed_vision.find_menu_items(
        _MENU_PROBE,
        x0=more[0] - 240, x1=more[0] - 30,
        y0=more[1] + 25, y1=more[1] + 470,
        logical_width=ui["logical_width"],
        logical_height=ui["logical_height"],
        threshold=ui.get("title_dark_threshold", 120),
        min_dark=ui.get("menu_min_dark", 6),
        gap=ui.get("menu_group_gap", 35),
    )


def _copy_link_point(groups: list[list[feed_vision.MenuItem]],
                     more: tuple[float, float]) -> tuple[float, float] | None:
    """按分组结构定位「复制链接」；结构对不上就返回 None（**不盲点**）。

    为什么不能退回写死坐标：文章被删 / 打开的是公众号主页时，菜单里根本
    没有这一项，按配置坐标点下去会落在菜单外或别的菜单项上（「投诉」
    「收藏」之类），剪贴板当然是空的，还把界面状态搅乱。
    """
    if len(groups) < 2 or len(groups[1]) < 2:
        return None
    # 横向位置不能用文字中心：菜单面板贴在「…」左侧约 180px 处，
    # 而「复制链接」这类短条目算出来的中心会落到面板左边缘之外
    # （实测 x≈1022，面板左沿≈1067），一点就把菜单点关、剪贴板为空。
    # 纵向 item.y 仍然按文字行给；横向取面板内固定的安全位置。
    return (more[0] - 110, groups[1][1].y)


def _deleted_page(cfg: dict, image: str | Path | None = None,
                  win: dict | None = None) -> dict:
    """判当前文章页是不是「已被发布者删除」这类空页。

    image 给了就用它（复用刚拍的菜单图，省一次截屏），没给就重新截一张
    （菜单已经收起时用）。任何失败都返回 deleted=False —— 检测本身不能
    变成新的失败点。
    """
    ui = cfg["ui"]
    if not ui.get("skip_deleted", 1):
        return {"deleted": False, "note": "「跳过已删除文章」已关闭"}
    path = image
    if path is None:
        path = _PAGE_PROBE
        try:
            gui.screenshot(path, ui["logical_width"], ui["logical_height"])
        except Exception as exc:
            return {"deleted": False,
                    "note": f"截图失败（{type(exc).__name__}: {exc}）"}
    rect = None
    if win:
        rect = (win["x"], win["y"], win["w"], win["h"])
    try:
        return feed_vision.looks_like_deleted_page(
            path,
            logical_width=ui["logical_width"],
            logical_height=ui["logical_height"],
            rect=rect,
            min_white_ratio=float(ui.get("deleted_min_white", 0.70)),
            min_yellow=int(ui.get("deleted_min_yellow", 800)),
        )
    except Exception as exc:
        return {"deleted": False,
                "note": f"检测失败（{type(exc).__name__}: {exc}）"}


def _open_and_copy(cfg: dict, x: float, y: float, used_centers,
                   *, label: str = "") -> tuple[str, feed_vision.Calibration,
                                                float, bool]:
    """点开一张卡片 → 「…」→「复制链接」→ 关窗。

    返回 (链接, 校准结果, 实际点击 y, 是否已关掉文章窗口)。

    **链接拿到就算成功**：关窗失败只让 closed=False，绝不把已经复制到的
    链接丢掉（早先这里直接抛异常，等于每篇都白抓一次）。界面恢复由调用方
    用 `nav.recover()` 处理，它恢复不了才停。

    其他任何一步不对就抛异常，由调用方决定「立刻停」还是「跳过继续」：
    `_CardSkipped` = 这篇本身没有链接可复制（被删/被屏蔽），跳过；
    `_CardFailed` = 界面状态可疑，按 PRD 停下等人。
    """
    ui = cfg["ui"]

    # 点之前先对齐标题行：给定坐标偏高会点中浅灰的公众号名，打开公众号
    # 主页而不是文章。每篇都重新截屏，顺带抵消列表漂移。
    cal = _probe_title_y(cfg, y, used_centers)
    used_y = cal.used_y
    if cal.moved:
        print(f"  · {label}标题行校准 y {y:g} → {used_y:g}（{cal.detail}）",
              flush=True)
    elif cal.status in ("no_title", "error", "disabled"):
        print(f"  · {label}{cal.detail}，按原坐标点击", flush=True)

    # 标题带守卫：给定点附近必须有未点过的标题带，否则拒绝盲点
    # （点中空白 / 时间戳 / 头像会开公众号主页，把聚合页切成号列表形态）。
    guarded_y = _guard_title(cfg, x, y, used_centers)
    if abs(guarded_y - used_y) > 6:
        print(f"  · {label}标题带守卫 y {used_y:g} → {guarded_y:g}", flush=True)
    used_y = guarded_y

    gui.clipboard_clear()
    _focus(cfg)
    gui.click(x, used_y)                  # 点开文章（只点加粗标题行）
    _sleep(ui["open_wait"])

    # 点击后必须确认文章浮窗真的打开了：没打开说明这次点击被别的窗口吃掉
    # 或落在了空白处。**绝不能**在这种状态下继续点「…」——那会点在主窗口
    # 顶栏 / 页面内容上（2026-11-07 事故：聚合页被切成号列表形态就是这么来
    # 的）。重新激活微信后再试一次；仍然没开就停轮等人。
    state = _article_window_state(cfg)
    if state is False:
        _sleep(0.8)
        state = _article_window_state(cfg)
    if state is False:
        print("  · 点击后文章窗口没打开，重新激活微信再点一次", flush=True)
        gui.activate_wechat()
        _sleep(0.6)
        # 点击落空最常见的两个原因之一是「微信在别的 Space」——
        # 在重试前跑一次**完整** Space 检查（截图判定 + 必要时强制切换），
        # 把修复挂在失败恢复路径上，而不是每次点击前都花秒级成本。
        try:
            nav.ensure_onscreen(cfg, verbose=True)
        except Exception as exc:
            raise _CardFailed(f"Space 检查失败：{exc}", cal, used_y)
        _focus(cfg)
        gui.click(x, used_y)
        _sleep(ui["open_wait"])
        state = _article_window_state(cfg)
        if state is False:
            _sleep(0.8)
            state = _article_window_state(cfg)
    if state is False:
        raise _CardFailed(
            "点击后文章窗口始终没有打开（点击可能被其它窗口吃掉，或落点无效）。"
            "在这种状态下继续点「…」会点到主窗口上、把聚合页切成号列表形态，"
            "所以停下等人介入", cal, used_y)

    # 「…」的位置按文章窗口实时几何算；拿不到窗口信息才退回配置坐标。
    # state 是 False 的情况上面已经拦下；这里再防一手：拿不到窗口信息
    # （None）时只有配置坐标可用，保持旧行为。
    win = _front_window()
    more = _more_point(win) or tuple(ui["more_button"])
    _focus(cfg)
    gui.click(*more)
    _sleep(ui["menu_wait"])

    groups = _menu_groups(cfg, more)
    copy_pt = _copy_link_point(groups, more)
    if copy_pt is None:
        # 菜单里认不出「复制链接」。最常见的原因是文章已被发布者删除：
        # 这类页面顶部菜单会少掉这一项。先用刚拍的菜单图判一下，命中就
        # 直接判「跳过」，连点都不用点（盲点写死坐标只会点到菜单外或别的
        # 菜单项上，把剪贴板和界面状态一起搞乱）。
        info = _deleted_page(cfg, _MENU_PROBE, win)
        if info["deleted"]:
            gui.press_escape()            # 先收菜单，否则 Cmd+W 会被它吃掉
            raise _CardSkipped(
                "文章已被发布者删除（页面为空、菜单里没有「复制链接」）",
                cal, used_y, kind="deleted", detail=info)
        if not groups:
            gui.press_escape()            # 别让残留菜单吃掉后面的 Cmd+W
            raise _CardFailed(
                "点了「…」但一个菜单文字行都没扫到（菜单可能没弹出，"
                "或「…」坐标 / 文章窗口几何变了）。不敢盲点配置坐标",
                cal, used_y)
        # 结构认不出来但不是删除页：仍旧按配置坐标试一次（老兜底），
        # 拿不到链接会落到下面的剪贴板检查。
        print("  · 菜单结构认不出「复制链接」，按配置坐标试一次", flush=True)
        copy_pt = tuple(ui["copy_link"])

    _focus(cfg)
    gui.click(*copy_pt)                   # 「复制链接」
    _sleep(ui["click_wait"] + 0.2)
    url = gui.clipboard_read()

    m = _URL_RE.search(url)
    if not m:
        # 菜单已经收起，重新截一张判是不是删除页：是就跳过换下一张，
        # 不是才按界面异常停下等人。
        info = _deleted_page(cfg, None, win)
        if info["deleted"]:
            raise _CardSkipped(
                "文章已被发布者删除（页面为空、剪贴板里没有链接）",
                cal, used_y, kind="deleted", detail=info)
        raise _CardFailed(
            f"剪贴板里不是公众号链接（得到 {url[:80]!r}）。"
            "可能是点到了公众号名打开主页、或「…」菜单没弹出 / "
            "「复制链接」位置变了（广告卡片也会这样）", cal, used_y)

    # 微信把公众号文章开成**独立的浮动窗口**（layer 3），点左上角红点常被
    # 吞掉，实测 Cmd+W 能可靠关掉。但**必须先确认前面那个确实是文章窗口**：
    # 在主窗口上按 Cmd+W 会把微信整个关掉（窗口消失、之后所有截图都拍不到
    # 它，本轮就是这么踩坑的）。
    from . import nav
    closed = nav.close_article(cfg)
    gui.clipboard_clear()
    return m.group(0), cal, used_y, closed


def _write_urls(obtained: list[str], append: bool) -> tuple[Path, list[str]]:
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
    return out, merged


def grab(cfg: dict, coords: list[tuple[float, float]], limit: int,
         dry_run: bool = False, append: bool = False) -> dict:
    preflight(cfg)
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
    skipped: list[dict] = []
    used_centers: list[float] = []      # 本轮已点过的标题行，供校准排除
    from . import nav
    for i, (x, y) in enumerate(coords, 1):
        if len(obtained) >= limit:
            break
        try:
            link, cal, used_y, closed = _open_and_copy(cfg, x, y, used_centers,
                                                       label=f"第 {i} 篇：")
        except _CardSkipped as exc:
            # 文章被删 / 没有链接可复制：内容问题，不该让整轮停下。
            # 关窗回到信息流后换下一个坐标继续。
            skipped.append({"index": i, "x": x, "y": y,
                            "kind": exc.kind, "reason": exc.reason})
            print(f"  · 跳过第 {i} 篇（{exc.reason}），换下一个坐标",
                  flush=True)
            # 这一行确实点过了：记进去，免得下一个坐标的校准又吸回同一张卡片
            if exc.used_y is not None:
                used_centers.append(exc.used_y)
            if not nav.recover(cfg):
                _abort(coords, i,
                       f"{exc.reason}；跳过之后回不到信息流",
                       obtained, cfg=cfg, cal=exc.cal, used_y=exc.used_y)
            continue
        except _CardFailed as exc:
            _abort(coords, i, exc.reason, obtained, cfg=cfg,
                   cal=exc.cal, used_y=exc.used_y)
            raise                            # 上面必然抛出，这里只为类型收敛
        calibrations.append({"index": i, "x": x, **cal.as_dict()})
        if link in obtained:
            _abort(coords, i,
                   f"又拿到同一篇 {link}，说明上一篇窗口没关掉或列表没滚动",
                   obtained, cfg=cfg, cal=cal, used_y=used_y)
        obtained.append(link)
        if not closed:                       # 链接已保住，但要停：界面回不去了
            _abort(coords, i,
                   "文章窗口关不掉，界面回不到信息流（下一次点击会落在旧文章上）",
                   obtained, cfg=cfg, cal=cal, used_y=used_y)
        # 记录这次点中的标题行；下一张校准时会排除它，避免压缩误差把
        # 下一个坐标又吸回同一张卡片。
        if cal.status in ("on_title", "moved_down", "moved_up"):
            used_centers.append(used_y)

    out, merged = _write_urls(obtained, append)
    return {"count": len(merged), "new_this_round": len(obtained),
            "file": str(out), "urls": merged, "skipped": skipped,
            "calibrations": calibrations}


def grab_auto(cfg: dict, limit: int, *, dry_run: bool = False,
              append: bool = False, max_rounds: int | None = None,
              verbose: bool = True) -> dict:
    """全自动抓取：自动进信息流 → 一屏一屏「从上到下」点开 → 不足则滚动。

    与 `grab` 的区别只在「坐标从哪来」和「失败怎么办」：

    * 坐标不再靠读图给，而是每轮重新截图、用像素找加粗标题行，
      滚动后位置变了也自然跟上；
    * 单张失败（最常见的是信息流里插的**广告卡片**，它没有「复制链接」）
      记为跳过并继续，但**连续**失败达到上限就立刻退出码 3 —— 那是界面
      真的不对了，不该继续盲点；
    * 文章被发布者删除（页面报「该内容已被发布者删除」、菜单里没有
      「复制链接」）属于**内容**问题，判 `_CardSkipped`：关窗换下一张，
      不累加失败计数；但连续跳过太多（默认 4 张）仍然停 —— 那多半不是
      个别文章被删，而是菜单结构整体失效了。
    """
    preflight(cfg)
    from . import nav                       # 延迟导入，避免与 nav 循环 import

    ui = cfg["ui"]
    if dry_run:
        return {"dry_run": True, "mode": "auto", "limit": limit,
                "would": "导航到公众号信息流后逐张点开、复制链接、滚动翻页"}

    nav.to_feed(cfg, verbose=verbose)
    obtained: list[str] = []
    skipped: list[dict] = []
    rounds = int(ui.get("auto_rounds", 6) if max_rounds is None else max_rounds)
    streak_limit = max(1, int(ui.get("auto_fail_streak", 2)))
    skip_limit = max(1, int(ui.get("auto_skip_streak", 4)))
    fp_tol = int(ui.get("card_fp_max_diff", 4))
    fail_streak = 0
    skip_streak = 0
    # 已经点过的标题行指纹。滚动后同一篇文章会出现在新屏的不同 y 上，
    # 光靠坐标认不出来，只能认标题行的像素分布（见 feed_vision.fps_close）。
    seen_fps: list[str] = []
    prev_fps: list[str] = []

    for round_no in range(1, rounds + 1):
        if len(obtained) >= limit:
            break
        # **必须每屏重新截图**：read_cards 默认读 /tmp/wd_nav.png，那是上次
        # to_feed 留下的图。滚动后不刷新就会一直按上一屏的坐标去点——本轮
        # 第 2 屏就是这么点了同一批位置，第 3 张直接点空。
        shot = nav._shot(cfg)
        cards = nav.read_cards(cfg, shot)
        if nav.looks_like_account_list(cards):
            # 号列表形态（一行一个号）：点标题不开文章，继续抓只会全错。
            _write_urls(obtained, append)
            raise GrabAborted(
                "信息流被切成了「号列表」形态（一行一个号、只显示每号最新一篇），"
                "点标题不会打开文章，抓取停止。\n"
                "  已取到 "
                f"{len(obtained)} 条链接，已写入 {project_path('out', '_raw_urls.json')}"
                "（未写台账）。请手动切回公众号卡片流后重跑。")
        if len(cards) < 2:                  # 被弹窗顶掉了，重新进一次
            nav.to_feed(cfg, verbose=verbose)
            shot = nav._shot(cfg)
            cards = nav.read_cards(cfg, shot)
        if not cards:
            break
        # 跨屏重叠判断：这一屏的指纹里应该还留着上一屏最后几张（滚动幅度
        # 小于一屏）。一条都对不上说明滚动跨过了整屏，中间那几张就漏了。
        overlapped = (not prev_fps) or any(
            any(feed_vision.fps_close(c.fp, p, fp_tol) for p in prev_fps)
            for c in cards)
        if not overlapped and verbose:
            print("  ! 本屏与上一屏没有重叠：滚动幅度可能跨过了整屏，"
                  "中间若有卡片就已漏抓", flush=True)
        if verbose:
            print(f"· 第 {round_no} 屏：{len(cards)} 张卡片"
                  + ("" if overlapped else "（与上屏无重叠）"), flush=True)
        used: list[float] = []
        new_links = 0
        for card in cards:
            if len(obtained) >= limit:
                break
            # 已经点过的同一篇文章（滚动带回来的）直接跳过，既不重复点，
            # 也不占「连续失败」计数。
            if any(feed_vision.fps_close(card.fp, s, fp_tol) for s in seen_fps):
                if verbose:
                    print(f"  · 已点过，跳过（y≈{card.y:.0f}）", flush=True)
                continue
            label = f"第 {len(obtained) + len(skipped) + 1} 张："
            try:
                link, cal, used_y, closed = _open_and_copy(cfg, card.x, card.y,
                                                           used, label=label)
            except _CardSkipped as exc:
                # 内容问题：文章被删 / 页面没有「复制链接」。关窗后换下一张，
                # 不清零、也不增加 fail_streak；卡片指纹记进 seen_fps，
                # 免得滚动带回来时又点一遍。
                skip_streak += 1
                skipped.append({"x": round(card.x, 1), "y": round(card.y, 1),
                                "kind": exc.kind, "reason": exc.reason})
                if card.fp:
                    seen_fps.append(card.fp)
                if exc.used_y is not None:
                    used.append(exc.used_y)     # 这一行点过了，下张别再吸回来
                if verbose:
                    print(f"  · 跳过一张（y≈{card.y:.0f}）：{exc.reason}",
                          flush=True)
                if not nav.recover(cfg):
                    _write_urls(obtained, append)
                    raise GrabAborted(
                        "跳过没有链接的卡片后回不到信息流 —— 界面状态已经不对了。\n"
                        f"  已取到 {len(obtained)} 条链接，已写入 "
                        f"{project_path('out', '_raw_urls.json')}（未写台账）")
                if skip_streak >= skip_limit:
                    _write_urls(obtained, append)
                    raise GrabAborted(
                        f"连续 {skip_streak} 张都没有「复制链接」（多半是被批量删除，"
                        "或菜单结构整体变了），停下等人看一眼。\n"
                        f"  已取到 {len(obtained)} 条链接、跳过 {len(skipped)} 张，"
                        f"已写入 {project_path('out', '_raw_urls.json')}（未写台账）"
                        + (f"：{skipped[-1]['reason']}" if skipped else ""))
                continue
            except _CardFailed as exc:
                fail_streak += 1
                skipped.append({"x": round(card.x, 1), "y": round(card.y, 1),
                                "kind": "error", "reason": exc.reason})
                if verbose:
                    print(f"  · 跳过一张（y≈{card.y:.0f}）：{exc.reason[:60]}",
                          flush=True)
                if not nav.recover(cfg):
                    _write_urls(obtained, append)
                    raise GrabAborted(
                        "跳过失败后回不到信息流 —— 界面状态已经不对了。\n"
                        f"  已取到 {len(obtained)} 条链接，已写入 "
                        f"{project_path('out', '_raw_urls.json')}（未写台账）")
                if fail_streak >= streak_limit:
                    _write_urls(obtained, append)
                    raise GrabAborted(
                        f"连续 {fail_streak} 张都没复制到链接，不再是偶发广告卡片，"
                        "按 PRD 停下等人介入。\n"
                        f"  已取到 {len(obtained)} 条链接、跳过 {len(skipped)} 张，"
                        f"已写入 {project_path('out', '_raw_urls.json')}（未写台账）"
                        + (f"：{skipped[-1]['reason']}" if skipped else ""))
                continue
            fail_streak = 0
            skip_streak = 0
            used.append(used_y)
            if card.fp:
                seen_fps.append(card.fp)
            if link not in obtained:
                obtained.append(link)
                new_links += 1
                if verbose:
                    print(f"  · 第 {len(obtained)} 篇 {link}", flush=True)
            if not closed:
                # 链接已经拿到，只是界面没回到信息流。恢复一次；恢复不了才停，
                # 已抓到的链接照旧落盘（不写台账）。
                if not nav.recover(cfg):
                    _write_urls(obtained, append)
                    raise GrabAborted(
                        "文章窗口关不掉、也回不到信息流。\n"
                        f"  已取到 {len(obtained)} 条链接，已写入 "
                        f"{project_path('out', '_raw_urls.json')}（未写台账）")

        if len(obtained) >= limit:
            break
        if new_links == 0:                  # 这一屏没有新文章，再滚也是重复
            break
        prev_fps = [c.fp for c in cards if c.fp]
        _focus(cfg)
        gui.scroll(ui.get("scroll_x", 700), ui.get("scroll_y", 650),
                   ui.get("scroll_amount", -12))
        _sleep(ui.get("scroll_wait", 1.4))

    out, merged = _write_urls(obtained, append)
    return {"mode": "auto", "count": len(merged), "new_this_round": len(obtained),
            "skipped": skipped, "file": str(out), "urls": merged}


def _sleep(seconds) -> None:
    import time
    time.sleep(float(seconds))
