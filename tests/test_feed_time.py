#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「最近 N 个自然日」抓取窗口相关单测。

关注四件事：
1. `feed_time.parse_stamp` 能认出微信信息流上真实出现过的那些写法；
2. **认不出就算「还在窗口内」** —— 宁可多抓，也别因为一次 OCR 误识漏掉；
3. 窗口默认 2 天（今天 + 昨天）：抓取层要滚到「前天」才停，
   `window_days=1` 能完整退回旧的「滚到昨天就停」；
4. scan 层按 `published_at` 精确过滤窗口，且**过滤发生在写投递台账之前**
   （否则窗口外的文章被记成「投递过」，下次重扫就补不回来了）。

全部用构造数据，不碰微信、不发网络请求、不改真实台账。
"""

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from src import feed_time
from src.config import DEFAULTS
from src.main import run
from src.ocr import Text

CN = timezone(timedelta(hours=8))
# 2026-11-08 是周日；凌晨一点半跑最容易踩「昨天」的边界。
NOW = datetime(2026, 11, 8, 23, 30, tzinfo=CN)
LATE_NIGHT = datetime(2026, 11, 9, 1, 30, tzinfo=CN)

DATE = "2026-11-08"
YESTERDAY = "2026-11-07"
DAY_BEFORE = "2026-11-06"


def _text(t: str, x: float, y: float, w: float = 40, h: float = 14) -> Text:
    return Text(text=t, x=x, y=y, w=w, h=h, conf=1.0)


class _Card:
    """最小卡片替身：只要有 y 就够了。"""

    def __init__(self, y: float, x: float = 690.0):
        self.y = y
        self.x = x


class TestParseStamp(unittest.TestCase):
    def _p(self, s: str, now=NOW):
        return feed_time.parse_stamp(s, now=now)

    def test_relative_hours(self):
        """「7小时前」在当晚 23:30 是今天，在次日凌晨 1:30 就已经是昨天。"""
        self.assertEqual(self._p("7小时前").days_ago, 0)
        self.assertEqual(self._p("7小时前", now=LATE_NIGHT).days_ago, 1)
        self.assertFalse(self._p("7小时前").before_today)
        self.assertTrue(self._p("7小时前", now=LATE_NIGHT).before_today)

    def test_just_now_and_minutes(self):
        self.assertEqual(self._p("刚刚").days_ago, 0)
        self.assertEqual(self._p("3分钟前").days_ago, 0)
        self.assertFalse(self._p("3分钟前").before_today)

    def test_yesterday_and_before(self):
        self.assertEqual(self._p("昨天").days_ago, 1)
        self.assertEqual(self._p("昨天 09:33").days_ago, 1)
        self.assertEqual(self._p("昨日 09:33").days_ago, 1)
        self.assertEqual(self._p("前天").days_ago, 2)
        self.assertEqual(self._p("前天 08:00").days_ago, 2)
        self.assertTrue(self._p("昨天").before_today)

    def test_today(self):
        self.assertEqual(self._p("今天").days_ago, 0)
        self.assertEqual(self._p("今天 01:05").days_ago, 0)
        self.assertFalse(self._p("今天 01:05").before_today)

    def test_bare_clock(self):
        """裸时刻比「现在」还晚 → 不可能是今天 → 至少昨天。"""
        self.assertEqual(self._p("08:15").days_ago, 0)
        self.assertEqual(self._p("23:15", now=LATE_NIGHT).days_ago, 1)

    def test_weekday(self):
        """星期天 = 今天的星期几 → 当天（保守）；星期四 = 3 天前。"""
        self.assertEqual(self._p("星期日").days_ago, 0)
        self.assertEqual(self._p("星期四").days_ago, 3)
        self.assertTrue(self._p("星期四").before_today)
        self.assertFalse(self._p("星期日").before_today)

    def test_dates(self):
        self.assertEqual(self._p("11月8日").days_ago, 0)
        self.assertEqual(self._p("11月7日").days_ago, 1)
        self.assertEqual(self._p("10/16").days_ago, 23)
        self.assertEqual(self._p("2026-11-07").days_ago, 1)
        self.assertTrue(self._p("11月7日").before_today)

    def test_future_date_means_last_year(self):
        """信息流里不该有未来日期；出现就当去年那条（更早）。"""
        st = self._p("12月31日")
        self.assertIsNotNone(st.days_ago)
        self.assertGreater(st.days_ago, 100)

    def test_ocr_misread_is_tolerated(self):
        """实测 OCR 把「9小时前」读成「9小附前」，不能因此认不出。"""
        st = self._p("9小附前")
        self.assertIsNotNone(st)
        self.assertEqual(st.days_ago, 0)

    def test_non_time_text_is_not_a_stamp(self):
        for s in ("clash：美国", "余下2篇", "10个朋友转过", "", "  "):
            self.assertIsNone(self._p(s), f"{s!r} 不该被当成时间")

    def test_unknown_date_never_counts_as_crossed(self):
        """认不出的日期必须**不算**滚出窗口。"""
        st = feed_time.Stamp(raw="13月45日", days_ago=None, kind="unknown")
        self.assertFalse(st.before_today)
        self.assertFalse(st.before_window(2))
        self.assertFalse(st.before_window(1))


class TestBeforeWindow(unittest.TestCase):
    """窗口判据：days_ago >= N 才算滚出窗口，昨天在 2 天窗口内。"""

    def _p(self, s: str):
        return feed_time.parse_stamp(s, now=NOW)

    def test_two_day_window_keeps_yesterday(self):
        self.assertFalse(self._p("今天").before_window(2))
        self.assertFalse(self._p("昨天").before_window(2))
        self.assertTrue(self._p("前天").before_window(2))
        self.assertTrue(self._p("11月5日").before_window(2))

    def test_one_day_window_matches_legacy(self):
        self.assertFalse(self._p("今天").before_window(1))
        self.assertTrue(self._p("昨天").before_window(1))

    def test_three_day_window(self):
        self.assertFalse(self._p("前天").before_window(3))
        self.assertTrue(self._p("3天前").before_window(3))

    def test_default_window_is_two_days(self):
        self.assertEqual(feed_time.DEFAULT_WINDOW_DAYS, 2)
        self.assertFalse(self._p("昨天").before_window())
        self.assertTrue(self._p("前天").before_window())


class TestFindStamp(unittest.TestCase):
    def test_pairs_stamp_with_card(self):
        """实测：时间戳在卡片标题正上方 22~25px、x≈822~868。"""
        texts = [
            _text("7小时前", 824, 152),
            _text("9小时前", 822, 280),
        ]
        cards = [_Card(184.0), _Card(310.0)]
        got = dict(feed_time.screen_stamps(texts, cards))
        self.assertEqual(got[184.0].raw, "7小时前")
        self.assertEqual(got[310.0].raw, "9小时前")

    def test_picks_closest_above(self):
        texts = [_text("昨天", 822, 140), _text("9小时前", 822, 160)]
        cards = [_Card(184.0)]
        got = feed_time.screen_stamps(texts, cards)
        self.assertEqual(got[0][1].raw, "9小时前")

    def test_ignores_text_outside_the_band(self):
        """公众号名在左侧，不该被当成时间戳。"""
        texts = [_text("某公众号", 300, 152)]
        self.assertIsNone(feed_time.find_stamp(texts, 184.0))

    def test_missing_stamp_returns_none(self):
        self.assertIsNone(feed_time.find_stamp([_text("某公众号", 300, 152)],
                                              184.0))


class TestScreenCrossed(unittest.TestCase):
    def test_not_crossed_when_all_today(self):
        texts = [_text("7小时前", 824, 152), _text("9小时前", 822, 280)]
        cards = [_Card(184.0), _Card(310.0)]
        cross = feed_time.screen_crossed(texts, cards, now=NOW)
        self.assertFalse(cross.crossed)

    def test_yesterday_still_inside_two_day_window(self):
        """默认窗口 = 今天 + 昨天：读到「昨天」不能停，还要继续往下滚。"""
        texts = [_text("7小时前", 824, 152), _text("昨天 09:33", 822, 280)]
        cards = [_Card(184.0), _Card(310.0)]
        cross = feed_time.screen_crossed(texts, cards, now=NOW)
        self.assertFalse(cross.crossed)

    def test_crossed_when_bottom_is_day_before(self):
        """前天（days_ago=2）才算出窗口。"""
        texts = [_text("昨天 09:33", 824, 152), _text("前天 08:00", 822, 280)]
        cards = [_Card(184.0), _Card(310.0)]
        cross = feed_time.screen_crossed(texts, cards, now=NOW)
        self.assertTrue(cross.crossed)
        self.assertEqual(cross.stamp.raw, "前天 08:00")
        self.assertIn("2 天窗口", cross.reason)

    def test_window_days_1_matches_legacy(self):
        """N=1 时行为完全等同旧版「滚到昨天就停」。"""
        texts = [_text("7小时前", 824, 152), _text("昨天 09:33", 822, 280)]
        cards = [_Card(184.0), _Card(310.0)]
        cross = feed_time.screen_crossed(texts, cards, now=NOW, window_days=1)
        self.assertTrue(cross.crossed)
        self.assertEqual(cross.stamp.raw, "昨天 09:33")

    def test_unknown_stamp_never_crosses(self):
        """认不出的时间不算滚出窗口（宁可多滚一屏）。"""
        texts = [_text("9小附前", 824, 152)]
        cards = [_Card(184.0)]
        self.assertFalse(feed_time.screen_crossed(texts, cards, now=NOW).crossed)

    def test_topmost_crossed_card_is_the_boundary(self):
        """信息流从上到下越来越旧：上面那张出窗口了，下面就都不用看了。"""
        texts = [_text("前天", 822, 100), _text("9小时前", 822, 280)]
        cards = [_Card(130.0), _Card(310.0)]
        cross = feed_time.screen_crossed(texts, cards, now=NOW)
        self.assertTrue(cross.crossed)
        self.assertAlmostEqual(cross.card_y, 130.0, delta=0.1)


class TestConfigDefaults(unittest.TestCase):
    def test_defaults_are_two_days_and_auto(self):
        self.assertEqual(DEFAULTS["feed_scope"], 1)
        self.assertEqual(DEFAULTS["feed_days"], 2)
        self.assertEqual(DEFAULTS["auto_summary"], 1)
        self.assertEqual(DEFAULTS["max_articles"], 120)
        self.assertEqual(DEFAULTS["account_max_pushes"], 20)

    def test_account_chain_keeps_its_own_limit(self):
        """信息流改成「窗口全部」，不能连带把按号抓取也改了。"""
        self.assertNotEqual(DEFAULTS["max_articles"],
                            DEFAULTS["account_max_pushes"])
        # 扩到两天后滚动上限相应放宽
        self.assertEqual(DEFAULTS["ui"]["auto_rounds"], 60)

    def test_settings_specs_expose_new_keys(self):
        from src import settings_ui
        keys = [s["key"] for s in settings_ui.specs()]
        self.assertIn("feed_scope", keys)
        self.assertIn("feed_days", keys)
        self.assertIn("account_max_pushes", keys)
        self.assertEqual(settings_ui.validate({"feed_scope": 0}),
                         {"feed_scope": 0})
        self.assertEqual(settings_ui.validate({"feed_days": 3}),
                         {"feed_days": 3})
        with self.assertRaises(RuntimeError):
            settings_ui.validate({"feed_scope": 2})
        with self.assertRaises(RuntimeError):
            settings_ui.validate({"feed_days": 8})


class TestGrabCrossedDay(unittest.TestCase):
    """`grab._crossed_day`：把 OCR 结果接到抓取循环里，窗口天数从 cfg 读。"""

    def _cfg(self, **over) -> dict:
        cfg = copy.deepcopy(DEFAULTS)
        cfg.update(over)
        return cfg

    def _kw(self) -> dict:
        return {"x0": 780.0, "x1": 900.0, "up": 52.0, "down": 4.0}

    def test_yesterday_does_not_stop_the_scroll(self):
        from src import grab
        texts = [_text("昨天 09:33", 822, 280)]
        cards = [_Card(310.0)]
        with mock.patch("src.ocr.recognize", return_value=texts):
            self.assertFalse(grab._crossed_day(self._cfg(), "/tmp/x.png",
                                               cards, self._kw(), verbose=False))

    def test_crossed_when_stamp_is_day_before(self):
        from src import grab
        texts = [_text("前天 08:00", 822, 280)]
        cards = [_Card(310.0)]
        with mock.patch("src.ocr.recognize", return_value=texts):
            self.assertTrue(grab._crossed_day(self._cfg(), "/tmp/x.png",
                                              cards, self._kw(), verbose=False))

    def test_feed_days_1_restores_legacy_stop(self):
        """把窗口调回 1 天，昨天就该停 —— 配置确实传到了判定层。"""
        from src import grab
        texts = [_text("昨天 09:33", 822, 280)]
        cards = [_Card(310.0)]
        with mock.patch("src.ocr.recognize", return_value=texts):
            self.assertTrue(grab._crossed_day(self._cfg(feed_days=1),
                                              "/tmp/x.png", cards, self._kw(),
                                              verbose=False))

    def test_not_crossed_when_all_today(self):
        from src import grab
        texts = [_text("7小时前", 824, 152)]
        cards = [_Card(184.0)]
        with mock.patch("src.ocr.recognize", return_value=texts):
            self.assertFalse(grab._crossed_day(self._cfg(), "/tmp/x.png",
                                               cards, self._kw(), verbose=False))

    def test_ocr_failure_never_crosses(self):
        """识别失败当「还在窗口内」，继续往下滚。"""
        from src import grab, ocr
        cards = [_Card(184.0)]
        warned = [False]
        with mock.patch("src.ocr.recognize", side_effect=ocr.OcrError("编不出来")):
            for _ in range(3):
                self.assertFalse(grab._crossed_day(
                    self._cfg(), "/tmp/x.png", cards, self._kw(),
                    verbose=True, warned=warned))
        # 同一个运行里只提醒一次，别刷屏
        self.assertTrue(warned[0])


class _Env:
    """把项目根换到临时目录，并挡掉网络 / GUI / 浏览器。"""

    def __init__(self, root: Path):
        self.root = root
        (root / "out").mkdir(parents=True, exist_ok=True)

    def fake_path(self, *parts):
        return self.root.joinpath(*parts)

    def patches(self, cfg: dict):
        def fake_meta(url):
            if "old" in url:
                day = YESTERDAY
            elif "ancient" in url:
                day = DAY_BEFORE
            else:
                day = DATE
            return {"url": url, "title": f"标题-{url[-3:]}", "source": "某公众号",
                    "published_at": f"{day} 08:00"}

        return [
            mock.patch("src.main.project_path", side_effect=self.fake_path),
            mock.patch("src.state.project_path", side_effect=self.fake_path),
            mock.patch("src.selection.project_path", side_effect=self.fake_path),
            mock.patch("src.main.load_settings", return_value=cfg),
            mock.patch("src.main.article.fetch_meta", side_effect=fake_meta),
            mock.patch("src.main.blacklist.load", return_value=[]),
            mock.patch("src.main._open_listing", mock.Mock()),
        ]

    def write_urls(self, urls):
        (self.root / "out" / "_raw_urls.json").write_text(
            json.dumps({"urls": urls}), encoding="utf-8")

    def inbox(self):
        p = self.root / "out" / f"inbox-{DATE}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def offered(self):
        p = self.root / "state" / "offered.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


URLS = [
    "https://mp.weixin.qq.com/s/today1",
    "https://mp.weixin.qq.com/s/today2",
    "https://mp.weixin.qq.com/s/old00",       # 昨天：窗口内
    "https://mp.weixin.qq.com/s/ancient",     # 前天：窗口外
]


class TestScanWindowFilter(unittest.TestCase):
    """scan 层按 published_at 精确复核「今天 + 昨天」窗口。

    这里用 --confirm 压回人工：测的是日期过滤，不是自动模式
    （默认自动会接着跑 hydrate，反而把关注点带偏）。
    """

    def _run(self, root: Path, cfg: dict, argv: list[str]):
        env = _Env(root)
        env.write_urls(URLS)
        patches = env.patches(cfg)
        for p in patches:
            p.start()
        try:
            code = run(argv + ["--confirm"])
        finally:
            for p in patches:
                p.stop()
        return env, code

    def _cfg(self, **over) -> dict:
        cfg = copy.deepcopy(DEFAULTS)
        cfg.update(over)
        return cfg

    def test_keeps_today_and_yesterday(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, code = self._run(Path(tmp), self._cfg(),
                                   ["scan", "--date", DATE, "--no-open"])
            self.assertEqual(code, 0)
            inbox = env.inbox()
            titles = [a["url"] for a in inbox["articles"]]
            self.assertEqual(len(titles), 3)
            self.assertIn("https://mp.weixin.qq.com/s/old00", titles)
            self.assertNotIn("https://mp.weixin.qq.com/s/ancient", titles)
            self.assertEqual(len(inbox["skipped_notoday"]), 1)
            self.assertEqual(inbox["skipped_notoday"][0]["url"],
                             "https://mp.weixin.qq.com/s/ancient")

    def test_window_recorded_in_inbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, _ = self._run(Path(tmp), self._cfg(),
                               ["scan", "--date", DATE, "--no-open"])
            inbox = env.inbox()
            self.assertEqual(inbox["feed_days"], 2)
            self.assertEqual(inbox["window_start"], YESTERDAY)
            self.assertEqual(inbox["window_end"], DATE)

    def test_days_1_keeps_only_today(self):
        """--days 1 单轮覆盖配置，完整退回旧的「只抓当天」。"""
        with tempfile.TemporaryDirectory() as tmp:
            env, code = self._run(Path(tmp), self._cfg(),
                                   ["scan", "--date", DATE, "--no-open",
                                    "--days", "1"])
            self.assertEqual(code, 0)
            inbox = env.inbox()
            self.assertEqual(inbox["feed_days"], 1)
            self.assertEqual(inbox["window_start"], DATE)
            self.assertEqual(len(inbox["articles"]), 2)
            self.assertEqual(len(inbox["skipped_notoday"]), 2)

    def test_days_3_keeps_three_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, _ = self._run(Path(tmp), self._cfg(),
                               ["scan", "--date", DATE, "--no-open",
                                "--days", "3"])
            inbox = env.inbox()
            self.assertEqual(inbox["window_start"], DAY_BEFORE)
            self.assertEqual(len(inbox["articles"]), 4)
            self.assertEqual(inbox["skipped_notoday"], [])

    def test_outside_window_never_marked_as_offered(self):
        """窗口外的文章不能进投递台账 —— 否则下次重扫会被 skipped_offered 挡掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            env, _ = self._run(Path(tmp), self._cfg(),
                               ["scan", "--date", DATE, "--no-open"])
            self.assertNotIn("https://mp.weixin.qq.com/s/ancient", env.offered())
            self.assertIn("https://mp.weixin.qq.com/s/today1", env.offered())
            # 昨天的现在在窗口内，会正常进清单，也就该记进台账
            self.assertIn("https://mp.weixin.qq.com/s/old00", env.offered())

    def test_feed_scope_off_keeps_everything(self):
        """关掉「窗口全部」时行为跟原来一样：不按日期过滤。"""
        with tempfile.TemporaryDirectory() as tmp:
            env, _ = self._run(Path(tmp), self._cfg(feed_scope=0),
                               ["scan", "--date", DATE, "--no-open"])
            inbox = env.inbox()
            self.assertEqual(len(inbox["articles"]), 4)
            self.assertEqual(inbox["skipped_notoday"], [])


