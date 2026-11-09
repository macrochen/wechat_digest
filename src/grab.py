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

**抓「最近 N 个自然日」**（2026-11-09）：以前抓到 `max_articles`（当时默认
20）篇就收手，现在默认滚到**窗口内内容滚完**为止。窗口 = 含今天往前
`feed_days` 个自然日（默认 2：今天 + 昨天）。判定靠卡片第一行右侧的推送时间
（OCR 认「7小时前 / 昨天 09:33」这类文字，见 `feed_time`）：一屏里只要出现
一张「早于窗口」的卡片，它下面就都是更早的内容，抓完这屏就停。
认不出时间就当还在窗口内（宁可多抓，scan 层再用服务端时间戳精确剔掉）。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

from . import feed_vision, gui
from .config import project_path
from .state import Seen, load_json

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
    """这张卡片本身没有可复制的链接（文章被删 / 广告卡 / 被屏蔽），跳过继续。

    与父类的区别只在**后果**：这是内容问题而不是界面问题，所以不计入
    「连续失败」（auto_fail_streak）、不留现场截图、不停轮。调用方必须先
    catch 本类再 catch 父类。`kind` 进 skipped 台账：deleted=被删、
    no_link=菜单里没有「复制链接」/ 点了也没拿到链接（多数是广告卡）。
    真出现菜单整体失效时，由 auto_skip_streak 连跳上限兜底停轮。
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
    """最前面的微信窗口是不是文章 / 主页浮窗。拿不到窗口信息时返回 None。

    只认独立浮窗。标签页形态（微信最大化）下恒 False —— 那是**故意的**：
    这种形态下 Cmd+W 会关掉整个微信窗口，本轮流程必须停在「没打开」上让人
    介入，而不是硬往下走。可修正的方向是开抓前把窗口退出全屏。
    """
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


def _dismiss_opened_article(cfg: dict) -> None:
    """拿不到链接时收菜单 + 关文章窗，别把残留窗口留给下一张卡片。

    2026-11-09 实盘：广告卡片没有「复制链接」，`_CardFailed` 直接抛出时
    菜单还开着、文章窗也没关；下一张卡片的坐标落在文章页上，被标题带守卫
    拒绝盲点，凑满连续 2 次失败后误报退出码 3。**失败路径必须自己收尾**，
    不能全指望调用方的 `nav.recover()` —— 后者只看像素，分不清文章页和
    卡片流（两者都能扫出 ≥2 条加粗文字带）。
    """
    try:
        gui.press_escape()          # 先收菜单，否则它会吃掉 Cmd+W
    except Exception:
        pass
    try:
        from . import nav
        nav.close_article(cfg)      # 带守卫：前面不是文章窗就什么都不做
    except Exception:
        pass


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
    # nav 必须在函数顶部导入：下面的「点击后文章窗口没打开」恢复路径也会
    # 用到 nav.ensure_onscreen。若只在结尾 close_article 前局部 import，
    # Python 会把 nav 判成局部变量，恢复路径先执行就抛 UnboundLocalError
    # （2026-11-08 实盘：被包装成「Space 检查失败」误报为界面问题）。
    from . import nav
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
            _dismiss_opened_article(cfg)   # 收菜单 + 关窗，别留给下一张
            raise _CardSkipped(
                "文章已被发布者删除（页面为空、菜单里没有「复制链接」）",
                cal, used_y, kind="deleted", detail=info)
        if not groups:
            _dismiss_opened_article(cfg)
            raise _CardFailed(
                "点了「…」但一个菜单文字行都没扫到（菜单可能没弹出，"
                "或「…」坐标 / 文章窗口几何变了）。不敢盲点配置坐标",
                cal, used_y)
        # 认不出「复制链接」= **绝不盲点写死坐标**。同一组菜单里就有
        # 「使用默认浏览器打开」，文章窗口几何一变，ui.copy_link 会落到
        # 它上面，直接把界面甩进 Chrome、微信主窗口从屏幕上消失
        # （2026-11-09 实盘）。按号抓取链 v1.15 已废掉写死坐标，
        # 信息流链在这里对齐：广告卡 / 删文本来就没有这一项，
        # 收好窗口判 no_link 跳过即可，绝不再补一刀。
        _dismiss_opened_article(cfg)
        raise _CardSkipped(
            "菜单里认不出「复制链接」，按「绝不盲点」跳过"
            "（广告卡 / 被删文章常见）",
            cal, used_y, kind="no_link")

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
            _dismiss_opened_article(cfg)
            raise _CardSkipped(
                "文章已被发布者删除（页面为空、剪贴板里没有链接）",
                cal, used_y, kind="deleted", detail=info)
        # 本次实盘命中的就是这条：广告卡没有「复制链接」，菜单还开着、
        # 文章窗也没关，下一张必然被标题带守卫拦下。
        #
        # 这张卡片本身没有可复制的链接，属于**内容**问题而不是界面失效，
        # 判 _CardSkipped：跳过换下一张、不计入 auto_fail_streak。配置注释
        # 里早就写了「页面没有「复制链接」时不计入连续失败」，旧实现却按
        # 界面异常抛出，导致信息流里连续 2 张广告就误报退出码 3。
        # 菜单要是真的整体失效，还有 auto_skip_streak（默认 4）连跳兜底。
        _dismiss_opened_article(cfg)
        raise _CardSkipped(
            f"剪贴板里不是公众号链接（得到 {url[:80]!r}）。"
            "可能是点到了公众号名打开主页、或「…」菜单没弹出 / "
            "「复制链接」位置变了（广告卡片也会这样）",
            cal, used_y, kind="no_link")

    # 微信把公众号文章开成**独立的浮动窗口**（layer 3），点左上角红点常被
    # 吞掉，实测 Cmd+W 能可靠关掉。但**必须先确认前面那个确实是文章窗口**：
    # 在主窗口上按 Cmd+W 会把微信整个关掉（窗口消失、之后所有截图都拍不到
    # 它，本轮就是这么踩坑的）。
    closed = nav.close_article(cfg)
    gui.clipboard_clear()
    return m.group(0), cal, used_y, closed


def _write_urls(obtained: list[str], append: bool) -> tuple[Path, list[str]]:
    """把本轮链接落盘；**已总结过的链接在复制出来后立刻丢掉**。

    2026-11-09 用户拍板：复制链接后就应该比对台账，处理过的文章不该继续
    存起来、更不该再走后面的取页面 / 总结链路。这里在写盘前用 `Seen` 过滤，
    `--append` 时也把历史文件里残留的同类链接一并收敛掉。

    只过滤**落盘内容**，不动 `obtained`：跨屏去重、完备性台账、连击判定都
    依赖原始列表，把它过滤掉反而会让同一张老卡在下一屏被反复点开。
    `scan` 里的台账过滤继续保留做双保险（还有 offered 那类要按 run_key 判）。
    """
    out = project_path("out", "_raw_urls.json")
    prev = load_json(out, {}) if append else {}
    prev_urls = (prev.get("urls") or []) if isinstance(prev, dict) else []
    # 一屏放不下 20 篇时会分多轮抓，--append 把各屏链接合并去重
    merged = list(dict.fromkeys(list(prev_urls) + obtained))
    seen = Seen()
    merged = [u for u in merged if not seen.has(u)]
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


def _screen_texts(cfg: dict, shot: str):
    """读一屏 OCR 文字。窗口判定与**广告卡拦截**共用同一套识别能力。

    不缓存：每屏截图都会被覆盖成同一个 /tmp 路径，按路径缓存会把上一屏
    的识别结果串到这一屏，广告标签 / 时间戳全部对错卡片。
    """
    from . import ocr
    ui = cfg["ui"]
    return ocr.recognize(shot,
                         logical_width=ui["logical_width"],
                         logical_height=ui["logical_height"])


def _crossed_day(cfg: dict, shot: str, cards, stamp_kw: dict, *,
                 verbose: bool = True, warned: list | None = None) -> bool:
    """这一屏里有没有「早于抓取窗口」的卡片推送时间。

    窗口 = 含今天往前 `feed_days` 个自然日（默认 2）；`days_ago >= feed_days`
    才算滚出窗口。识别不出来时返回 False（当作还在窗口内）——宁可多滚一屏、
    多抓几篇窗口外的，也不能因为一次 OCR 误识就提前收工、把窗口内的文章
    漏在下面。真正精确的过滤在 scan 层：用 `published_at` 逐篇比对。
    """
    from . import feed_time, ocr
    window_days = max(1, int(cfg.get("feed_days",
                                     feed_time.DEFAULT_WINDOW_DAYS)))
    try:
        texts = _screen_texts(cfg, shot)
    except ocr.OcrError as exc:
        if verbose and not (warned and warned[0]):
            print(f"  ! 认不出推送时间（{exc}），本轮不判窗口，继续往下滚",
                  flush=True)
            if warned is not None:
                warned[0] = True
        return False
    stamps = feed_time.screen_stamps(texts, cards, **stamp_kw)
    if verbose:
        marks = [st.raw for _, st in stamps if st is not None]
        if marks:
            print("  · 本屏推送时间：" + " / ".join(marks), flush=True)
    cross = feed_time.screen_crossed(texts, cards,
                                    window_days=window_days, **stamp_kw)
    if cross.crossed and verbose:
        print(f"  · {cross.reason}", flush=True)
    return cross.crossed


def _open_and_copy_retry(cfg: dict, x: float, y: float, used_centers, *,
                         label: str = ""):
    """标题带守卫拒点时重截图再试 1~2 次，别让一次截图时机不对就漏一张卡。

    只重试**界面异常**（_CardFailed）。内容问题（_CardSkipped：广告卡、
    文章被删）已经关过窗，重试只会重复点击，原样抛出。
    """
    tries = max(0, int(cfg["ui"].get("title_retry", 2)))
    wait = float(cfg["ui"].get("title_retry_wait", 0.5))
    for attempt in range(tries + 1):
        try:
            return _open_and_copy(cfg, x, y, used_centers, label=label)
        except _CardSkipped:
            raise
        except _CardFailed:
            if attempt >= tries:
                raise
            _sleep(wait)
    raise AssertionError("unreachable")


def _overlap_with(cards, prev_fps: list[str], tol: int,
                  *, last_fp: str = "", max_y: float = 420) -> bool:
    """这一屏和上一屏是否**真的**有重叠（= 滚动没跨过整屏）。

    旧判据「任意一张卡对上就算」太松：跨掉中间几张、只剩一张碰巧相似
    时照样通过。收紧为：

    1. 优先要求**上一屏最后一张**出现在本屏前部（向下滚后它该在上面）；
    2. 退一步，至少 2 张卡与上屏匹配才算（容忍 OCR 抖动）；
    3. 都不满足 -> False，调用方强制回滚半屏补扫。
    """
    if not prev_fps:
        return True
    hits = [c for c in cards if getattr(c, "fp", "")
            and any(feed_vision.fps_close(c.fp, p, tol) for p in prev_fps)]
    if last_fp:
        tail = [c for c in hits
                if feed_vision.fps_close(c.fp, last_fp, tol)]
        if tail and min(c.y for c in tail) <= max_y:
            return True
    return len(hits) >= 2


def _scroll_back_half(cfg: dict) -> None:
    """向上回滚约半屏，把「一步跨过整屏」漏掉的中间卡片找回来。"""
    ui = cfg["ui"]
    amount = abs(int(ui.get("scroll_amount", -12)))
    back = max(1, amount // 2)
    _focus(cfg)
    gui.scroll(ui.get("scroll_x", 700), ui.get("scroll_y", 650), back)
    _sleep(ui.get("scroll_wait", 1.4))


_SHIFT_PREV = Path("/tmp/wd_shift_prev.png")
_SHIFT_CUR = Path("/tmp/wd_shift_cur.png")


def _target_step(cards, ui: dict) -> float:
    """单次滚动的目标位移：2 倍中位卡片间距，且不超过 scroll_max_step_px。

    实测卡片间距 126~145px，2 倍约 265px，能保证相邻两屏至少还有 2 张卡
    重叠；上限 320px 防止微信把「1 行」放大后一步跨过整屏。
    """
    cap = float(ui.get("scroll_max_step_px", 320))
    ys = sorted(float(getattr(c, "y", 0)) for c in cards)
    gaps = sorted(b - a for a, b in zip(ys, ys[1:]) if b > a)
    if not gaps:
        return cap
    median = gaps[len(gaps) // 2]
    return max(80.0, min(2.0 * median, cap))


def _scroll_with_measure(cfg: dict, cards, shot: str,
                         *, verbose: bool = True) -> dict:
    """滚一屏并**实测位移**，超出目标就向上微调，返回本次滚动的可观测信息。

    以前只发「行数」、从不测像素；现在用 `feed_vision.measure_shift` 比较
    滚动前后的标题列 profile。测量失败（截图缺失 / 内容整体变了）不阻断
    流程，只把 `risky=True` 记进台账，下一屏的收紧重叠判据会触发补扫。
    """
    import shutil
    from . import nav
    ui = cfg["ui"]
    x = ui.get("scroll_x", 700)
    y = ui.get("scroll_y", 650)
    amount = int(ui.get("scroll_amount", -12))
    target = _target_step(cards, ui)
    logical_h = int(ui.get("logical_height", 832))
    gui.scroll(x, y, amount)
    _sleep(ui.get("scroll_wait", 1.4))
    info: dict = {"shift": None, "target": round(target, 1),
                  "adjusted": 0, "risky": True}
    try:
        shutil.copyfile(shot, _SHIFT_PREV)
        cur = nav._shot(cfg, str(_SHIFT_CUR))
        shift = feed_vision.measure_shift(_SHIFT_PREV, cur,
                                          logical_height=logical_h)
        if shift is None:
            return info
        for _ in range(max(0, int(ui.get("scroll_shift_retries", 3)))):
            if shift <= target:
                break
            gui.scroll(x, y, max(1, abs(amount) // 2))
            _sleep(ui.get("scroll_wait", 1.4))
            cur = nav._shot(cfg, str(_SHIFT_CUR))
            new_shift = feed_vision.measure_shift(_SHIFT_PREV, cur,
                                                  logical_height=logical_h)
            if new_shift is None:
                break
            shift = new_shift
            info["adjusted"] += 1
        info["shift"] = shift
        info["risky"] = bool(shift > target)
        if verbose and info["risky"]:
            print(f"  ! 实测位移 {shift}px 大于目标 {target:.0f}px，"
                  "下一屏会强制补扫", flush=True)
    except Exception as exc:            # 测量是增强，绝不能让抓取崩掉
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def grab_auto(cfg: dict, limit: int, *, dry_run: bool = False,
              append: bool = False, max_rounds: int | None = None,
              verbose: bool = True, resume: bool = False,
              no_gate: bool = False) -> dict:
    """全自动抓取：自动进信息流 → 一屏一屏「从上到下」点开 → 不足则滚动。

    完备性铁律（2026-11-09 起，见 docs/PRD-抓取完备性.md）
    ------------------------------------------------------
    * **跳过必须有内容级凭证**：像素指纹只在**同一屏内**防重复吸附；
      跨屏一律重新点开，用拿到的 URL 去重。旧的全局 `seen_fps` 会把两篇
      不同的短标题误判成同一篇（实测汉明距离 3 <= 容差 4），直接永久漏。
    * **不因一屏没新货就收工**：只有 OCR 判到窗口边界 / 篇数上限 /
      轮数上限才停；连续 `stalled_rounds` 屏无新增才判「到底」。
    * **跨过整屏要补**：`_overlap_with` 为假时先向上回滚半屏重新截图，
      换回一屏有重叠的卡片再处理，不再只打印一行警告。
    * **每张卡都有下落**：linked / duplicate / no_link / deleted / error
      全部记进 `screens`；`error` 是缺口，写进 `missing`。
    * **结束要对账**：写 `out/_grab_ledger-<date>.json` +
      `_grab_missing-<date>.json`，按 `grab_gate_policy=block` 在有缺口时
      写 `out/_plan/grab-gate-<date>.json` 拦住 scan，K=0 才算通过。

    `--resume` 的取舍：GUI 滚动位置不可复现（微信会往信息流顶部插新文章），
    按「台账里的屏号 + 位移」精确回到旧位置并不比从头重抓更可靠。所以
    resume = 带着上轮缺口清单**从头重抓一遍**，重新对账；K=0 才解除闸门。
    多花几分钟，换来的是确定性。
    """
    preflight(cfg)
    from . import nav                       # 延迟导入，避免与 nav 循环 import

    ui = cfg["ui"]
    if dry_run:
        return {"dry_run": True, "mode": "auto", "limit": limit,
                "would": "导航到公众号信息流后逐张点开、复制链接、滚动翻页"}

    # 「最近 N 天全部」模式（feed_scope=1）：不再卡篇数，改成滚到窗口外为止。
    # feed_scope=0 时退回老行为：抓到 limit 篇就收手。
    feed_scope = int(cfg.get("feed_scope", 1))
    feed_days = max(1, int(cfg.get("feed_days", 2)))
    stamp_box = ui.get("feed_stamp_x") or [780.0, 900.0]
    stamp_kw = {
        "x0": float(stamp_box[0]), "x1": float(stamp_box[1]),
        "up": float(ui.get("feed_stamp_up", 52)),
        "down": float(ui.get("feed_stamp_down", 4)),
    }
    ocr_warned = [False]
    from . import grab_ledger                  # 纯数据模块，延迟导入即可
    date = grab_ledger.today()
    prior_missing = grab_ledger.read_missing(date) if resume else []
    if verbose and prior_missing:
        print(f"· --resume：带着上轮 {len(prior_missing)} 处缺口从头重抓，"
              "重新对账后 K=0 才解除闸门", flush=True)

    nav.to_feed(cfg, verbose=verbose)
    obtained: list[str] = []
    skipped: list[dict] = []
    missing: list[dict] = []
    screens: list[dict] = []
    rounds = int(ui.get("auto_rounds", 40) if max_rounds is None else max_rounds)
    streak_limit = max(1, int(ui.get("auto_fail_streak", 2)))
    skip_limit = max(1, int(ui.get("auto_skip_streak", 4)))
    stall_limit = max(1, int(ui.get("stalled_rounds", 3)))
    fp_tol = int(ui.get("card_fp_max_diff", 4))
    stalled = 0
    prev_fps: list[str] = []
    prev_last_fp = ""
    crossed = False
    rounds_done = 0
    stop_reason = "round_limit"

    for round_no in range(1, rounds + 1):
        rounds_done = round_no
        if len(obtained) >= limit:
            stop_reason = "limit"
            break
        # **必须每屏重新截图**：read_cards 默认读 /tmp/wd_nav.png，那是上次
        # to_feed 留下的图。滚动后不刷新就会一直按上一屏的坐标去点。
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
            stop_reason = "no_cards"
            break

        # 跨屏重叠：旧版只打印警告就继续往下滚，中间那几张永远补不回来。
        # 现在先向上回滚半屏、重新截图，换一屏有重叠的卡片再处理。
        overlapped = _overlap_with(cards, prev_fps, fp_tol,
                                   last_fp=prev_last_fp)
        if not overlapped:
            if verbose:
                print("  ! 本屏与上屏无重叠：回滚半屏补扫，避免跨过中间卡片",
                      flush=True)
            _scroll_back_half(cfg)
            shot = nav._shot(cfg)
            back_cards = nav.read_cards(cfg, shot)
            if back_cards:
                cards = back_cards
                overlapped = _overlap_with(cards, prev_fps, fp_tol,
                                           last_fp=prev_last_fp)
        if verbose:
            print(f"· 第 {round_no} 屏：{len(cards)} 张卡片"
                  + ("" if overlapped else "（补扫后仍无重叠）"), flush=True)

        # 广告卡拦截 —— 铁律：**绝对不点**，且必须排在任何点击之前。
        # 点广告卡会直接跳默认浏览器、把微信窗口从屏幕上弄丢，之后每张
        # 卡片的标题带守卫都会在浏览器/桌面的截图里拒点，整轮报废。
        # 不受 feed_scope 影响：按篇数模式同样不许点广告。
        from . import feed_time
        try:
            texts = _screen_texts(cfg, shot)
        except Exception as exc:
            # 认不出字就没法排除广告。宁可停轮，也绝不带着未知卡片盲点。
            _write_urls(obtained, append)
            raise GrabAborted(
                "本屏文字识别失败，无法在点击前排除广告卡 —— 按「广告卡绝对不点」"
                "停下等人介入，不盲点。\n"
                f"  原因：{type(exc).__name__}: {exc}\n"
                f"  已取到 {len(obtained)} 条链接，已写入 "
                f"{project_path('out', '_raw_urls.json')}（未写台账）")
        ad_marks = feed_time.find_ad_labels(texts, cards, **stamp_kw)
        if ad_marks and verbose:
            print(f"  · 广告卡拦截：{len(ad_marks)} 张（标签 "
                  + " / ".join(sorted(set(ad_marks.values())))
                  + "），按铁律不点、直接跳过", flush=True)

        # 窗口判定：这一屏里有没有「早于最近 feed_days 天」的卡片推送时间。
        crossed = False
        if feed_scope:
            crossed = _crossed_day(cfg, shot, cards, stamp_kw,
                                   verbose=verbose, warned=ocr_warned)
        used: list[float] = []
        screen_results: list[dict] = []
        screen_missing = 0
        new_links = 0
        fail_streak = 0                  # 连跳按屏重置：跨屏会重新点开
        skip_streak = 0
        for card in cards:
            if len(obtained) >= limit:
                break
            # 广告卡：在点击循环的第一道门就拦掉，**绝不调用 _open_and_copy**。
            # 不是「点开之后发现没链接再跳过」——点开这一步本身就已经
            # 把界面甩进浏览器了。记进台账但不算缺口、不计连跳、不点。
            if card.y in ad_marks:
                used.append(card.y)
                skipped.append({"x": round(card.x, 1), "y": round(card.y, 1),
                                "kind": "ad",
                                "reason": "广告卡（OCR 标签 "
                                          f"{ad_marks[card.y]!r}），按铁律不点"})
                screen_results.append({"y": round(card.y, 1),
                                       "outcome": "ad_skipped",
                                       "label": ad_marks[card.y]})
                if verbose:
                    print(f"  · 跳过广告卡（y≈{card.y:.0f}）："
                          f"{ad_marks[card.y]!r} —— 绝对不点", flush=True)
                continue
            # 同屏去重点**只按 y 距离**：同一张卡的两行标题间距 <=30px。
            # 绝不能用指纹相似度 —— 实测「美股何时抄底？」和「忐忑，等周一」
            # 这两篇不同文章的指纹汉明距离只有 3（<= 容差 4），旧版就是靠
            # 它判「已点过」直接 continue，造成永久漏抓。跨屏更是一律重点，
            # 用拿到的 URL 去重。
            if any(abs(card.y - u) <= 30 for u in used):
                screen_results.append({"y": round(card.y, 1),
                                       "outcome": "duplicate_screen"})
                continue
            label = f"第 {len(obtained) + len(skipped) + 1} 张："
            try:
                link, cal, used_y, closed = _open_and_copy_retry(
                    cfg, card.x, card.y, used, label=label)
            except _CardSkipped as exc:
                # 内容问题：文章被删 / 广告卡没有「复制链接」。关窗换下一张，
                # 不计入 fail_streak；但连续太多仍停（菜单结构整体失效）。
                skip_streak += 1
                skipped.append({"x": round(card.x, 1), "y": round(card.y, 1),
                                "kind": exc.kind, "reason": exc.reason})
                screen_results.append({"y": round(card.y, 1),
                                       "outcome": ("deleted" if exc.kind == "deleted"
                                                   else "no_link"),
                                       "reason": exc.reason})
                if exc.used_y is not None:
                    used.append(exc.used_y)
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
                # 界面异常 = 本该拿到却没拿到，算**缺口**，交给 P1 补扫/闸门。
                fail_streak += 1
                screen_missing += 1
                skipped.append({"x": round(card.x, 1), "y": round(card.y, 1),
                                "kind": "error", "reason": exc.reason})
                screen_results.append({"y": round(card.y, 1),
                                       "outcome": "error",
                                       "reason": exc.reason})
                missing.append({"round": round_no, "x": round(card.x, 1),
                                "y": round(card.y, 1), "fp": card.fp,
                                "reason": exc.reason, "retry_count": 0})
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
            if link not in obtained:
                obtained.append(link)
                new_links += 1
                screen_results.append({"y": round(card.y, 1),
                                       "outcome": "linked", "url": link})
                if verbose:
                    print(f"  · 第 {len(obtained)} 篇 {link}", flush=True)
            else:
                screen_results.append({"y": round(card.y, 1),
                                       "outcome": "duplicate", "url": link})
            if not closed:
                # 链接已经拿到，只是界面没回到信息流。恢复一次；恢复不了才停。
                if not nav.recover(cfg):
                    _write_urls(obtained, append)
                    raise GrabAborted(
                        "文章窗口关不掉、也回不到信息流。\n"
                        f"  已取到 {len(obtained)} 条链接，已写入 "
                        f"{project_path('out', '_raw_urls.json')}（未写台账）")

        screens.append({
            "round": round_no,
            "cards": [{"x": round(c.x, 1), "y": round(c.y, 1),
                       "fp": getattr(c, "fp", ""),
                       "peak": getattr(c, "peak", 0)} for c in cards],
            "results": screen_results,
            "overlap_ok": overlapped,
            "new_urls": new_links,
            "missing": screen_missing,
        })
        prev_fps = [c.fp for c in cards if getattr(c, "fp", "")]
        prev_last_fp = (getattr(cards[-1], "fp", "") or "") if cards else ""

        if len(obtained) >= limit:
            stop_reason = "limit"
            break
        if crossed:
            # 已经滚出窗口：再往下滚只有更早的内容，窗口内的已经抓完。
            if verbose:
                print(f"· 已滚出最近 {feed_days} 天窗口，停止滚动", flush=True)
            stop_reason = "window_edge"
            break
        # **不再一屏没新货就收工**。只有连续 stall_limit 屏既没有新链接、
        # 也没有待补缺口，才认为真的到底了。
        if new_links == 0 and screen_missing == 0:
            stalled += 1
            if stalled >= stall_limit:
                stop_reason = "stalled"
                if verbose:
                    print(f"· 连续 {stalled} 屏没有新链接，判到底停止", flush=True)
                break
        else:
            stalled = 0
        _focus(cfg)
        screens[-1]["scroll"] = _scroll_with_measure(
            cfg, cards, shot, verbose=verbose)

    out, merged = _write_urls(obtained, append)
    policy = str(cfg.get("grab_gate_policy") or "block").lower()
    info = _persist_ledger(date, screens, missing, stop_reason=stop_reason,
                           feed_days=feed_days, resume=resume, policy=policy,
                           no_gate=no_gate, verbose=verbose)
    return {"mode": "auto", "count": len(merged), "new_this_round": len(obtained),
            "skipped": skipped, "missing": missing, "screens": screens,
            "stop_reason": stop_reason, **info,
            "file": str(out), "urls": merged,
            "rounds": rounds_done, "crossed_day": crossed,
            "feed_scope": feed_scope, "feed_days": feed_days}


def _persist_ledger(date: str, screens: list[dict], missing: list[dict], *,
                    stop_reason: str, feed_days: int, resume: bool,
                    policy: str, no_gate: bool, verbose: bool = True) -> dict:
    """落盘台账 / 缺口清单 / 闸门。单独抽出来，单测可整体 mock 掉避免污染 out/。"""
    from . import grab_ledger
    coverage = grab_ledger.compute_coverage(screens)
    ledger = grab_ledger.write_ledger(
        date, screens, coverage, stop_reason=stop_reason,
        feed_days=feed_days, resume=resume)
    missing_file = grab_ledger.write_missing(date, missing)
    gate = None
    if policy == "block" and coverage["missing"] > 0 and not no_gate:
        gate = grab_ledger.write_gate(date, coverage)
    elif resume and coverage["missing"] == 0:
        if grab_ledger.clear_gate(date) and verbose:
            print("· 缺口已补齐（K=0），完备性闸门已解除", flush=True)
    if verbose:
        print(grab_ledger.summary_line(coverage), flush=True)
        if coverage["missing"]:
            print(f"  ! {coverage['missing']} 篇没拿到，缺口清单："
                  f"{missing_file}", flush=True)
        if gate:
            print("  ⛔ 完备性闸门已阻断 scan：先跑 grab --resume 补齐，"
                  "或单轮 scan --ignore-grab-gate 绕过（会留痕）", flush=True)
    return {"coverage": coverage, "ledger": str(ledger),
            "missing_file": str(missing_file), "gate": bool(gate),
            "gate_policy": policy}


def _sleep(seconds) -> None:
    import time
    time.sleep(float(seconds))
