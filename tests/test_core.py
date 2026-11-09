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

    def test_title_dash_allowed_body_dash_rejected(self):
        """标题必须原样照抄，原文标题里的 `——` 不能算违规。"""
        with tempfile.TemporaryDirectory() as tmp:
            p = self._pending(tmp, [
                "## [1. 《马失前蹄 —— 碳酸锂期权总结》小马白话期权](http://a)\n\n"
                "正文里没有破折号，只有正常句子。"])
            out = prompt.merge_batches(p)
            self.assertIn("—— 碳酸锂期权总结", out)

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


class TestOffered(unittest.TestCase):
    """投递台账：进过清单就算看过，只有「同一批重扫」才允许再进来一次。"""

    def test_same_run_not_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "offered.json"
            with mock.patch("src.state.project_path", return_value=target):
                off = state.Offered()
                arts = _articles(2)
                off.mark(arts, "run-A", "2026-11-07")
                # 同一批链接重扫（run_key 相同）不算重复，允许重建候选池
                self.assertFalse(off.has(arts[0]["url"], "run-A"))
                # 换一批新链接（哪怕里面夹着老文章）就当作已看过
                self.assertTrue(off.has(arts[0]["url"], "run-B"))

    def test_unknown_url_and_times(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "offered.json"
            with mock.patch("src.state.project_path", return_value=target):
                off = state.Offered()
                self.assertFalse(off.has("https://mp.weixin.qq.com/s/none", "run-A"))
                arts = _articles(1)
                off.mark(arts, "run-A", "2026-11-07")
                first = off.data[arts[0]["url"]]["first_at"]
                off.mark(arts, "run-A", "2026-11-07")
                rec = state.Offered().data[arts[0]["url"]]
                self.assertEqual(rec["times"], 2)
                self.assertEqual(rec["first_at"], first)  # 首次投递时间不被覆盖

    def test_backfill_from_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            inbox = {"date": "2026-11-06", "run_key": "2026-11-06-old",
                     "articles": _articles(2)}
            (out / "inbox-2026-11-06.json").write_text(
                json.dumps(inbox, ensure_ascii=False), encoding="utf-8")
            (out / "_raw_urls.json").write_text(json.dumps(
                {"urls": ["https://mp.weixin.qq.com/s/only_url"]}),
                encoding="utf-8")
            target = Path(tmp) / "offered.json"
            with mock.patch("src.state.project_path", return_value=target):
                off = state.Offered()
                added, used = off.backfill(out)
                self.assertEqual(added, 3)
                self.assertIn("inbox-2026-11-06.json", used)
                # 补录的老文章对任何新批次都算「看过」
                self.assertTrue(off.has(_articles(1)[0]["url"], "2026-11-07-new"))
                self.assertTrue(off.has("https://mp.weixin.qq.com/s/only_url",
                                        "2026-11-07-new"))
                # 再次回填不重复计数
                self.assertEqual(off.backfill(out)[0], 0)


class TestScanOfferedFilter(unittest.TestCase):
    """scan 要真的把「上次投递过但没总结」的文章过滤掉，并自动打开清单页。

    统一加 --confirm 压回人工：这里测的是 offered 台账，默认自动模式
    会接着跑 hydrate，反而把关注点带偏。
    """

    def _scan(self, root: Path, urls: list[str]):
        from src.main import run
        out = root / "out"
        out.mkdir(parents=True, exist_ok=True)
        (out / "_raw_urls.json").write_text(
            json.dumps({"urls": urls}), encoding="utf-8")

        def fake_pp(*parts):
            return root.joinpath(*parts)

        def fake_meta(url):
            return {"url": url, "title": f"标题-{url[-6:]}",
                    "source": "某公众号", "published_at": "2026-11-07 08:00"}

        with mock.patch("src.main.project_path", side_effect=fake_pp), \
                mock.patch("src.state.project_path", side_effect=fake_pp), \
                mock.patch("src.main.article.fetch_meta", side_effect=fake_meta), \
                mock.patch("src.main.blacklist.load", return_value=[]), \
                mock.patch("src.main._open_listing") as opened:
            code = run(["scan", "--date", "2026-11-07", "--no-open",
                        "--confirm"])
        return code, json.loads(
            (out / "inbox-2026-11-07.json").read_text(encoding="utf-8"))

    def test_offered_filtered_on_next_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = ["https://mp.weixin.qq.com/s/aaa", "https://mp.weixin.qq.com/s/bbb"]
            code, box = self._scan(root, old)
            self.assertEqual(code, 0)
            self.assertEqual(len(box["articles"]), 2)
            self.assertEqual(box["skipped_offered"], [])

            # 第二批：夹着两篇上次没勾选的旧文 + 一篇新文
            code, box = self._scan(root, old + ["https://mp.weixin.qq.com/s/ccc"])
            self.assertEqual(code, 0)
            self.assertEqual([a["url"] for a in box["articles"]],
                             ["https://mp.weixin.qq.com/s/ccc"])
            self.assertEqual(len(box["skipped_offered"]), 2)

    def test_same_batch_rescan_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            urls = ["https://mp.weixin.qq.com/s/aaa"]
            self._scan(root, urls)
            _, box = self._scan(root, urls)  # 同一批重扫：修元信息失败用
            self.assertEqual(len(box["articles"]), 1)
            self.assertEqual(box["skipped_offered"], [])

    def test_include_offered_opt_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = ["https://mp.weixin.qq.com/s/aaa"]
            self._scan(root, old)
            from src.main import run

            def fake_pp(*parts):
                return root.joinpath(*parts)

            with mock.patch("src.main.project_path", side_effect=fake_pp), \
                    mock.patch("src.state.project_path", side_effect=fake_pp), \
                    mock.patch("src.main.article.fetch_meta",
                               side_effect=lambda u: {"url": u, "title": "t",
                                                      "source": "s"}), \
                    mock.patch("src.main.blacklist.load", return_value=[]), \
                    mock.patch("src.main._open_listing"):
                run(["scan", "--date", "2026-11-07", "--include-offered",
                     "--no-open", "--confirm"])
            box = json.loads((root / "out" / "inbox-2026-11-07.json")
                             .read_text(encoding="utf-8"))
            self.assertEqual(len(box["articles"]), 1)
            self.assertEqual(box["skipped_offered"], [])


class TestWechatAppName(unittest.TestCase):
    """前台 App 名判定要能吃下中英文两种写法。

    微信 CFBundleDisplayName 是 `WeChat`，窗口所有者名却是「微信」，
    用 `== "微信"` 会永远认为「微信不在前台」。
    """

    def test_accepts_both_names(self):
        from src import gui
        for name in ("微信", "WeChat", "wechat"):
            self.assertTrue(gui.is_wechat_app(name), name)

    def test_rejects_others_and_empty(self):
        from src import gui
        for name in ("loginwindow", "WorkBuddy", "WeChatWork", "", None):
            self.assertFalse(gui.is_wechat_app(name), name)


class TestOpenAndCopyRecovery(unittest.TestCase):
    """点击落空后的恢复路径必须能跑到 Space 检查。

    2026-11-08 实盘踩坑：_open_and_copy 把 `from . import nav` 写在函数
    结尾（只给 close_article 用），Python 因此把 nav 判成局部变量；而
    「点击后文章窗口没打开」的恢复路径先引用 nav.ensure_onscreen，
    必然抛 UnboundLocalError，被包装成「Space 检查失败」误报成界面问题。
    这条路径平时走不到，所以原有的 159 个单测没覆盖。
    """

    def test_recovery_reaches_ensure_onscreen(self):
        from src import feed_vision, grab, nav

        cal = feed_vision.Calibration(
            given_y=285.0, used_y=285.0, status="disabled",
            detail="标题行校准已关闭")
        cfg = {"ui": {"open_wait": 0}}

        with mock.patch.object(grab, "_probe_title_y", return_value=cal), \
                mock.patch.object(grab, "_guard_title",
                                  side_effect=lambda c, x, y, used: y), \
                mock.patch.object(grab, "_focus"), \
                mock.patch.object(grab, "_sleep"), \
                mock.patch.object(grab, "_article_window_state",
                                  return_value=False), \
                mock.patch.object(grab.gui, "clipboard_clear"), \
                mock.patch.object(grab.gui, "click"), \
                mock.patch.object(grab.gui, "activate_wechat"), \
                mock.patch.object(nav, "ensure_onscreen") as onscreen:
            with self.assertRaises(grab._CardFailed) as ctx:
                grab._open_and_copy(cfg, 545, 285, set())

        # Space 检查真的被调用了；失败原因是「窗口始终没打开」这个预期
        # 的停轮结果，而不是 nav 未绑定导致的「Space 检查失败」。
        self.assertEqual(onscreen.call_count, 1)
        self.assertNotIn("Space 检查失败", str(ctx.exception))
        self.assertIn("文章窗口始终没有打开", str(ctx.exception))


class TestCloseArticleSafety(unittest.TestCase):
    """Cmd+W 打给主窗口会把微信整个关掉（2026-11-08 实测两次）。"""

    def test_unknown_window_state_never_sends_cmdw(self):
        """读不到窗口信息时不知道快捷键会打给谁，宁可不按。"""
        from src import nav
        with mock.patch.object(nav, "article_window_open", return_value=None), \
                mock.patch.object(nav.gui, "cmd_key") as cmd, \
                mock.patch.object(nav, "_sleep"):
            self.assertFalse(nav.close_article({"ui": {}}, tries=2))
        self.assertEqual(cmd.call_count, 0)

    def test_no_article_does_not_send_cmdw(self):
        from src import nav
        with mock.patch.object(nav, "article_window_open", return_value=False), \
                mock.patch.object(nav, "ensure_focus"), \
                mock.patch.object(nav.gui, "cmd_key") as cmd, \
                mock.patch.object(nav, "_sleep"):
            self.assertTrue(nav.close_article({"ui": {}}, tries=2))
        self.assertEqual(cmd.call_count, 0)


if __name__ == "__main__":
    unittest.main()
