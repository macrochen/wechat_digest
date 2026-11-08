#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按号抓取的单元测试（合成 OCR 文本，不碰真实截图与 GUI）。

覆盖的是「看错一行的代价很高」那几处：
  * 号名匹配（配错 = 取别人家的 20 篇）
  * 左栏切行（号名必须是组内最上面那行）
  * 头条取组内第一条（取到第二条 = 与用户「只要第一篇」的要求不符）
  * 到底判定与「找不到号必须停」
  * scope 路径隔离（按号的简报不能串到信息流那份里）
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import account, ocr
from src.config import (batches_dir, inbox_file, inbox_matches, pending_file)


def T(text: str, x: float, y: float, w: float = 120, h: float = 18,
      conf: float = 1.0) -> ocr.Text:
    return ocr.Text(text=text, x=x, y=y, w=w, h=h, conf=conf)


class TestNameMatch(unittest.TestCase):
    """号名判定：宁可找不到（停轮可解释），也不许配错号。"""

    CFG = {"ui": {"account_name_ratio": 0.86}}

    def hit(self, got, want, ratio=0.86):
        return account._name_hit(got, account.normalize(want),
                                 {"account_name_ratio": ratio})

    def test_exact_and_normalized(self):
        self.assertTrue(self.hit("EarlETF", "EarlETF"))
        self.assertTrue(self.hit("earletf", "EarlETF"))          # 不分大小写
        self.assertTrue(self.hit("ＥａｒｌＥＴＦ", "EarlETF"))     # 全角半角
        self.assertTrue(self.hit(" 猫笔刀 ", "猫笔刀"))            # 去空白

    def test_ocr_glued_prefix_still_hits(self):
        # 实测：头像上的字被粘到号名前，认成「ETD iETF令狐冲」
        self.assertTrue(self.hit("ETD iETF令狐冲", "iETF令狐冲"))

    def test_short_name_is_not_fuzzy_matched(self):
        # 4 个字以下的号名不做包含 / 近似匹配，否则「ETF」会到处乱配
        self.assertFalse(self.hit("ETF研究", "ETF", ratio=0.9))

    def test_different_account_never_matches(self):
        self.assertFalse(self.hit("韭菜投资学", "集思录"))
        self.assertFalse(self.hit("复利先生", "猫笔刀"))

    def test_empty_side_never_matches(self):
        self.assertFalse(self.hit("", "EarlETF"))
        self.assertFalse(self.hit("EarlETF", ""))


class TestAccountRows(unittest.TestCase):
    """左栏切行：一行 = 号名 + 该号最新标题（相距约 22px），行距 68px。"""

    CFG = {"ui": {"account_list_x": [318, 618],
                  "account_name_x": [345, 560],
                  "account_footer_y": 782}}

    def test_name_is_top_line_of_group(self):
        texts = [T("猫笔刀", 345, 110, 66, 16),
                 T("恒科也算亚太之光了", 345, 132, 200, 14)]
        rows = account.account_rows(self.CFG, texts)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "猫笔刀")
        self.assertEqual(rows[0].title, "恒科也算亚太之光了")

    def test_rows_split_by_pitch(self):
        texts = [T("猫笔刀", 345, 110, 66, 16),
                 T("复利先生", 345, 178, 72, 16),
                 T("刘备教授", 345, 246, 72, 16)]
        rows = account.account_rows(self.CFG, texts)
        self.assertEqual([r.name for r in rows],
                         ["猫笔刀", "复利先生", "刘备教授"])

    def test_click_point_avoids_star_icon(self):
        # 右端 x≈600 是星标，点它会收藏而不是选中；点号名文字左半边才安全
        texts = [T("星辰大海的边界", 345, 110, 190, 16)]
        row = account.account_rows(self.CFG, texts)[0]
        self.assertLess(row.x, 345 + 60 + 1)
        self.assertAlmostEqual(row.x, 345 + 60)

    def test_right_pane_text_ignored(self):
        # 右栏正文也在 y 范围内，但 x 不在号名列，不能混进来
        texts = [T("猫笔刀", 345, 110, 66, 16),
                 T("这不是号名这是一篇文章的标题啊啊", 700, 110, 400, 16)]
        rows = account.account_rows(self.CFG, texts)
        self.assertEqual([r.name for r in rows], ["猫笔刀"])


