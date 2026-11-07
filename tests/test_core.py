#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核心逻辑单测：全部用构造数据，不碰微信、不发网络请求、不改真实台账。"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import article, prompt, publish, selection, state


def _articles(n, prefix="文章"):
    return [{
        "index": i, "title": f"{prefix}{i}：一个完全不同的主题和细节描述",
        "url": f"https://mp.weixin.qq.com/s/test{i}",
        "source": f"公众号{i}", "published_at": "2026-11-07 08:00",
        "content": f"这是第{i}篇文章的正文，讲的内容彼此完全不同。" * 30,
    } for i in range(1, n + 1)]


class TestSimilarity(unittest.TestCase):
    def test_shingles_and_jaccard(self):
        a = state.shingles("同一篇文章被两个号转载了")
        b = state.shingles("同一篇文章被两个号转载了")
        c = state.shingles("完全无关的另外一件事")
        self.assertAlmostEqual(state.jaccard(a, b), 1.0)
        self.assertLess(state.jaccard(a, c), 0.3)

    def test_dedup_similar_keeps_first(self):
        base = _articles(3)
        base[2]["content"] = base[0]["content"]  # 第 3 篇是第 1 篇的转载
        kept, dropped = state.dedup_similar(base, 0.75)
        self.assertEqual([a["index"] for a in kept], [1, 2])
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["kept_url"], base[0]["url"])
        self.assertAlmostEqual(dropped[0]["score"], 1.0)


class TestClean(unittest.TestCase):
    def test_strip_junk_keeps_body(self):
        raw = ("今天聊聊市场的三条线索。\n"
               "扫码关注公众号\n长按识别二维码\n广告\n"
               "点个在看\n往期回顾\n第二段讲估值和风险。\n阅读原文")
        out = article.strip_junk(raw)
        self.assertIn("今天聊聊市场的三条线索", out)
        self.assertIn("第二段讲估值和风险", out)
        for junk in ("扫码关注", "长按识别", "往期回顾", "阅读原文", "点个在看"):
            self.assertNotIn(junk, out)

    def test_extract_content_only_js_content(self):
        page = ('<html><body><p>页面外文字</p>'
                '<div id="js_content"><p>正文第一段</p>'
                '<script>垃圾脚本</script><p>正文第二段</p></div>'
                '<p>页面外结尾</p></body></html>')
        out = article.extract_content(page)
        self.assertIn("正文第一段", out)
        self.assertIn("正文第二段", out)
        self.assertNotIn("页面外文字", out)
        self.assertNotIn("垃圾脚本", out)

    def test_parse_time_from_ct(self):
        html = 'var ct = "1794011782";'
        self.assertRegex(article._parse_time(html),
                         r"^2026-11-07 \d{2}:\d{2}$")


class TestBatches(unittest.TestCase):
    def test_20_articles_split_into_2(self):
        batches = prompt.make_batches(_articles(20), 15, 3)
        self.assertEqual(len(batches), 2)
        self.assertEqual(sum(len(b) for b in batches), 20)
        self.assertTrue(all(len(b) <= 15 for b in batches))

    def test_same_event_cluster_not_split(self):
        arts = _articles(20)
        for i in (6, 12, 18):
            arts[i - 1]["title"] = "英伟达发布新一代芯片：同一场发布会的不同报道"
        batches = prompt.make_batches(arts, 15, 3)
        found = [b for b in batches if sum(
            1 for a in b if "英伟达发布" in a["title"]) == 3]
        self.assertEqual(len(found), 1, "同一个事件簇被拆到不同批了")

    def test_no_articles(self):
        self.assertEqual(prompt.make_batches([], 15, 3), [])


