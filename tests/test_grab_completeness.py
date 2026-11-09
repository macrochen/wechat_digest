# -*- coding: utf-8 -*-
"""抓取完备性改造（docs/PRD-抓取完备性.md）的回归测试。

按实施顺序分三批：
  P0 主循环：不许靠指纹跳过、不许一屏无新增就收工、跨屏要补、失败要重试；
  P1 闭环：coverage 口径、闸门读写、--resume；
  P2 位移：measure_shift 与目标步长。

所有用例都不碰真实 GUI：nav / gui / _open_and_copy 全部 mock，
遇到退出码 3 的路径按铁律停轮，测试里也照旧断言异常。
"""

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src import feed_vision, grab, grab_ledger, main, nav
from src.config import DEFAULTS

URL = "https://mp.weixin.qq.com/s/"

# grab_auto 结束时会写 out/_grab_ledger-*.json 和闸门文件。单测一律把
# _persist_ledger 换掉，绝不污染真实 out/（尤其不能误写阻断标记拦住真 scan）。
FAKE_LEDGER = {
    "coverage": {"seen": 0, "linked": 0, "duplicate": 0, "no_link": 0,
                 "deleted": 0, "missing": 0, "screens": 0, "unique_cards": 0},
    "ledger": "/tmp/wd-unit-ledger.json",
    "missing_file": "/tmp/wd-unit-missing.json",
    "gate": False, "gate_policy": "block",
}


def fake_persist(*_a, **_kw):
    return dict(FAKE_LEDGER)


def card(i, *, y=None, fp=None, peak=120):
    return SimpleNamespace(x=680.0,
                           y=(280.0 + 132.0 * i) if y is None else float(y),
                           peak=peak, fp=fp if fp is not None else f"fp{i:02d}")


def skipped(kind="no_link", used_y=None):
    return grab._CardSkipped(f"测试用 {kind}", None, used_y, kind=kind)


def failed():
    return grab._CardFailed("标题带守卫拒绝盲点", None, 280.0)


class _GrabHarness:
    """把 grab_auto 的 GUI 依赖全部换成可控的假实现。"""

    def __init__(self, screens, open_effect, *, ui=None, max_rounds=None,
                 overlap=True, fixed=None, texts=None, texts_error=None):
        # grab_auto 在「一屏不足 2 张卡」时会重读一次，单屏用例用 fixed
        # 恒返回同一屏，避免把后面的屏提前消耗掉。
        self.fixed = fixed
        self.screens = [list(s) for s in screens]
        self.last = self.screens[0] if self.screens else []
        self.open_effect = open_effect
        self.ui = ui or {}
        self.max_rounds = max_rounds
        self.overlap = overlap
        # 每屏 OCR 文本（广告标签就在这里）；默认空 = 没有广告卡。
        self.texts = [] if texts is None else texts
        self.texts_error = texts_error
        self.written = []
        self.mocks = {}

    def cfg(self):
        return {"feed_scope": 0, "feed_days": 2,
                "ui": {**DEFAULTS["ui"], "auto_rounds": 8,
                       "stalled_rounds": 3, "title_retry": 2,
                       "title_retry_wait": 0, "scroll_wait": 0,
                       "auto_fail_streak": 2, "auto_skip_streak": 4,
                       **self.ui}}

    def read_cards(self, *_a, **_kw):
        # 回滚补扫会多读一次；屏用完后始终返回最后一屏，模拟「滚到底」。
        if self.fixed is not None:
            return self.fixed
        if len(self.screens) > 1:
            self.last = self.screens.pop(0)
        elif self.screens:
            self.last = self.screens[0]
        return self.last

    def write_urls(self, urls, append):
        self.written.append(list(urls))
        merged = list(dict.fromkeys(sum(self.written, [])))
        return Path("/tmp/wd-unit-urls.json"), merged

    def run(self):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(grab, "preflight"))
            stack.enter_context(mock.patch.object(nav, "to_feed"))
            stack.enter_context(mock.patch.object(nav, "_shot",
                                                  return_value="/tmp/wd-unit.png"))
            stack.enter_context(mock.patch.object(nav, "read_cards",
                                                  side_effect=self.read_cards))
            stack.enter_context(mock.patch.object(
                nav, "looks_like_account_list", return_value=False))
            stack.enter_context(mock.patch.object(nav, "recover",
                                                  return_value=True))
            stack.enter_context(mock.patch.object(
                grab, "_open_and_copy", side_effect=self.open_effect))
            stack.enter_context(mock.patch.object(
                grab, "_write_urls", side_effect=self.write_urls))
            # 广告卡拦截每屏要 OCR 一次；单测不碰真实 Vision，默认「无广告」。
            ocr_patch = (mock.patch.object(grab, "_screen_texts",
                                           side_effect=self.texts_error)
                         if self.texts_error else
                         mock.patch.object(grab, "_screen_texts",
                                           return_value=self.texts))
            stack.enter_context(ocr_patch)
            stack.enter_context(mock.patch.object(grab, "_focus"))
            stack.enter_context(mock.patch.object(grab, "_sleep"))
            stack.enter_context(mock.patch.object(grab.gui, "scroll"))
            stack.enter_context(mock.patch.object(
                grab, "_persist_ledger", side_effect=fake_persist))
            self.mocks["back"] = stack.enter_context(
                mock.patch.object(grab, "_scroll_back_half"))
            if self.overlap is not None:
                stack.enter_context(mock.patch.object(
                    grab, "_overlap_with", return_value=self.overlap))
            kw = {"verbose": False}
            if self.max_rounds is not None:
                kw["max_rounds"] = self.max_rounds
            return grab.grab_auto(self.cfg(), 20, **kw)


