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
                  "account_bottom_rounds": 4,
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

    def test_never_settling_aborts_instead_of_grabbing_stale_articles(self):
        """滚不到底就继续取 = 静默抓几个月前的旧推送，必须停轮。"""
        from src.grab import GrabAborted
        n = {"i": 0}

        def growing(*a, **k):
            n["i"] += 1
            return (n["i"],)
        with mock.patch.object(account, "_pane_sig", side_effect=growing), \
             mock.patch.object(account, "_scroll_pane") as sp:
            with self.assertRaises(GrabAborted) as ctx:
                account.scroll_to_bottom(self.CFG, verbose=False)
        self.assertIn("没判定到底", str(ctx.exception))
        self.assertEqual(sp.call_count, 4)

    def test_empty_signature_never_counts_as_settled(self):
        """某一屏没读到时间行，不能因此就判「到底」。"""
        from src.grab import GrabAborted
        with mock.patch.object(account, "_pane_sig", return_value=()), \
             mock.patch.object(account, "_scroll_pane"):
            with self.assertRaises(GrabAborted):
                account.scroll_to_bottom(self.CFG, verbose=False)

    def test_pane_sig_uses_only_timestamp_lines(self):
        """正文/浮标的 OCR 抖动不能影响「到底」判定。"""
        cfg = {"ui": {"account_pane_left": 620}}
        texts = [T("昨天 08:31", 850, 500, 90, 16),
                 T("正文第一段内容", 760, 540, 300, 20),
                 T("11月1日 07:32", 850, 620, 110, 16),
                 T("本站精华", 700, 790, 80, 14)]
        self.assertEqual(account._pane_sig(cfg, texts),
                         ("昨天 08:31", "11月1日 07:32"))


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
                 mock.patch.object(account, "_pane_header", return_value=None), \
                 mock.patch.object(account, "find_account", return_value=None), \
                 mock.patch.object(account, "_write_urls") as w:
                with self.assertRaises(GrabAborted) as ctx:
                    account.grab_account(self.CFG, "没有这个号", 5, verbose=False)
            self.assertIn("没找到", str(ctx.exception))
            w.assert_not_called()          # 一条都没取到，不落任何文件
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_already_on_target_account_skips_left_pane_search(self):
        """右栏已经开着目标号时，不该再去左栏扫 8 屏还白停一轮。

        上次中断后接着跑、或用户手工点开号，都是这种状态；左栏往往停在
        别的位置，find_account 反而找不到。
        """
        hdr = mock.Mock(text="EarlETF")
        with tempfile.TemporaryDirectory() as d, \
             mock.patch("src.grab.preflight"), \
             mock.patch.object(account.nav, "page_state",
                               return_value={"mode": "list"}), \
             mock.patch.object(account.nav, "to_feed"), \
             mock.patch.object(account.gui, "activate_wechat"), \
             mock.patch.object(account, "_pane_header", return_value=hdr), \
             mock.patch.object(account, "find_account") as find, \
             mock.patch.object(account, "open_account") as open_, \
             mock.patch.object(account, "scroll_to_bottom",
                               return_value={"at_bottom": True}), \
             mock.patch.object(account, "headlines", return_value=[]), \
             mock.patch.object(account, "_scroll_pane"), \
             mock.patch.object(account, "project_path",
                               side_effect=lambda *p: Path(d).joinpath(*p)):
            res = account.grab_account(self.CFG, "EarlETF", 1, verbose=False)
        find.assert_not_called()
        open_.assert_not_called()
        self.assertEqual(res["account_title"], "EarlETF")
        self.assertEqual(res["count"], 0)

    def test_wrong_pane_header_still_goes_to_left_pane(self):
        """右栏是别的号时，不能偷懒跳过找号（否则取成别人家的文章）。"""
        hdr = mock.Mock(text="猫笔刀")
        row = account.AccountRow(name="EarlETF", x=380, y=120, title="头条")
        with mock.patch("src.grab.preflight"), \
             mock.patch.object(account.nav, "page_state",
                               return_value={"mode": "list"}), \
             mock.patch.object(account.nav, "to_feed"), \
             mock.patch.object(account.gui, "activate_wechat"), \
             mock.patch.object(account, "_pane_header", return_value=hdr), \
             mock.patch.object(account, "find_account", return_value=row) as find, \
             mock.patch.object(account, "open_account") as open_:
            with mock.patch.object(account, "scroll_to_bottom",
                                   return_value={"at_bottom": True}), \
                 mock.patch.object(account, "headlines", return_value=[]), \
                 mock.patch.object(account, "_scroll_pane"), \
                 mock.patch.object(account, "_write_urls",
                                   return_value=Path("/tmp/x.json")):
                account.grab_account(self.CFG, "EarlETF", 1, verbose=False)
        find.assert_called_once()
        open_.assert_called_once()

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


