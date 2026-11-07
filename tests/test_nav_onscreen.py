# -*- coding: utf-8 -*-
"""nav.ensure_onscreen 的测试：微信不在当前屏幕时强制切回。

判定以截图内容为准（read_cards / read_icons 特征），窗口 onscreen 标志
只作辅助通道 —— mock 时按返回值组合覆盖四条路径。
"""

import unittest
from unittest import mock

from src import nav


def _w(owner="微信", layer=0, on=1, x=0, y=30, w=1280, h=727):
    return {"index": 0, "id": 1, "layer": layer, "onscreen": bool(on),
            "x": x, "y": y, "w": w, "h": h, "owner": owner}


class EnsureOnscreenTests(unittest.TestCase):
    def _run(self, *, ui_visible_seq, windows_seq=None):
        """ui_visible_seq: 每次 _wechat_ui_visible 依次返回的值。

        windows_seq: 每次 winpos.windows() 依次返回（列表或 None）；
        默认恒返回一个 on=1 主窗口。
        """
        ui_it = iter(ui_visible_seq)
        if windows_seq is None:
            windows_fn = lambda **kw: [_w(on=1)]  # noqa: E731
        else:
            ws_it = iter(windows_seq)
            windows_fn = lambda **kw: next(ws_it)  # noqa: E731
        with mock.patch.object(nav, "_wechat_ui_visible",
                               side_effect=lambda c: next(ui_it)), \
                mock.patch.object(nav.winpos, "windows",
                                  side_effect=windows_fn), \
                mock.patch.object(nav.subprocess, "run") as run, \
                mock.patch.object(nav, "_sleep"):
            result = nav.ensure_onscreen({}, verbose=False)
        return result, run.call_count

    def test_ui_visible_immediate(self):
        result, opens = self._run(ui_visible_seq=[True])
        self.assertTrue(result["onscreen"])
        self.assertEqual(result["evidence"], "screenshot")
        self.assertEqual(opens, 0)          # 本来就在，不该 open -a

    def test_window_flag_fallback_when_ui_not_recognized(self):
        # 截图无微信特征（如聊天页变体），但窗口标志在屏 → 放行
        result, opens = self._run(ui_visible_seq=[False])
        self.assertTrue(result["onscreen"])
        self.assertEqual(result["evidence"], "window_flag")
        self.assertEqual(opens, 0)

    def test_offscreen_forced_back_by_screenshot(self):
        # 屏上无微信 + 标志离屏 → open -a → 复验截图出现微信特征
        result, opens = self._run(
            ui_visible_seq=[False, False, True],
            windows_seq=[[_w(on=0)], [_w(on=0)], [_w(on=0)]])
        self.assertTrue(result["onscreen"])
        self.assertEqual(result["evidence"], "screenshot")
        self.assertEqual(opens, 1)

    def test_never_comes_back_raises(self):
        with mock.patch.object(nav, "_wechat_ui_visible",
                               side_effect=lambda c: False), \
                mock.patch.object(nav.winpos, "windows",
                                  side_effect=lambda **kw: [_w(on=0)]), \
                mock.patch.object(nav.subprocess, "run"), \
                mock.patch.object(nav, "_sleep"):
            with self.assertRaises(Exception):
                nav.ensure_onscreen({}, verbose=False)

    def test_missing_window_and_no_ui_raises_to_ensure_front_path(self):
        # 既没微信特征也没有任何主窗口（被关掉）→ 反复 open -a 后停轮
        with mock.patch.object(nav, "_wechat_ui_visible",
                               side_effect=lambda c: False), \
                mock.patch.object(nav.winpos, "windows",
                                  side_effect=lambda **kw: None), \
                mock.patch.object(nav.subprocess, "run"), \
                mock.patch.object(nav, "_sleep"):
            with self.assertRaises(Exception):
                nav.ensure_onscreen({}, verbose=False)


if __name__ == "__main__":
    unittest.main()
