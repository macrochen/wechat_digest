#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""屏幕几何自检的判定测试（纯数据，不截真屏）。

背景：抓取的像素判据全部建立在「截图空间 == CGEvent 空间 == 1280x832」
这个前提上。分辨率变化、宽高比变化、扩展屏被 screencapture 拼进同一张图，
都不会让程序报错——只会静默点到别处。这里锁住的就是那几条判据。
"""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from src import gui

EXPECT = {"expected_w": 1280, "expected_h": 832}


def _disp(w=1280, h=832, scale=1.0, displays=1):
    return {"x": 0.0, "y": 0.0, "w": w, "h": h,
            "px": int(w * scale), "py": int(h * scale),
            "scale": scale, "displays": displays}


def _codes(issues):
    return [i["code"] for i in issues]


class TestJudgeGeometry(unittest.TestCase):
    def test_normal_1x_passes(self):
        ok, issues = gui.judge_geometry(
            display=_disp(), raw_size=(1280, 832), **EXPECT)
        self.assertTrue(ok, issues)
        self.assertEqual(issues, [])

    def test_retina_2x_equal_ratio_passes(self):
        """本机实测就是这种：逻辑 1280x832，截图实际 2560x1664。"""
        ok, issues = gui.judge_geometry(
            display=_disp(scale=1.0), raw_size=(2560, 1664), **EXPECT)
        self.assertTrue(ok, issues)

    def test_other_logical_resolution_rejected(self):
        """1512x982 这类同为 1.54 比例的屏，比例判据看不出问题，
        但 CGEvent 坐标空间变了，必须靠逻辑尺寸判据拦下。"""
        ok, issues = gui.judge_geometry(
            display=_disp(w=1512, h=982, scale=2.0), raw_size=(3024, 1964),
            **EXPECT)
        self.assertFalse(ok)
        self.assertEqual(_codes(issues), ["logical_mismatch"])

    def test_scaled_resolution_without_display_info(self):
        """查不到显示器信息时，靠截图比例兜底。"""
        ok, issues = gui.judge_geometry(raw_size=(1280, 800), **EXPECT)
        self.assertFalse(ok)
        self.assertEqual(_codes(issues), ["aspect_mismatch"])

    def test_sidecar_stitched_left_right_rejected(self):
        """iPad 随航放在右侧：screencapture 拍出 2304x832 的拼合图。"""
        ok, issues = gui.judge_geometry(
            display=_disp(displays=2), raw_size=(2304, 832), **EXPECT)
        self.assertFalse(ok)
        self.assertEqual(_codes(issues), ["aspect_mismatch"])
        self.assertIn("扩展屏", issues[0]["message"])

    def test_stitched_vertical_rejected(self):
        ok, issues = gui.judge_geometry(
            display=_disp(displays=2), raw_size=(1280, 1664), **EXPECT)
        self.assertFalse(ok)
        self.assertEqual(_codes(issues), ["aspect_mismatch"])

    def test_tolerance_boundary(self):
        """默认 1% 容差：0.5% 的偏差放行，2% 的偏差拦下。"""
        ok, _ = gui.judge_geometry(raw_size=(1287, 832), **EXPECT)
        self.assertTrue(ok, "0.5% 偏差应在容差内")
        ok, issues = gui.judge_geometry(raw_size=(1306, 832), **EXPECT)
        self.assertFalse(ok, "2% 偏差应被拦下")
        self.assertEqual(_codes(issues), ["aspect_mismatch"])

    def test_custom_tolerance_is_honoured(self):
        ok, _ = gui.judge_geometry(raw_size=(1306, 832), tolerance=0.05, **EXPECT)
        self.assertTrue(ok, "放宽到 5% 后 2% 偏差应放行")

    def test_unknown_never_blocks(self):
        """什么都查不到时放行：诊断信息缺失不该拦住正常抓取。"""
        ok, issues = gui.judge_geometry(**EXPECT)
        self.assertTrue(ok)
        self.assertEqual(issues, [])

    def test_zero_sizes_do_not_crash(self):
        ok, issues = gui.judge_geometry(
            display=_disp(w=0, h=0), raw_size=(0, 0), **EXPECT)
        self.assertTrue(ok, issues)


class TestPngSize(unittest.TestCase):
    def _fake_png(self, w, h) -> Path:
        blob = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
                + struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00"
                + b"\x00\x00\x00\x00")
        f = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        f.write(blob)
        f.close()
        return Path(f.name)

    def test_reads_ihdr(self):
        p = self._fake_png(2560, 1664)
        try:
            self.assertEqual(gui.png_size(p), (2560, 1664))
        finally:
            p.unlink()

    def test_rejects_non_png(self):
        p = Path(tempfile.mkstemp()[1])
        p.write_bytes(b"not a png at all, definitely not")
        try:
            self.assertIsNone(gui.png_size(p))
        finally:
            p.unlink()

    def test_missing_file(self):
        self.assertIsNone(gui.png_size("/tmp/definitely-not-here-12345.png"))


if __name__ == "__main__":
    unittest.main()
