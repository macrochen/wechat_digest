#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""信息流卡片的推送时间识别，以及「这一屏有没有滚出抓取窗口」的判定。

为什么需要它
------------
「默认抓最近 N 个自然日（N=2：今天 + 昨天）」意味着抓取层不能再卡
「20 篇」这个数，只能一直往下滚，直到**窗口内内容滚完**为止。信息流是
时间倒序的（顶部最新、往下越来越旧），所以只要知道某一屏里最上面那张
「已经早于窗口」的卡片，后面就都是更早的内容了 —— 这一屏抓完就可以收工。

怎么知道卡片是哪天推的
----------------------
微信「公众号」信息流的卡片长这样：

    ┌ 公众号名                    7小时前 ┐   ← 第一行右侧是推送时间
    │ 《文章标题》                          │   ← 第二行是加粗标题（要点的就是它）
    └ 摘要 / 封面 / 余下 N 篇 ┘

标题行是纯像素判据找出来的（`feed_vision`），而时间是**文字**，只能靠 OCR。
本模块只干两件事：

1. `parse_stamp`：把一段文字解析成「几天前」（0=今天、1=昨天……认不出=None）；
2. `find_stamp` / `screen_crossed`：把 OCR 结果跟卡片标题位置对上号，
   判断这一屏有没有「早于抓取窗口」的卡片（`days_ago >= window_days`）。

两条保守原则（宁可多抓，也别漏抓）
--------------------------------
* **认不出就当还在窗口内**：`days_ago=None` 绝不算「滚出窗口」。宁可多滚一屏、
  多抓几篇窗口外的（后面的 scan 层会用服务端时间戳精确剔掉），也不能因为
  一次 OCR 误识就提前收工，把窗口内的文章漏在下面。
* **只认「确定更早」**：整串必须完整匹配某个时间格式才算数，半截不算。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

CN = timezone(timedelta(hours=8))

# 抓取窗口默认「今天 + 昨天」共 2 个自然日。调用方（grab）会传配置里的
# feed_days 覆盖它；这里只放一个不依赖配置的兜底值，便于单测与直接调用。
DEFAULT_WINDOW_DAYS = 2

# 时间戳所在的水平带（逻辑坐标）。实测微信信息流卡片第一行右侧的推送时间
# 落在 x≈822~868（公众号名在左侧 x<700），所以只在右半边找。
STAMP_X0 = 780.0
STAMP_X1 = 900.0
# 卡片标题正上方找时间戳的范围（标题中心往上 4~52px）。
# 实测卡片间距约 126px，所以这个窗口不会串到上一张卡片的标题。
STAMP_UP = 52.0
STAMP_DOWN = 4.0

_WEEKDAY_MAP = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5,
    "日": 6, "天": 6, "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6,
}

# 相对时间：数字 + 单位 + 前
_RE_REL = re.compile(r"(\d+)(秒|分钟|分|小时|天)前")
# 今天/昨天/前天（时刻可选）
_RE_TODAY = re.compile(r"今天(?:\s*(\d{1,2})(?::(\d{2}))?)?")
_RE_YEST = re.compile(r"(?:昨天|昨日)(?:\s*(\d{1,2})(?::(\d{2}))?)?")
_RE_DAYB = re.compile(r"前天(?:\s*(\d{1,2})(?::(\d{2}))?)?")
# 裸时刻
_RE_CLOCK = re.compile(r"(\d{1,2}):(\d{2})")
# 星期几
_RE_WEEKDAY = re.compile(r"(?:星期|周|礼拜)([一二三四五六日天1-7])")
# X月X日（可带星期）
_RE_CN_DATE = re.compile(
    r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日"
    r"(?:[ ,]*(?:周|星期)([一二三四五六日天1-7]))?")
# 2026-11-08 / 2026/11/08
_RE_ISO = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
# 11/08
_RE_SLASH = re.compile(r"(\d{1,2})[-/.](\d{1,2})")


@dataclass(frozen=True)
class Stamp:
    """一个推送时间。`days_ago` = 0 今天、1 昨天、2 前天……None 认不出。"""
    raw: str
    days_ago: int | None
    kind: str

    @property
    def known(self) -> bool:
        return self.days_ago is not None

    @property
    def before_today(self) -> bool:
        """确定早于今天（1 天窗口用的旧判据，保留给单测与 N=1 的场景）。"""
        return self.days_ago is not None and self.days_ago >= 1

    def before_window(self, window_days: int = DEFAULT_WINDOW_DAYS) -> bool:
        """确定早于「含今天往前 N 个自然日」的窗口。认不出的一律 False。

        N=1 等价于 `before_today`（只抓当天）；N=2 时昨天仍在窗口内，
        要看到前天（days_ago>=2）才算滚出窗口。
        """
        n = max(1, int(window_days))
        return self.days_ago is not None and self.days_ago >= n

    def as_dict(self) -> dict:
        return {"raw": self.raw, "days_ago": self.days_ago, "kind": self.kind}


@dataclass(frozen=True)
class Cross:
    """一屏的「有没有滚出抓取窗口」判定结果。"""
    crossed: bool
    stamp: Stamp | None = None
    card_y: float | None = None
    reason: str = ""


