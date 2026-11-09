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
   **全程不开浏览器**。菜单项位置也用 OCR 找文字，不写死 y。
   关标签首选 **Cmd+W**（2026-11-08 用户实盘指出比 OCR 找「×」可靠）：快捷键不认
   标签栏文字，多个残留标签也能一次一次关掉。但不能裸按——见 `_cmdw_guard()`。
   早前记的「Cmd+W 在这个外壳里实测无效」多半是当时菜单没收、或快捷键发给了
   主窗口；现在按完必复验，确实无效就自动退回原来的 OCR 点「×」。
4. 右栏是聊天式时间线：每次推送上面有一行时间（「昨天 08:31」「星期三 07:31」），
   下面是这次推送的卡片；**卡片里第一张（带封面图的那条）才是头条**，同一次
   推送的其余几条不要——这就是用户说的「一天发了多篇，我只需要取第一篇」。
5. 最新的一次推送在**最底部**，所以先滚到底，再一屏一屏往上翻。

分工
----
* OCR（`ocr.py`）只负责**认字**：左栏的号名、右栏的时间行、标题文字。
* 点哪里仍然由**像素判据**决定（`feed_vision.find_title_bands` 找加粗标题带），
  OCR 认错字也不会让点击落到别处。
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

# Cmd+W 在本轮进程里是否被证实可用：None=还没试过 / True=可用 / False=已判无效。
# 早前实测有过「按了没反应」的结论，若它成立，不能让 20 篇每篇都白等一轮快捷键；
# 第一次失败后本轮直接走 OCR 兜底。跨轮不持久化，换环境会重新试。
_CMDW_STATE: bool | None = None

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
    """右栏**时间分隔行**的文字指纹（判断滚动有没有到底）。

    早先取右栏所有文字：OCR 对正文换行、浮标、选中态的细微差异会让每一
    次指纹都不同，滚 14 次也判不出「到底」，于是停在几个月前的旧推送上。
    时间行数量少、文字稳定，是更可靠的锚点。空指纹不参与「没变化」计数，
    免得某一屏没读到时间行就被误判成到底。
    """
    ui = cfg["ui"]
    ts = texts if texts is not None else _texts(cfg)
    left = float(ui["account_pane_left"]) - 10
    return tuple(t.text.strip() for t in sorted(ts, key=lambda t: t.y)
                 if t.x >= left and _TS_RE.match((t.text or "").strip()))


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
    rounds = int(ui.get("account_bottom_rounds", 30))
    prev = _pane_sig(cfg)
    same = 0
    for i in range(1, rounds + 1):
        _scroll_pane(cfg, -step)
        cur = _pane_sig(cfg)
        same = same + 1 if (cur and cur == prev) else 0
        prev = cur
        if same >= need:
            if verbose:
                print(f"· 已滚到底（第 {i} 屏，连续 {same} 次时间行不变）",
                      flush=True)
            return {"rounds": i, "at_bottom": True}
    # 没到底就继续取，拿到的会是几个月前的旧推送，却会被当成「最新 20 篇」
    # 写进候选池和台账。按项目铁律宁可停轮，不静默降级。
    raise _fail(
        f"滚了 {rounds} 屏仍没判定到底（时间行一直在变，或滚轮没生效）。\n"
        "  没有「按当前屏继续」这条路：那样抓到的是旧文章，不是最新推送。\n"
        "  请确认右栏能正常滚动（把鼠标停在右栏会话区），或调大 "
        "ui.account_scroll_step / ui.account_bottom_rounds 后重跑。")


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
      * 高 12~30px：正文摘要和图片里的小字（实测 h=10）被排除；
      * **组内优先取高 12~24px 的行**：大图推送的封面**宣传语**会被 OCR 读成
        28~36px 的大字（实测「指数盈利预测，」h=36.1 /「独家免费开放」h=28.8），
        它比真标题更靠上，只按 12~30 过滤会把封面语当成头条、点上去等于点图片。
        整组都没有标题字号的行时才退回宽松池（30px）。
      * 在时间线下方 6px 以外、400px 以内。
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


def _close_marks(texts: list[ocr.Text], win: dict) -> list[ocr.Text]:
    """标签栏里带「×」的行。

    macOS Vision 常常把 × **并进标签标题**读成一行（实测
    `'= 两个风险平价追踪2611 ×'`，x=100..304），也可能单独成行 `'×'`。
    所以它不在窗口右半边，按「x > 半个窗口宽」找必然落空；这里只限定
    标签栏的 y 区间和左半边（右侧是 🌐 / ⋯ 那一片，不该点）。
    """
    lo, hi = win["y"] + 10, win["y"] + 72
    marks = "×✕✖xX"
    return [t for t in texts
            if any(m in (t.text or "") for m in marks)
            and lo <= t.cy <= hi
            and t.x < win["x"] + win["w"] * 0.6]


