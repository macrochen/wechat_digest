#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按号抓取：进某个公众号的「列表模式」会话页，从最新往旧取 N 篇头条。

为什么要单独一条链路
--------------------
信息流那套（`grab.grab_auto`）是「点卡片 → 独立浮动窗口 → 点「…」→
复制链接 → Cmd+W 关窗」。到了公众号会话页，界面完全是另一回事，2026-11-08
在这台机器上实测到的关键差异：

1. **文章不开浮窗**，而是主窗口里的**标签页**（也会以独立浮窗出现，两种形态
   都实测过）。所以 `article_window_open` 恒为假，`grab._open_and_copy` 那套
   「点完验证浮窗真的开了」的判据在这里全部失效。
2. 标签栏右上角那组按钮的**位置和数量都不固定**（2026-11-08 用户指出、当天实测
   确认）：全屏标签页形态下工具栏在 y≈48，独立浮窗形态下在 y≈187；按钮从右到左
   依次是「⋯」「地球」「🔍」，但**有时没有地球**。早先写死 (1218,48)，那个点
   恰好是全屏形态下的**地球**（=「使用默认浏览器打开」），于是每篇都把文章甩到
   浏览器里去了。现在的做法见 `_toolbar_row()` / `_more_button()`：按窗口几何
   算工具栏行，再在这一行上找最右的图标簇。
3. 取 URL 走「⋯ 菜单 → 复制链接」：菜单里有「转发给朋友 / 分享到朋友圈 / 收藏 /
   使用默认浏览器打开 / 添加浮窗 / 复制链接」，点「复制链接」直接把 URL 写进剪贴板。
   **全程不开浏览器、不按 Cmd+W**（Cmd+W 在这个外壳里实测无效）。菜单项位置也用
   OCR 找文字，不写死 y。
4. 右栏是聊天式时间线：每次推送上面有一行时间（「昨天 08:31」「星期三 07:31」），
   下面是这次推送的卡片；**卡片里第一张（带封面图的那条）才是头条**，同一次
   推送的其余几条不要——这就是用户说的「一天发了多篇，我只需要取第一篇」。
5. 最新的一次推送在**最底部**，所以先滚到底，再一屏一屏往上翻。

分工
----
* OCR（`ocr.py`）负责**认字与菜单项**：左栏号名、右栏时间行与标题文字、
  「⋯」菜单里的「复制链接」。
* **点击坐标以像素判据为主**：头条用 OCR 行的几何（x/y/字高）筛、标签栏按钮
  用「窗口几何 + 暗像素聚簇」算；右栏封面图/缩略图会和标题粘成超高像素带，
  所以这里**不用** `feed_vision.find_title_bands`（那是信息流卡片的判据）。
  OCR 认错字最多让标题写歪，不会让点击落到别处。
* 认不出、对不上、切不过去，一律 `GrabAborted`（退出码 3）停轮等人，
  绝不猜、不盲点、不挪坐标重试。
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from . import feed_vision, gui, nav, ocr, winpos
from .config import project_path

_SHOT = Path("/tmp/wd_acct.png")

# 时间分隔行：「昨天 08:31」「星期三 07:31」「11月6日 08:31」。
# 末尾允许最多 4 个非数字杂字符：OCR 实测会把分隔线读成「星期四 07:32|」，
# 不放宽就会漏掉这一整次推送。
_TS_RE = re.compile(
    r"^(今天|昨天|前天|星期[一二三四五六日天]|"
    r"\d{1,2}月\d{1,2}日|\d{4}年\d{1,2}月\d{1,2}日)?\s*\d{1,2}:\d{2}[^\d]{0,4}$")
_URL_RE = re.compile(r"https?://mp\.weixin\.qq\.com/s[^\s\"'<>]*")


class AccountError(RuntimeError):
    """按号抓取过程中的可解释失败（上层转成退出码 3）。"""


def _fail(message: str):
    from .grab import GrabAborted
    return GrabAborted(message)


def _sleep(seconds: float) -> None:
    time.sleep(float(seconds))


def normalize(name: str) -> str:
    """公众号名规范化：全半角统一、去所有空白、英文不分大小写。"""
    s = unicodedata.normalize("NFKC", name or "")
    s = re.sub(r"\s+", "", s)
    return s.casefold()


def slug(name: str) -> str:
    """号名 → 文件名片段（保留中文，只清掉路径分隔符与空白）。"""
    s = unicodedata.normalize("NFKC", name or "").strip()
    s = re.sub(r"[\s/\\:*?\"<>|]+", "-", s)
    return s.strip("-") or "account"


def _name_hit(got: str, want_normalized: str, ui: dict) -> bool:
    """认出来的名字是不是要找的那个。

    默认**精确相等**（规范化后），跟黑名单同一套规矩；OCR 偶尔会把
    「iETF」认成「ETD」，所以给一个可关的近似兜底（`account_name_ratio`，
    0 = 关掉）。近似只在 ≥3 个字时才启用，免得一个字母的号乱配。
    """
    g = normalize(got)
    if not g or not want_normalized:
        return False
    if g == want_normalized:
        return True
    # OCR 有时会把头像上的字粘到号名前面（实测「iETF令狐冲」被认成
    # 「ETD iETF令狐冲」），所以名字被完整包含时也算命中；门槛 4 个字，
    # 免得「ETF」这种短词到处乱配。
    if len(want_normalized) >= 4 and want_normalized in g:
        return True
    ratio = float(ui.get("account_name_ratio", 0) or 0)
    if ratio <= 0 or len(g) < 3 or len(want_normalized) < 3:
        return False
    return SequenceMatcher(None, g, want_normalized).ratio() >= ratio