class P0SimilarFingerprintTests(unittest.TestCase):
    """A1：诊断图里两篇不同短标题的指纹汉明距离只有 3，绝不能被误跳过。"""

    def test_similar_fp_in_same_screen_are_both_opened(self):
        # 指纹只有 2 个 hex 字符不同，fps_close(容差 4) 会判「同一行」。
        a = card(0, fp="a1b2c3d4")
        b = card(1, fp="a1b2c3XX")
        self.assertTrue(feed_vision.fps_close(a.fp, b.fp, 4))

        calls = []

        def open_effect(_cfg, _x, y, _used, **_kw):
            calls.append(y)
            return (URL + str(len(calls)), None, y, True)

        r = _GrabHarness([], open_effect, fixed=[a, b], max_rounds=1).run()
        self.assertEqual(len(calls), 2, "两篇不同文章都必须被点开")
        self.assertEqual(r["new_this_round"], 2)
        self.assertEqual([x["outcome"] for x in r["screens"][0]["results"]],
                         ["linked", "linked"])

    def test_same_screen_duplicate_row_by_y_is_not_reclicked(self):
        """同一张卡的两行标题（y 差 <=30px）仍要防重复吸附。"""
        a = card(0, y=280.0, fp="aaaaaaaa")
        b = card(1, y=298.0, fp="bbbbbbbb")
        calls = []

        def open_effect(_cfg, _x, y, _used, **_kw):
            calls.append(y)
            return (URL + str(len(calls)), None, y, True)

        r = _GrabHarness([], open_effect, fixed=[a, b], max_rounds=1).run()
        self.assertEqual(len(calls), 1)
        self.assertEqual(r["screens"][0]["results"][1]["outcome"],
                         "duplicate_screen")


class P0CrossScreenTests(unittest.TestCase):
    """跨屏一律重点，靠 URL 去重，而不是靠「这一屏没有新货」就停。"""

    def test_same_fp_next_screen_is_reopened_and_deduped_by_url(self):
        a = card(0, fp="same-a")
        b = card(1, fp="same-b")
        calls = []

        def open_effect(_cfg, _x, y, _used, **_kw):
            calls.append(y)
            return (URL + "shared", None, y, True)

        r = _GrabHarness([[a, b], [a, b]], open_effect, max_rounds=2).run()
        self.assertEqual(len(calls), 4, "跨屏不能靠指纹跳过，两屏共点 4 次")
        self.assertEqual(r["new_this_round"], 1, "同一 URL 只算一篇")
        self.assertEqual(r["screens"][1]["results"][0]["outcome"], "duplicate")

    def test_one_screen_without_new_links_does_not_stop(self):
        """旧实现 new_links==0 立刻 break，第二屏的内容根本滚不到。"""
        calls = []

        def open_effect(_cfg, _x, y, _used, **_kw):
            calls.append(y)
            if len(calls) <= 2:
                raise skipped()
            return (URL + "late", None, y, True)

        r = _GrabHarness([[card(0), card(1)], [card(2), card(3)]],
                         open_effect, max_rounds=2).run()
        self.assertEqual(r["new_this_round"], 1)
        self.assertGreaterEqual(r["rounds"], 2)

    def test_stalled_rounds_stops_eventually(self):
        """不能无限滚：连续 stalled_rounds 屏无新增才判到底。"""
        def open_effect(_cfg, _x, y, _used, **_kw):
            raise skipped(used_y=y)

        h = _GrabHarness([[card(0), card(1)]] * 8, open_effect,
                         ui={"stalled_rounds": 3}, max_rounds=8)
        r = h.run()
        self.assertEqual(r["stop_reason"], "stalled")
        self.assertEqual(r["rounds"], 3)


