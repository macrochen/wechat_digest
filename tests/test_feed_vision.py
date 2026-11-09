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
from src.config import DEFAULTS

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
        # preflight 会真的去看屏幕是否锁定 / 微信是否在跑，测试不该依赖
        # 本机当时的屏幕状态（锁屏时曾把这条用例误判成失败）。
        with mock.patch.object(grab, "preflight"), \
             mock.patch.object(grab.gui, "screenshot",
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


def _make_bands(path: Path, specs) -> Path:
    """按 image 像素画若干深色矩形。specs: [(y0,y1,x0,x1,color)]。"""
    px = bytearray(b"\xff" * (W * H * 3))
    for y0, y1, x0, x1, color in specs:
        for y in range(y0, y1):
            for x in range(x0, x1):
                i = (y * W + x) * 3
                px[i:i + 3] = bytes(color)
    return _write_png(path, px)


class TestFindAllTitleBands(unittest.TestCase):
    def test_filters_short_noise_and_tall_avatar(self):
        """只保留标题高度的带：4px 小字噪点、30px 头像都要被过滤。"""
        with tempfile.TemporaryDirectory() as d:
            p = _make_bands(Path(d) / "feed.png", [
                (90, 96, 260, 440, (30, 30, 30)),     # 标题，逻辑高 10
                (70, 72, 260, 300, (40, 40, 40)),     # 4px 噪点
                (110, 125, 260, 300, (35, 35, 35)),   # 30px 头像
            ])
            bands = feed_vision.find_all_title_bands(
                p, logical_width=LW, logical_height=LH)
            self.assertEqual([round(b.center) for b in bands], [185])


class TestSnap(unittest.TestCase):
    def test_compressed_coords_map_to_distinct_titles(self):
        """视觉模型压缩间距后的坐标，应按顺序落到 3 条不同的标题行。"""
        with tempfile.TemporaryDirectory() as d:
            p = _make_bands(Path(d) / "feed.png", [
                (90, 96, 260, 440, (30, 30, 30)),     # 逻辑 185
                (154, 160, 260, 440, (30, 30, 30)),   # 逻辑 313
                (218, 224, 260, 440, (30, 30, 30)),   # 逻辑 441
            ])
            used, got = [], []
            for y in (175, 240, 305):                 # 压缩后的 y
                c = feed_vision.snap(p, y, logical_width=LW,
                                     logical_height=LH, used_centers=used)
                got.append(c.used_y)
                used.append(c.used_y)
            self.assertEqual(len(set(round(g) for g in got)), 3)
            for g, want in zip(got, (185, 313, 441)):
                self.assertAlmostEqual(g, want, delta=6)

    def test_skips_title_already_used(self):
        """已经点过的标题行不能再被吸回来，要跳到下一条。"""
        with tempfile.TemporaryDirectory() as d:
            p = _make_bands(Path(d) / "feed.png", [
                (90, 96, 260, 440, (30, 30, 30)),     # 185
                (154, 160, 260, 440, (30, 30, 30)),   # 313
            ])
            c = feed_vision.snap(p, 185, logical_width=LW,
                                 logical_height=LH, used_centers=[185])
            self.assertAlmostEqual(c.used_y, 313, delta=6)


class TestFindCardTitles(unittest.TestCase):
    def test_keeps_real_titles_and_merges_card_lines(self):
        """只留真标题：弱的小字带、头像块都要被过滤，x 落在文字中间。"""
        with tempfile.TemporaryDirectory() as d:
            p = _make_bands(Path(d) / "feed.png", [
                (60, 77, 250, 300, (35, 35, 35)),     # 头像（逻辑高 34）
                (120, 126, 250, 440, (30, 30, 30)),   # 卡片1 标题（逻辑 240~252）
                (150, 153, 250, 262, (40, 40, 40)),   # 「余下 1 篇」弱带
                (184, 190, 250, 420, (30, 30, 30)),   # 卡片2 标题（逻辑 368~380）
            ])
            cards = feed_vision.find_card_titles(
                p, logical_width=LW, logical_height=LH)
            self.assertEqual(len(cards), 2)
            self.assertAlmostEqual(cards[0].y, 246, delta=4)
            self.assertAlmostEqual(cards[1].y, 374, delta=4)
            # 点击点落在标题文字的横向中间（逻辑 x 690 附近）
            self.assertAlmostEqual(cards[0].x, 690, delta=6)
            self.assertGreater(cards[0].peak, 55)

    def test_two_line_title_counts_as_one_card(self):
        with tempfile.TemporaryDirectory() as d:
            p = _make_bands(Path(d) / "feed.png", [
                (120, 126, 250, 440, (30, 30, 30)),   # 第 1 行
                (132, 138, 250, 440, (30, 30, 30)),   # 第 2 行（间距 24 逻辑像素）
                (230, 236, 250, 440, (30, 30, 30)),   # 下一张卡片
            ])
            cards = feed_vision.find_card_titles(
                p, logical_width=LW, logical_height=LH)
            self.assertEqual(len(cards), 2)
            self.assertAlmostEqual(cards[0].y, 246, delta=4)


class TestFindIconBlocks(unittest.TestCase):
    def test_finds_blue_entry_and_ignores_noise(self):
        with tempfile.TemporaryDirectory() as d:
            p = _make_bands(Path(d) / "feed.png", [
                (190, 208, 36, 54, (40, 110, 230)),   # 「公众号」图标 36×36
                (60, 62, 40, 47, (40, 110, 230)),     # 小蓝色噪点
                (200, 206, 200, 240, (60, 60, 60)),   # 深色文字，不是图标
            ])
            icons = feed_vision.find_icon_blocks(
                p, logical_width=LW, logical_height=LH)
            self.assertEqual(len(icons), 1)
            self.assertAlmostEqual(icons[0].x, 90, delta=4)
            self.assertAlmostEqual(icons[0].y, 397, delta=6)
            self.assertGreater(icons[0].w, 30)


class TestNav(unittest.TestCase):
    # 以 DEFAULTS 为底板再覆盖：形态识别要读 account_toggle_* 等一批实测量，
    # 手写最小 dict 的话，将来加一个坐标键就会在测试里 KeyError，
    # 而真正的实盘代码其实没坏。
    CFG = {"ui": {**DEFAULTS["ui"],
                  "logical_width": LW, "logical_height": LH,
                  "nav_tries": 3, "nav_wait": 0.1, "close_wait": 0.1,
                  "card_min_peak": 55, "card_min_y": 200}}

    def _shots(self, d, *images):
        """按顺序返回的截图内容（最后一次之后重复最后一张）。"""
        seq = list(images)
        state = {"i": 0}

        def fake(path, w, h):
            src = seq[min(state["i"], len(seq) - 1)]
            state["i"] += 1
            Path(path).write_bytes(Path(src).read_bytes())
            return Path(path)
        return fake

    def test_clicks_blue_icon_until_feed_shows(self):
        from src import nav
        with tempfile.TemporaryDirectory() as d:
            lst = _make_bands(Path(d) / "list.png", [
                (190, 208, 36, 54, (40, 110, 230)),   # 只有会话列表，没有卡片
            ])
            feed = _make_bands(Path(d) / "feed.png", [
                (120, 126, 250, 440, (30, 30, 30)),
                (184, 190, 250, 440, (30, 30, 30)),
            ])
            with mock.patch.object(nav, "_preflight"), \
                 mock.patch.object(nav, "ensure_onscreen"), \
                 mock.patch.object(nav, "ensure_front"), \
                 mock.patch.object(nav.gui, "activate_wechat"), \
                 mock.patch.object(nav.gui, "screenshot",
                                   side_effect=self._shots(d, lst, feed)), \
                 mock.patch.object(nav.gui, "click") as click:
                r = nav.to_feed(self.CFG, verbose=False)
            self.assertTrue(r["in_feed"])
            self.assertEqual(r["clicks"], 1)
            self.assertEqual(len(r["cards"]), 2)
            click.assert_called_once()
            self.assertAlmostEqual(click.call_args[0][0], 90, delta=4)

    def test_already_in_feed_does_not_click(self):
        from src import nav
        with tempfile.TemporaryDirectory() as d:
            feed = _make_bands(Path(d) / "feed.png", [
                (120, 126, 250, 440, (30, 30, 30)),
                (184, 190, 250, 440, (30, 30, 30)),
            ])
            with mock.patch.object(nav, "_preflight"), \
                 mock.patch.object(nav, "ensure_onscreen"), \
                 mock.patch.object(nav, "ensure_front"), \
                 mock.patch.object(nav.gui, "activate_wechat"), \
                 mock.patch.object(nav.gui, "screenshot",
                                   side_effect=self._shots(d, feed)), \
                 mock.patch.object(nav.gui, "click") as click:
                r = nav.to_feed(self.CFG, verbose=False)
            self.assertTrue(r["in_feed"])
            self.assertEqual(r["clicks"], 0)
            click.assert_not_called()

    def test_missing_icon_aborts_instead_of_blind_clicking(self):
        from src import nav
        with tempfile.TemporaryDirectory() as d:
            blank = _make_bands(Path(d) / "blank.png", [])
            with mock.patch.object(nav, "_preflight"), \
                 mock.patch.object(nav, "ensure_onscreen"), \
                 mock.patch.object(nav, "ensure_front"), \
                 mock.patch.object(nav.gui, "activate_wechat"), \
                 mock.patch.object(nav.gui, "screenshot",
                                   side_effect=self._shots(d, blank)), \
                 mock.patch.object(nav.gui, "click") as click:
                with self.assertRaises(grab.GrabAborted) as ctx:
                    nav.to_feed(self.CFG, verbose=False)
            click.assert_not_called()
            self.assertIn("公众号", str(ctx.exception))


class TestCardTitles(unittest.TestCase):
    """短标题回收 + 跨屏指纹（2026-11-08 漏点 / 重复点修复）。"""

    def _cards(self, path, **kw):
        kw.setdefault("logical_width", LW)
        kw.setdefault("logical_height", LH)
        kw.setdefault("min_y", 100)
        return feed_vision.find_card_titles(path, **kw)

    def test_short_title_recovered(self):
        """八九个字的短标题峰值不到 55，也要能认出来。"""
        with tempfile.TemporaryDirectory() as d:
            p = _make_bands(Path(d) / "feed.png", [
                (90, 96, 260, 440, (30, 30, 30)),    # 强标题，逻辑 y≈185
                (150, 156, 260, 310, (30, 30, 30)),  # 短标题，宽 50px，逻辑 y≈305
            ])
            cards = self._cards(p)
            self.assertEqual([round(c.y) for c in cards], [185, 305])

    def test_noise_near_strong_title_is_not_a_card(self):
        """同一张卡片里的「余下 N 篇」离强标题很近，不能当标题。"""
        with tempfile.TemporaryDirectory() as d:
            p = _make_bands(Path(d) / "feed.png", [
                (90, 96, 260, 440, (30, 30, 30)),    # 强标题，y≈185
                (105, 108, 260, 310, (30, 30, 30)),  # 小字，y≈211，间距 26
            ])
            cards = self._cards(p)
            self.assertEqual([round(c.y) for c in cards], [185])

    def test_misaligned_weak_band_ignored(self):
        """「常看的号」头像下的名字不左对齐，不能当标题。"""
        with tempfile.TemporaryDirectory() as d:
            p = _make_bands(Path(d) / "feed.png", [
                (90, 96, 260, 440, (30, 30, 30)),    # 卡片标题，x0 逻辑 520
                (150, 156, 400, 450, (30, 30, 30)),  # 居中排在头像下的名字
            ])
            cards = self._cards(p)
            self.assertEqual([round(c.y) for c in cards], [185])

    def test_fingerprint_is_stable_and_discriminating(self):
        with tempfile.TemporaryDirectory() as d:
            a = _make_bands(Path(d) / "a.png", [(90, 96, 260, 440, (30, 30, 30))])
            b = _make_bands(Path(d) / "b.png", [(150, 156, 260, 440, (30, 30, 30))])
            c = _make_bands(Path(d) / "c.png", [(90, 96, 260, 300, (30, 30, 30))])
            fa = self._cards(a)[0].fp
            fb = self._cards(b)[0].fp       # 同一行文字，只是换了 y
            fc = self._cards(c)[0].fp       # 文字更短
            self.assertTrue(fa and fb and fc)
            self.assertTrue(feed_vision.fps_close(fa, fb))
            self.assertFalse(feed_vision.fps_close(fa, fc))

    def test_fps_close_rejects_bad_input(self):
        self.assertFalse(feed_vision.fps_close("", "abc"))
        self.assertFalse(feed_vision.fps_close("abc", "abcd"))


class TestDeletedPage(unittest.TestCase):
    """文章被发布者删除：整页近白 + 居中黄色感叹号图标。"""

    def _page(self, d, name, specs):
        return _make_bands(Path(d) / name, specs)

    def test_deleted_page_detected(self):
        with tempfile.TemporaryDirectory() as d:
            # 图标 35x35 图像像素（= 70 逻辑 px），居中
            p = self._page(d, "deleted.png", [
                (165, 200, 300, 335, (245, 200, 20)),
            ])
            info = feed_vision.looks_like_deleted_page(
                p, logical_width=LW, logical_height=LH)
            self.assertTrue(info["deleted"], info)
            self.assertGreater(info["white_ratio"], 0.9)

    def test_plain_text_article_is_not_deleted(self):
        """纯文字白底文章没有黄色图标，不能误判。"""
        with tempfile.TemporaryDirectory() as d:
            p = self._page(d, "text.png", [
                (60, 66, 120, 520, (30, 30, 30)),
                (80, 86, 120, 500, (30, 30, 30)),
                (100, 106, 120, 480, (30, 30, 30)),
            ])
            info = feed_vision.looks_like_deleted_page(
                p, logical_width=LW, logical_height=LH)
            self.assertFalse(info["deleted"], info)

    def test_yellow_picture_on_colored_page_is_not_deleted(self):
        """有黄色配图但不是白底（正常图片文章），不能误判。"""
        with tempfile.TemporaryDirectory() as d:
            p = self._page(d, "pic.png", [
                (0, 416, 0, 640, (60, 120, 160)),      # 整页蓝灰背景
                (165, 200, 300, 335, (245, 200, 20)),  # 黄色块
            ])
            info = feed_vision.looks_like_deleted_page(
                p, logical_width=LW, logical_height=LH)
            self.assertFalse(info["deleted"], info)

    def test_rect_excludes_icon_outside_window(self):
        """图标不在给定窗口矩形内时不算命中（避免把别的窗口算进来）。"""
        with tempfile.TemporaryDirectory() as d:
            p = self._page(d, "deleted.png", [
                (165, 200, 300, 335, (245, 200, 20)),
            ])
            info = feed_vision.looks_like_deleted_page(
                p, logical_width=LW, logical_height=LH,
                rect=(0, 0, 400, 800))        # 窗口只覆盖左半屏
            self.assertFalse(info["deleted"], info)


class TestCopyLinkPoint(unittest.TestCase):
    def _groups(self, sizes):
        return [[feed_vision.MenuItem(y=200.0 + i * 27, x=1100.0, peak=60)
                 for i in range(n)] for n in sizes]

    def test_uses_second_item_of_second_group(self):
        pt = grab._copy_link_point(self._groups([3, 2, 2]), (1258, 101))
        self.assertEqual(pt, (1258 - 110, 227.0))

    def test_single_item_group_is_not_clicked(self):
        """第二组只有一项时结构已经不对，宁可判「没有链接」也不盲点。"""
        self.assertIsNone(grab._copy_link_point(self._groups([3, 1]), (1258, 101)))

    def test_one_group_only_is_not_clicked(self):
        self.assertIsNone(grab._copy_link_point(self._groups([3]), (1258, 101)))

    def test_empty_menu_is_not_clicked(self):
        self.assertIsNone(grab._copy_link_point([], (1258, 101)))


class TestAutoSkip(unittest.TestCase):
    """自动抓取里「文章被删」应跳过换下一张，而不是整轮停下。"""

    def setUp(self):
        # 不要把台账 / 闸门写进真实 out/。
        p = mock.patch.object(grab, "_persist_ledger", return_value={})
        p.start()
        self.addCleanup(p.stop)

    CFG = {"ui": {**DEFAULTS["ui"],
                  "logical_width": LW, "logical_height": LH,
                  # 完备性改造后跨屏一律重新点开（靠 URL 去重），这些单屏
                  # 行为用例只跑一屏，避免 mock 的返回序列被第二屏耗尽。
                  "auto_rounds": 1, "auto_fail_streak": 2,
                  "auto_skip_streak": 4, "title_probe": 1}}

    def _cards(self, n):
        # fp 要**明显不同**：fps_close 的容差是 4 个字符，短指纹会被误认成同一行
        return [feed_vision.CardTitle(x=560, y=240 + i * 128, peak=120,
                                      lines=1, x0=520, x1=900,
                                      fp=str(i) * 8)
                for i in range(n)]

    def test_deleted_card_skipped_then_next_gathered(self):
        from src import nav
        cal = feed_vision.Calibration(240, 246, "on_title", "已在标题行")
        url = "https://mp.weixin.qq.com/s/xyz"
        seq = [grab._CardSkipped("文章已被发布者删除", cal, 246, kind="deleted"),
               (url, cal, 374, True)]

        def fake_open(*a, **kw):
            item = seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch.object(grab, "preflight"), \
             mock.patch.object(grab, "_focus"), \
             mock.patch.object(nav, "to_feed"), \
             mock.patch.object(nav, "_shot", return_value="/tmp/x.png"), \
             mock.patch.object(nav, "read_cards",
                               return_value=self._cards(2)), \
             mock.patch.object(nav, "recover", return_value=True), \
             mock.patch.object(grab, "_screen_texts", return_value=[]), \
             mock.patch.object(grab, "_open_and_copy", side_effect=fake_open), \
             mock.patch.object(grab, "_write_urls",
                               return_value=(Path("/tmp/_raw_urls.json"), [url])), \
             mock.patch.object(grab.gui, "scroll"):
            r = grab.grab_auto(self.CFG, 2, verbose=False)
        self.assertEqual(r["count"], 1)
        self.assertEqual([s["kind"] for s in r["skipped"]], ["deleted"])

    def test_too_many_skips_stops_instead_of_looping(self):
        """整屏都是被删的文章时仍要停（跳过上限），但不能报成「连续失败」。"""
        from src import nav
        cal = feed_vision.Calibration(240, 246, "on_title", "已在标题行")
        cards = self._cards(6)

        def always_skip(*a, **kw):
            raise grab._CardSkipped("文章已被发布者删除", cal, 246,
                                    kind="deleted")

        with mock.patch.object(grab, "preflight"), \
             mock.patch.object(grab, "_focus"), \
             mock.patch.object(nav, "to_feed"), \
             mock.patch.object(nav, "_shot", return_value="/tmp/x.png"), \
             mock.patch.object(nav, "read_cards", return_value=cards), \
             mock.patch.object(nav, "recover", return_value=True), \
             mock.patch.object(grab, "_screen_texts", return_value=[]), \
             mock.patch.object(grab, "_open_and_copy", side_effect=always_skip), \
             mock.patch.object(grab, "_write_urls",
                               return_value=(Path("/tmp/_raw_urls.json"), [])), \
             mock.patch.object(grab.gui, "scroll"):
            with self.assertRaises(grab.GrabAborted) as ctx:
                grab.grab_auto(self.CFG, 6, verbose=False)
        msg = str(ctx.exception)
        self.assertIn("没有「复制链接」", msg)
        self.assertIn("跳过", msg)
        self.assertNotIn("连续 2 张都没复制到链接", msg)


if __name__ == "__main__":
    unittest.main()