def _shot(cfg: dict, path: str | Path = _SHOT) -> str:
    ui = cfg["ui"]
    gui.screenshot(path, ui["logical_width"], ui["logical_height"])
    return str(path)


def _texts(cfg: dict, path: str | Path | None = None) -> list[ocr.Text]:
    ui = cfg["ui"]
    return ocr.recognize(path or _shot(cfg),
                         logical_width=ui["logical_width"],
                         logical_height=ui["logical_height"])


# ---------------------------------------------------------------- 左栏（号列表）

@dataclass(frozen=True)
class AccountRow:
    """左栏的一行：号名 + 该号最新一篇的标题（第二行，可能为空）。"""
    name: str
    x: float          # 点击点（落在号名文字上，避开右端星标）
    y: float
    title: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "x": round(self.x, 1),
                "y": round(self.y, 1), "title": self.title}


def account_rows(cfg: dict, texts: list[ocr.Text]) -> list[AccountRow]:
    """把左栏的文字行按「一行一个号」切出来。

    行的结构（实测）：号名一行、该号最新标题一行（相距约 22px），行距 68px。
    所以用「相邻行间距 > 40px 就换行」来切；行内字号最大的那行当号名。
    """
    ui = cfg["ui"]
    lx0, _lx1 = ui["account_list_x"]
    nx0, nx1 = ui["account_name_x"]
    bottom = float(ui["account_footer_y"]) + 20
    cand = [t for t in texts
            if nx0 <= t.cx <= nx1 and 80 <= t.cy <= bottom
            and t.x >= lx0 and 11 <= t.h <= 26]
    cand.sort(key=lambda t: t.y)
    groups: list[list[ocr.Text]] = []
    for t in cand:
        if groups and t.y - groups[-1][-1].y <= 40:
            groups[-1].append(t)
        else:
            groups.append([t])
    out: list[AccountRow] = []
    for grp in groups:
        # 号名一定是组里**最上面**那行：卡片样式是「号名在上、最新标题在下」。
        # 早先按「字号最大」挑，实测被噪声带偏（疯狂计划 h=17 比猫笔刀 h=16
        # 大 1px，于是把标题当成了号名），结构性规则比字号稳。
        name = grp[0]
        title = next((t for t in grp[1:] if t.y > name.y), None)
        # 点号名文字的左半边：右端 x=600 是星标，点它会收藏而不是选中
        out.append(AccountRow(
            name=name.text,
            x=name.x + min(name.w / 2, 60),
            y=name.cy,
            title=title.text if title else ""))
    return out


def _list_sig(cfg: dict, texts: list[ocr.Text] | None = None) -> tuple:
    ui = cfg["ui"]
    lx0, lx1 = ui["account_list_x"]
    ts = texts if texts is not None else _texts(cfg)
    return tuple(t.text for t in sorted(ts, key=lambda t: t.y)
                 if lx0 <= t.cx <= lx1)


def _scroll_list(cfg: dict, amount: int) -> None:
    ui = cfg["ui"]
    lx0, lx1 = ui["account_list_x"]
    gui.scroll((lx0 + lx1) / 2, float(ui["account_row_first_y"]) + 280, amount)
    _sleep(ui.get("scroll_wait", 1.4))


def find_account(cfg: dict, name: str, *, rounds: int | None = None,
                 verbose: bool = True) -> AccountRow | None:
    """在左栏里按名字找号：先回到列表顶部，再一屏一屏往下扫。

    找不到就返回 None（调用方停轮），**不做任何近似猜测式点击**。
    左栏按更新时间倒序，方向固定才好收敛；最多扫 `account_scan_rounds` 屏。
    """
    ui = cfg["ui"]
    rounds = int(ui.get("account_scan_rounds", 8) if rounds is None else rounds)
    want = normalize(name)
    step = int(ui.get("account_scroll_step", 8))

    for _ in range(4):                       # 先滚到列表顶部
        before = _list_sig(cfg)
        _scroll_list(cfg, step)
        if _list_sig(cfg) == before:
            break

    seen_sigs: set[tuple] = set()
    for i in range(rounds + 1):
        texts = _texts(cfg)
        rows = account_rows(cfg, texts)
        for row in rows:
            if _name_hit(row.name, want, ui):
                if verbose:
                    print(f"· 左栏找到「{row.name}」（第 {i + 1} 屏，y≈{row.y:.0f}）",
                          flush=True)
                return row
        sig = tuple(normalize(r.name) for r in rows)
        if sig and sig in seen_sigs:         # 这一屏没变化 = 左栏到底了
            break
        seen_sigs.add(sig)
        _scroll_list(cfg, -step)
    return None


def _pane_header(cfg: dict, texts: list[ocr.Text] | None = None) -> ocr.Text | None:
    """右栏顶部那个号名（用来复验「进对了号」）。"""
    ts = texts if texts is not None else _texts(cfg)
    cand = [t for t in ts if 620 <= t.cx <= 980 and 40 <= t.cy <= 72]
    return min(cand, key=lambda t: t.x) if cand else None