class P0RetryAndMissingTests(unittest.TestCase):
    """守卫拒点要重试；重试耗尽才算缺口，且不能把内容问题当界面异常重试。"""

    def test_guard_failure_retries_then_succeeds(self):
        calls = []

        def open_effect(_cfg, _x, y, _used, **_kw):
            calls.append(y)
            if len(calls) < 3:
                raise failed()
            return (URL + "retried", None, y, True)

        r = _GrabHarness([], open_effect, fixed=[card(0)],
                         max_rounds=1).run()
        self.assertEqual(len(calls), 3)
        self.assertEqual(r["new_this_round"], 1)
        self.assertEqual(r["missing"], [])

    def test_skipped_content_is_not_retried(self):
        calls = []

        def open_effect(_cfg, _x, y, _used, **_kw):
            calls.append(y)
            raise skipped(used_y=y)

        r = _GrabHarness([], open_effect, fixed=[card(0)],
                         max_rounds=1).run()
        self.assertEqual(len(calls), 1, "广告卡已关窗，重试只会重复点击")
        self.assertEqual(r["missing"], [], "广告卡不算缺口")
        self.assertEqual(r["screens"][0]["results"][0]["outcome"], "no_link")

    def test_exhausted_retry_becomes_missing(self):
        def open_effect(_cfg, _x, y, _used, **_kw):
            raise failed()

        r = _GrabHarness([], open_effect, fixed=[card(0)],
                         max_rounds=1).run()
        self.assertEqual(len(r["missing"]), 1)
        self.assertEqual(r["screens"][0]["missing"], 1)
        self.assertEqual(r["screens"][0]["results"][0]["outcome"], "error")


class P0OverlapTests(unittest.TestCase):
    """跨过整屏不再只打印警告，要真的回滚补扫。"""

    def test_no_overlap_triggers_rollback(self):
        def open_effect(_cfg, _x, y, _used, **_kw):
            return (URL + "x", None, y, True)

        # overlap=None 表示不 mock _overlap_with，走真实判据；
        # 两屏 fp 完全不同 -> 第 2 屏判无重叠。
        h = _GrabHarness([[card(0, fp="aaaaaaaa"), card(1, fp="cccccccc")],
                          [card(2, fp="bbbbbbbb"), card(3, fp="dddddddd")]],
                         open_effect, overlap=None, max_rounds=2)
        r = h.run()
        self.assertGreaterEqual(h.mocks["back"].call_count, 1)
        self.assertFalse(r["screens"][1]["overlap_ok"])


class P1CoverageTests(unittest.TestCase):
    """coverage 口径：广告 / 删文 / 重复都不算缺口，未知 outcome 不许被吞。"""

    @staticmethod
    def _screens(outcomes):
        return [{"round": 1,
                 "cards": [{"fp": f"fp{i}"} for i in range(len(outcomes))],
                 "results": [{"outcome": o} for o in outcomes],
                 "overlap_ok": True, "new_urls": 0, "missing": 0}]

    def test_identity_holds_and_only_error_is_missing(self):
        cov = grab_ledger.compute_coverage(self._screens(
            ["linked", "duplicate", "duplicate_screen",
             "no_link", "deleted", "error"]))
        self.assertEqual(cov["seen"], 6)
        self.assertEqual(cov["duplicate"], 2)
        self.assertEqual(cov["no_link"], 1)
        self.assertEqual(cov["deleted"], 1)
        self.assertEqual(cov["missing"], 1)
        self.assertEqual(cov["linked"] + cov["duplicate"] + cov["no_link"] +
                         cov["deleted"] + cov["missing"], cov["seen"])

    def test_all_ads_and_deleted_is_zero_missing(self):
        """否则闸门天天误报，会把自动链路彻底堵死。"""
        cov = grab_ledger.compute_coverage(self._screens(
            ["no_link", "no_link", "deleted"]))
        self.assertEqual(cov["missing"], 0)
        self.assertEqual(cov["seen"], 3)

    def test_unknown_outcome_is_treated_as_missing(self):
        cov = grab_ledger.compute_coverage(self._screens(["weird_new_status"]))
        self.assertEqual(cov["missing"], 1)

    def test_empty_screens_is_zero(self):
        self.assertEqual(grab_ledger.compute_coverage([])["missing"], 0)