class TestAdLabels(unittest.TestCase):
    """广告卡标签：只在「卡片标题正上方、右侧时间戳区」认，绝不误杀正文。

    2026-11-09 实盘：广告卡在同一位置显示「广告 ⌄」，OCR 读作 '广告、'
    （x=830 y=508 w=36 h=14，cx=848 cy=515，conf 0.30），普通文章卡
    那里是推送时间。点广告卡会直接跳浏览器，所以必须在点击前拦掉。
    """

    def test_real_ocr_label_matches_card(self):
        texts = [_text("广告、", 830, 508, w=36, h=14)]
        self.assertEqual(feed_time.find_ad_labels(texts, [_Card(539)]),
                         {539: "广告、"})

    def test_timestamp_is_not_ad(self):
        texts = [_text("39分钟前", 822, 508, w=60)]
        self.assertEqual(feed_time.find_ad_labels(texts, [_Card(539)]), {})

    def test_body_word_does_not_match(self):
        # 正文里出现「广告」二字的普通文章：x 不在时间戳区，不能误杀
        texts = [_text("本文谈广告投放", 520, 539, w=120)]
        self.assertEqual(feed_time.find_ad_labels(texts, [_Card(539)]), {})

    def test_label_outside_card_window_does_not_match(self):
        texts = [_text("广告", 830, 400, w=30)]
        self.assertEqual(feed_time.find_ad_labels(texts, [_Card(539)]), {})

    def test_only_the_labeled_card_is_marked(self):
        cards = [_Card(280), _Card(412), _Card(544)]
        texts = [_text("1小时前", 822, 255, w=60),
                 _text("广告、", 830, 383, w=36, h=14),
                 _text("39分钟前", 822, 519, w=60)]
        self.assertEqual(list(feed_time.find_ad_labels(texts, cards)), [412])

    def test_english_label_and_empty_texts(self):
        self.assertEqual(feed_time.find_ad_labels([], [_Card(280)]), {})
        texts = [_text("Sponsored", 830, 255, w=90, h=14)]
        self.assertEqual(list(feed_time.find_ad_labels(texts, [_Card(280)])),
                         [280])

    def test_garbled_label_is_still_blocked(self):
        # 「广告」被读成既不含关键词、也不是时间的乱码，仍按广告拦下。
        texts = [_text("広告v", 830, 508, w=36, h=14)]
        self.assertEqual(list(feed_time.find_ad_labels(texts, [_Card(539)])),
                         [539])

    def test_ocr_typo_yesterday_is_still_clickable(self):
        """实盘回归：Vision 把「昨天」读成「昨大」，不能当成广告误杀。"""
        for raw, days in (("昨大", 1), ("昨夭", 1), ("前大", 2)):
            stamp = feed_time.parse_stamp(raw)
            self.assertIsNotNone(stamp, raw)
            self.assertEqual(stamp.days_ago, days, raw)
            texts = [_text(raw, 830, 508, w=36, h=14)]
            self.assertEqual(feed_time.find_ad_labels(texts, [_Card(539)]),
                             {}, raw)

    def test_stamp_like_garble_stays_clickable(self):
        """像时间但认不出的错字，一律放行，绝不误杀。"""
        for raw in ("咋天", "9小附前", "星期大", "2大前"):
            texts = [_text(raw, 830, 508, w=60, h=14)]
            self.assertEqual(feed_time.find_ad_labels(texts, [_Card(539)]),
                             {}, raw)

    def test_garbled_label_without_time_word_still_blocked(self):
        """不含任何时间字的乱码标签，保险判据照样拦（铁律没被削弱）。"""
        for raw in ("広告v", "戸告", "厂吿", "推厂"):
            texts = [_text(raw, 830, 508, w=36, h=14)]
            self.assertEqual(list(feed_time.find_ad_labels(
                texts, [_Card(539)])), [539], raw)

    def test_zone_never_read_stays_clickable(self):
        # 整块区域一个字都没读到 = OCR 漏读，不能把正常文章全判成广告。
        self.assertEqual(feed_time.find_ad_labels([], [_Card(539)]), {})


if __name__ == "__main__":
    unittest.main()
