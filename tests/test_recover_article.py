# -*- coding: utf-8 -*-
"""2026-11-09 实盘 bug：广告卡跳过后界面没恢复，连续 2 次失败误报退出码 3。

两处修复各锁一层：

* `grab._dismiss_opened_article` —— 拿不到链接时自己收菜单 + 关文章窗，
  不把残留窗口留给下一张卡片；
* `nav.recover` / `nav._at_feed` —— 不再只看「扫到 >=2 条标题带」就宣称
  回到信息流（文章页同样能扫出 2 条），且 `to_feed()` 之后必须复验。

复现现场：第 1、2 张正常拿到链接，第 3 张广告卡没有「复制链接」被跳过，
第 4 张在文章页上被标题带守卫拒绝盲点，凑满连续 2 次失败停轮。
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from src import grab, nav


CFG = {
    "ui": {
        "open_wait": 0, "menu_wait": 0, "click_wait": 0, "close_wait": 0,
        "more_button": [1256, 47], "copy_link": [1110, 243],
        "account_list_x": [318, 618],
        "logical_width": 1280, "logical_height": 832,
    }
}


class AtFeedTests(unittest.TestCase):
    """_at_feed 必须挡住文章窗 / 列表模式 / 卡片太少的情形。"""

    def _run(self, *, article_open=False, left=False,
             account_list=False, cards=None):
        cards = [object(), object()] if cards is None else cards
        with mock.patch.object(nav, "article_window_open",
                               return_value=article_open) as front, \
                mock.patch.object(nav, "_shot",
                                  return_value="/tmp/wd-shot.png") as shot, \
                mock.patch.object(nav, "read_cards", return_value=cards), \
                mock.patch.object(nav, "left_pane_present",
                                  return_value=left), \
                mock.patch.object(nav, "looks_like_account_list",
                                  return_value=account_list):
            result = nav._at_feed(CFG)
        return result, front, shot

    def test_open_article_window_is_not_feed(self):
        """前面还是独立文章窗时直接短路，连截图都不该拍。"""
        ok, _, shot = self._run(article_open=True)
        self.assertFalse(ok)
        shot.assert_not_called()

    def test_list_mode_is_not_feed(self):
        ok, _, _ = self._run(left=True)
        self.assertFalse(ok)

    def test_account_list_is_not_feed(self):
        ok, _, _ = self._run(account_list=True)
        self.assertFalse(ok)

    def test_too_few_cards_is_not_feed(self):
        ok, _, _ = self._run(cards=[object()])
        self.assertFalse(ok)

    def test_card_feed_is_accepted(self):
        ok, _, _ = self._run()
        self.assertTrue(ok)


class RecoverTests(unittest.TestCase):
    """recover 不能无条件 return True；to_feed 之后必须复验。"""

    def _run(self, at_feed_seq, to_feed_raises=False):
        with mock.patch.object(nav, "close_article", return_value=False), \
                mock.patch.object(nav, "_at_feed",
                                  side_effect=list(at_feed_seq)) as at_feed, \
                mock.patch.object(
                    nav, "to_feed",
                    side_effect=(RuntimeError("nav failed")
                                 if to_feed_raises else None)) as to_feed:
            result = nav.recover(CFG)
        return result, at_feed, to_feed

    def test_already_recovered_short_circuits(self):
        result, at_feed, to_feed = self._run([True])
        self.assertTrue(result)
        self.assertEqual(at_feed.call_count, 1)
        to_feed.assert_not_called()

    def test_to_feed_failure_returns_false(self):
        result, _, _ = self._run([False], to_feed_raises=True)
        self.assertFalse(result)

    def test_no_unconditional_true_after_to_feed(self):
        """旧版在这里直接 return True —— 正是本次误报的根源。"""
        result, at_feed, to_feed = self._run([False, False])
        self.assertFalse(result)
        to_feed.assert_called_once()
        self.assertEqual(at_feed.call_count, 2)

    def test_true_only_after_second_check_passes(self):
        result, at_feed, to_feed = self._run([False, True])
        self.assertTrue(result)
        to_feed.assert_called_once()
        self.assertEqual(at_feed.call_count, 2)


class DismissArticleTests(unittest.TestCase):
    """失败路径必须自己收尾，不能留给下一张卡片。"""

    def test_escape_then_close(self):
        with mock.patch.object(grab.gui, "press_escape") as esc, \
                mock.patch.object(nav, "close_article",
                                  return_value=True) as close:
            grab._dismiss_opened_article(CFG)
        esc.assert_called_once()
        close.assert_called_once_with(CFG)

    def test_close_failure_is_swallowed(self):
        with mock.patch.object(grab.gui, "press_escape") as esc, \
                mock.patch.object(nav, "close_article",
                                  side_effect=RuntimeError("boom")):
            grab._dismiss_opened_article(CFG)   # 不该把原始错误盖掉
        esc.assert_called_once()

    def test_failed_copy_dismisses_before_raising(self):
        """实盘命中路径：剪贴板里没有 mp.weixin.qq.com 链接。"""
        cal = SimpleNamespace(used_y=100.0, moved=False, status="disabled",
                              detail="已关闭", as_dict=lambda: {})
        with mock.patch.object(grab, "_probe_title_y", return_value=cal), \
                mock.patch.object(grab, "_guard_title",
                                  return_value=100.0), \
                mock.patch.object(grab, "_focus"), \
                mock.patch.object(grab, "_sleep"), \
                mock.patch.object(grab, "_article_window_state",
                                  return_value=True), \
                mock.patch.object(
                    grab, "_front_window",
                    return_value={"x": 0, "y": 0, "w": 100, "h": 100}), \
                mock.patch.object(grab, "_more_point",
                                  return_value=(70, 20)), \
                mock.patch.object(grab, "_menu_groups",
                                  return_value=[[]]), \
                mock.patch.object(grab, "_copy_link_point",
                                  return_value=None), \
                mock.patch.object(grab, "_deleted_page",
                                  return_value={"deleted": False}), \
                mock.patch.object(grab.gui, "clipboard_clear"), \
                mock.patch.object(grab.gui, "clipboard_read",
                                  return_value=""), \
                mock.patch.object(grab.gui, "click"), \
                mock.patch.object(grab, "_dismiss_opened_article") as dismiss:
            with self.assertRaises(grab._CardSkipped) as ctx:
                grab._open_and_copy(CFG, 600.0, 100.0, [])
        # 内容问题，不是界面异常：不计入 auto_fail_streak，连续 2 张广告
        # 不该凑满失败次数误报退出码 3。
        self.assertEqual(ctx.exception.kind, "no_link")
        dismiss.assert_called_once_with(CFG)


class AutoGrabSkipStreakTests(unittest.TestCase):
    """广告卡连续出现时不该误报退出码 3，但连跳兜底不能被放宽。"""

    def _cards(self, n):
        return [SimpleNamespace(x=680.0, y=286.0 + 140 * i,
                                fp=f"fp{i}") for i in range(n)]

    def _run(self, n):
        def no_link(*args, **kwargs):
            raise grab._CardSkipped("菜单里没有「复制链接」", None,
                                    args[2], kind="no_link")

        from pathlib import Path
        with mock.patch.object(grab, "preflight"), \
                mock.patch.object(nav, "to_feed"), \
                mock.patch.object(nav, "_shot",
                                  return_value="/tmp/wd-shot.png"), \
                mock.patch.object(nav, "read_cards",
                                  return_value=self._cards(n)), \
                mock.patch.object(nav, "looks_like_account_list",
                                  return_value=False), \
                mock.patch.object(nav, "recover", return_value=True), \
                mock.patch.object(grab, "_crossed_day", return_value=False), \
                mock.patch.object(grab, "_screen_texts", return_value=[]), \
                mock.patch.object(grab, "_open_and_copy",
                                  side_effect=no_link), \
                mock.patch.object(grab, "_write_urls",
                                  return_value=(Path("/tmp/urls.json"), [])), \
                mock.patch.object(grab, "_persist_ledger", return_value={}), \
                mock.patch.object(grab, "_focus"), \
                mock.patch.object(grab, "_sleep"), \
                mock.patch.object(grab.gui, "scroll"), \
                mock.patch.object(grab.feed_vision, "fps_close",
                                  return_value=False):
            # 单屏用例：完备性改造后跨屏会重新点开（靠 URL 去重），
            # 这里只验证「一屏内连续 no_link 该不该停轮」。
            return grab.grab_auto(CFG, 10, max_rounds=1, verbose=False)

    def test_two_no_link_cards_do_not_abort(self):
        """旧实现把它们算进 auto_fail_streak=2，直接停轮。"""
        result = self._run(2)
        self.assertEqual(result["new_this_round"], 0)
        self.assertEqual([s["kind"] for s in result["skipped"]],
                         ["no_link", "no_link"])

    def test_four_consecutive_skips_still_abort(self):
        """菜单整体失效时仍要有连跳上限，不能一味放宽。"""
        with self.assertRaises(grab.GrabAborted):
            self._run(4)


if __name__ == "__main__":
    unittest.main()