def _normalize(text: str) -> str:
    """去掉空白、全角转半角，并修掉几个常见的 OCR 误识。"""
    s = (text or "").strip()
    s = s.replace("：", ":").replace("．", ".").replace("∶", ":")
    s = re.sub(r"\s+", "", s)
    # 「9小附前」→「9小时前」这类误识。只对「数字+单位+前」这种短串生效，
    # 范围收得死死的，不会误伤正常文字。
    if re.fullmatch(r"\d+[^\d]{1,2}前", s):
        s = s.replace("小附", "小时").replace("小肘", "小时")
        s = s.replace("大前", "天前")
    # 「天」被 Vision 读成「大 / 夭 / 犬」很常见（只差一横）。2026-11-09
    # 实盘连着两屏把「昨天」读成「昨大」，被广告保险判据当成乱码标签拦下，
    # 白丢两篇。整串精确匹配才归一，不会误伤正文里的「昨大」之类。
    if re.fullmatch(r"昨[大夭犬太夫曰]", s):
        s = "昨天"
    elif re.fullmatch(r"前[大夭犬太夫]", s):
        s = "前天"
    return s


def _stamp_from_date(raw: str, year: int, month: int, day: int,
                      today: date) -> Stamp:
    """把年月日折算成「几天前」。日期非法就认不出（返回 days_ago=None）。"""
    try:
        d = date(year, month, day)
    except ValueError:
        return Stamp(raw, None, "unknown")
    if d > today:
        # 信息流里不该出现「未来」的推送：多半是去年那条，或者跨年了。
        try:
            d = date(year - 1, month, day)
        except ValueError:
            d = date(year - 1, month, 28)
    return Stamp(raw, (today - d).days, "date")


