#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三页共用导航的回归测试。

2026-11-08 用户实盘发现的 bug：/review?scope=EarlETF 上能看到「选文清单 /
本地阅读 / 参数设置」三项，跳到 /settings 或 /read 就只剩两项。原因是
nav_html() 把「选文清单」的显隐绑在 has_review 上，而 do_GET 的 has_review
又取决于「当前请求 scope 下有没有待确认清单」——/settings 没带 ?scope=，
退回服务启动时的 scope，找不到按号抓取那份候选池，于是页签被砍掉。
"""

from __future__ import annotations

import re
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from src import review

FOUR = ("选文清单", "本轮文章", "本地阅读", "参数设置")


def _links(page: str) -> list[str]:
    # 只看顶部导航里的 href，避免正文里的链接混进来
    m = re.search(r'<nav class="wdnav".*?</nav>', page, re.S)
    return re.findall(r'href="([^"]+)"', m.group(0)) if m else []


class TestNavHtml(unittest.TestCase):
    def test_four_entries_regardless_of_has_review(self):
        """候选池有没有、显不显示，导航都必须是四项（含本轮文章）。"""
        for has_review in (False, True, None):
            with self.subTest(has_review=has_review):
                nav = review.nav_html("settings", has_review)
                for text in FOUR:
                    self.assertIn(text, nav)

    def test_scope_is_on_every_link_including_empty(self):
        nav = review.nav_html("read", scope="EarlETF")
        self.assertEqual(_links(nav), ["/review?scope=EarlETF",
                                       "/grab?scope=EarlETF",
                                       "/read?scope=EarlETF",
                                       "/settings?scope=EarlETF"])
        # 空 scope 也必须带参数：不带会退回服务启动时的 scope，看错候选池
        self.assertEqual(_links(review.nav_html("read", scope="")),
                         ["/review?scope=", "/grab?scope=",
                          "/read?scope=", "/settings?scope="])

    def test_chinese_scope_is_url_encoded(self):
        self.assertIn("/review?scope=%E7%8C%AB%E7%AC%94%E5%88%80",
                      _links(review.nav_html("review", scope="猫笔刀")))

    def test_active_entry_is_highlighted(self):
        nav = review.nav_html("settings", scope="")
        self.assertIn('href="/settings?scope=" class="on"', nav)
        self.assertNotIn('href="/review?scope=" class="on"', nav)

    def test_none_scope_keeps_plain_links_for_legacy_callers(self):
        self.assertEqual(_links(review.nav_html("read")),
                         ["/review", "/grab", "/read", "/settings"])

    def test_grab_entry_is_highlighted(self):
        nav = review.nav_html("grab", scope="")
        self.assertIn('href="/grab?scope=" class="on"', nav)
        self.assertNotIn('href="/review?scope=" class="on"', nav)


class TestEndToEnd(unittest.TestCase):
    """真实起一次本地服务，复现「/settings 没带 scope」那条路径。"""

    def setUp(self):
        self._old_scope = review._ACTIVE_SCOPE
        review._ACTIVE_SCOPE = ""
        handler = type("H", (review._Handler,), {
            "inbox": {}, "state": {}, "has_review": False, "dynamic": True,
        })
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        review._ACTIVE_SCOPE = self._old_scope

    def _get(self, path: str) -> tuple[int, str]:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{path}", timeout=3) as r:
            return r.status, r.read().decode("utf-8")

    def test_settings_and_read_keep_all_four_entries_without_scope(self):
        # 模拟 bug 现场：启动 scope 是信息流那份，按号抓取的清单在另一个 scope，
        # 于是 /settings（不带 ?scope=）判成「没有待确认清单」。
        with mock.patch.object(review, "current_inbox", return_value=None), \
             mock.patch.object(review, "latest_inbox", return_value=None):
            for path in ("/settings", "/read", "/", "/review", "/grab"):
                with self.subTest(path=path):
                    code, page = self._get(path)
                    self.assertEqual(code, 200)
                    for text in FOUR:
                        self.assertIn(text, page)
                    self.assertEqual(len(_links(page)), 4)

    def test_scope_survives_when_moving_between_pages(self):
        with mock.patch.object(review, "current_inbox", return_value=None), \
             mock.patch.object(review, "latest_inbox", return_value=None):
            for path in ("/settings?scope=EarlETF", "/read?scope=EarlETF",
                         "/review?scope=EarlETF", "/grab?scope=EarlETF"):
                with self.subTest(path=path):
                    _, page = self._get(path)
                    self.assertEqual(
                        _links(page),
                        ["/review?scope=EarlETF", "/grab?scope=EarlETF",
                         "/read?scope=EarlETF", "/settings?scope=EarlETF"])

    def test_grab_page_shows_only_this_round(self):
        """用户 2026-11-09 拍板：本轮文章页只列新文章，重复内容连数据都不带。"""
        fake = {
            "date": "2026-11-09", "scope": "", "generated_at": "17:00:00",
            "run_key": "k1", "phase": "scan", "inbox_count": 1,
            "summary": {"total": 2, "unique_urls": 2, "url_dup_groups": 0,
                        "title_dup_groups": 0, "cross_round": 1, "in_inbox": 1,
                        "counts": {}, "ledger": {}},
            "dup_groups": [],
            "articles": [
                {"index": 1, "url": "https://mp.weixin.qq.com/s/AAA",
                 "title": "甲文章", "source": "号A", "published_at": "2026-11-09",
                 "status": "in_inbox", "status_label": "进本轮清单",
                 "url_dup": False, "cross_round": False, "title_dup": False},
                {"index": 2, "url": "https://mp.weixin.qq.com/s/BBB",
                 "title": "乙文章", "source": "号B", "published_at": "2026-11-08",
                 "status": "seen", "status_label": "已总结过（跳过）",
                 "url_dup": False, "cross_round": True, "title_dup": False},
            ],
        }
        with mock.patch.object(review, "latest_grab_review", return_value=fake):
            code, page = self._get("/grab?scope=")
        self.assertEqual(code, 200)
        self.assertIn("甲文章", page)
        # 已总结过的文章、重复统计、折叠入口、放行按钮一律不出现
        for text in ("乙文章", "已总结过", "跨轮重复", "URL 重复",
                     "标题重复", "折叠", "id=\"submit\""):
            with self.subTest(text=text):
                self.assertNotIn(text, page)
        self.assertNotIn("mp.weixin.qq.com/s/BBB", page)  # 内嵌 JSON 也不带


if __name__ == "__main__":
    unittest.main()