class TestCloseArticle(unittest.TestCase):
    """关标签：Cmd+W 首选（带安全门），OCR 点「×」只作兜底。"""

    WIN = {"x": 0.0, "y": 29.0, "w": 1280.0, "h": 803.0}

    def setUp(self):
        # 快捷键真会发系统事件，所有用例都必须拦住；进程级「Cmd+W 是否有效」
        # 的记忆也要在用例之间复位。
        prev = account._CMDW_STATE
        patcher = mock.patch.multiple(account.gui, press_escape=mock.DEFAULT,
                                      cmd_key=mock.DEFAULT)
        self.gui = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, account, "_CMDW_STATE", prev)
        account._CMDW_STATE = None
        self.CFG = {"ui": {"account_close_cmdw_rounds": 3,
                           "account_cmdw_wait": 0,
                           "account_close_wait": 0,
                           "logical_width": 1280, "logical_height": 832}}

    def test_glued_title_line_is_recognized_and_points_at_row_end(self):
        texts = [T("= 两个风险平价追踪2611 ×", 100, 42, 204, 16),
                 T("Q", 322, 42, 16, 16),
                 T("#…", 1212, 42, 48, 16)]
        marks = account._close_marks(texts, self.WIN)
        self.assertEqual(len(marks), 1)
        x, y = account._close_point(marks[0])
        self.assertAlmostEqual(x, 297)       # right - 7 = 100+204-7
        self.assertAlmostEqual(y, 50)

    def test_standalone_x_uses_its_center(self):
        x, _ = account._close_point(T("×", 288, 42, 14, 16))
        self.assertAlmostEqual(x, 295)

    def test_already_back_on_list_is_a_noop(self):
        with mock.patch.object(account, "_is_article_page", return_value=False), \
             mock.patch.object(account.gui, "screenshot") as shot:
            account._close_article({"ui": {"account_close_wait": 0}},
                                   verbose=False)
        shot.assert_not_called()

    def test_close_failure_keeps_the_already_copied_url(self):
        url = "https://mp.weixin.qq.com/s/abc"
        with mock.patch.object(account.nav, "ensure_focus"), \
             mock.patch.object(account.gui, "click"), \
             mock.patch.object(account, "_is_article_page", return_value=True), \
             mock.patch.object(account, "_copy_link_via_menu",
                               return_value=url), \
             mock.patch.object(account, "_close_article",
                               side_effect=RuntimeError("关不掉")):
            with self.assertRaises(RuntimeError) as ctx:
                account.grab_headline({"ui": {"account_open_wait": 0}},
                                     account.Headline("标题", 800, 300, "k"),
                                     verbose=False)
        self.assertEqual(ctx.exception.partial_url, url)

    # ---- Cmd+W 首选路径 --------------------------------------------------

    def test_cmdw_is_preferred_and_no_ocr_is_needed(self):
        with mock.patch.object(account, "_is_article_page",
                               side_effect=[True, True, False]), \
             mock.patch.object(account, "_cmdw_guard", return_value=None), \
             mock.patch.object(account, "_main_window_alive",
                               return_value=True), \
             mock.patch.object(account, "_close_by_ocr") as by_ocr, \
             mock.patch.object(account.ocr, "recognize") as recognize:
            account._close_article(self.CFG, verbose=False)
        self.gui["cmd_key"].assert_called_once_with(13)
        self.gui["press_escape"].assert_called_once()
        by_ocr.assert_not_called()
        recognize.assert_not_called()
        self.assertTrue(account._CMDW_STATE)

    def test_escape_alone_closes_tab_without_keypress(self):
        # 直接调 _close_by_cmdw：第一次 _is_article_page 就是 Esc 之后的复验
        with mock.patch.object(account, "_is_article_page",
                               side_effect=[False]), \
             mock.patch.object(account, "_cmdw_guard", return_value=None):
            self.assertTrue(account._close_by_cmdw(self.CFG, rounds=3,
                                                   verbose=False))
        self.gui["cmd_key"].assert_not_called()

    def test_residual_tabs_are_closed_one_by_one(self):
        # Esc1 / 按1后 / Esc2 / 按2后：第二个标签关掉才回列表
        with mock.patch.object(account, "_is_article_page",
                               side_effect=[True, True, True, False]), \
             mock.patch.object(account, "_cmdw_guard", return_value=None), \
             mock.patch.object(account, "_main_window_alive",
                               return_value=True):
            self.assertTrue(account._close_by_cmdw(self.CFG, rounds=3,
                                                   verbose=False))
        self.assertEqual(self.gui["cmd_key"].call_count, 2)

    def test_cmdw_is_never_sent_when_guard_refuses(self):
        with mock.patch.object(account, "_cmdw_guard",
                               return_value="前台 App 不是微信"), \
             mock.patch.object(account, "_main_window_alive",
                               return_value=True):
            self.assertFalse(account._close_by_cmdw(self.CFG, rounds=3,
                                                    verbose=False))
        self.gui["cmd_key"].assert_not_called()
        self.gui["press_escape"].assert_not_called()

    def test_main_window_disappearing_stops_immediately(self):
        with mock.patch.object(account, "_is_article_page", return_value=True), \
             mock.patch.object(account, "_cmdw_guard", return_value=None), \
             mock.patch.object(account, "_main_window_alive",
                               return_value=False):
            with self.assertRaises(Exception) as ctx:
                account._close_by_cmdw(self.CFG, rounds=3, verbose=False)
        self.assertIn("主窗口", str(ctx.exception))
        self.assertEqual(self.gui["cmd_key"].call_count, 1)

    def test_guard_rejects_non_wechat_front_app(self):
        with mock.patch.object(account.gui, "frontmost_app",
                               return_value="WorkBuddy"):
            self.assertIn("前台 App", account._cmdw_guard() or "")

    def test_guard_rejects_missing_main_window(self):
        with mock.patch.object(account.gui, "frontmost_app",
                               return_value="微信"), \
             mock.patch.object(account.winpos, "windows", return_value=[]):
            self.assertIn("主窗口", account._cmdw_guard() or "")

    def test_ineffective_cmdw_falls_back_and_is_not_retried(self):
        with mock.patch.object(account, "_is_article_page", return_value=True), \
             mock.patch.object(account, "_close_by_cmdw",
                               return_value=False) as by_cmdw, \
             mock.patch.object(account, "_close_by_ocr") as by_ocr:
            account._close_article(self.CFG, verbose=False)
            account._close_article(self.CFG, verbose=False)
        by_cmdw.assert_called_once()          # 第二次直接用 OCR，不空等快捷键
        self.assertEqual(by_ocr.call_count, 2)
        self.assertFalse(account._CMDW_STATE)

    def test_rounds_zero_skips_cmdw_entirely(self):
        cfg = {"ui": dict(self.CFG["ui"], account_close_cmdw_rounds=0)}
        with mock.patch.object(account, "_is_article_page", return_value=True), \
             mock.patch.object(account, "_close_by_cmdw") as by_cmdw, \
             mock.patch.object(account, "_close_by_ocr") as by_ocr:
            account._close_article(cfg, verbose=False)
        by_cmdw.assert_not_called()
        by_ocr.assert_called_once()