class P1GateTests(unittest.TestCase):
    """台账 / 缺口清单 / 闸门读写，全部落在临时目录。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        p = mock.patch.object(grab_ledger, "project_path",
                              side_effect=lambda *parts: root.joinpath(*parts))
        p.start()
        self.addCleanup(p.stop)

    def test_gate_write_read_clear(self):
        self.assertIsNone(grab_ledger.read_gate("2026-11-09"))
        grab_ledger.write_gate("2026-11-09", {"missing": 2})
        self.assertEqual(grab_ledger.read_gate("2026-11-09")["coverage"]["missing"],
                         2)
        self.assertTrue(grab_ledger.clear_gate("2026-11-09"))
        self.assertIsNone(grab_ledger.read_gate("2026-11-09"))
        self.assertFalse(grab_ledger.clear_gate("2026-11-09"))

    def test_missing_and_ledger_roundtrip(self):
        grab_ledger.write_missing("2026-11-09", [{"round": 2, "y": 505.0}])
        self.assertEqual(len(grab_ledger.read_missing("2026-11-09")), 1)
        grab_ledger.write_ledger("2026-11-09", [], {"missing": 0},
                                 stop_reason="window_edge")
        self.assertEqual(grab_ledger.read_ledger("2026-11-09")["stop_reason"],
                         "window_edge")

    def test_scan_blocked_by_gate(self):
        grab_ledger.write_gate("2026-11-09", {"missing": 2})
        self.assertTrue(main._grab_gate_blocks("2026-11-09", "", False,
                                               verbose=False))
        self.assertFalse(main._grab_gate_blocks("2026-11-09", "", True,
                                                verbose=False),
                         "单轮逃生口要能绕过")
        self.assertFalse(main._grab_gate_blocks("2026-11-09", "EarlETF", False,
                                                verbose=False),
                         "按号抓取链不受闸门影响")
        grab_ledger.clear_gate("2026-11-09")
        self.assertFalse(main._grab_gate_blocks("2026-11-09", "", False,
                                                verbose=False))


class P1PersistTests(unittest.TestCase):
    """_persist_ledger：有缺口写闸门；K=0 时 --resume 解除闸门。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        p = mock.patch.object(grab_ledger, "project_path",
                              side_effect=lambda *parts: root.joinpath(*parts))
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def _persist(outcome, **kw):
        screens = [{"round": 1, "cards": [{"fp": "a"}],
                    "results": [{"outcome": outcome}],
                    "overlap_ok": True, "new_urls": 0, "missing": 0}]
        args = dict(stop_reason="window_edge", feed_days=2, resume=False,
                    policy="block", no_gate=False, verbose=False)
        args.update(kw)
        return grab._persist_ledger("2026-11-09", screens,
                                    [{"round": 1, "y": 505.0}], **args)

    def test_error_writes_gate(self):
        info = self._persist("error")
        self.assertEqual(info["coverage"]["missing"], 1)
        self.assertTrue(info["gate"])
        self.assertIsNotNone(grab_ledger.read_gate("2026-11-09"))

    def test_ad_does_not_write_gate(self):
        info = self._persist("no_link")
        self.assertEqual(info["coverage"]["missing"], 0)
        self.assertFalse(info["gate"])
        self.assertIsNone(grab_ledger.read_gate("2026-11-09"))

    def test_warn_policy_and_no_gate_skip_blocking(self):
        self.assertFalse(self._persist("error", policy="warn")["gate"])
        self.assertFalse(self._persist("error", no_gate=True)["gate"])

    def test_resume_with_zero_missing_clears_gate(self):
        grab_ledger.write_gate("2026-11-09", {"missing": 1})
        info = self._persist("linked", resume=True)
        self.assertEqual(info["coverage"]["missing"], 0)
        self.assertIsNone(grab_ledger.read_gate("2026-11-09"))