class TestHeadlines(unittest.TestCase):
    """头条 = 每一次推送（时间行）下面的第一条标题；组内其余不要。"""

    CFG = {"ui": {"account_footer_y": 782,
                  "account_title_x": [760, 1200]}}

    def test_only_first_of_each_push(self):
        texts = [
            T("昨天 08:31", 850, 300, 90, 16),
            T("恒科也算亚太之光了", 760, 330, 300, 20),   # 头条
            T("申万一级行业相对强弱一览", 760, 380, 280, 18),  # 同日第二篇
            T("星期三 07:31", 850, 460, 110, 16),
            T("美股为什么又跌了", 760, 490, 240, 20),      # 头条
        ]
        hls = account.headlines(self.CFG, texts=texts, path="/dev/null")
        self.assertEqual([h.title for h in hls],
                         ["恒科也算亚太之光了", "美股为什么又跌了"])
        self.assertEqual([h.ts for h in hls], ["昨天 08:31", "星期三 07:31"])

    def test_stamp_with_trailing_noise_still_recognized(self):
        # 实测 OCR 把分隔线读成「星期四 07:32|」，漏掉它就漏掉整次推送
        texts = [T("星期四 07:32|", 850, 300, 110, 16),
                 T("恒科也算亚太之光了", 760, 330, 300, 20)]
        hls = account.headlines(self.CFG, texts=texts, path="/dev/null")
        self.assertEqual(len(hls), 1)

    def test_small_text_and_thumbnail_ignored(self):
        texts = [
            T("昨天 08:31", 850, 300, 90, 16),
            T("图片里的小字", 760, 330, 80, 10),          # h 太小（实测 h=10）
            T("右侧缩略图标题块", 1205, 330, 60, 18),      # x 超出正文列
            T("真正的头条在这里", 760, 370, 260, 20),
        ]
        hls = account.headlines(self.CFG, texts=texts, path="/dev/null")
        self.assertEqual([h.title for h in hls], ["真正的头条在这里"])

    def test_key_survives_across_screens(self):
        # 同一篇在滚动后重复出现时，key 必须一致，否则会重复取链接
        a = account.headlines(self.CFG, texts=[
            T("昨天 08:31", 850, 300, 90, 16),
            T("恒科也算亚太之光了", 760, 330, 300, 20)], path="/dev/null")
        b = account.headlines(self.CFG, texts=[
            T("昨天 08:31", 850, 500, 90, 16),
            T(" 恒科也算亚太之光了 ", 760, 530, 300, 20)], path="/dev/null")
        self.assertEqual(a[0].key, b[0].key)


class TestScrollToBottom(unittest.TestCase):
    CFG = {"ui": {"account_bottom_confirm": 2, "account_scroll_step": 8,
                  "account_scroll_x": 900, "account_scroll_y": 480,
                  "scroll_wait": 0}}

    def test_stops_after_repeated_unchanged_content(self):
        sigs = [("a",), ("b",), ("b",), ("b",)]
        it = iter(sigs)
        with mock.patch.object(account, "_pane_sig",
                               side_effect=lambda *a, **k: next(it)), \
             mock.patch.object(account, "_scroll_pane"):
            r = account.scroll_to_bottom(self.CFG, verbose=False)
        self.assertTrue(r["at_bottom"])

    def test_reports_not_bottom_when_never_settles(self):
        n = {"i": 0}

        def growing(*a, **k):
            n["i"] += 1
            return (n["i"],)
        with mock.patch.object(account, "_pane_sig", side_effect=growing), \
             mock.patch.object(account, "_scroll_pane"):
            r = account.scroll_to_bottom(self.CFG, verbose=False)
        self.assertFalse(r["at_bottom"])
        self.assertEqual(r["rounds"], 14)