def _close_point(t: ocr.Text) -> tuple[float, float]:
    """这一行里 × 的可点位置：独立成行就点中心，并进标题就点行尾。"""
    if (t.text or "").strip().strip("=·• ") in ("×", "✕", "✖", "x", "X"):
        return t.cx, t.cy
    # × 是标题行的最后一个字符，宽度约 10~14px；行尾往回 7px 稳落其上。
    return t.right - 7, t.cy


def _main_window_alive() -> bool | None:
    """微信主窗口还在屏上吗。None = 窗口列表读不到，不误报。"""
    ws = winpos.windows()
    if ws is None:
        return None
    return winpos.find_wechat(ws) is not None


def _cmdw_guard() -> str | None:
    """发 Cmd+W 前的安全门：返回「不能发」的原因，None = 可以发。

    **Cmd+W 打给谁就关谁**，三种情形必须挡住：
      * 前台 App 不是微信（WorkBuddy 常在工具调用后自己弹到最前）→ 快捷键
        会关掉别人的窗口，而且微信这边什么都没发生；
      * 微信主窗口不在屏上 → 说明已经被关掉了，再按只会更糟；
      * 窗口列表读不到 → 无法确认打给谁，宁可不按。

    历史坑：在主窗口上按 Cmd+W 会把微信整个关掉（窗口消失、之后所有截图都
    拍不到它，只能手动点程序坞图标恢复，`open -a WeChat` 救不回来）。所以
    按完还要查一次 `_main_window_alive()`，一旦主窗口没了立即停轮。
    """
    if not gui.is_wechat_app(gui.frontmost_app()):
        return "前台 App 不是微信"
    ws = winpos.windows()
    if ws is None:
        return "读不到微信窗口列表"
    if winpos.find_wechat(ws) is None:
        return "微信主窗口不在屏幕上"
    return None


def _close_by_cmdw(cfg: dict, *, rounds: int, verbose: bool = True) -> bool:
    """用 Cmd+W 关文章标签，返回「左栏是否真的回来了」。

    一次只关一个标签：之前 OCR 方案踩过「几个残留文章标签叠着」的情况，
    快捷键正好可以一轮一轮关干净，每轮都复验，不靠猜。
    """
    ui = cfg["ui"]
    wait = float(ui.get("account_cmdw_wait", 1.0))
    for i in range(1, max(1, int(rounds)) + 1):
        reason = _cmdw_guard()
        if reason:
            if _main_window_alive() is False:
                raise _fail("微信主窗口不在屏幕上，不能继续关标签。\n"
                            "  请点程序坞里的微信图标恢复主窗口后重跑。")
            if verbose:
                print(f"  · 不发 Cmd+W：{reason}", flush=True)
            return False
        # 菜单没收起时快捷键会被它吃掉（信息流链路实测过），先 Esc 收掉。
        gui.press_escape()
        _sleep(0.2)
        if not _is_article_page(cfg):
            return True                 # Esc 自己就把标签收了
        if verbose:
            print(f"  · Cmd+W 关文章标签（第 {i}/{rounds} 次）", flush=True)
        gui.cmd_key(13)                 # 13 = kVK_ANSI_W
        _sleep(wait)
        if _main_window_alive() is False:
            raise _fail(
                "Cmd+W 关掉的是微信主窗口，不是文章标签。\n"
                "  请点程序坞里的微信图标把主窗口恢复出来、切回公众号列表模式后重跑。\n"
                "  若这台机器上 Cmd+W 每次都关主窗口，把 ui.account_close_cmdw_rounds\n"
                "  设为 0 可完全跳过快捷键，只用 OCR 点「×」。")
        if not _is_article_page(cfg):
            return True
        if verbose:
            print("  · 左栏还没回来，可能还有残留标签，再按一次", flush=True)
    return False


def _close_by_ocr(cfg: dict, *, verbose: bool = True) -> None:
    """兜底：按 OCR 那一行的右端定位「×」，点完复验左栏回来。

    Cmd+W 不可用 / 无效时才走这里。保留原来那套逻辑，标签数量一变位置就变，
    所以仍按行尾现算，不写死坐标。
    """
    ui = cfg["ui"]
    win = _wechat_window(cfg)
    if win is None:
        raise _fail("找不到微信窗口，无法关闭文章标签。")
    shot = gui.screenshot(_SHOT)
    texts = ocr.recognize(shot, logical_width=ui["logical_width"],
                          logical_height=ui["logical_height"])
    cand = _close_marks(texts, win)
    if not cand:
        raise _fail(
            "Cmd+W 没关掉标签，标签栏上也没读到「×」，不知道点哪里关文章。\n"
            f"  现场截图：{shot}\n"
            "  请手工关掉这个文章标签后重跑。")
    # 多个标签时取最靠右那个（最新打开的通常在最右）
    t = max(cand, key=lambda t: t.right)
    for i, dx in enumerate((0, -7), start=1):
        x, y = _close_point(t)
        x += dx
        if verbose:
            print(f"  · 关闭文章标签（{x:.0f},{y:.0f}，第 {i} 次）", flush=True)
        gui.click(x, y)
        _sleep(ui.get("account_close_wait", 1.4))
        if not _is_article_page(cfg):
            return
        if verbose:
            print("  · 左栏还没回来，换个位置再点一次", flush=True)
    raise _fail(
        "Cmd+W 和两次「×」都没关掉文章标签（左栏没回来）。\n"
        f"  现场截图：{shot}\n"
        "  请手工关掉文章标签并切回公众号列表模式后重跑。")