class P2MeasureShiftTests(unittest.TestCase):
    """A6：用内存里的合成像素测位移，不写任何 PNG、不依赖 tempfile。"""

    H, W = 832, 200

    @staticmethod
    def _strength(k: int) -> int:
        """确定性伪随机、非周期的标题行强度（0 或 40~159 个暗像素）。

        不能用等间距条纹：位移恰好是条纹周期的整数倍时图形完全重合，
        会同时出现多个等价最优解，绝对位移根本不可判（首版夹具的坑）。
        真实截图的标题长短不一，本来就是非周期的。
        """
        r = (k * 1103515245 + 12345) & 0x7FFFFFFF
        if (r >> 16) % 3:
            return 0
        return 40 + (r >> 8) % 120

    def _image(self, shift: int, *, h: int | None = None) -> feed_vision.Image:
        height = self.H if h is None else h
        rows = []
        for y in range(height):
            row = bytearray([255]) * (self.W * 3)
            width = self._strength(y + shift)
            for x in range(20, 20 + width):
                row[x * 3] = row[x * 3 + 1] = row[x * 3 + 2] = 0
            rows.append(row)
        return feed_vision.Image(w=self.W, h=height, ch=3, rows=rows)

    def _measure(self, shift: int, **kw):
        prev, cur = self._image(0), self._image(shift)
        with mock.patch.object(feed_vision, "decode_png",
                               side_effect=[prev, cur]):
            return feed_vision.measure_shift(
                "prev.png", "cur.png", x0=20, x1=180,
                band_h=48, step=2, **kw)

    def test_shift_100_250_500(self):
        for d in (100, 250, 500):
            with self.subTest(shift=d):
                got = self._measure(d)
                self.assertIsNotNone(got, f"{d}px 位移应该测得出")
                self.assertLessEqual(abs(got - d), 8)

    def test_logical_height_scales_result(self):
        got = self._measure(200, logical_height=416)   # 图像高 832 -> 一半
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got, 100, delta=4)

    def test_size_mismatch_returns_none(self):
        prev, cur = self._image(0), self._image(100, h=400)
        with mock.patch.object(feed_vision, "decode_png",
                               side_effect=[prev, cur]):
            self.assertIsNone(feed_vision.measure_shift(
                "a", "b", x0=20, x1=180))

    def test_blank_image_returns_none(self):
        blank = feed_vision.Image(
            w=self.W, h=self.H, ch=3,
            rows=[bytearray([255]) * (self.W * 3) for _ in range(self.H)])
        with mock.patch.object(feed_vision, "decode_png",
                               side_effect=[blank, blank]):
            self.assertIsNone(feed_vision.measure_shift(
                "a", "b", x0=20, x1=180))

    def test_bad_png_returns_none(self):
        with mock.patch.object(feed_vision, "decode_png",
                               side_effect=ValueError("不是 PNG")):
            self.assertIsNone(feed_vision.measure_shift(
                "a", "b", x0=20, x1=180))


class P2TargetStepTests(unittest.TestCase):
    """目标步长 = 2 倍中位卡间距，且不超过 scroll_max_step_px。"""

    def test_two_card_gaps(self):
        cards = [card(0), card(1), card(2)]     # y=280/412/544，间距 132
        self.assertAlmostEqual(grab._target_step(cards, {}), 264.0)

    def test_cap_applies(self):
        self.assertEqual(grab._target_step(
            [card(0), card(1), card(2)], {"scroll_max_step_px": 200}), 200.0)

    def test_single_card_uses_cap(self):
        self.assertEqual(grab._target_step(
            [card(0)], {"scroll_max_step_px": 320}), 320.0)

    def test_tiny_gap_has_floor(self):
        cards = [card(0, y=280.0), card(1, y=290.0)]
        self.assertEqual(grab._target_step(cards, {}), 80.0)