def open_account(cfg: dict, row: AccountRow, name: str, *,
                 verbose: bool = True) -> str:
    """点开左栏那行，并**复验右栏顶部标题就是目标号**。

    复验这一步不能省：点偏一点会打开别的号，接着取的 20 篇就是别家的。
    点完没换号就再点一次，两次都不对就停轮（不往下走）。
    """
    ui = cfg["ui"]
    wait = float(ui.get("account_open_account_wait", 1.8))
    for attempt in (1, 2):
        nav.ensure_focus(cfg, verbose=False)
        if verbose:
            print(f"· 打开「{row.name}」（点 {row.x:.0f},{row.y:.0f}）", flush=True)
        gui.click(row.x, row.y)
        _sleep(wait)
        head = _pane_header(cfg)
        got = head.text if head else "（空）"
        if head and _name_hit(head.text, normalize(name), ui):
            return head.text
        if verbose:
            print(f"  · 右栏顶部是「{got}」，不是「{name}」，重试一次", flush=True)
        nav.ensure_focus(cfg, verbose=False)
    raise _fail(
        f"点了「{row.name}」两次，右栏顶部都不是「{name}」（最后一次是「{got}」）。\n"
        "  可能是左栏行位置变了，或点到了星标。请手工点开一次确认后重跑。")


# ---------------------------------------------------------------- 右栏（会话页）

@dataclass(frozen=True)
class Headline:
    """一次推送的头条：可点坐标 + 去重键。"""
    title: str
    x: float
    y: float
    key: str
    ts: str = ""

    def as_dict(self) -> dict:
        return {"title": self.title, "x": round(self.x, 1),
                "y": round(self.y, 1), "ts": self.ts}


def _pane_sig(cfg: dict, texts: list[ocr.Text] | None = None) -> tuple:
    """右栏内容的文字指纹（判断「滚动后有没有变化」，即有没有到底/到顶）。"""
    ui = cfg["ui"]
    ts = texts if texts is not None else _texts(cfg)
    left = float(ui["account_pane_left"]) - 10
    return tuple(t.text for t in sorted(ts, key=lambda t: t.y)
                 if t.x >= left)


def _scroll_pane(cfg: dict, amount: int) -> None:
    ui = cfg["ui"]
    gui.scroll(float(ui["account_scroll_x"]), float(ui["account_scroll_y"]),
               amount)
    _sleep(ui.get("scroll_wait", 1.4))


def scroll_to_bottom(cfg: dict, *, verbose: bool = True) -> dict:
    """滚到最底部（= 最新一次推送）。连续 `account_bottom_confirm` 次滚动
    内容不变，就认为到底了。

    不用「滚几屏」硬编码：右栏打开时停在顶部还是底部并不固定，
    靠「滚不动了」判定对两种情况都成立。
    """
    ui = cfg["ui"]
    need = max(1, int(ui.get("account_bottom_confirm", 2)))
    step = int(ui.get("account_scroll_step", 8))
    prev = _pane_sig(cfg)
    same = 0
    for i in range(1, 15):
        _scroll_pane(cfg, -step)
        cur = _pane_sig(cfg)
        same = same + 1 if cur == prev else 0
        prev = cur
        if same >= need:
            if verbose:
                print(f"· 已滚到底（第 {i} 屏，连续 {same} 次无变化）", flush=True)
            return {"rounds": i, "at_bottom": True}
    if verbose:
        print("  ! 滚了 14 次还没到底，按当前屏继续", flush=True)
    return {"rounds": 14, "at_bottom": False}


def title_bands(cfg: dict, path: str | Path) -> list[feed_vision.Band]:
    """右栏里的「加粗标题带」（像素判据，不含正文灰字）。"""
    ui = cfg["ui"]
    img = feed_vision.decode_png(path)
    img.sx = ui["logical_width"] / img.w
    img.sy = ui["logical_height"] / img.h
    x0, x1 = ui["account_title_x"]
    raw = feed_vision.find_title_bands(
        img, 0, img.h,
        threshold=ui.get("title_dark_threshold", 120),
        min_dark=ui.get("title_min_dark", 10),
        x0=int(x0 / img.sx), x1=int(x1 / img.sx))
    h0 = float(ui.get("account_title_min_height", 6))
    h1 = float(ui.get("account_title_max_height", 24))
    peak = int(ui.get("account_min_peak", 45))
    return [b for b in raw
            if h0 <= (b.y1 - b.y0) <= h1 and b.peak >= peak]


def _line_near(texts: list[ocr.Text], y: float, tol: float = 10) -> ocr.Text | None:
    cand = [t for t in texts if abs(t.cy - y) <= tol]
    return min(cand, key=lambda t: abs(t.cy - y)) if cand else None