def _close_article(cfg: dict, *, verbose: bool = True) -> None:
    """关掉当前文章标签，回到公众号列表模式。

    首选 **Cmd+W**（用户 2026-11-08 实盘建议）：不依赖标签栏 OCR，多个残留
    标签也能逐个关。发键前过 `_cmdw_guard()`，按完复验左栏回来。
    如果这台机器上快捷键确实无效（早前有过一次实测结论），本轮进程记住并
    退回 OCR 点「×」，不让 20 篇每篇都白等一轮快捷键。
    """
    global _CMDW_STATE
    # 点完「复制链接」后，微信有时自己就把标签关了、回到列表页。
    # 这时再动手只会误报「关不掉」，所以先做幂等判断。
    if not _is_article_page(cfg):
        if verbose:
            print("  · 文章标签已关闭（左栏已回来），跳过", flush=True)
        return
    rounds = int(cfg["ui"].get("account_close_cmdw_rounds", 3))
    if rounds > 0 and _CMDW_STATE is not False:
        if _close_by_cmdw(cfg, rounds=rounds, verbose=verbose):
            _CMDW_STATE = True
            return
        _CMDW_STATE = False
        if verbose:
            print("  · Cmd+W 这条路没走通，本轮改用 OCR 点「×」", flush=True)
    _close_by_ocr(cfg, verbose=verbose)


def grab_headline(cfg: dict, hl: Headline, *, verbose: bool = True,
                  tries: int = 2) -> str:
    """取一篇头条的链接：点标题 → 「⋯」菜单 → 「复制链接」→ Cmd+W 关标签。

    **不开浏览器**。标签栏按钮按窗口几何现算，不再写死坐标；关标签默认走
    Cmd+W（带安全门），失效才退回 OCR 点「×」。
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
    except Exception as exc:
        # 链接已经复制到了，别让「关标签失败」把它一起丢掉；调用方会把它
        # 写进 partial 文件（不覆盖上一轮那份完整的链接）。
        exc.partial_url = url
        raise
    return url


# ---------------------------------------------------------------- 主流程

def _write_urls(cfg: dict, name: str, urls: list[str],
                *, partial: bool = False) -> Path:
    """写链接清单。中途失败只写 `*_partial*.json`，**不碰主文件**。

    一次跑 20 篇，跑到第 1 篇就失败时，如果把空/半截结果写进主文件，
    上一轮辛苦抓到的整份链接就没了（2026-11-08 实盘踩过）。
    """
    import json
    stem = f"_raw_urls{'_partial' if partial else ''}-{slug(name)}.json"
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
                "would": "切列表模式 → 确认右栏是目标号（不是就在左栏找）→ "
                         "滚到底 → 逐次推送取头条（⋯ → 复制链接 → Cmd+W 关标签）"}

    initial = nav.page_state(cfg)["mode"]
    gui.activate_wechat()
    nav.to_feed(cfg, verbose=verbose, want="list")

    # 用户常常已经手工把目标号点开了（尤其上次中断后接着跑）。这时左栏
    # 列表往往停在别的位置，再去扫 8 屏反而找不到、白停一轮；右栏页头
    # 就是现成的、更可靠的「进对号了没」证据，和 open_account 的复验同源。
    head = _pane_header(cfg)
    if head and _name_hit(head.text, normalize(name), ui):
        if verbose:
            print(f"· 右栏已经是「{head.text}」，跳过左栏找号", flush=True)
        row = AccountRow(name=head.text, x=0.0, y=0.0, title=head.text)
    else:
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
                got = list(urls)
                already = getattr(exc, "partial_url", None)
                if already and already not in got:
                    got.append(already)
                out = _write_urls(cfg, name, got, partial=True)
                raise _fail(
                    f"取《{hl.title or '（无标题）'}》（{hl.ts}）时失败，已停止。\n"
                    f"  已取到 {len(got)} 条，写入 {out.name}（partial，"
                    "主文件未改动、未写任何台账）\n"
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
        if idle >= 2:                        # 连着两屏没新东西 = 到头了
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
