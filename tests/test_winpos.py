#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""窗口层级判断的单元测试（纯数据，不碰真实屏幕）。"""

from __future__ import annotations

import unittest

from src import winpos


def _w(index, owner, x, y, w, h, *, layer=0, onscreen=True, wid=None):
    return {"index": index, "id": wid if wid is not None else 1000 + index,
            "layer": layer, "onscreen": onscreen,
            "x": x, "y": y, "w": w, "h": h, "owner": owner}


class TestFindWechat(unittest.TestCase):
    def test_picks_largest_wechat_window(self):
        ws = [
            _w(0, "微信", 375, 116, 598, 640),      # 「我」小窗
            _w(1, "微信", 0, 30, 1280, 727),        # 主窗口
            _w(2, "WorkBuddy", 40, 30, 1200, 727),
        ]
        main = winpos.find_wechat(ws)
        self.assertEqual(main["w"], 1280)

    def test_none_when_no_window(self):
        self.assertIsNone(winpos.find_wechat([_w(0, "WorkBuddy", 0, 0, 100, 100)]))

    def test_none_when_info_unavailable(self):
        self.assertIsNone(winpos.find_wechat(None))


class TestBlockers(unittest.TestCase):
    def test_window_in_front_counts_as_blocker(self):
        main = _w(2, "微信", 0, 30, 1280, 727)
        ws = [
            _w(0, "WorkBuddy", 40, 30, 1200, 727),
            _w(1, "别的东西", 0, 0, 10, 10),
            main,
        ]
        names = [w["owner"] for w in winpos.blockers(main, ws)]
        self.assertEqual(names, ["WorkBuddy"])      # 只有和主窗口重叠的才算

    def test_window_behind_is_not_blocker(self):
        main = _w(0, "微信", 0, 30, 1280, 727)
        ws = [main, _w(1, "WorkBuddy", 40, 30, 1200, 727)]
        self.assertEqual(winpos.blockers(main, ws), [])
        self.assertTrue(winpos.is_front(main, ws))


class TestFrontAndFreePoint(unittest.TestCase):
    def test_covered_window_is_not_front(self):
        main = _w(1, "微信", 0, 30, 1280, 727)
        ws = [_w(0, "WorkBuddy", 0, 30, 1280, 727), main]
        self.assertFalse(winpos.is_front(main, ws))
        self.assertIsNone(winpos.free_point(main, ws))

    def test_partially_covered_finds_visible_pixel(self):
        # 2026-11-07 行为收紧：free_point 的 x 候选限制在窗口中央带
        # (0.25~0.75 倍宽)。微信是无边框自定义窗口，左右边缘全是功能控件
        # （左侧聊天列表条目、右上通知/切换按钮），早先「露边就点」会误触
        # 控件把公众号聚合页切成号列表形态。中央带被完全挡住时返回 None，
        # 让 ensure_front 报错停轮等人 —— 安全优先于硬点。
        main = _w(1, "微信", 0, 30, 1280, 727)
        ws = [_w(0, "WorkBuddy", 40, 30, 1200, 727), main]
        self.assertFalse(winpos.is_front(main, ws))
        self.assertIsNone(winpos.free_point(main, ws))

    def test_center_strip_visible_returns_center_point(self):
        # 中央带露出来时，点标题栏中央（只抬升窗口，不碰两侧控件区）。
        main = _w(1, "微信", 0, 30, 1280, 727)
        ws = [_w(0, "WorkBuddy", 700, 300, 500, 400), main]
        pt = winpos.free_point(main, ws)
        self.assertIsNotNone(pt)
        x, y = pt
        self.assertGreaterEqual(x, main["w"] * 0.4)
        self.assertLessEqual(x, main["w"] * 0.6)
        self.assertEqual(y, main["y"] + 12)

    def test_unknown_info_is_not_treated_as_broken(self):
        main = _w(0, "微信", 0, 30, 1280, 727)
        self.assertTrue(winpos.is_front(main, None))
        self.assertEqual(winpos.free_point(main, None),
                         (640.0, 42.0))


if __name__ == "__main__":
    unittest.main()


class TestOtherAppOnly(unittest.TestCase):
    def test_wechat_own_windows_are_not_blockers(self):
        """微信自己的文章窗口盖在主窗口上不算遮挡（点击仍然打给微信）。"""
        main = _w(2, "微信", 0, 30, 1280, 727)
        ws = [
            _w(0, "微信", 483, -528, 1206, 569),   # 文章窗口
            _w(1, "WorkBuddy", 224, -900, 1200, 727),  # 完全跑到屏幕外的别家窗口
            main,
        ]
        self.assertEqual(winpos.blockers(main, ws), [])
        self.assertTrue(winpos.is_front(main, ws))