def _cover_center(path: str | Path, y0: float, y1: float, *,
                  x0: float = 770, x1: float = 1130,
                  ratio: float = 0.30, min_run: float = 24) -> float | None:
    """时间行下方**第一块卡片内容**的中心 y；纯图推送没有 OCR 标题时用它。

    右栏背景是浅灰 (245,245,245)；卡片是白底或彩色封面。按行统计
    x∈[770,1130] 里「彩色（max-min>25）或近白（min>250）」的像素占比：

    ======  ==========================================  ==========
    位置    实测（out/probe-pane-layout.png）            内容占比
    ======  ==========================================  ==========
    背景     y=80/100/430/445/470/560                    0.00
    封面     y=130/250/500（红色）                        0.89~1.00
    卡片白底 y=300/350（含灰字）、y=400                   0.39~1.00
    ======  ==========================================  ==========

    取占比 ≥0.30 的第一个连续 ≥24px 的段，段中心就是可点的封面/卡片内部。
    **只取第一段** = 这次推送的头条卡片，组内第 2、3 篇天然被跳过。
    """
    img = feed_vision.decode_png(path)
    sx, sy = img.w / 1280.0, img.h / 832.0
    px0, px1 = int(x0 * sx), min(int(x1 * sx), img.w)
    if px1 <= px0:
        return None
    run: list[float] = []
    for py in range(max(0, int(y0 * sy)), min(int(y1 * sy), img.h)):
        row = img.rows[py]
        n = 0
        for px in range(px0, px1):
            i = px * img.ch
            r, g, b = row[i], row[i + 1], row[i + 2]
            if max(r, g, b) - min(r, g, b) > 25 or min(r, g, b) > 250:
                n += 1
        if n / (px1 - px0) >= ratio:
            run.append(py / sy)
        else:
            if run and run[-1] - run[0] + 1 >= min_run:
                return (run[0] + run[-1]) / 2
            run = []
    if run and run[-1] - run[0] + 1 >= min_run:
        return (run[0] + run[-1]) / 2
    return None


def headlines(cfg: dict, texts: list[ocr.Text] | None = None,
              path: str | Path | None = None) -> list[Headline]:
    """本屏每一次推送的**头条**（组内第一条），按屏幕从上到下排列。

    规则：OCR 找时间分隔行 → 每条时间线之后，取**第一条像标题的文字行**，
    它就是这次推送的头条；同一次推送里的第 2、3 条排在后面，天然不会选中。

    为什么用 OCR 行而不是像素标题带（早先的写法，已废弃）：右栏的卡片
    有封面图和右侧缩略图，它们和标题在像素上**连成一条超高的带子**
    （实测一处 155px 高），按「标题带高度 6~24」过滤会直接把头条整条丢掉，
    于是错取成组内第二条。OCR 行不存在这个合并问题。

    「像标题」的判据（都从实测里来）：
      * x 落在正文列（≥760），右端缩略图里的字被排除；
      * **左边界必须对齐标题列**（x ≤ 870）：右栏卡片的标题一律左对齐在
        x≈778~790；卡片右上角的「• clash：美国」这类位置标签 OCR 会读成
        x≈1145~1202 的一行字（h 也在 12~24 之间），只按「x ≥ 760」拦不住，
        点上去当然打不开文章；
      * 高 12~48px：正文摘要和图片里的小字（实测 h=10）被排除；
      * **组内优先取高 12~24px 的行**：大图推送的封面**宣传语**会被 OCR 读成
        28~43px 的大字（实测「指数盈利预测，」h=36.1 / 图上乱码 h=43），
        它比真标题更靠上，有真标题时靠字号优先把它筛掉。
        但**有些推送整张就是封面图**，时间行下面没有独立标题行，
        卡在 30px 会让整组取不到任何行、往上翻变成空屏。
        放宽到 48px 后这行成为点击锚点——实测点在封面里能打开头条文章。
      * 在时间线下方 6px 以外、400px 以内。
      * **整组都读不出标题时**（纯图推送）不放弃这一组：改用
        `_cover_center()` 在像素上找时间行下方第一块卡片内容的中心当锚点。
        实测点在封面内部能打开头条文章；只取第一块 = 组内第一篇。
    """
    ui = cfg["ui"]
    path = path or _shot(cfg)
    texts = texts if texts is not None else _texts(cfg, path)
    footer = float(ui["account_footer_y"])
    tx0, tx1 = ui["account_title_x"]
    x_left_max = float(ui.get("account_title_left_max", 870))
    h_tight = float(ui.get("account_title_max_h", 24))
    h_hard = float(ui.get("account_title_hard_max", 30))
    stamps = [t for t in texts
              if _TS_RE.match((t.text or "").strip())
              and 840 <= t.cx <= 1120 and 60 <= t.cy < footer]
    stamps.sort(key=lambda t: t.y)
    lines = [t for t in texts
             if tx0 - 8 <= t.x <= min(x_left_max, tx1)
             and 12 <= t.h <= h_hard
             and 60 <= t.cy < footer]
    out: list[Headline] = []
    for i, st in enumerate(stamps):
        lo = st.bottom + 6
        hi = stamps[i + 1].y - 2 if i + 1 < len(stamps) else footer
        cand = [t for t in lines if lo < t.cy < hi and t.cy - st.cy < 400]
        if not cand:
            # 纯图推送：时间行下方整张是封面，OCR 一行标题都读不出来。
            # 实测点封面内部能打开头条文章，所以用像素找这块内容的中心当锚点，
            # 而不是让这一屏变成「0 次推送」、连着两屏就误判到头了。
            try:
                cover = _cover_center(path, lo, min(hi, footer))
            except (OSError, ValueError):
                cover = None        # 截图读不出就当没有，不把整轮抓取带崩
            if cover is not None:
                out.append(Headline(
                    title="",
                    x=float(ui.get("account_cover_x", 850)),
                    y=cover,
                    key="cover:" + (normalize(st.text) or f"y{int(st.cy)}"),
                    ts=st.text))
            continue
        tight = [t for t in cand if t.h <= h_tight]
        line = min(tight or cand, key=lambda t: t.y)    # 组内第一条 = 头条
        out.append(Headline(title=line.text,
                            x=line.x + min(line.w / 2, 80),
                            y=line.cy,
                            key=normalize(line.text) or f"y{int(line.cy)}",
                            ts=st.text))
    return out


