#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动导航：把微信切回「公众号」信息流，并读出一屏文章卡片的可点位置。

为什么要它
----------
抓取前必须停在「常看的号 + 一张张文章卡片」那一屏。以前这一步靠读图给
坐标、再让人手动点回去，结果：列表动态排序，盲点会误开「我」聊天窗、
或点开一个空白的文章标签页，整轮抓取卡住。

这里改成**纯像素**的三步闭环，不需要 OCR、不需要辅助功能权限：

1. 截图 → 找左侧那个实心蓝色方块（「公众号」入口，约 36×36，实测整屏
   唯一的 30px 以上纯蓝色块），点它；
2. 再截图 → 用 `feed_vision.find_card_titles` 数标题：真标题峰值 100+、
   卡片间距约 128px，一屏至少 2 张才算进了信息流；
3. 没进去就重试（图标会随列表重排，每轮都重新定位），超过次数报错退出
   码 3，绝不无限点击。

任何一步失败都**不猜测、不盲点**：找不到蓝色图标就明确说「微信窗口可能
不在屏幕上或被遮挡」。导航本身不写台账、不取正文、不算任何授权。
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from . import feed_vision, gui, winpos

# 注意：`grab` 会在函数内部反过来 import 本模块（自动抓取要用导航），
# 所以这里不在模块顶层 import grab，避免循环导入。

_NAV_SHOT = Path("/tmp/wd_nav.png")


def _sleep(seconds: float) -> None:
    time.sleep(float(seconds))


def _fail(message: str):
    """构造需要人介入的异常（延迟导入，避免与 grab 循环 import）。"""
    from .grab import GrabAborted
    return GrabAborted(message)


def _preflight(cfg: dict) -> None:
    from .grab import preflight
    preflight(cfg)


def ensure_focus(cfg: dict, *, verbose: bool = False) -> dict:
    """轻量版 ensure_front：每次点击前调一次，快路径几乎零成本。

    本机实测：WorkBuddy 自己的窗口会在每次工具调用后自动弹到最前，把微信
    盖住。抓取过程中任何一次点击赶上这个时机就会点空（剪贴板为空）。
    所以「点之前先确认微信在前」必须做成每次点击的固定动作。

    Space（屏幕）检查在这里走 **light 模式**：只确认窗口存在，不截图。
    抓取中途文章浮窗开着是常态，完整版会误判「微信不在屏」；
    Space 真不对时点击会落空，由点后验证发现并在恢复路径跑完整版。
    """
    return ensure_front(cfg, verbose=verbose, light=True)


def article_window_open(cfg: dict) -> bool | None:
    """最前面那个微信窗口是不是「文章窗口」（而不是主窗口）。

    Cmd+W 打给谁就关谁：如果在主窗口上按，会**把主窗口关掉**（微信整个
    隐藏，之后所有截图都拍不到它，只能手动点菜单栏捞回来）。所以关窗
    之前必须先确认前面那个确实是文章窗口。
    """
    from . import winpos
    ws = winpos.windows()
    if ws is None:
        return None
    front = winpos.front_wechat(ws)
    main = winpos.find_wechat(ws)
    if front is None or main is None:
        return None
    return front["id"] != main["id"]


def close_article(cfg: dict, *, tries: int = 2) -> bool:
    """关掉前面的文章窗口；前面不是文章窗口就什么都不做。

    **只对独立浮窗发 Cmd+W**。2026-11-08 22:5x 实测：微信最大化/全屏时，
    文章开在主窗口的标签页里，此时 Cmd+W 的语义是**关掉整个微信窗口**
    （进程还在、窗口消失，只能 `open -a WeChat` 捞回来），不是关标签。
    `article_window_open` 在标签页形态下恒返回 False，这里正好直接 return，
    不会盲按。所以这个形态必须靠「退出全屏」解决，不能靠快捷键。
    """
    ui = cfg["ui"]
    for _ in range(max(1, tries)):
        state = article_window_open(cfg)
        if state is False:                 # 已经回到主窗口，不用关
            return True
        if state is None:
            # 拿不到窗口信息 = 不知道快捷键会打给谁。**绝不能盲按**：
            # 2026-11-08 实测两次，盲按 Cmd+W 都关掉了微信主窗口。宁可不关，
            # 让调用方按「关不掉」处理（文章链接通常已经拿到了）。
            return False
        ensure_focus(cfg, verbose=False)
        gui.cmd_key(13)                    # 13 = kVK_ANSI_W
        _sleep(ui.get("close_wait", 1.8))
    return article_window_open(cfg) is False


