#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动模式（抓完直接总结、跳过人工确认）单测。

2026-11-09 起 `auto_summary` **默认 1**，且 scan 在自动模式下写完信号后
**接着跑 hydrate**（抓完直接取正文）。关注四件事：

1. 默认就是自动：scan 自己写全选清单 + 放行信号，并直接产出待总结包；
2. 关掉（`auto_summary=0` 或 `--confirm`）时行为与旧版完全一致 ——
   scan 不写信号、hydrate 照样拒绝；
3. `--no-hydrate` 能只出候选池，信号机制不变；单独跑 hydrate 仍只认信号；
4. 自动模式的信号仍由**同一个** `write_request` 写（mode=auto），
   hydrate 的校验一行没放宽。

全部在临时目录里跑，不碰真实台账、不联网、不开浏览器。
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import settings_ui
from src.config import DEFAULTS
from src.main import run

DATE = "2026-11-07"


def _cfg(**over) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    cfg.update(over)
    return cfg


class _Env:
    """把项目根换到临时目录，并挡掉网络 / GUI / 浏览器。"""

    def __init__(self, root: Path):
        self.root = root
        (root / "out").mkdir(parents=True, exist_ok=True)

    def fake_path(self, *parts):
        return self.root.joinpath(*parts)

    def patches(self, cfg: dict, opened: mock.Mock):
        def fake_meta(url):
            return {"url": url, "title": f"标题-{url[-3:]}",
                    "source": "某公众号", "published_at": f"{DATE} 08:00"}

        # cmd_hydrate 调 build_pending 时会带 scope=...，替身必须收下
        def fake_pending(inbox, kept, run_key, batch_size, max_parallel, **kw):
            return self.fake_pending(run_key, inbox, kept)

        return [
            mock.patch("src.main.project_path", side_effect=self.fake_path),
            mock.patch("src.state.project_path", side_effect=self.fake_path),
            mock.patch("src.selection.project_path", side_effect=self.fake_path),
            mock.patch("src.main.load_settings", return_value=cfg),
            mock.patch("src.main.article.fetch_meta", side_effect=fake_meta),
            # 正文按 url 区分，别让相似度去重把两篇并成一篇
            mock.patch("src.main.article.fetch_content",
                       side_effect=lambda url, n: {
                           "content": f"{url} 的正文，足够长。" * 20,
                           "content_chars": 200, "too_short": False}),
            mock.patch("src.main.prompt.build_pending", side_effect=fake_pending),
            mock.patch("src.main.blacklist.load", return_value=[]),
            mock.patch("src.main._open_listing", opened),
        ]

    def write_urls(self, urls):
        (self.root / "out" / "_raw_urls.json").write_text(
            json.dumps({"urls": urls}), encoding="utf-8")

    def plan(self):
        p = self.root / "out" / "_plan" / f"selected-{DATE}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def request(self):
        p = self.root / "out" / "_plan" / f"summary-request-{DATE}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def inbox(self):
        p = self.root / "out" / f"inbox-{DATE}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def pending(self):
        p = self.root / "out" / "pending_summary.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def fake_pending(self, run_key, inbox, kept):
        """替身：真 build_pending 会读 prompts/summary.md，临时目录里没有。"""
        doc = {"run_key": run_key, "date": inbox["date"], "batches": [],
               "articles": kept, "article_count": len(kept)}
        (self.root / "out" / "pending_summary.json").write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return doc


URLS = ["https://mp.weixin.qq.com/s/a1", "https://mp.weixin.qq.com/s/b2"]


