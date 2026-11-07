#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按号抓取：进某个公众号的「列表模式」会话页，从最新往旧取 N 篇头条。

为什么要单独一条链路
--------------------
信息流那套（`grab.grab_auto`）是「点卡片 → 独立浮动窗口 → 点「…」→
复制链接 → Cmd+W 关窗」。到了公众号会话页，界面完全是另一回事，2026-11-08
在这台机器上实测到的关键差异：

1. **文章不开浮窗**，而是在主窗口里多一个**标签页**。所以 `article_window_open`
   恒为假，`grab._open_and_copy` 那套「点完验证浮窗真的开了」的判据在这里
   全部失效，Cmd+W 更是绝对不能用（会关掉微信主窗口，2026-11-07 已踩过）。
2. 标签页右端那个「…」**不能点**：菜单在鼠标按下时就弹出、松开正好落在第一项，
   实测一点就把标签关了（试了两次都一样）。所以这里绕开它。
3. 标签页右端还有一个**地球图标 = 「在浏览器打开」**：点它，文章在默认浏览器
   里打开，微信这边**自动关掉标签、回到公众号页**。于是拿 URL 变成：
   浏览器地址栏 Cmd+L → Cmd+C → 读剪贴板。既准确（拿到的是完整 URL，不靠
   截图猜），又完全不用碰 Cmd+W。
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

from . import feed_vision, gui, nav, ocr
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
      * 高 12~30px：正文摘要和图片里的小字（实测 h=10）被排除；
      * 在时间线下方 6px 以外、400px 以内。
    """
    ui = cfg["ui"]
    path = path or _shot(cfg)
    texts = texts if texts is not None else _texts(cfg, path)
    footer = float(ui["account_footer_y"])
    tx0, tx1 = ui["account_title_x"]
    stamps = [t for t in texts
              if _TS_RE.match((t.text or "").strip())
              and 840 <= t.cx <= 1120 and 60 <= t.cy < footer]
    stamps.sort(key=lambda t: t.y)
    lines = [t for t in texts
             if tx0 - 8 <= t.x <= tx1 and 12 <= t.h <= 30
             and 60 <= t.cy < footer]
    out: list[Headline] = []
    for i, st in enumerate(stamps):
        lo = st.bottom + 6
        hi = stamps[i + 1].y - 2 if i + 1 < len(stamps) else footer
        cand = [t for t in lines if lo < t.cy < hi and t.cy - st.cy < 400]
        if not cand:
            continue
        line = min(cand, key=lambda t: t.y)            # 组内第一条 = 头条
        out.append(Headline(title=line.text,
                            x=line.x + min(line.w / 2, 80),
                            y=line.cy,
                            key=normalize(line.text) or f"y{int(line.cy)}",
                            ts=st.text))
    return out


# ---------------------------------------------------------------- 取链接

def _is_article_page(cfg: dict, texts: list[ocr.Text] | None = None) -> bool:
    """点文章后，界面是不是变成「主窗口内的文章标签页」了。

    判据：右栏顶部那个号名不见了（列表模式的标志），文章页的标签栏文字
    在 x<500，不会冒充号名。
    """
    return _pane_header(cfg, texts) is None


def _browser_front(cfg: dict, timeout: float) -> str | None:
    """等到默认浏览器起来（点「地球」之后）。"""
    names = [str(n).casefold() for n in cfg["ui"].get("account_browsers", [])]
    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        app = gui.frontmost_app()
        if app and any(n in app.casefold() for n in names):
            return app
        _sleep(0.4)
    return None


def grab_headline(cfg: dict, hl: Headline, *, verbose: bool = True,
                  tries: int = 2) -> str:
    """取一篇头条的链接：点标题 → 地球（在浏览器打开）→ 浏览器读地址栏。

    **全程不按 Cmd+W**：微信这边是「地球」自己把标签关掉、回到公众号页，
    浏览器那边 Cmd+W 只关它自己的标签页。这样彻底避开「在微信主窗口按
    Cmd+W 会把微信关掉」那个坑。
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

    gx, gy = ui["account_tab_globe"]
    if verbose:
        print(f"  · 在浏览器打开（点地球 {gx:.0f},{gy:.0f}）", flush=True)
    gui.click(gx, gy)
    app = _browser_front(cfg, ui.get("account_browser_wait", 6.0))
    if app is None:
        raise _fail(
            "点了标签页右端的「在浏览器打开」，但没有浏览器接管前台。\n"
            f"  当前前台是「{gui.frontmost_app() or '未知'}」。\n"
            "  请确认系统默认浏览器可用；也可以手工点一次那个地球图标看看。")

    gui.clipboard_clear()
    _sleep(0.35)
    gui.cmd_key(37)              # Cmd+L 聚焦地址栏
    _sleep(0.30)
    gui.cmd_key(8)               # Cmd+C 复制地址
    _sleep(0.50)
    raw = gui.clipboard_read()
    m = _URL_RE.search(raw or "")
    if not m:
        gui.cmd_key(13)          # 关掉这个浏览器标签，别留垃圾
        _sleep(0.4)
        gui.activate_wechat(1.2)
        raise _fail(
            "浏览器地址栏里没读到公众号链接"
            f"（剪贴板：{(raw or '')[:60]!r}）。\n"
            "  可能是地址栏没聚焦（浏览器是别的界面），或文章页还没加载完。")

    url = m.group(0)
    gui.cmd_key(13)              # 关掉浏览器标签（只影响浏览器）
    _sleep(0.5)
    gui.activate_wechat(1.4)     # 回微信：地球已把标签关掉，应该停在公众号页
    state = nav.page_state(cfg)
    if state["mode"] != "list":
        raise _fail(
            "取完链接回到微信后，界面不在公众号列表模式"
            f"（识别为 {state['mode']}）。\n"
            f"  现场截图：{state['image']}\n"
            "  请手工切回列表模式再重跑。")
    return url


# ---------------------------------------------------------------- 主流程

def _write_urls(cfg: dict, name: str, urls: list[str]) -> Path:
    import json
    out = project_path("out", f"_raw_urls-{slug(name)}.json")
    payload = {"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "account": name, "count": len(urls), "urls": urls}
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
                         "逐次推送取头条（地球 → 浏览器读 URL）"}

    initial = nav.page_state(cfg)["mode"]
    gui.activate_wechat()
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
                out = _write_urls(cfg, name, urls)
                raise _fail(
                    f"取《{hl.title or '（无标题）'}》（{hl.ts}）时失败，已停止。\n"
                    f"  已取到 {len(urls)} 条，已写入 {out}（未写任何台账）\n"
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