class TestFindAccount(unittest.TestCase):
    CFG = {"ui": {"account_scan_rounds": 3, "account_scroll_step": 8,
                  "account_list_x": [318, 618], "account_name_x": [345, 560],
                  "account_footer_y": 782, "account_name_ratio": 0.86,
                  "account_row_first_y": 110, "scroll_wait": 0}}

    def test_returns_row_when_found(self):
        texts = [T("EarlETF", 345, 110, 66, 16)]
        with mock.patch.object(account, "_texts", return_value=texts), \
             mock.patch.object(account, "_list_sig", return_value=("x",)), \
             mock.patch.object(account, "_scroll_list"):
            row = account.find_account(self.CFG, "EarlETF", verbose=False)
        self.assertIsNotNone(row)
        self.assertEqual(row.name, "EarlETF")

    def test_missing_account_returns_none_and_stops(self):
        """找不到就是 None（上层停轮），不许退化成「点第一个号」。"""
        texts = [T("猫笔刀", 345, 110, 66, 16), T("复利先生", 345, 178, 72, 16)]
        calls = {"n": 0}

        def scroll(cfg, amount):
            calls["n"] += 1
        with mock.patch.object(account, "_texts", return_value=texts), \
             mock.patch.object(account, "_list_sig", return_value=("x",)), \
             mock.patch.object(account, "_scroll_list", side_effect=scroll):
            row = account.find_account(self.CFG, "根本不存在的号", verbose=False)
        self.assertIsNone(row)
        # 扫不到就停：屏数受 account_scan_rounds 限制，不会无限滚
        self.assertLessEqual(calls["n"], 2 * self.CFG["ui"]["account_scan_rounds"] + 8)