class TestAutoSummary(unittest.TestCase):
    def _call(self, env: _Env, cfg: dict, argv: list[str]):
        opened = mock.Mock()
        patches = env.patches(cfg, opened)
        for p in patches:
            p.start()
        try:
            code = run(argv)
        finally:
            for p in patches:
                p.stop()
        return code, opened

    def _run_scan(self, root: Path, cfg: dict, argv: list[str]):
        env = _Env(root)
        env.write_urls(URLS)
        code, opened = self._call(env, cfg, argv)
        return env, code, opened

    # ---------------------------------------------------------- 默认自动
    def test_default_is_auto_and_runs_hydrate(self):
        """什么都不加：scan 自己放行，并直接产出待总结包。"""
        with tempfile.TemporaryDirectory() as tmp:
            env, code, opened = self._run_scan(
                Path(tmp), _cfg(), ["scan", "--date", DATE, "--no-open"])
            self.assertEqual(code, 0)
            plan, req = env.plan(), env.request()
            self.assertEqual(plan["mode"], "auto")
            self.assertEqual(plan["selected"], [1, 2])
            self.assertEqual(req["mode"], "auto")
            self.assertEqual(req["source"], "scan")
            self.assertEqual(req["selected"], 2)
            # 抓完直接总结：候选池已经变成 hydrated，待总结包已落盘
            self.assertEqual(env.inbox()["phase"], "hydrated")
            self.assertIsNotNone(env.pending())
            self.assertEqual(env.pending()["article_count"], 2)
            # 自动模式下不该弹清单页
            opened.assert_not_called()

    def test_config_on_writes_auto_signal(self):
        """配置显式打开时同样自动，信号带 auto 指纹。"""
        with tempfile.TemporaryDirectory() as tmp:
            env, code, opened = self._run_scan(
                Path(tmp), _cfg(auto_summary=1),
                ["scan", "--date", DATE, "--no-open"])
            self.assertEqual(code, 0)
            self.assertEqual(env.request()["mode"], "auto")
            self.assertEqual(env.request()["source"], "scan")
            self.assertEqual(env.inbox()["phase"], "hydrated")
            opened.assert_not_called()

    # ---------------------------------------------------------- 人工逃生口
    def test_config_off_keeps_gate(self):
        """关着的时候：scan 只出候选池，不写任何放行信号、不取正文。"""
        with tempfile.TemporaryDirectory() as tmp:
            env, code, opened = self._run_scan(
                Path(tmp), _cfg(auto_summary=0),
                ["scan", "--date", DATE, "--no-open"])
            self.assertEqual(code, 0)
            self.assertEqual(len(env.inbox()["articles"]), 2)
            self.assertEqual(env.inbox()["phase"], "scan")
            self.assertIsNone(env.plan())
            self.assertIsNone(env.request())
            self.assertIsNone(env.pending())
            opened.assert_not_called()   # --no-open 压掉了清单页

    def test_confirm_overrides_default(self):
        """--confirm 必须能压掉默认的自动，回到清单页等人工提交。"""
        with tempfile.TemporaryDirectory() as tmp:
            env, code, opened = self._run_scan(
                Path(tmp), _cfg(), ["scan", "--date", DATE, "--confirm"])
            self.assertEqual(code, 0)
            self.assertIsNone(env.request(), "--confirm 必须能压掉自动")
            self.assertIsNone(env.plan())
            self.assertEqual(env.inbox()["phase"], "scan")
            opened.assert_called_once()  # 回到人工确认：照旧弹清单页

    def test_flag_overrides_config_off(self):
        """配置关着时，--auto-summary 能只放开本轮。"""
        with tempfile.TemporaryDirectory() as tmp:
            env, _, _ = self._run_scan(
                Path(tmp), _cfg(auto_summary=0),
                ["scan", "--date", DATE, "--no-open", "--auto-summary"])
            self.assertEqual(env.request()["mode"], "auto")
            self.assertEqual(env.inbox()["phase"], "hydrated")

    def test_no_hydrate_stops_at_candidate_pool(self):
        """--no-hydrate：信号照写，但停在候选池，等单独跑 hydrate。"""
        with tempfile.TemporaryDirectory() as tmp:
            env, code, _ = self._run_scan(
                Path(tmp), _cfg(),
                ["scan", "--date", DATE, "--no-open", "--no-hydrate"])
            self.assertEqual(code, 0)
            self.assertEqual(env.request()["mode"], "auto")
            self.assertEqual(env.inbox()["phase"], "scan")
            self.assertIsNone(env.pending())

    # ---------------------------------------------------------- 闸门本身
    def test_hydrate_refused_without_signal(self):
        """没信号（人工模式、也没点提交）时 hydrate 必须拒绝，且不取正文。"""
        with tempfile.TemporaryDirectory() as tmp:
            env, _, _ = self._run_scan(
                Path(tmp), _cfg(auto_summary=0),
                ["scan", "--date", DATE, "--no-open"])
            cfg = _cfg(auto_summary=0)
            code, _ = self._call(env, cfg, ["hydrate"])
            self.assertEqual(code, 1)
            self.assertIsNone(env.pending())
            self.assertEqual(env.inbox()["phase"], "scan")

    def test_hydrate_runs_after_no_hydrate_signal(self):
        """同一个放行信号机制：自动模式写下的信号能让后来单独跑的 hydrate 放行。"""
        with tempfile.TemporaryDirectory() as tmp:
            env, _, _ = self._run_scan(
                Path(tmp), _cfg(),
                ["scan", "--date", DATE, "--no-open", "--no-hydrate"])
            code, _ = self._call(env, _cfg(), ["hydrate"])
            self.assertEqual(code, 0)
            self.assertEqual(env.inbox()["phase"], "hydrated")
            self.assertIsNotNone(env.pending())
            self.assertEqual(env.pending()["article_count"], 2)


class TestAutoSummarySettings(unittest.TestCase):
    def test_spec_present_and_validated(self):
        keys = [s["key"] for s in settings_ui.specs()]
        self.assertIn("auto_summary", keys)
        self.assertEqual(DEFAULTS["auto_summary"], 1)
        self.assertEqual(settings_ui.validate({"auto_summary": 1}),
                         {"auto_summary": 1})
        self.assertEqual(settings_ui.validate({"auto_summary": "0"}),
                         {"auto_summary": 0})
        with self.assertRaises(RuntimeError):
            settings_ui.validate({"auto_summary": 2})


if __name__ == "__main__":
    unittest.main()