# ---------------------------------------------------------------- 取链接

def _is_article_page(cfg: dict, texts: list[ocr.Text] | None = None,
                     image_path: str | Path | None = None) -> bool:
    """点文章后，界面是不是变成「主窗口内的文章标签页」了。

    判据：**左栏消失**（`nav.left_pane_present()` 为假）。列表模式的左栏
    是一整列公众号，点开文章后整页变成阅读区，那条竖带不再存在。

    不能用「右栏顶部号名不见了」（早先的写法）：文章页顶部的 Chromium
    **标签栏**文字正好落在号名那个 y 区间（实测读到「E 华泰柏瑞的 ETF
    skill，非常强× Q」），于是文章明明打开了也判成没打开、再点一次。
    `texts` 参数保留只为兼容旧调用，判据已不再看 OCR。
    """
    return not nav.left_pane_present(cfg, image_path)


def _wechat_window(cfg: dict) -> dict | None:
    """面积最大的微信 layer0 窗口（= 当前的文章窗口）。

    文章在**两种形态**下打开过（2026-11-08 实测两次，切换原因未完全确定）：
      * 主窗口内的标签页：窗口就是全屏 (0,29) 1280x803，工具栏 y≈48；
      * 独立浮窗：(11,158) 1206x569，工具栏 y≈187。

    所以工具栏位置只能按窗口几何现算。早先写死 (1218,48)：在全屏形态下
    那一点是**地球**，在浮窗形态下连工具栏都不在 —— 两种都会点错。
    """
    ws = winpos.windows() or []
    cand = [w for w in ws
            if ("微信" in str(w.get("owner")) or "eChat" in str(w.get("owner")))
            and w.get("layer") == 0 and w["w"] > 300 and w["h"] > 200]
    return max(cand, key=lambda w: w["w"] * w["h"]) if cand else None


def _toolbar_row(shot: str | Path, win: dict) -> float:
    """文章工具栏的中心 y：窗口顶部 90px 内右侧 30% 处暗像素最多的一行。

    工具栏上有一排图标（× 🔍 🌐 ⋯），暗像素比标题栏空白区多一个量级。
    实测全屏形态得 y=48、浮窗形态得 y=187，与肉眼读数一致。
    """
    img = feed_vision.decode_png(shot)
    sx, sy = img.w / 1280.0, img.h / 832.0
    x_lo = int((win["x"] + win["w"] * 0.70) * sx)
    x_hi = min(int((win["x"] + win["w"]) * sx), img.w)
    best_y, best_n = win["y"] + 19, 0
    for y in range(int(win["y"] * sy),
                   min(int((win["y"] + 90) * sy), img.h)):
        row = img.rows[y]
        n = 0
        for x in range(x_lo, x_hi):
            i = x * img.ch
            if min(row[i], row[i + 1], row[i + 2]) < 160:
                n += 1
        if n > best_n:
            best_y, best_n = y, n
    return best_y / sy


def _more_button(shot: str | Path, win: dict, y: float) -> tuple[float, float] | None:
    """工具栏最右那个「⋯」按钮的中心（逻辑坐标）。

    取工具栏上下各 8px 的一条带，按列统计暗像素并聚簇，**从右往左**取
    第一个宽度 5~42px 的簇。宽度门限用来跳过窗口右边缘/滚动条这种细长噪声；
    只要最右那个图标，就不必判断旁边有没有地球（两个版本都满足「⋯ 在最右」）。
    """
    img = feed_vision.decode_png(shot)
    sx, sy = img.w / 1280.0, img.h / 832.0
    x_lo = int((win["x"] + win["w"] * 0.55) * sx)
    x_hi = min(int((win["x"] + win["w"] - 6) * sx), img.w)
    prof: dict[int, int] = {}
    for yy in range(int((y - 8) * sy), int((y + 8) * sy)):
        if yy < 0 or yy >= img.h:
            continue
        row = img.rows[yy]
        for x in range(max(0, x_lo), x_hi):
            i = x * img.ch
            if min(row[i], row[i + 1], row[i + 2]) < 160:
                prof[x] = prof.get(x, 0) + 1
    if not prof:
        return None
    cols = sorted(prof)
    clusters: list[list[int]] = [[cols[0]]]
    for c in cols[1:]:
        if c - clusters[-1][-1] > 5:
            clusters.append([c])
        else:
            clusters[-1].append(c)
    for cl in reversed(clusters):
        if 5 <= (cl[-1] - cl[0]) / sx <= 42:
            return (((cl[0] + cl[-1]) / 2) / sx, y)
    return None


