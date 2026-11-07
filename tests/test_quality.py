#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无标题 / 闲聊文章的过滤规则测试。"""

from __future__ import annotations

import unittest

from src import quality


class TestWhyNotTitle(unittest.TestCase):
    def test_normal_article_passes(self):
        self.assertEqual(quality.why_not_title(
            {"source": "人物", "title": "厉娜：女孩子之间的关系，很敏感"}), "")

    def test_empty_source_dropped(self):
        self.assertIn("公众号名", quality.why_not_title(
            {"source": "", "title": "随便聊聊"}))

    def test_body_as_title_dropped(self):
        self.assertIn("换行", quality.why_not_title(
            {"source": "个人随想录",
             "title": "大周末的，和你们聊几句归义军。\n\n归义军是唐末到宋初"}))

    def test_overlong_title_dropped(self):
        self.assertIn("80", quality.why_not_title(
            {"source": "某号", "title": "字" * 81}))

    def test_short_title_is_fine(self):
        self.assertEqual(quality.why_not_title(
            {"source": "歪圈儿", "title": "AI一天8个鬼故事"}), "")


class TestFilterArticles(unittest.TestCase):
    def test_split_keeps_and_drops(self):
        kept, dropped = quality.filter_articles([
            {"source": "人物", "title": "正常标题"},
            {"source": "", "title": "闲聊"},
            {"source": "某号", "title": "正文\n换行"},
        ])
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(dropped), 2)
        self.assertTrue(all(a.get("reason") for a in dropped))


if __name__ == "__main__":
    unittest.main()