class TestMerge(unittest.TestCase):
    def _pending(self, tmp, texts):
        infos = []
        for i, text in enumerate(texts, 1):
            out = Path(tmp) / f"batch-{i}.md"
            out.write_text(text, encoding="utf-8")
            infos.append({"id": i, "output": str(out), "count": 1,
                          "indices": [i], "file": ""})
        return {"batches": infos}

    def test_renumber_and_separator(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._pending(tmp, [
                "## [7. 《甲》号A](http://a)\n\n正文甲。\n\n---",
                "## [9. 《乙》号B](http://b)\n\n正文乙。\n\n---",
            ])
            out = prompt.merge_batches(p)
            self.assertIn("## [1. 《甲》", out)
            self.assertIn("## [2. 《乙》", out)
            self.assertNotIn("## [7.", out)

    def test_reject_dash_and_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._pending(tmp, ["## [1. 《甲》号A](http://a)\n\n用了一个破折号——不行。"])
            with self.assertRaisesRegex(RuntimeError, "破折号"):
                prompt.merge_batches(p)
            p2 = self._pending(tmp, ["## [1. 《甲》号A](http://a)\n\nTODO 待补"])
            with self.assertRaisesRegex(RuntimeError, "占位符"):
                prompt.merge_batches(p2)

    def test_missing_batch_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._pending(tmp, ["## [1. 《甲》号A](http://a)\n\n正文。"])
            p["batches"].append({"id": 2, "output": str(Path(tmp) / "nope.md")})
            with self.assertRaisesRegex(RuntimeError, "缺第 2 批"):
                prompt.merge_batches(p)


class TestSelection(unittest.TestCase):
    def test_expired_plan_means_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "plan.json"
            with mock.patch("src.selection.plan_path", return_value=plan):
                inbox = {"date": "2026-11-07", "generated_at": "T1",
                         "articles": _articles(3)}
                selection.write_plan(inbox, [1], "manual")
                inbox["generated_at"] = "T2"  # 候选池被重扫覆盖
                self.assertIsNone(selection.read_plan(inbox))
                self.assertEqual(
                    selection.read_plan({"date": "2026-11-07",
                                         "generated_at": "T1"}), [1])


class TestToken(unittest.TestCase):
    def test_read_local_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "secrets.local.json"
            f.write_text(json.dumps({"zenmark_token": "TEST_TOKEN_abc+;=:xyz"}),
                         encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                token, where = publish.read_token({"token_file": str(f)})
            self.assertEqual(token, "TEST_TOKEN_abc+;=:xyz")
            self.assertEqual(where, "secrets.local.json")

    def test_env_overrides_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "secrets.local.json"
            f.write_text(json.dumps({"zenmark_token": "FROM_FILE"}),
                         encoding="utf-8")
            with mock.patch.dict(os.environ, {"ZENMARK_API_TOKEN": "FROM_ENV"}):
                token, where = publish.read_token({"token_file": str(f)})
            self.assertEqual(token, "FROM_ENV")
            self.assertIn("环境变量", where)

    def test_missing_file_raises(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(publish.PublishError):
                publish.read_token({"token_file": "/tmp/definitely-no-token.json"})

    def test_empty_token_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "secrets.local.json"
            f.write_text(json.dumps({"zenmark_token": ""}), encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(publish.PublishError):
                    publish.read_token({"token_file": str(f)})


class TestIsolation(unittest.TestCase):
    def test_src_never_references_external_project(self):
        src_dir = Path(__file__).resolve().parents[1] / "src"
        for f in src_dir.glob("*.py"):
            text = f.read_text(encoding="utf-8")
            for forbidden in ("daily_digest_hub", "sources.yaml", "token_source"):
                self.assertNotIn(forbidden, text,
                                 f"{f.name} 不应出现 {forbidden}")


class TestLedger(unittest.TestCase):
    def test_runs_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "runs.json"
            with mock.patch("src.state.project_path", return_value=target):
                runs = state.Runs()
                self.assertIsNone(runs.done("k1"))
                runs.begin("k1", {"articles": 3})
                runs.finish("k1", {"id": "cloud_1"})
                self.assertEqual(runs.done("k1")["id"], "cloud_1")

    def test_published_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "published.json"
            with mock.patch("src.state.project_path", return_value=target):
                pub = state.Published()
                digest = state.sha256_text("一份简报")
                self.assertIsNone(pub.same("k1", digest))
                pub.mark("k1", digest, {"id": "cloud_x"})
                self.assertEqual(pub.same("k1", digest)["id"], "cloud_x")
                self.assertIsNone(pub.same("k1", state.sha256_text("改过了")))


if __name__ == "__main__":
    unittest.main()