class TestGrabAccountAborts(unittest.TestCase):
    CFG = {"ui": {"account_scan_rounds": 1}, "max_articles": 20}

    def test_missing_account_aborts_without_writing(self):
        from src.grab import GrabAborted
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(account, "preflight", create=True), \
                 mock.patch("src.grab.preflight"), \
                 mock.patch.object(account.nav, "page_state",
                                   return_value={"mode": "list"}), \
                 mock.patch.object(account.nav, "to_feed"), \
                 mock.patch.object(account.gui, "activate_wechat"), \
                 mock.patch.object(account, "find_account", return_value=None), \
                 mock.patch.object(account, "_write_urls") as w:
                with self.assertRaises(GrabAborted) as ctx:
                    account.grab_account(self.CFG, "没有这个号", 5, verbose=False)
            self.assertIn("没找到", str(ctx.exception))
            w.assert_not_called()          # 一条都没取到，不落任何文件
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_zero_limit_rejected_before_touching_ui(self):
        with mock.patch("src.grab.preflight"):
            with self.assertRaises(account.AccountError):
                account.grab_account(self.CFG, "EarlETF", 0, verbose=False)

    def test_urls_file_name_carries_account(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(account, "project_path",
                                   side_effect=lambda *p: Path(d).joinpath(*p)):
                path = account._write_urls(
                    self.CFG, "猫笔刀", ["https://mp.weixin.qq.com/s/xyz"])
                self.assertEqual(path.name, "_raw_urls-猫笔刀.json")

    def test_partial_never_overwrites_the_last_good_file(self):
        """中途失败只留 partial；上一轮辛苦抓到的整份链接不能被 0 条覆盖。"""
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(account, "project_path",
                                   side_effect=lambda *p: Path(d).joinpath(*p)):
                full = account._write_urls(
                    self.CFG, "EarlETF",
                    ["https://mp.weixin.qq.com/s/old1",
                     "https://mp.weixin.qq.com/s/old2"])
                before = full.read_text(encoding="utf-8")
                part = account._write_urls(self.CFG, "EarlETF", [],
                                           partial=True)
                self.assertEqual(part.name, "_raw_urls_partial-EarlETF.json")
                self.assertEqual(full.read_text(encoding="utf-8"), before)

    def test_mid_run_failure_writes_partial_only(self):
        from src.grab import GrabAborted
        row = account.AccountRow(name="EarlETF", x=380, y=117, title="头条")
        hl = account.Headline(title="标题", x=800, y=300, key="k", ts="08:30")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            outdir = root / "out"
            outdir.mkdir()
            old = '{"count": 7, "urls": ["https://mp.weixin.qq.com/s/old"]}'
            (outdir / "_raw_urls-EarlETF.json").write_text(old,
                                                           encoding="utf-8")
            with mock.patch("src.grab.preflight"), \
                 mock.patch.object(account.nav, "page_state",
                                   return_value={"mode": "list"}), \
                 mock.patch.object(account.nav, "to_feed"), \
                 mock.patch.object(account.gui, "activate_wechat"), \
                 mock.patch.object(account, "find_account", return_value=row), \
                 mock.patch.object(account, "open_account"), \
                 mock.patch.object(account, "scroll_to_bottom",
                                   return_value={"at_bottom": True}), \
                 mock.patch.object(account, "headlines", return_value=[hl]), \
                 mock.patch.object(account, "grab_headline",
                                   side_effect=GrabAborted("boom")), \
                 mock.patch.object(account, "_scroll_pane"), \
                 mock.patch.object(account, "project_path",
                                   side_effect=lambda *p: root.joinpath(*p)):
                with self.assertRaises(GrabAborted) as ctx:
                    account.grab_account(self.CFG, "EarlETF", 3, verbose=False)
            self.assertIn("partial", str(ctx.exception))
            self.assertEqual(
                (outdir / "_raw_urls-EarlETF.json").read_text(encoding="utf-8"),
                old)
            self.assertTrue(
                (outdir / "_raw_urls_partial-EarlETF.json").exists())


class TestScopePaths(unittest.TestCase):
    """按号抓取的产物必须与信息流那份**物理隔离**，台账不隔离。"""

    def test_inbox_file_names(self):
        self.assertEqual(inbox_file("2026-11-08", "EarlETF"),
                         "inbox-2026-11-08-EarlETF.json")
        self.assertEqual(inbox_file("2026-11-08"), "inbox-2026-11-08.json")

    def test_inbox_matches_is_exact_about_scope(self):
        self.assertTrue(inbox_matches("inbox-2026-11-08-EarlETF.json", "EarlETF"))
        self.assertFalse(inbox_matches("inbox-2026-11-08.json", "EarlETF"))
        self.assertTrue(inbox_matches("inbox-2026-11-08.json", ""))
        # 有 scope 的那份不能被「无 scope」当成信息流那份复用
        self.assertFalse(inbox_matches("inbox-2026-11-08-EarlETF.json", ""))

    def test_pending_and_batches_separated(self):
        self.assertEqual(pending_file("EarlETF"), "pending_summary-EarlETF.json")
        self.assertEqual(pending_file(), "pending_summary.json")
        self.assertEqual(batches_dir("EarlETF"), "_batches-EarlETF")
        self.assertEqual(batches_dir(), "_batches")

    def test_run_key_differs_per_scope(self):
        from src.main import _run_key
        urls = ["https://mp.weixin.qq.com/s/a"]
        self.assertNotEqual(_run_key("2026-11-08", urls, "EarlETF"),
                            _run_key("2026-11-08", urls))
        self.assertEqual(_run_key("2026-11-08", urls, "EarlETF"),
                         _run_key("2026-11-08", urls, "EarlETF"))

    def test_scope_inferred_from_raw_urls_file(self):
        from src.main import _scope_from_urls_file
        self.assertEqual(_scope_from_urls_file(Path("out/_raw_urls-EarlETF.json")),
                         "EarlETF")
        self.assertEqual(_scope_from_urls_file(Path("out/_raw_urls-猫笔刀.json")),
                         "猫笔刀")
        # 信息流那份没有尾段 → 不推断出 scope
        self.assertEqual(_scope_from_urls_file(Path("out/_raw_urls.json")), "")
        self.assertEqual(
            _scope_from_urls_file(Path("out/inbox-2026-11-08-EarlETF.json")), "")


class TestSlug(unittest.TestCase):
    def test_keeps_chinese_strips_path_chars(self):
        self.assertEqual(account.slug("猫笔刀"), "猫笔刀")
        self.assertEqual(account.slug("a/b:c"), "a-b-c")
        self.assertEqual(account.slug("   "), "account")


class TestReviewScope(unittest.TestCase):
    """常驻服务按请求里的 `?scope=` 决定看哪份候选池。

    一个服务可能先服务信息流那份、后服务按号那份，光靠启动参数会看错池子。
    """

    def scope_of(self, query: str) -> str:
        import types
        from src import review
        fake = types.SimpleNamespace(path="/review" + query)
        return review._Handler.request_scope(fake)

    def test_query_beats_startup_scope(self):
        from src import review
        old = review._ACTIVE_SCOPE
        review._ACTIVE_SCOPE = "EarlETF"
        try:
            # 显式空 scope = 「我要信息流那份」，与「没带参数」区分开
            self.assertEqual(self.scope_of("?scope="), "")
            self.assertEqual(self.scope_of("?scope=%E7%8C%AB%E7%AC%94%E5%88%80"),
                             "猫笔刀")
            self.assertEqual(self.scope_of(""), "EarlETF")
        finally:
            review._ACTIVE_SCOPE = old

    def test_review_url_always_carries_scope_param(self):
        from src.review import review_url
        self.assertEqual(review_url("http://127.0.0.1:7777", "EarlETF"),
                         "http://127.0.0.1:7777/review?scope=EarlETF")
        # 空 scope 也必须带上参数，否则服务会退回启动时那个 scope
        self.assertEqual(review_url("http://127.0.0.1:7777", ""),
                         "http://127.0.0.1:7777/review?scope=")

    def test_page_payload_and_posts_carry_scope(self):
        """清单页内嵌 scope，且保存 / 提交请求要把它带回去。"""
        from src import review
        box = {"date": "2026-11-08", "generated_at": "x", "phase": "scan",
               "run_key": "k", "scope": "EarlETF", "account": "EarlETF",
               "articles": [{"index": 1, "title": "标题一", "source": "EarlETF",
                             "published_at": "",
                             "url": "https://mp.weixin.qq.com/s/a"}]}
        page = review.build_page(box)
        self.assertIn('"scope": "EarlETF"', page)
        self.assertIn("function query()", page)
        # 关键一行：POST 必须拼上 ?scope=，否则提交会落到另一个 scope
        self.assertIn("fetch(path + query()", page)


def _png(path: Path, blocks: list[tuple[int, int, int, int]],
         w: int = 1280, h: int = 832) -> Path:
    """写一张 8-bit RGB、filter=0 的 PNG：白底 + 若干黑矩形 (x0,x1,y0,y1)。

    只为测标签栏的像素定位；不依赖 screencapture，也不需要 Pillow。
    """
    import struct
    import zlib

    def chunk(typ: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + typ + data +
                struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    lines = []
    for y in range(h):
        row = bytearray(b"\xff" * (w * 3))
        for x0, x1, y0, y1 in blocks:
            if y0 <= y < y1:
                for x in range(x0, min(x1, w)):
                    row[x * 3:x * 3 + 3] = b"\x00\x00\x00"
        lines.append(b"\x00" + bytes(row))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    blob = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(b"".join(lines))) +
            chunk(b"IEND", b""))
    path.write_bytes(blob)
    return path