class P2OverlapTightenedTests(unittest.TestCase):
    """旧判据「任意一张对上就算」太松，收紧后要能拦住跨整屏。"""

    def test_prev_last_card_in_front_passes(self):
        cards = [card(0, y=300.0, fp="cccccccc")]
        self.assertTrue(grab._overlap_with(
            cards, ["aaaaaaaa", "cccccccc"], 4, last_fp="cccccccc"))

    def test_single_lucky_match_no_longer_passes(self):
        cards = [card(0, y=600.0, fp="aaaaaaaa")]
        self.assertFalse(grab._overlap_with(
            cards, ["aaaaaaaa", "cccccccc"], 4, last_fp="cccccccc"),
            "只剩 1 张匹配、且上屏最后一张不见 => 判无重叠，触发补扫")

    def test_two_matches_still_pass(self):
        cards = [card(0, y=600.0, fp="aaaaaaaa"),
                 card(1, y=700.0, fp="cccccccc")]
        self.assertTrue(grab._overlap_with(
            cards, ["aaaaaaaa", "cccccccc"], 4, last_fp="zzzzzzzz"))

    def test_first_screen_is_always_ok(self):
        self.assertTrue(grab._overlap_with([card(0)], [], 4))


class P2DynamicGapWiringTests(unittest.TestCase):
    """动态弱带间距默认关闭；开启时 read_cards 要把开关传下去。"""

    def test_default_is_off(self):
        self.assertEqual(DEFAULTS["ui"]["card_dynamic_weak_gap"], 0)

    def test_read_cards_passes_flag(self):
        cfg = {"ui": {**DEFAULTS["ui"], "card_dynamic_weak_gap": 1}}
        with mock.patch.object(nav.feed_vision, "find_card_titles",
                               return_value=[]) as f:
            nav.read_cards(cfg, "dummy.png")
        self.assertTrue(f.call_args.kwargs.get("dynamic_weak_gap"))


class AdCardGuardTests(unittest.TestCase):
    """铁律：广告卡绝对不能点（2026-11-09 用户手动复现）。

    点广告卡会直接跳默认浏览器、把微信窗口从屏幕上弄丢，之后每张卡的
    标题带守卫都会在浏览器 / 桌面的截图里拒点，整轮以退出码 3 报废。
    所以拦截必须发生在**点击之前**，不能等「点开后发现没有复制链接」。
    """

    @staticmethod
    def _label(cy, text="广告、"):
        return SimpleNamespace(text=text, cx=848.0, cy=float(cy))

    @staticmethod
    def _linker(clicked):
        def open_one(_cfg, x, y, used, **_kw):
            clicked.append(y)
            return (URL + str(int(y)), None, y, True)
        return open_one

    def test_ad_card_never_reaches_click(self):
        cards = [card(0), card(1), card(2)]        # y = 280 / 412 / 544
        clicked = []
        h = _GrabHarness([cards], self._linker(clicked), fixed=cards,
                         max_rounds=1, texts=[self._label(390)])
        r = h.run()
        self.assertEqual(clicked, [280.0, 544.0], "广告卡 y=412 绝不能被点")
        self.assertEqual(r["new_this_round"], 2)
        ads = [s for s in r["skipped"] if s["kind"] == "ad"]
        self.assertEqual(len(ads), 1)
        self.assertAlmostEqual(ads[0]["y"], 412.0)
        outcomes = [x["outcome"] for x in r["screens"][0]["results"]]
        self.assertIn("ad_skipped", outcomes)

    def test_normal_stamp_is_not_ad(self):
        cards = [card(0)]
        clicked = []
        texts = [SimpleNamespace(text="39分钟前", cx=848.0, cy=255.0)]
        h = _GrabHarness([cards], self._linker(clicked), fixed=cards,
                         max_rounds=1, texts=texts)
        r = h.run()
        self.assertEqual(clicked, [280.0])
        self.assertFalse([s for s in r["skipped"] if s["kind"] == "ad"])

    def test_ad_skipped_is_not_a_gap(self):
        cov = grab_ledger.compute_coverage([{
            "cards": [],
            "results": [{"y": 412.0, "outcome": "ad_skipped"},
                        {"y": 280.0, "outcome": "linked"}]}])
        self.assertEqual(cov["ad"], 1)
        self.assertEqual(cov["missing"], 0, "广告卡本来就不该点，不算缺口")
        self.assertEqual(cov["seen"], 2)

    def test_ocr_failure_stops_without_clicking(self):
        cards = [card(0)]
        clicked = []
        h = _GrabHarness([cards], self._linker(clicked), fixed=cards,
                         max_rounds=1,
                         texts_error=RuntimeError("OCR 工具挂了"))
        with self.assertRaises(grab.GrabAborted) as ctx:
            h.run()
        self.assertIn("广告卡", str(ctx.exception))
        self.assertEqual(clicked, [], "认不出字时一张都不许点")


if __name__ == "__main__":
    unittest.main()