def _copy_link_via_menu(cfg: dict, *, verbose: bool = True) -> str:
    """⋯ → 「复制链接」→ 读剪贴板。

    为什么不点旁边那个地球：地球 = 「使用默认浏览器打开」，会把文章甩到
    浏览器里（用户 2026-11-08 明确反馈不要这样）。「⋯」菜单里有「复制链接」，
    只在本地写剪贴板，微信这边也不关标签。
    菜单项位置同样靠 OCR 找文字，不写死 y：菜单项会随版本增删。
    """
    ui = cfg["ui"]
    win = _wechat_window(cfg)
    if win is None:
        raise _fail("CGWindowList 里找不到微信的大窗口，无法定位标签栏按钮。")
    shot = gui.screenshot(_SHOT)
    row = _toolbar_row(shot, win)
    btn = _more_button(shot, win, row)
    if btn is None:
        raise _fail(
            "在标签栏第 "
            f"{row:.0f} 行上没找到「⋯」按钮（窗口 "
            f"{win['x']:.0f},{win['y']:.0f} {win['w']:.0f}x{win['h']:.0f}）。\n"
            f"  现场截图：{shot}\n"
            "  多半是文章还没加载完，或这个版本工具栏布局又变了。"
            "请跑 `account --probe` 看一眼这一屏。")
    if verbose:
        print(f"  · 打开「⋯」菜单（{btn[0]:.0f},{btn[1]:.0f}，"
              f"标签栏第 {row:.0f} 行）", flush=True)
    gui.click(btn[0], btn[1])
    _sleep(ui.get("account_menu_wait", 1.0))

    shot2 = gui.screenshot(_SHOT)
    texts = ocr.recognize(shot2, logical_width=ui["logical_width"],
                          logical_height=ui["logical_height"])
    item = next((t for t in texts if "复制链接" in (t.text or "")), None)
    if item is None:
        gui.press_escape()
        _sleep(0.3)
        seen = "、".join(t.text for t in sorted(
            [t for t in texts if t.x >= win["x"] + win["w"] * 0.6],
            key=lambda t: t.cy)[:8])
        raise _fail(
            "「⋯」菜单里没找到「复制链接」。\n"
            f"  这一屏读到的菜单文字：{seen or '（一个都没读到）'}\n"
            f"  现场截图：{shot2}")
    if verbose:
        print(f"  · 点「复制链接」（{item.cx:.0f},{item.cy:.0f}）", flush=True)
    gui.clipboard_clear()
    _sleep(0.3)
    gui.click(item.cx, item.cy)
    _sleep(ui.get("account_copy_wait", 0.8))
    raw = gui.clipboard_read() or ""
    m = _URL_RE.search(raw)
    if not m:
        raise _fail(
            "点了「复制链接」，但剪贴板里没有公众号链接。\n"
            f"  剪贴板内容：{raw[:60]!r}")
    return m.group(0)


def _close_button(shot: str | Path, win: dict, y: float) -> tuple[float, float] | None:
    """标签栏上**当前标签**的「×」中心（逻辑坐标）。

    不能用 OCR 定位：Vision 把整个标签连同 × 和放大镜读成**一行**
    （实测「= 华泰柏瑞的 ETF skill，非常强× Q」，bbox x=100..338），
    「×」既不是独立行，bbox 中心也落在标题文字中间；早先还把候选限制在
    窗口右半边 x≥640，而标签栏在**左侧**，于是必然找不到。

    纯像素（实测这一屏的列聚簇，逻辑坐标）：

    ======  ==================================  ==========
    簇       x 范围                             含义
    ======  ==================================  ==========
    65..76   12px / 42                         后退/前进箭头
    99..114  16px / 173                        标签 favicon
    123..268 若干个 20~57px 的宽簇             标签标题文字
    **291..298  8px / 22**                     **×（要点的）**
    325..336 12px / 48                         放大镜（搜索标签）
    ======  ==================================  ==========

    规则：在工具栏行 y±10 的带里按列统计暗像素、聚簇，取宽 5~16px 的
    「细簇」（标题文字都是 ≥20px 的宽簇），排除 x<90 的导航箭头；
    **倒数第二个细簇就是 ×**（最后一个是放大镜）。这样标题长短、
    有没有地球图标都不影响定位。
    """
    img = feed_vision.decode_png(shot)
    sx, sy = img.w / 1280.0, img.h / 832.0
    x_lo = int((win["x"] + 55) * sx)
    x_hi = min(int((win["x"] + 520) * sx), img.w)
    prof: dict[int, int] = {}
    for yy in range(int((y - 10) * sy), int((y + 10) * sy)):
        if yy < 0 or yy >= img.h:
            continue
        row = img.rows[yy]
        for x in range(max(0, x_lo), x_hi):
            i = x * img.ch
            if min(row[i], row[i + 1], row[i + 2]) < 170:
                prof[x] = prof.get(x, 0) + 1
    if not prof:
        return None
    cols = sorted(prof)
    clusters: list[list[int]] = [[cols[0]]]
    for c in cols[1:]:
        if c - clusters[-1][-1] > 3:
            clusters.append([c])
        else:
            clusters[-1].append(c)
    thin = []
    for cl in clusters:
        x0, x1 = cl[0] / sx, cl[-1] / sx
        total = sum(prof[i] for i in cl)
        if 5 <= x1 - x0 + 1 <= 16 and total <= 90 and x0 >= win["x"] + 90:
            thin.append((x0, x1))
    if not thin:
        return None
    target = thin[-2] if len(thin) >= 2 else thin[-1]
    return ((target[0] + target[1]) / 2, y)