def parse_stamp(text: str, *, now: datetime | None = None) -> Stamp | None:
    """把一段文字解析成推送时间。

    返回 `None` 表示**这不是时间**（比如「clash：美国」这种位置标签）；
    返回 `Stamp(days_ago=None)` 表示「像时间但认不出」——两种情况都**不算**
    滚出窗口。想判窗口请用 `Stamp.before_window(N)`（N=1 时等价于
    旧判据 `before_today`）。
    """
    orig = (text or "").strip()
    s = _normalize(text)
    if not s:
        return None
    now = now or datetime.now(CN)
    today = now.date()

    if s in ("刚刚", "刚才", "刚"):
        return Stamp(orig, 0, "relative")

    m = _RE_REL.fullmatch(s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit == "天":
            return Stamp(orig, max(0, n), "relative")
        if unit == "秒":
            delta = timedelta(seconds=n)
        elif unit in ("分", "分钟"):
            delta = timedelta(minutes=n)
        else:
            delta = timedelta(hours=n)
        # 「23 小时前」在凌晨跑时其实是昨天 —— 按真实时刻算，别拍脑袋。
        dt = now - delta
        return Stamp(orig, max(0, (today - dt.date()).days), "relative")

    if _RE_TODAY.fullmatch(s):
        return Stamp(orig, 0, "today")
    if _RE_YEST.fullmatch(s):
        return Stamp(orig, 1, "yesterday")
    if _RE_DAYB.fullmatch(s):
        return Stamp(orig, 2, "yesterday")

    m = _RE_CLOCK.fullmatch(s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            now_min = now.hour * 60 + now.minute
            # 时刻比「现在」还晚 → 不可能是今天 → 至少昨天。
            if hh * 60 + mm > now_min:
                return Stamp(orig, 1, "clock")
            return Stamp(orig, 0, "clock")

    m = _RE_WEEKDAY.fullmatch(s)
    if m:
        wd = _WEEKDAY_MAP.get(m.group(1))
        if wd is not None:
            # 0 = 今天这个星期几（也可能是一周前，保守当今天）
            return Stamp(orig, (today.weekday() - wd) % 7, "weekday")

    m = _RE_CN_DATE.fullmatch(s)
    if m:
        year = int(m.group(1)) if m.group(1) else today.year
        return _stamp_from_date(orig, year, int(m.group(2)), int(m.group(3)), today)

    m = _RE_ISO.fullmatch(s)
    if m:
        return _stamp_from_date(orig, int(m.group(1)), int(m.group(2)),
                                int(m.group(3)), today)

    m = _RE_SLASH.fullmatch(s)
    if m:
        return _stamp_from_date(orig, today.year, int(m.group(1)),
                                int(m.group(2)), today)

    return None


def find_stamp(texts, card_y: float, *, now: datetime | None = None,
               x0: float = STAMP_X0, x1: float = STAMP_X1,
               up: float = STAMP_UP, down: float = STAMP_DOWN) -> Stamp | None:
    """给一张卡片找它的推送时间（找不到返回 None）。

    `texts` 是 `ocr.Text` 列表；卡片标题中心在 `card_y`，时间戳在它正上方
    4~52px、右侧 x∈[780,900] 的那一行。取**离标题最近**的那一条。
    """
    best_y: float | None = None
    best: Stamp | None = None
    for t in texts:
        if not (x0 <= t.cx <= x1):
            continue
        cy = t.cy
        if not (card_y - up <= cy <= card_y - down):
            continue
        stamp = parse_stamp(t.text, now=now)
        if stamp is None:
            continue
        if best_y is None or cy > best_y:
            best_y, best = cy, stamp
    return best


# 广告卡标签。微信信息流广告在**普通卡片显示推送时间的同一个位置**
# 显示「广告 ⌄」：2026-11-09 实盘 OCR 读作 '广告、'（cx=848 cy=515，
# conf 0.30），正好落在 STAMP_X0..STAMP_X1 里。下拉箭头可能被读成
# 顿号 / v / ∨，所以只匹配前面的关键词，不做整串相等。
AD_LABEL_WORDS = ("广告", "赞助", "推广", "sponsored")


# 时间戳的「气味」：这些字只要出现一个，这行字就更像是被 OCR 读花的推送
# 时间，而不是广告标签。**宁可放行，也不能把「昨大」这种错字误杀**。
# 纯 ASCII 串不算（"Sponsored" 之类交给主判据的 sponsored 关键词处理）。
_STAMP_HINTS = ("前", "天", "昨", "今", "明", "周", "星期", "时", "分",
                "秒", "月", "日", "号", "刚", "点", "钟")


def _looks_like_stamp(text: str) -> bool:
    """像不像被 OCR 读花的时间戳（而不是广告标签）。"""
    s = _normalize(text)
    if not s or len(s) > 14:
        return False
    if all(ord(ch) < 128 for ch in s):
        return False
    if any(w in s for w in _STAMP_HINTS):
        return True
    return bool(re.search(r"\d", s))


def find_ad_labels(texts, cards, *,
                   x0: float = STAMP_X0, x1: float = STAMP_X1,
                   up: float = STAMP_UP, down: float = STAMP_DOWN) -> dict:
    """找出带广告标签的卡片，返回 {卡片标题 y: OCR 读到的标签文字}。

    **广告卡绝对不能点**：它不像公众号文章那样开微信文章浮窗，点标题会
    直接跳到默认浏览器，把微信窗口从屏幕上弄丢；后面每张卡片的「点击前
    标题带守卫」就会在浏览器 / 桌面 / 别的 App 的截图里找不到标题带，
    连续拒点后整轮以「需要人介入」报废（2026-11-09 实盘）。

    所以这一步必须在**任何点击之前**做完，用与 `find_stamp` 完全相同的
    区域（卡片标题正上方、右侧 x∈[780,900]）把标签和卡片配起来。
    只认这个区域：正文里出现「广告」二字的普通文章不会被误杀。
    """
    marks: dict = {}
    for c in cards:
        zone = [t for t in texts
                if x0 <= t.cx <= x1
                and c.y - up <= t.cy <= c.y - down]
        hit = None
        for t in zone:
            s = (t.text or "").strip()
            if any(w in s.casefold() for w in AD_LABEL_WORDS):
                hit = s
                break
        if hit is None and zone and not any(
                parse_stamp(t.text or "") or _looks_like_stamp(t.text or "")
                for t in zone):
            # 公众号文章卡在这个位置**只会**是推送时间。读到了文字、既解析
            # 不出时间、又完全不像「被读花的时间戳」，才是广告标签乱码。
            # 2026-11-09 实盘：只按「解析不出时间」判，把 OCR 读成「昨大」
            # 的「昨天」误杀了两篇 —— 时间戳错字必须先放行（上面
            # _looks_like_stamp），宁可多抓也别漏。整块区域一个字都没读到
            # （OCR 漏读）时同样保持可点。
            hit = "非时间戳文字：" + " / ".join(
                (t.text or "").strip() for t in zone[:2])
        if hit is not None:
            marks[c.y] = hit
    return marks


def screen_stamps(texts, cards, *, now: datetime | None = None, **kw):
    """逐张卡片配出时间戳，返回 [(卡片 y, Stamp|None), ...]（按 y 从上到下）。"""
    ordered = sorted(cards, key=lambda c: c.y)
    return [(c.y, find_stamp(texts, c.y, now=now, **kw)) for c in ordered]


def screen_crossed(texts, cards, *, now: datetime | None = None,
                   window_days: int = DEFAULT_WINDOW_DAYS, **kw) -> Cross:
    """这一屏有没有「早于抓取窗口」的卡片。

    信息流从上到下越来越旧，所以只要**有一张**卡片的推送时间确定早于
    「含今天往前 window_days 个自然日」这个窗口，它下面的内容就都是更早的
    —— 这一屏抓完就可以停止滚动了。认不出的时间不算数（宁可多滚一屏）。

    window_days=1 就是原来的「滚到昨天就停」；默认 2 = 滚到出现前天及更早。
    """
    n = max(1, int(window_days))
    for y, stamp in screen_stamps(texts, cards, now=now, **kw):
        if stamp is not None and stamp.before_window(n):
            return Cross(True, stamp, y,
                         f"卡片 y≈{y:.0f} 的推送时间是「{stamp.raw}」"
                         f"（{stamp.days_ago} 天前），已经超出最近 {n} 天窗口")
    return Cross(False)