def _shot(cfg: dict, path: Path | str = _NAV_SHOT) -> str:
    ui = cfg["ui"]
    ensure_focus(cfg)
    gui.screenshot(path, ui["logical_width"], ui["logical_height"])
    return str(path)


def read_cards(cfg: dict, image_path: str | Path = _NAV_SHOT) -> list[feed_vision.CardTitle]:
    """读一屏文章卡片的可点位置（逻辑坐标，含横向文字范围）。"""
    ui = cfg["ui"]
    return feed_vision.find_card_titles(
        image_path,
        logical_width=ui["logical_width"],
        logical_height=ui["logical_height"],
        threshold=ui.get("title_dark_threshold", 120),
        min_dark=ui.get("title_min_dark", 10),
        min_logical_height=ui.get("title_min_height", 6),
        max_logical_height=ui.get("title_max_height", 22),
        min_peak=ui.get("card_min_peak", 55),
        min_y=ui.get("card_min_y", 110),
        weak_min_peak=ui.get("card_weak_min_peak", 26),
        weak_min_gap=ui.get("card_weak_min_gap", 90),
        left_align_tol=ui.get("card_left_align_tol", 14),
        dynamic_weak_gap=bool(ui.get("card_dynamic_weak_gap", 0)),
    )


def wait_for_cards(cfg: dict, *, timeout: float | None = None,
                   poll: float | None = None
                   ) -> tuple[str, list[feed_vision.CardTitle]]:
    """反复截图直到信息流出现至少 2 张卡片，或超时。

    以前是「点完死等 2.2 秒再判断」，公众号列表有时加载更慢，于是刚点进去
    还没渲染就被判成「没进去」，白白重试到放弃。改成轮询：能进就立刻返回，
    真进不去也只在 timeout 之后才失败。
    """
    ui = cfg["ui"]
    timeout = float(ui.get("nav_load_timeout", 8.0) if timeout is None else timeout)
    poll = float(ui.get("nav_load_poll", 0.8) if poll is None else poll)
    deadline = time.time() + timeout
    shot = _shot(cfg)
    cards = read_cards(cfg, shot)
    while len(cards) < 2 and time.time() < deadline:
        _sleep(poll)
        shot = _shot(cfg)
        cards = read_cards(cfg, shot)
    return shot, cards


def read_icons(cfg: dict, image_path: str | Path = _NAV_SHOT) -> list[feed_vision.IconBlock]:
    """读左侧会话列表里的实心蓝色方块（「公众号」入口候选）。"""
    ui = cfg["ui"]
    return feed_vision.find_icon_blocks(
        image_path,
        logical_width=ui["logical_width"],
        logical_height=ui["logical_height"],
    )