# 实测这一屏（2026-11-08，全屏标签页）的标签栏暗像素簇，逻辑坐标：
#   65..76 导航 / 99..114 favicon / 123..268 标题文字 / 291..298 × /
#   325..336 放大镜 / 1206..1226 地球 / 1236..1260 ⋯
_TOOLBAR_BLOCKS = [
    (65, 77, 42, 56), (99, 115, 41, 57),
    (123, 180, 43, 55), (185, 205, 43, 55),
    (210, 234, 43, 55), (243, 269, 43, 55),
    (291, 299, 46, 52),          # ×
    (325, 337, 46, 52),          # 放大镜
    (1206, 1226, 43, 55),        # 地球（这个版本有）
    (1236, 1260, 43, 55),        # ⋯（永远在最右）
]
WIN = {"x": 0.0, "y": 29.0, "w": 1280.0, "h": 803.0}


class TestToolbarButtons(unittest.TestCase):
    """标签栏按钮按窗口几何 + 像素现算，绝不写死坐标。

    用户 2026-11-08 反馈：这个版本的工具栏「有时只有 ⋯，有时在 ⋯ 左边
    多一个小地球」，而且文章窗口可能全屏、可能是浮窗。写死 (1218,48)
    在前者点中地球（把文章甩进浏览器）、在后者连工具栏都够不着。
    """

    def test_close_and_more_land_on_the_right_icons(self):
        with tempfile.TemporaryDirectory() as d:
            shot = _png(Path(d) / "tb.png", _TOOLBAR_BLOCKS)
            row = account._toolbar_row(shot, WIN)
            self.assertTrue(40 <= row <= 60, row)
            close = account._close_button(shot, WIN, row)
            more = account._more_button(shot, WIN, row)
            self.assertIsNotNone(close)
            self.assertIsNotNone(more)
            self.assertAlmostEqual(close[0], 294.5, delta=3)
            self.assertAlmostEqual(more[0], 1248, delta=4)
            # 「⋯」在最右，「×」在左侧标签上，两者绝不能混
            self.assertLess(close[0], 400)
            self.assertGreater(more[0], 1200)

    def test_no_globe_version_still_finds_more(self):
        """只有 ⋯ 没有地球时也必须定位到 ⋯（用户说的两种形态之一）。"""
        blocks = [b for b in _TOOLBAR_BLOCKS if b[0] != 1206]
        with tempfile.TemporaryDirectory() as d:
            shot = _png(Path(d) / "tb.png", blocks)
            row = account._toolbar_row(shot, WIN)
            more = account._more_button(shot, WIN, row)
            self.assertIsNotNone(more)
            self.assertAlmostEqual(more[0], 1248, delta=4)

    def test_close_tracks_title_width(self):
        """标签标题短了，× 跟着左移；写死坐标必错。"""
        shifted = [(x0, x1, y0, y1) if x0 < 120 or x0 > 300 else
                   (x0 - 60, x1 - 60, y0, y1)
                   for x0, x1, y0, y1 in _TOOLBAR_BLOCKS]
        with tempfile.TemporaryDirectory() as d:
            shot = _png(Path(d) / "tb.png", shifted)
            row = account._toolbar_row(shot, WIN)
            close = account._close_button(shot, WIN, row)
            self.assertIsNotNone(close)
            self.assertAlmostEqual(close[0], 234.5, delta=3)

    def test_blank_toolbar_returns_none_instead_of_clicking(self):
        with tempfile.TemporaryDirectory() as d:
            shot = _png(Path(d) / "blank.png", [])
            self.assertIsNone(account._close_button(shot, WIN, 49))
            self.assertIsNone(account._more_button(shot, WIN, 49))


