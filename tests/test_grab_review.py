#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取明细（`src/grab_review.py`）：三层重复判定 + 只读不改台账。

2026-11-09 用户要求「把抓取的所有文章列表放到服务页面中，看一下重复性」。
清单页只显示能总结的新文章，所以单独出一份覆盖全部链接的明细；这里锁住
URL 重复 / 标题重复 / 跨轮重复的口径，以及「绝不写 seen/offered」这条底线。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import grab, grab_review

U1 = "https://mp.weixin.qq.com/s/AAA"
U2 = "https://mp.weixin.qq.com/s/BBB"
U3 = "https://mp.weixin.qq.com/s/CCC"
U4 = "https://mp.weixin.qq.com/s/DDD"


class TestGrabReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "out").mkdir()
        (self.root / "state").mkdir()
        # 台账路径也隔到临时目录，保证单测完全不碰真实项目文件
        self.patcher = mock.patch.object(
            grab_review, "project_path",
            side_effect=lambda *parts: self.root.joinpath(*parts))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def _urls(self, urls):
        (self.root / "out" / "_raw_urls.json").write_text(
            json.dumps({"urls": urls}), encoding="utf-8")

    def _inbox(self, **kw):
        box = {"date": "2026-11-09", "run_key": "k1", "phase": "scan",
               "scope": "", "articles": [], "failed_meta": [],
               "skipped_seen": [], "skipped_offered": [],
               "skipped_blacklist": [], "skipped_notitle": [],
               "skipped_notoday": []}
        box.update(kw)
        (self.root / "out" / "inbox-2026-11-09.json").write_text(
            json.dumps(box, ensure_ascii=False), encoding="utf-8")

    def _ledgers(self, seen=None, offered=None):
        (self.root / "state" / "seen.json").write_text(
            json.dumps(seen or {}, ensure_ascii=False), encoding="utf-8")
        (self.root / "state" / "offered.json").write_text(
            json.dumps(offered or {}, ensure_ascii=False), encoding="utf-8")

    def _article(self, url, title, status_key):
        return {"url": url, "title": title, "source": "号A",
                "published_at": "2026-11-09 08:00"}

    def test_url_level_duplication(self):
        self._urls([U1, U1, U2])
        self._ledgers()
        self._inbox(articles=[self._article(U1, "甲", "in_inbox")])
        d = grab_review.build("2026-11-09")
        s = d["summary"]
        self.assertEqual((s["total"], s["unique_urls"], s["url_dup_groups"]),
                         (3, 2, 1))
        self.assertEqual([r["url_dup"] for r in d["articles"]],
                         [True, True, False])

    def test_title_dup_needs_different_urls(self):
        """全角/空白差异归一后同标题，且必须是不同 URL 才算一组。"""
        self._urls([U1, U2, U3])
        self._ledgers()
        self._inbox(articles=[
            {"url": U1, "title": "同一篇　文章", "source": "号A"},
            {"url": U2, "title": "同一篇文章", "source": "号B"},
            {"url": U3, "title": "另一篇", "source": "号C"}])
        d = grab_review.build("2026-11-09")
        self.assertEqual(d["summary"]["title_dup_groups"], 1)
        self.assertEqual([r["title_dup"] for r in d["articles"]],
                         [True, True, False])
        self.assertEqual(d["dup_groups"][0]["count"], 2)

    def test_same_url_twice_is_not_a_title_group(self):
        self._urls([U1, U1])
        self._ledgers()
        self._inbox()
        d = grab_review.build("2026-11-09")
        self.assertEqual(d["summary"]["url_dup_groups"], 1)
        self.assertEqual(d["summary"]["title_dup_groups"], 0)

    def test_status_classification_priority(self):
        self._urls([U1, U2, U3, "https://mp.weixin.qq.com/s/DDD"])
        self._ledgers()
        self._inbox(
            articles=[{"url": U1, "title": "进清单"}],
            skipped_blacklist=[{"url": U2, "title": "拉黑"}],
            skipped_notoday=[{"url": U3, "title": "窗口外"}],
            skipped_seen=["https://mp.weixin.qq.com/s/DDD"])
        d = grab_review.build("2026-11-09")
        self.assertEqual([r["status"] for r in d["articles"]],
                         ["in_inbox", "blacklist", "window_out", "seen"])
        self.assertEqual(d["summary"]["counts"],
                         {"in_inbox": 1, "blacklist": 1,
                          "window_out": 1, "seen": 1})

    def test_cross_round_comes_from_both_ledgers(self):
        self._urls([U1, U2, U3])
        self._ledgers(
            seen={U1: {"title": "甲", "source": "号A"}},
            offered={U2: {"title": "乙", "source": "号B"}})
        self._inbox(articles=[{"url": U3, "title": "丙"}])
        d = grab_review.build("2026-11-09")
        self.assertEqual([r["cross_round"] for r in d["articles"]],
                         [True, True, False])
        self.assertEqual(d["summary"]["cross_round"], 2)
        # 台账里的标题要能补进「未取到元信息」的空缺
        self.assertEqual(d["articles"][0]["title"], "甲")

    def test_write_is_read_only_for_ledgers(self):
        self._urls([U1])
        self._ledgers(seen={}, offered={})
        self._inbox(articles=[{"url": U1, "title": "甲"}])
        before = {p.name: p.read_text(encoding="utf-8")
                  for p in (self.root / "state").iterdir()}
        p, d = grab_review.write("2026-11-09")
        self.assertTrue(p.exists())
        self.assertEqual(p.name, "_grab_review-2026-11-09.json")
        self.assertEqual(d["summary"]["total"], 1)
        after = {p.name: p.read_text(encoding="utf-8")
                 for p in (self.root / "state").iterdir()}
        self.assertEqual(before, after)

    def test_empty_or_missing_files_do_not_crash(self):
        d = grab_review.build("2026-11-09")
        self.assertEqual(d["summary"]["total"], 0)
        self.assertEqual(d["articles"], [])

    def test_scope_uses_its_own_urls_file(self):
        (self.root / "out" / "_raw_urls-EarlETF.json").write_text(
            json.dumps({"urls": [U1]}), encoding="utf-8")
        self._urls([U2, U3])
        self._ledgers()
        self._inbox()
        self.assertEqual(grab_review.build("2026-11-09", "EarlETF")
                         ["summary"]["total"], 1)
        self.assertEqual(grab_review.build("2026-11-09")["summary"]["total"], 2)