def looks_like_account_list(cards: list[feed_vision.CardTitle]) -> bool:
    """当前是不是「号列表」形态：一行一个号、只显示每号最新一篇。

    背景（2026-11-07 事故）：抓取中点中卡片非标题区域会打开公众号主页 /
    号详情，把「公众号」聚合页切成号列表形态——标题行密（间距 ~55px）、
    一屏 8~10 行。这种形态下点标题**不开文章**（只是选中该号），抓取必然
    全部失败。卡片流的大卡片间距 ≥90px、一屏最多 4~5 张，用「行数多 +
    间距小」即可区分。

    2026-11-08 起这个形态**不再只是错误**：「按号抓取」就是主动进这形态
    （见 `account.py`）。所以它退化成「形态识别」的一道判据，是否算错由
    调用方决定（`to_feed` 要卡片流、`to_list` 要号列表）。
    """
    if len(cards) < 6:
        return False
    gaps = sorted(b.y - a.y for a, b in zip(cards, cards[1:])
                  if b.y - a.y > 0)
    if not gaps:
        return False
    return gaps[len(gaps) // 2] <= 70


def _dark_count(path: str | Path, box: tuple[float, float, float, float],
                threshold: int = 175) -> int:
    """截图里某个矩形内的暗像素数（逻辑坐标）。识别不了就返回 0。"""
    x0, y0, x1, y1 = box
    try:
        img = feed_vision.decode_png(path)
    except Exception:                                  # noqa: BLE001
        return 0
    sx = img.w / 1280.0 if img.w else 1.0        # 截图是 1280 宽的缩放图
    sy = img.h / 832.0 if img.h else 1.0
    n = 0
    for y in range(max(0, int(y0 / sy)), min(img.h, int(y1 / sy))):
        row = img.rows[y]
        for x in range(max(0, int(x0 / sx)), min(img.w, int(x1 / sx))):
            i = x * img.ch
            if img.ch >= 3:
                if max(row[i], row[i + 1], row[i + 2]) < threshold:
                    n += 1
            elif row[i] < threshold:
                n += 1
    return n


def _toggle_box(cfg: dict, which: str) -> tuple[float, float, float, float]:
    x, y = cfg["ui"][f"account_toggle_to_{which}"]
    return (x - 12, y - 10, x + 12, y + 10)


def _nonwhite_ratio(path: str | Path,
                    box: tuple[float, float, float, float],
                    threshold: int = 245) -> float:
    """截图某个矩形内「非纯白」像素的占比。识别不了返回 0.0（不冒充结论）。"""
    x0, y0, x1, y1 = box
    try:
        img = feed_vision.decode_png(path)
    except Exception:                                  # noqa: BLE001
        return 0.0
    sx = img.w / 1280.0 if img.w else 1.0
    sy = img.h / 832.0 if img.h else 1.0
    tot = hit = 0
    for y in range(max(0, int(y0 / sy)), min(img.h, int(y1 / sy))):
        row = img.rows[y]
        for x in range(max(0, int(x0 / sx)), min(img.w, int(x1 / sx))):
            i = x * img.ch
            tot += 1
            if img.ch >= 3:
                if min(row[i], row[i + 1], row[i + 2]) < threshold:
                    hit += 1
            elif row[i] < threshold:
                hit += 1
    return hit / tot if tot else 0.0


def left_pane_present(cfg: dict,
                      image_path: str | Path | None = None) -> bool:
    """左栏（一列公众号）在不在 —— 这是**列表模式唯一干净的结构判据**。

    取左右栏分界线附近那一条竖带（x≈619，y 150~700）：列表模式下它整条
    都被左栏底色/分割线盖住，非白占比 **98.8%~100%**；卡片流是 17%~32%，
    文章页 30% —— 三者差得很开，阈值 0.90 没有灰区。

    为什么不用「中栏 (593,60) 有暗像素」当判据（早先的写法）：文章页顶部
    被 Chromium 的**标签栏**占据，那个位置上正好是标签文字，暗像素够多，
    于是文章页会被误判成列表模式 —— 这正是第一次实跑「点了标题却判定没
    打开文章、重试后卡住」的原因。左栏存不存在不受标签栏影响。
    """
    path = image_path or _shot(cfg)
    ui = cfg["ui"]
    x_edge = float(ui["account_list_x"][1])          # 618：左栏右边界
    box = (x_edge - 2, 150.0, x_edge + 5, 700.0)
    need = float(ui.get("account_left_ratio", 0.90))
    return _nonwhite_ratio(path, box) >= need


def page_state(cfg: dict, image_path: str | Path | None = None) -> dict:
    """当前是「公众号」页的哪种形态。纯像素，不碰 OCR。

    判据（2026-11-08 二次实测）：

    * **列表模式**：左栏（一列公众号）存在，见 `left_pane_present()`：
      分界线那一竖带非白占比 98.8%~100%，卡片流 17%~32%、文章页 30%。
      早先用的「中栏 (593,60) 暗像素」在**文章页**会被标签栏文字骗到，
      已废弃——它让文章页被当成列表模式，进而点不开下一行。
    * **卡片模式**：没有左栏，但能数到 ≥2 张卡片且不是号列表形态。
    * 右上角 (1250,60) **不能**当判据：列表模式是「公众号主页」人像图标、
      卡片模式是「切列表」图标，两边都有暗像素（实测 101 / 128）。
    """
    path = image_path or _shot(cfg)
    cards = read_cards(cfg, path)
    if left_pane_present(cfg, path):
        return {"mode": "list", "cards": cards, "image": str(path),
                "left_pane": True}
    if len(cards) >= 2 and not looks_like_account_list(cards):
        return {"mode": "card", "cards": cards, "image": str(path),
                "left_pane": False}
    if looks_like_account_list(cards):
        return {"mode": "list", "cards": cards, "image": str(path),
                "left_pane": False}
    return {"mode": "unknown", "cards": cards, "image": str(path),
            "left_pane": False}


def switch_mode(cfg: dict, want: str, *, verbose: bool = True) -> str:
    """在「公众号」页上把卡片流 / 号列表互切，返回切换后的形态。

    **两个按钮位置不一样，别混用**：
      切列表 = 右上角 `account_toggle_to_list`；
      切卡片 = 中栏表头 `account_toggle_to_card`。
    列表模式下右上角那个位置是「公众号主页」人像图标，点错会开主页。

    切完必须复验；三次都切不过去就报错，不硬来。
    """
    ui = cfg["ui"]
    wait = float(ui.get("mode_wait", 1.8))
    for attempt in range(1, 4):
        state = page_state(cfg)
        if state["mode"] == want:
            return want
        if state["mode"] == "unknown":
            raise _fail(
                "当前不在「公众号」页面（既看不到卡片流，也看不到号列表），"
                "没法切形态。请先把微信切到「公众号」页再重跑。")
        point = tuple(ui["account_toggle_to_list"] if want == "list"
                      else ui["account_toggle_to_card"])
        if verbose:
            print(f"· 切到{'列表' if want == 'list' else '卡片'}模式"
                  f"（第 {attempt} 次，点 {point[0]:.0f},{point[1]:.0f}）",
                  flush=True)
        gui.click(*point)
        _sleep(wait)
    state = page_state(cfg)
    if state["mode"] == want:
        return want
    raise _fail(
        f"连续 3 次都没能切到{'列表' if want == 'list' else '卡片'}模式"
        f"（现在识别为 {state['mode']}）。\n"
        f"  现场截图：{state['image']}\n"
        "  多半是微信界面版本变了或窗口几何变了，请手工切一次再重跑。")


def to_list(cfg: dict, *, verbose: bool = True) -> dict:
    """确保停在「公众号」页的**列表模式**（一行一个号）。

    这是「按号抓取」的入口形态。返回 {"mode": "list", "cards": [...]}。
    """
    _preflight(cfg)
    gui.activate_wechat(0.8)
    ensure_onscreen(cfg, verbose=verbose)
    ensure_front(cfg, verbose=verbose)
    close_article(cfg)                 # 残留文章标签页会盖住整个页面
    state = page_state(cfg)
    if state["mode"] == "unknown":
        # 不在公众号页：先走一次信息流导航（它会点左侧「公众号」入口）
        to_feed(cfg, verbose=verbose)
        state = page_state(cfg)
    if state["mode"] != "list":
        switch_mode(cfg, "list", verbose=verbose)
        state = page_state(cfg)
    if verbose:
        print("· 已停在公众号列表模式", flush=True)
    return {"mode": "list", "clicks": 0,
            "cards": [c.as_dict() for c in state["cards"]]}


def to_card(cfg: dict, *, verbose: bool = True) -> dict:
    """确保停在卡片流形态（信息流抓取要它，跑完按号抓取也要切回来）。"""
    if page_state(cfg)["mode"] == "list":
        switch_mode(cfg, "card", verbose=verbose)
    state = page_state(cfg)
    return {"mode": state["mode"],
            "cards": [c.as_dict() for c in state["cards"]]}


def _feed_state(cfg: dict, shot) -> tuple[list[feed_vision.CardTitle], bool]:
    """读一屏卡片，返回 (卡片, 是否为可信的卡片流形态)。"""
    cards = read_cards(cfg, shot)
    return cards, len(cards) >= 2 and not looks_like_account_list(cards)


def status(cfg: dict) -> dict:
    """当前是否在信息流、是否能看到「公众号」入口。只截图，不点击。"""
    _preflight(cfg)
    gui.activate_wechat(0.6)
    shot = _shot(cfg)
    cards = read_cards(cfg, shot)
    icons = read_icons(cfg, shot)
    return {
        "image": shot,
        "in_feed": len(cards) >= 2,
        "cards": [c.as_dict() for c in cards],
        "icons": [i.as_dict() for i in icons[:3]],
    }


def wechat_main_window() -> dict | None:
    """找微信主窗口（**含离屏窗口在内**，>=600 宽的 layer 0 窗口）。

    关键在于必须查 `include_offscreen=True`：微信主窗口留在别的 Space
    （桌面）时，普通查询里它根本不出现，看起来就像「窗口被关了」。
    """
    from . import winpos
    ws = winpos.windows(include_offscreen=True)
    if ws is None:
        return None
    for w in ws:
        if ("微信" in w["owner"] or "WeChat" in w["owner"]) \
                and w["layer"] == 0 and w["w"] >= 600 and w["h"] >= 400:
            return w
    return None


def wechat_any_window() -> dict | None:
    """微信是否还有任何可用窗口（主窗口或文章浮窗，不看 onscreen 标志）。

    全屏 Space 怪癖下标志可能恒 0，所以这里只按「窗口存在与否」判断，
    用于每次点击前的轻量检查：窗口被关掉/最小化时才需要恢复动作。
    """
    from . import winpos
    ws = winpos.windows(include_offscreen=True)
    if ws is None:
        return None
    for w in ws:
        if ("微信" in w["owner"] or "WeChat" in w["owner"]) \
                and w["layer"] in (0, 3) and w["w"] >= 500 and w["h"] >= 300:
            return w
    return None


def _wechat_ui_visible(cfg: dict) -> bool:
    """从**截图内容**判定当前屏幕上是不是微信界面。

    为什么不用 CGWindowList 的 onscreen 标志：实测微信处于**全屏 Space**
    时，主窗口明明显示在当前屏幕上，标志却恒为 0（WindowServer 对全屏
    窗口的已知怪癖），只信标志会把「好好的微信」误判成「在别的桌面」。
    这里改用两个纯像素特征：① 公众号信息流卡片标题（≥2 张）；② 左侧
    实心蓝色方块（聊天列表里的「公众号」入口，v1.8 的 find_icon_blocks）。
    聊天列表和公众号页都能命中；命中即说明当前屏就是微信。
    """
    path = _take_shot(cfg)
    try:
        if len(read_cards(cfg, path)) >= 2:
            return True
        if read_icons(cfg, path):
            return True
    except Exception:
        return False
    return False


def _take_shot(cfg: dict) -> str:
    """截一张图到临时路径（不走 ensure_focus，避免递归调用本模块检查）。"""
    ui = cfg["ui"]
    path = "/tmp/wd_onscreen.png"
    gui.screenshot(path, ui["logical_width"], ui["logical_height"])
    return path


def ensure_onscreen(cfg: dict, *, verbose: bool = True,
                    light: bool = False) -> dict:
    """确保微信界面显示在**当前屏幕（当前 Space）**上，不在就强制切回来。

    背景：微信（尤其全屏时）经常和 WorkBuddy / Chrome 各占一个 Space，
    当前屏是别的桌面时，截图拍到的是别的应用、点击全部落空，整轮抓取
    中断。强制的手段是 `open -a WeChat`：LaunchServices 激活语义上等价于
    点一下程序坞图标，会把当前 Space 切到微信所在的桌面。

    判定以**截图内容**为准（信息流卡片 / 蓝色公众号图标），窗口标志只作
    辅助通道：全屏 Space 的窗口 onscreen 标志不可靠（恒 0），只信标志会
    误报。

    **两种模式**（light 参数，默认完整版）：

    - 完整版（关键节点：导航开始、点击失败恢复）：截图判定 + open -a
      逐秒复验。注意抓取中途**文章浮窗开着是常态**——浮窗上没有卡片和
      蓝方块，完整版会误判，所以浮窗开着时不要用完整版。
    - 轻量版（light=True，每次点击前）：只确认微信窗口存在（含离屏查询，
      被关掉/最小化才 open -a 恢复），**不截图、不判 Space**。Space 真不
      对时点击会落空，由「点后验证文章窗口真的打开了」发现，并在恢复
      路径上再跑一次完整版——不在每次点击上都花秒级成本。
    """
    if light:
        for _ in range(3):
            if wechat_any_window() is not None:
                return {"onscreen": True, "known": True, "evidence": "light"}
            if verbose:
                print("· 微信窗口不见了（被关/最小化），尝试恢复", flush=True)
            subprocess.run(["open", "-a", "WeChat"], check=False)
            for _ in range(4):
                _sleep(1.0)
                if wechat_any_window() is not None:
                    return {"onscreen": True, "known": True,
                            "evidence": "light"}
        raise _fail(
            "微信窗口不在了（被关掉且没能恢复）。\n"
            "  点程序坞里的微信图标把它恢复出来再重跑。")

    for attempt in range(5):
        if _wechat_ui_visible(cfg):
            if verbose and attempt:
                print("· 微信界面已显示在当前屏幕", flush=True)
            return {"onscreen": True, "known": True,
                    "evidence": "screenshot"}
        # 截图没有微信特征时，先想想是不是「文章浮窗开着」这个常态：
        # 浮窗内容既不是卡片也没有蓝方块，完整版必然认不出 —— 先关掉
        # 残留浮窗再判，避免把正常抓取现场当成「微信在别的 Space」。
        if attempt == 0:
            try:
                if article_window_open(cfg):
                    close_article(cfg)
                    if _wechat_ui_visible(cfg):
                        return {"onscreen": True, "known": True,
                                "evidence": "screenshot"}
            except Exception:
                pass
        main = wechat_main_window()
        if main is not None and main["onscreen"]:
            # 窗口标志说在屏上但截图没有微信特征：可能是聊天页/设置页等
            # 特征覆盖不到的界面。放行，交给下游 to_feed / ensure_front
            # 继续处理（它们有自己的界面判定）。
            return {"onscreen": True, "known": True,
                    "evidence": "window_flag", "window": main}
        if verbose:
            print(f"· 当前屏幕不是微信（在别的 Space），强制切换"
                  f"（第 {attempt + 1} 次）", flush=True)
        subprocess.run(["open", "-a", "WeChat"], check=False)
        for _ in range(6):            # Space 切换有延迟，逐秒复验
            _sleep(1.0)
            if _wechat_ui_visible(cfg):
                return {"onscreen": True, "known": True,
                        "evidence": "screenshot"}
            main = wechat_main_window()
            if main is not None and main["onscreen"]:
                return {"onscreen": True, "known": True,
                        "evidence": "window_flag", "window": main}
    raise _fail(
        "尝试 5 次都没能让微信界面显示到当前屏幕。\n"
        "  如果微信文章浮窗正开着，这是误报 —— 请关掉浮窗回到信息流后"
        "再重跑；否则请手动点程序坞里的微信图标切到微信桌面。")


def ensure_front(cfg: dict, *, verbose: bool = True,
                 light: bool = False) -> dict:
    """确保微信窗口在最前**且**微信是前台 App，否则点击会被浪费。

    两个坑叠在一起：

    1. 窗口被挡住 → 点击落到遮挡者身上（WorkBuddy 会自己弹到最前）；
    2. 窗口可见、但微信不是前台 App → macOS 会**吞掉这次点击**只用来激活
       窗口（默认不开启 click-through），于是「点了卡片却没打开文章」。

    所以判定条件是「没被遮挡 且 前台是微信」；不满足就点一下微信窗口里
    没被盖住的点（优先标题栏：既抬升窗口又激活 App，不触发任何内容）。
    连一个像素都不露时直接报错退出——**不再去点菜单栏**：实测那样会把
    微信「窗口」菜单留在屏幕上，之后的点击全被菜单吃掉，反而更糟。

    第一道门是 `ensure_onscreen`：主窗口在别的 Space 上时先强制切过来，
    否则后面所有判定都在对着错误的桌面截图。light=True 时走轻量通道
    （只确认窗口存在，不判 Space）——每次点击前都用它，秒级成本留给了
    关键节点和点击失败的恢复路径。
    """
    from . import winpos

    ensure_onscreen(cfg, verbose=verbose, light=light)

    ws: list[dict] | None = None
    main: dict | None = None
    for _attempt in range(4):
        ws = winpos.windows()
        main = winpos.find_wechat(ws)
        if main is None:
            break
        if winpos.is_front(main, ws) and gui.is_wechat_app(gui.frontmost_app()):
            return {"raised": False, "known": True, "window": main, "clicks": 0}
        point = winpos.free_point(main, ws)
        if point is None:
            # 被上层窗口完全盖住（常见是 WorkBuddy 自己的窗口整个压上来），
            # 任何点击都会落到遮挡者身上。先 AppleScript 激活微信 ——
            # activate 会把微信窗口整体提到最前，不涉及任何像素级点击，
            # 是唯一安全的抬升手段；救不回来下一轮再判，连续失败才放弃。
            if verbose:
                print("· 微信被完全盖住，先激活微信抬窗", flush=True)
            gui.activate_wechat(0.8)
            continue
        if verbose:
            print(f"· 微信不在最前，先点它的标题栏 ({point[0]:.0f},{point[1]:.0f})",
                  flush=True)
        gui.click(*point)
        _sleep(0.55)

    ws = winpos.windows()
    main = winpos.find_wechat(ws)
    if main is not None and winpos.is_front(main, ws) \
            and gui.is_wechat_app(gui.frontmost_app()):
        return {"raised": True, "known": True, "window": main, "clicks": 1}
    if ws is not None and main is None:
        allw = winpos.windows(include_offscreen=True)
        exists = bool(allw) and any(
            "微信" in w["owner"] and w["layer"] == 0 and w["w"] >= 600
            for w in allw)
        if not exists:
            raise _fail(
                "微信主窗口不在屏幕上（被关掉了 / 最小化到程序坞了）。\n"
                "  点程序坞里的微信图标，或在微信「窗口」菜单里选「微信」，"
                "把它恢复出来再重跑。")
    if main is None:
        return {"raised": False, "known": False}
    raise _fail(
        "微信窗口没能回到最前（被其它窗口完全盖住，或前台 App 抢不回来）。\n"
        "  请把挡着微信的窗口（常见是 WorkBuddy 自己）移开或缩小，再重跑 ——"
        " 否则点击会打到别的窗口上。")


def _wait_page(cfg: dict, *, timeout: float | None = None,
               poll: float | None = None) -> tuple[str, dict]:
    """轮询到「公众号」页出现（卡片流或号列表都算），返回 (截图, 形态)。

    以前只认卡片：进号列表形态要干等 `nav_load_timeout` 才失败。现在两种
    形态都算「进了公众号页」，判形态交给 `page_state`。
    """
    ui = cfg["ui"]
    timeout = float(ui.get("nav_load_timeout", 8.0) if timeout is None else timeout)
    poll = float(ui.get("nav_load_poll", 0.8) if poll is None else poll)
    deadline = time.time() + timeout
    shot = _shot(cfg)
    state = page_state(cfg, shot)
    while state["mode"] == "unknown" and time.time() < deadline:
        _sleep(poll)
        shot = _shot(cfg)
        state = page_state(cfg, shot)
    return shot, state


def to_feed(cfg: dict, *, tries: int | None = None,
            verbose: bool = True, want: str = "card") -> dict:
    """确保停在「公众号」页，并是 `want` 指定那种形态；必要时自动点击。

    want="card"（默认）＝信息流抓取要的卡片流；
    want="list" ＝按号抓取要的号列表。

    2026-11-08 的行为变更：以前遇到号列表形态直接报错让人手工切；现在会
    **先尝试点切换按钮自动切**（`switch_mode`），切不过去才报错。这条对
    「按号抓取跑完忘了切回来」很关键——否则下一次信息流抓取会莫名停轮。

    返回 {"in_feed": 是否卡片流, "mode": "card"/"list", "clicks": n, "cards": [...]}；
    失败抛 `grab.GrabAborted`（退出码 3）。
    """
    _preflight(cfg)
    ui = cfg["ui"]
    tries = int(ui.get("nav_tries", 3) if tries is None else tries)
    label = "列表" if want == "list" else "卡片"

    # 关键节点：先做一次完整的 Space 检查（截图判定 + 必要时强制切换）。
    gui.activate_wechat(0.8)
    ensure_onscreen(cfg, verbose=verbose)
    ensure_front(cfg, verbose=verbose)
    close_article(cfg)          # 残留的文章窗口会盖住左侧「公众号」入口

    def _ok(shot: str, state: dict, clicks: int) -> dict:
        return {"in_feed": want == "card", "mode": want, "clicks": clicks,
                "cards": [c.as_dict() for c in state["cards"]]}

    shot = _shot(cfg)
    state = page_state(cfg, shot)
    if state["mode"] == want:
        if verbose:
            print(f"· 已停在公众号{label}模式（本屏 {len(state['cards'])} 条）",
                  flush=True)
        return _ok(shot, state, 0)
    if state["mode"] in ("card", "list"):
        switch_mode(cfg, want, verbose=verbose)
        state = page_state(cfg)
        return _ok(state["image"], state, 1)

    icons = read_icons(cfg, shot)
    if not icons:
        raise _fail(
            "既没看到公众号信息流，也没在左侧列表里找到「公众号」图标。\n"
            "  左侧会话列表可能滚到了看不到「公众号」的位置，"
            "或窗口不是 1280x832 的全屏布局。\n"
            "  请把微信主窗口最大化、把左侧列表滚到能看到「公众号」那一行，再重跑。")

    clicks = 0
    tried_points: list[tuple[float, float]] = []
    for attempt in range(1, tries + 1):
        # 同色的群头像也可能被当成「公众号」入口，所以点完必须校验：
        # 没进公众号页就换个候选，候选都用完就重新截图定位。
        icon = icons[0] if icons else None
        if icon is None:
            shot = _shot(cfg)
            icons = read_icons(cfg, shot)
            icon = icons[0] if icons else None
        if icon is None:
            break
        icons = icons[1:]
        if any(abs(icon.x - px) < 8 and abs(icon.y - py) < 8
               for px, py in tried_points):
            continue
        tried_points.append((icon.x, icon.y))
        if verbose:
            print(f"· 第 {attempt} 次点「公众号」入口 "
                  f"({icon.x:.0f},{icon.y:.0f})", flush=True)
        gui.click(icon.x, icon.y)
        clicks += 1
        shot, state = _wait_page(cfg)
        if state["mode"] == want:
            if verbose:
                print(f"· 已进入公众号{label}模式"
                      f"（本屏 {len(state['cards'])} 条）", flush=True)
            return _ok(shot, state, clicks)
        if state["mode"] in ("card", "list"):
            # 进去了但形态不对：切一次再复验
            switch_mode(cfg, want, verbose=verbose)
            state = page_state(cfg)
            if state["mode"] == want:
                return _ok(state["image"], state, clicks)
        if not icons:                     # 候选用完，重截图再定位
            icons = read_icons(cfg, shot)

    raise _fail(
        f"连续 {tries} 次点「公众号」入口都没进入公众号{label}模式。\n"
        f"  现场截图：{shot}\n"
        "  多半是窗口几何变了，或左侧列表滚到了看不到「公众号」的位置。"
        "请把微信主窗口最大化、把左侧列表滚到能看到「公众号」那一行，再重跑。")


def _at_feed(cfg: dict) -> bool:
    """界面确实停在公众号**卡片流**，而不是文章页 / 号列表。

    只看 `len(read_cards) >= 2` 会把文章页误判成卡片流：文章页里的加粗
    文字同样能扫出两条「标题带」（2026-11-09 实盘 y≈281 / 508）。
    所以先用窗口几何挡一道（前面还是独立文章窗就不算），再用左栏
    存在性挡掉列表模式。
    """
    if article_window_open(cfg) is True:   # 前面还是独立文章窗，没关掉
        return False
    try:
        shot = _shot(cfg)
        cards = read_cards(cfg, shot)
    except Exception:
        return False
    if left_pane_present(cfg, shot) or looks_like_account_list(cards):
        return False
    return len(cards) >= 2


def recover(cfg: dict) -> bool:
    """点错 / 弹窗后把界面拉回信息流。

    先 Cmd+W 关掉误开或没加载完的文章标签（微信的文章页是同一个多标签
    窗口里的新标签），再**复验**是否真的回到卡片流；没回去就重新走一次
    导航并再复验一次。返回 True 表示现在确实停在信息流。

    2026-11-09 修：旧版只看「扫到 ≥2 条标题带」就 return True，文章页
    也能满足；`to_feed()` 之后更是无条件 return True。结果广告卡跳过后
    界面还停在文章页，下一张被标题带守卫拒绝盲点，凑满连续 2 次失败
    误报退出码 3。现在两处都必须过 `_at_feed()`。
    """
    try:
        close_article(cfg)                 # 只在前面确实是文章窗口时才关
    except Exception:                      # 关窗失败不该盖掉原始错误
        pass
    if _at_feed(cfg):
        return True
    try:
        to_feed(cfg, verbose=False)
    except Exception:
        return False
    return _at_feed(cfg)


def nav(cfg: dict, *, out: str | None = None) -> dict:
    """CLI 入口：导航到信息流并输出本屏卡片坐标。"""
    result = to_feed(cfg)
    ui = cfg["ui"]
    if out:
        p = Path(out).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    result["coords"] = ";".join(
        f"{c['x']:.0f},{c['y']:.0f}" for c in result.get("cards", []))
    result["logical_size"] = [ui["logical_width"], ui["logical_height"]]
    return result