class TestCloseArticle(unittest.TestCase):
    """关标签：点算出来的 ×，且必须确认真的回到列表模式。"""

    CFG = {"ui": {"logical_width": 1280, "logical_height": 832,
                  "account_close_wait": 0}}

    def test_closes_and_verifies_list_mode(self):
        from src.grab import GrabAborted
        with tempfile.TemporaryDirectory() as d:
            shot = _png(Path(d) / "tb.png", _TOOLBAR_BLOCKS)
            with mock.patch.object(account, "_wechat_window", return_value=WIN), \
                 mock.patch.object(account.gui, "screenshot", return_value=shot), \
                 mock.patch.object(account, "_is_article_page", return_value=False), \
                 mock.patch.object(account.nav, "page_state",
                                   return_value={"mode": "list",
                                                 "image": str(shot)}), \
                 mock.patch.object(account.gui, "click") as click, \
                 mock.patch.object(account, "_sleep"):
                account._close_article(self.CFG, verbose=False)
            self.assertEqual(click.call_count, 1)
            x, y = click.call_args.args
            self.assertAlmostEqual(x, 294.5, delta=3)
            self.assertTrue(40 <= y <= 60)

    def test_aborts_when_close_button_missing(self):
        from src.grab import GrabAborted
        with tempfile.TemporaryDirectory() as d:
            shot = _png(Path(d) / "blank.png", [])
            with mock.patch.object(account, "_wechat_window", return_value=WIN), \
                 mock.patch.object(account.gui, "screenshot", return_value=shot), \
                 mock.patch.object(account.gui, "click") as click:
                with self.assertRaises(GrabAborted):
                    account._close_article(self.CFG, verbose=False)
            click.assert_not_called()

    def test_aborts_when_not_back_in_list_mode(self):
        """点了 × 却切到卡片流 / 仍是文章页，必须停轮而不是继续点下一屏。"""
        from src.grab import GrabAborted
        with tempfile.TemporaryDirectory() as d:
            shot = _png(Path(d) / "tb.png", _TOOLBAR_BLOCKS)
            with mock.patch.object(account, "_wechat_window", return_value=WIN), \
                 mock.patch.object(account.gui, "screenshot", return_value=shot), \
                 mock.patch.object(account, "_is_article_page", return_value=False), \
                 mock.patch.object(account.nav, "page_state",
                                   return_value={"mode": "card",
                                                 "image": str(shot)}), \
                 mock.patch.object(account.gui, "click"), \
                 mock.patch.object(account, "_sleep"):
                with self.assertRaises(GrabAborted):
                    account._close_article(self.CFG, verbose=False)


if __name__ == "__main__":
    unittest.main()