class TestPartialWrites(unittest.TestCase):
    """中途失败不能把上一轮那份完整链接覆盖成 0 条（实盘踩过）。"""

    def test_partial_goes_to_its_own_file(self):
        import json
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(account, "project_path",
                               side_effect=lambda *p: Path(d).joinpath(*p)):
            main = account._write_urls({}, "EarlETF", ["https://mp.weixin.qq.com/s/old"])
            before = main.read_text(encoding="utf-8")
            part = account._write_urls({}, "EarlETF", [], partial=True)
            self.assertEqual(part.name, "_raw_urls_partial-EarlETF.json")
            self.assertEqual(main.read_text(encoding="utf-8"), before)
            self.assertTrue(json.loads(part.read_text(encoding="utf-8"))["partial"])

    def test_failed_run_writes_partial_including_copied_url(self):
        import json
        from src.grab import GrabAborted
        cfg = {"ui": {"account_scan_rounds": 1, "account_scroll_step": 8},
               "max_articles": 20}
        hl = account.Headline(title="标题", x=800, y=300, key="k", ts="08:30")
        err = GrabAborted("关标签失败")
        err.partial_url = "https://mp.weixin.qq.com/s/got"
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "out").mkdir()
            main = root / "out" / "_raw_urls-EarlETF.json"
            main.write_text('{"count": 7}', encoding="utf-8")
            with mock.patch("src.grab.preflight"), \
                 mock.patch.object(account.nav, "page_state",
                                   return_value={"mode": "list"}), \
                 mock.patch.object(account.nav, "to_feed"), \
                 mock.patch.object(account.gui, "activate_wechat"), \
                 mock.patch.object(account, "_pane_header",
                                   return_value=mock.Mock(text="EarlETF")), \
                 mock.patch.object(account, "scroll_to_bottom",
                                   return_value={"at_bottom": True}), \
                 mock.patch.object(account, "headlines", return_value=[hl]), \
                 mock.patch.object(account, "grab_headline", side_effect=err), \
                 mock.patch.object(account, "project_path",
                                   side_effect=lambda *p: root.joinpath(*p)):
                with self.assertRaises(GrabAborted):
                    account.grab_account(cfg, "EarlETF", 3, verbose=False)
            self.assertEqual(json.loads(main.read_text())["count"], 7)
            part = json.loads((root / "out" / "_raw_urls_partial-EarlETF.json")
                              .read_text())
            self.assertEqual(part["urls"], ["https://mp.weixin.qq.com/s/got"])
            self.assertTrue(part["partial"])


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


if __name__ == "__main__":
    unittest.main()