class TestWriteUrlsDropsSeen(unittest.TestCase):
    """用户 2026-11-09 拍板：复制链接后立刻比对台账，已总结过的不再存。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "out").mkdir()
        patcher = mock.patch.object(
            grab, "project_path",
            side_effect=lambda *parts: self.root.joinpath(*parts))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _seen(self, urls):
        class FakeSeen:
            def has(self, url):
                return url in urls
        return mock.patch.object(grab, "Seen", return_value=FakeSeen())

    def _prev(self, urls):
        (self.root / "out" / "_raw_urls.json").write_text(
            json.dumps({"urls": urls}), encoding="utf-8")

    def test_current_and_previous_seen_links_are_dropped(self):
        self._prev([U1, U2])
        with self._seen({U1}):
            path, merged = grab._write_urls([U3, U1, U4], append=True)
        # 顺序保留、去重、已总结过的 U1 从历史与本轮里一起消失
        self.assertEqual(merged, [U2, U3, U4])
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["urls"], [U2, U3, U4])
        # obtained 原样保留：跨屏去重 / 完备性台账还要用
        self.assertEqual(saved["new_this_round"], 3)

    def test_no_append_also_filters(self):
        with self._seen({U2}):
            _, merged = grab._write_urls([U1, U2], append=False)
        self.assertEqual(merged, [U1])

    def test_all_seen_writes_empty_list(self):
        with self._seen({U1, U2}):
            path, merged = grab._write_urls([U1, U2], append=False)
        self.assertEqual(merged, [])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["urls"], [])


if __name__ == "__main__":
    unittest.main()