def _wait_for_list(cfg: dict, timeout: float) -> bool:
    """关标签有动画，**轮询**等列表模式真的回来，不靠固定 sleep。"""
    poll = float(cfg["ui"].get("account_close_poll", 0.4))
    deadline = time.time() + float(timeout)
    while True:
        st = nav.page_state(cfg)
        if st["mode"] == "list" and not _is_article_page(cfg, image_path=st.get("image")):
            return True
        if time.time() >= deadline:
            return False
        _sleep(poll)


def _close_article(cfg: dict, *, verbose: bool = True,
                   max_close: int | None = None) -> None:
    """关文章标签，回到公众号列表模式。

    两个实测坑叠在一起（2026-11-08 实跑第一篇就停在这里）：

    1. **关闭有动画**：点完 × 固定等 1.4s 就去判形态，看到的还是旧画面，
       于是误报「没回到列表模式」。改成每 0.4s 轮询一次、最多等 3s。
    2. **会残留多个标签**：上次异常退出留下的文章标签还在，关掉当前这个
       微信只是切到下一个文章标签，仍然是文章页。所以循环点 ×，
       最多 `account_close_max` 个，直到左栏那列号真的回来。

    × 的位置随标签数量/标题长短变化，用 `_close_button()` 现算，不写死坐标。
    **不用 Cmd+W** —— 在这个 Chromium 外壳里实测按了没有任何反应
    （`nav.close_article()` 那套 Cmd+W 在这里清不掉残留标签）。
    """
    ui = cfg["ui"]
    timeout = float(ui.get("account_close_wait", 3.0))
    tries = int(max_close or ui.get("account_close_max", 4))
    last_btn: tuple[float, float] | None = None
    for i in range(1, tries + 1):
        st = nav.page_state(cfg)
        if st["mode"] == "list" and not _is_article_page(cfg, image_path=st.get("image")):
            return
        win = _wechat_window(cfg)
        if win is None:
            break
        shot = gui.screenshot(_SHOT)
        row = _toolbar_row(shot, win)
        btn = _close_button(shot, win, row)
        if btn is None:
            last_btn = None
            break
        if verbose:
            print(f"  · 关闭文章标签（{btn[0]:.0f},{btn[1]:.0f}"
                  f"{'，第 ' + str(i) + ' 个' if i > 1 else ''}）", flush=True)
        gui.click(btn[0], btn[1])
        last_btn = btn
        if _wait_for_list(cfg, timeout):
            return

    st = nav.page_state(cfg)
    if st["mode"] == "list" and not _is_article_page(cfg, image_path=st.get("image")):
        return
    # 还有 × 说明仍在文章页 —— 这时绝不能点形态切换按钮（那个位置在文章页
    # 是工具栏图标，点下去会开浏览器）。直接停轮让人接手。
    if last_btn is not None:
        raise _fail(
            f"连关 {tries} 个文章标签后仍在文章页（识别为 {st['mode']}）。\n"
            f"  现场截图：{st.get('image')}\n"
            "  请手工关掉剩下的文章标签、切回列表模式后重跑。")
    # 没有 × 了：已经回到公众号页，只是形态是卡片流 —— 安全，切一下
    nav.switch_mode(cfg, "list", verbose=verbose)
    st = nav.page_state(cfg)
    if st["mode"] != "list":
        raise _fail(
            f"关完文章标签后没能切回列表模式（识别为 {st['mode']}）。\n"
            f"  现场截图：{st.get('image')}\n"
            "  请手工切回列表模式后重跑。")


def grab_headline(cfg: dict, hl: Headline, *, verbose: bool = True,
                  tries: int = 2) -> str:
    """取一篇头条的链接：点标题 → 「⋯」菜单 → 「复制链接」→ 关标签。

    **不开浏览器、不按 Cmd+W**。标签栏按钮按窗口几何现算，不再写死坐标。
    """
    ui = cfg["ui"]
    opened = False
    for attempt in range(1, max(1, tries) + 1):
        nav.ensure_focus(cfg, verbose=False)
        gui.click(hl.x, hl.y)
        _sleep(ui.get("account_open_wait", 3.0))
        if _is_article_page(cfg):
            opened = True
            break
        if verbose:
            print(f"  · 第 {attempt} 次点击没打开文章页，重试", flush=True)
    if not opened:
        raise _fail(
            f"点了《{hl.title or '（无标题）'}》{tries} 次都没打开文章页"
            f"（坐标 {hl.x:.0f},{hl.y:.0f}）。\n"
            "  多半是这行文字已经不是标题（列表滚动了 / 版面变了）。"
            "请手工点一次确认后重跑。")

    url = _copy_link_via_menu(cfg, verbose=verbose)
    try:
        _close_article(cfg, verbose=verbose)
    except Exception as exc:               # noqa: BLE001 - 链接要留下来
        # 链接已经拿到了，不能因为「没回到列表模式」把它一起丢掉：
        # 挂到异常上，让 grab_account 写进 partial 文件。
        err = _fail(f"链接已取到，但没回到列表模式：{exc}")
        err.partial_url = url
        raise err
    return url


# ---------------------------------------------------------------- 主流程

