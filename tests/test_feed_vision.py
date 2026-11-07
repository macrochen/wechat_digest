#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""点击前标题行校准的单元测试（合成 PNG，不依赖真实截图与 GUI 权限）。"""

from __future__ import annotations

import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from src import feed_vision, grab

W, H = 640, 416              # 半分辨率探测图
LW, LH = 1280, 832           # 逻辑分辨率


def _chunk(typ: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + typ + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))


def _write_png(path: Path, pixels: bytearray) -> Path:
    """写 8-bit RGB、filter=0、非隔行 PNG。"""
    raw = bytearray()
    for y in range(H):
        raw.append(0)
        raw += pixels[y * W * 3:(y + 1) * W * 3]
    data = (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + _chunk(b"IEND", b""))
    path.write_bytes(data)
    return path


def _make_feed(path: Path, *, extra_bands: bool = False) -> Path:
    """仿信息流卡片：浅灰来源名行 + 下方深色加粗标题行。"""
    px = bytearray(b"\xff" * (W * H * 3))

    def rect(y0, y1, x0, x1, color):
        for y in range(y0, y1):
            for x in range(x0, x1):
                i = (y * W + x) * 3
                px[i:i + 3] = bytes(color)

    # 逻辑 y≈146~152：公众号名（浅灰，深色阈值 120 判不到）
    rect(73, 76, 260, 340, (160, 160, 160))
    # 逻辑 y≈180~192：加粗标题
    rect(90, 96, 260, 440, (35, 35, 35))

    if extra_bands:
        # 逻辑 y≈280~284：弱带（「余下 N 篇」之类），离得更近但不是标题
        rect(140, 142, 260, 272, (40, 40, 40))
        # 逻辑 y≈310~322：真正的标题带，明显更强
        rect(155, 161, 260, 400, (30, 30, 30))
    return _write_png(path, px)


class TestDecodePng(unittest.TestCase):
    def test_rgb_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = _make_feed(Path(d) / "feed.png")
            img = feed_vision.decode_png(p)
            self.assertEqual((img.w, img.h, img.ch), (W, H, 3))
            # 深色标题带的中心像素应是近黑
            row = img.rows[92]
            self.assertLess(row[300 * 3], 60)
            # 浅灰来源名不算深色
            self.assertEqual(feed_vision._dark_count(img, 74, 260, 340), 0)
            self.assertGreater(feed_vision._dark_count(img, 92, 260, 440), 100)

    def test_rejects_bad_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.png"
            p.write_bytes(b"not a png")
            with self.assertRaises(ValueError):
                feed_vision.decode_png(p)


class TestCalibrate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = _make_feed(Path(self.tmp.name) / "feed.png")

    def tearDown(self):
        self.tmp.cleanup()

    def _cal(self, y, **kw):
        return feed_vision.calibrate(
            self.path, y, logical_width=LW, logical_height=LH, **kw)

    def test_source_line_moves_down_to_title(self):
        c = self._cal(150)
        self.assertEqual(c.status, "moved_down")
        self.assertTrue(c.moved)
        self.assertAlmostEqual(c.used_y, 184, delta=4)
        self.assertNotEqual(c.given_y, c.used_y)

    def test_already_on_title_stays_put(self):
        c = self._cal(184)
        self.assertEqual(c.status, "on_title")
        self.assertEqual(c.used_y, 184)

    def test_no_band_keeps_original(self):
        c = self._cal(600)
        self.assertEqual(c.status, "no_title")
        self.assertEqual(c.used_y, 600)

    def test_missing_image_degrades_gracefully(self):
        c = feed_vision.calibrate(
            Path(self.tmp.name) / "nope.png", 150,
            logical_width=LW, logical_height=LH)
        self.assertEqual(c.status, "error")
        self.assertEqual(c.used_y, 150)

    def test_strong_band_wins_over_nearby_weak_band(self):
        with tempfile.TemporaryDirectory() as d:
            p = _make_feed(Path(d) / "feed.png", extra_bands=True)
            c = feed_vision.calibrate(
                p, 270, logical_width=LW, logical_height=LH)
            self.assertEqual(c.status, "moved_down")
            self.assertAlmostEqual(c.used_y, 316, delta=4)

    def test_can_be_disabled_by_search_range(self):
        c = self._cal(150, search_down=0, search_up=0)
        self.assertEqual(c.status, "no_title")
        self.assertEqual(c.used_y, 150)


class TestGrabIntegration(unittest.TestCase):
    def test_probe_disabled_does_not_touch_screen(self):
        cfg = {"ui": {"title_probe": 0, "logical_width": LW,
                      "logical_height": LH}}
        with mock.patch.object(grab.gui, "screenshot",
                               side_effect=AssertionError("不应截屏")):
            c = grab._probe_title_y(cfg, 150)
        self.assertEqual(c.status, "disabled")
        self.assertEqual(c.used_y, 150)

    def test_dry_run_reports_probe_state_without_gui(self):
        cfg = {"ui": {"title_probe": 1, "logical_width": LW,
                      "logical_height": LH,
                      "more_button": [1258, 101], "copy_link": [1110, 300],
                      "close_window": [76, 108]}}
        with mock.patch.object(grab.gui, "screenshot",
                               side_effect=AssertionError("不应截屏")):
            r = grab.grab(cfg, [(560, 150)], 20, dry_run=True)
        self.assertTrue(r["dry_run"])
        self.assertTrue(r["title_probe"])

    def test_abort_leaves_evidence_and_guides_user(self):
        cfg = {"ui": {"logical_width": LW, "logical_height": LH}}
        cal = feed_vision.Calibration(150, 182, "moved_down", "下移到标题行")
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out"
            with mock.patch.object(
                    grab, "project_path",
                    side_effect=lambda *parts: out / parts[-1]), \
                 mock.patch.object(
                    grab.gui, "screenshot",
                    side_effect=lambda p, w, h: Path(p).write_bytes(b"x")):
                with self.assertRaises(grab.GrabAborted) as ctx:
                    grab._abort([(560, 150)], 1, "剪贴板为空", [],
                                cfg=cfg, cal=cal, used_y=182)
            msg = str(ctx.exception)
            self.assertIn("公众号主页", msg)
            self.assertIn("Cmd+W", msg)
            self.assertIn("现场截图", msg)
            self.assertIn("y=150 → 实际 y=182", msg)
            self.assertTrue((out / "_raw_urls_partial.json").exists())


if __name__ == "__main__":
    unittest.main()