def _write_urls(cfg: dict, name: str, urls: list[str],
                *, partial: bool = False) -> Path:
    """写本轮结果。**失败留底走 partial，绝不覆盖上一次成功的整份文件**。

    `scan --urls-file` 只认 `_raw_urls-<号名>.json`；partial 是给人看的
    现场证据，不进正式链路，所以单独一个文件名。
    """
    import json
    stem = (f"_raw_urls_partial-{slug(name)}.json" if partial
            else f"_raw_urls-{slug(name)}.json")
    out = project_path("out", stem)
    payload = {"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "account": name, "count": len(urls), "urls": urls,
               "partial": partial}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    return out


def grab_account(cfg: dict, name: str, limit: int, *, verbose: bool = True,
                 dry_run: bool = False, restore_card: bool = True) -> dict:
    """按号抓取主流程：列表模式 → 找号 → 进号 → 滚到底 → 逐次推送取头条。"""
    ui = cfg["ui"]
    from .grab import preflight
    preflight(cfg)
    if limit <= 0:
        raise AccountError("篇数必须大于 0")
    if dry_run:
        return {"dry_run": True, "account": name, "limit": limit,
                "would": "切列表模式 → 左栏按号名找 → 点开 → 滚到底 → "
                         "逐次推送取头条（⋯ → 复制链接 → 关标签）"}

    initial = nav.page_state(cfg)["mode"]
    gui.activate_wechat()
    # 上次异常退出会攒下文章标签；`nav.close_article()` 用的 Cmd+W 在这个
    # Chromium 外壳里无效，得用「×」逐个清。列表模式下这一步是空操作。
    _close_article(cfg, verbose=verbose)
    nav.to_feed(cfg, verbose=verbose, want="list")

    row = find_account(cfg, name, verbose=verbose)
    if row is None:
        raise _fail(
            f"左栏滚了 {ui.get('account_scan_rounds', 8)} 屏也没找到「{name}」。\n"
            "  请确认号名写法（要和左栏显示的一致），或先把左栏滚到能看到它。")
    open_account(cfg, row, name, verbose=verbose)

    bottom = scroll_to_bottom(cfg, verbose=verbose)
    urls: list[str] = []
    seen: set[str] = set()
    skipped: list[dict] = []
    idle = 0
    for screen in range(1, 41):
        if len(urls) >= limit:
            break
        hls = headlines(cfg)
        fresh = [h for h in hls if h.key not in seen]
        if verbose:
            print(f"· 第 {screen} 屏：{len(hls)} 次推送，其中 {len(fresh)} 条没取过",
                  flush=True)
        idle = idle + 1 if not fresh else 0
        # 从下往上取：越靠下越新
        for hl in sorted(fresh, key=lambda h: -h.y):
            if len(urls) >= limit:
                break
            seen.add(hl.key)
            try:
                url = grab_headline(cfg, hl, verbose=verbose)
            except Exception as exc:
                partial_url = getattr(exc, "partial_url", None)
                if partial_url and partial_url not in urls:
                    urls.append(partial_url)
                out = _write_urls(cfg, name, urls, partial=True)
                raise _fail(
                    f"取《{hl.title or '（无标题）'}》（{hl.ts}）时失败，已停止。\n"
                    f"  本轮已取到 {len(urls)} 条（含已复制链接但关标签失败的"
                    "那一条），留在 {out}（partial，"
                    "**没有覆盖上一次成功的 _raw_urls 文件**，也未写任何台账）\n"
                    f"  原因：{exc}")
            if url in urls:
                skipped.append({"title": hl.title, "url": url,
                                "reason": "重复链接"})
                continue
            urls.append(url)
            if verbose:
                print(f"  · 第 {len(urls)} 篇 {hl.title[:26] or '(无标题)'} "
                      f"{url}", flush=True)
        if len(urls) >= limit:
            break
        # 连着几屏没新东西才到头。给 3 屏：会话页会懒加载，且偶尔有一屏
        # 正好停在两张卡片之间、连时间行都看不见。
        if idle >= int(ui.get("account_idle_rounds", 3)):
            if verbose:
                print("· 再往上也没有新的推送了", flush=True)
            break
        _scroll_pane(cfg, int(ui.get("account_scroll_step", 8)))

    out = _write_urls(cfg, name, urls)
    if restore_card and initial == "card":
        try:
            nav.to_card(cfg, verbose=verbose)
        except Exception as exc:             # noqa: BLE001 - 收尾失败不影响结果
            print(f"  ! 收尾切回卡片模式失败（{exc}），下次跑信息流前请手工切一下",
                  flush=True)
    return {"account": name, "count": len(urls), "urls": urls,
            "skipped": skipped, "file": str(out),
            "at_bottom": bottom.get("at_bottom"),
            "account_title": row.title}


def probe(cfg: dict) -> dict:
    """调试用：把当前这一屏的形态、左栏号名、右栏头条都打出来（不点击）。"""
    state = nav.page_state(cfg)
    texts = _texts(cfg, state["image"])
    rows = account_rows(cfg, texts)
    head = _pane_header(cfg, texts)
    hls = headlines(cfg, texts, state["image"]) if state["mode"] == "list" else []
    return {"mode": state["mode"],
            "pane_header": head.text if head else None,
            "accounts": [r.as_dict() for r in rows],
            "headlines": [h.as_dict() for h in hls],
            "image": state["image"]}
