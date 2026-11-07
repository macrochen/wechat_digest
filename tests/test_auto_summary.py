#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动模式（跳过人工确认）单测。

关注三件事：
1. 关着的时候行为与原来完全一致 —— scan 不写信号、hydrate 照样拒绝；
2. 开着的时候 scan 用**同一个** write_request 代写信号（mode=auto），
   hydrate 的校验逻辑一行没放宽；
3. --auto-summary / --confirm 能只覆盖本次运行。

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

        def fake_pending(inbox, kept, run_key, batch_size, max_parallel):
            return self.fake_pending(run_key, inbox, kept)

        return [
            mock.patch("src.main.project_path", side_effect=self.fake_path),
            mock.patch("src.state.project_path", side_effect=self.fake_path),
            mock.patch("src.selection.project_path", side_effect=self.fake_path),
            mock.patch("src.main.load_settings", return_value=cfg),
            mock.patch("src.main.article.fetch_meta", side_effect=fake_meta),
            mock.patch("src.main.article.fetch_content",
                       side_effect=lambda url, n: {
                           "content": "正文内容，足够长。" * 20,
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
    def _run(self, root: Path, cfg: dict, argv: list[str]):
        env = _Env(root)
        env.write_urls(URLS)
        opened = mock.Mock()
        patches = env.patches(cfg, opened)
        for p in patches:
            p.start()
        try:
            code = run(argv)
        finally:
            for p in patches:
                p.stop()
        return env, code, opened

    def test_default_off_keeps_gate(self):
        """关着的时候：scan 只出候选池，不写任何放行信号。"""
        with tempfile.TemporaryDirectory() as tmp:
            env, code, opened = self._run(
                Path(tmp), _cfg(), ["scan", "--date", DATE, "--no-open"])
            self.assertEqual(code, 0)
            self.assertEqual(len(env.inbox()["articles"]), 2)
            self.assertIsNone(env.plan())
            self.assertIsNone(env.request())

    def test_config_on_writes_auto_signal(self):
        """配置打开：scan 自己写全选清单 + 带 auto 指纹的放行信号。"""
        with tempfile.TemporaryDirectory() as tmp:
            env, code, opened = self._run(
                Path(tmp), _cfg(auto_summary=1),
                ["scan", "--date", DATE, "--no-open"])
            self.assertEqual(code, 0)
            plan, req = env.plan(), env.request()
            self.assertEqual(plan["mode"], "auto")
            self.assertEqual(plan["selected"], [1, 2])
            self.assertEqual(req["mode"], "auto")
            self.assertEqual(req["source"], "scan")
            self.assertEqual(req["selected"], 2)
            # 自动模式下不该弹清单页，也不需要人看
            opened.assert_not_called()

    def test_flag_overrides_config(self):
        """--auto-summary 只放开本轮；--confirm 能压掉配置里的自动。"""
        with tempfile.TemporaryDirectory() as tmp:
            env, _, _ = self._run(
                Path(tmp), _cfg(), ["scan", "--date", DATE,
                                    "--no-open", "--auto-summary"])
            self.assertEqual(env.request()["mode"], "auto")
        with tempfile.TemporaryDirectory() as tmp:
            env, _, opened = self._run(
                Path(tmp), _cfg(auto_summary=1), ["scan", "--date", DATE,
                                                  "--confirm"])
            self.assertIsNone(env.request(), "--confirm 必须能压掉配置")
            self.assertIsNone(env.plan())
            opened.assert_called_once()  # 回到人工确认：照旧弹清单页

    def test_hydrate_refused_without_signal(self):
        """没信号（关着、也没点提交）时 hydrate 必须拒绝，且不取正文。"""
        with tempfile.TemporaryDirectory() as tmp:
            env, _, _ = self._run(
                Path(tmp), _cfg(), ["scan", "--date", DATE, "--no-open"])
            with mock.patch("src.main.load_settings", return_value=_cfg()):
                with mock.patch("src.main.project_path",
                                side_effect=env.fake_path), \
                        mock.patch("src.state.project_path",
                                   side_effect=env.fake_path), \
                        mock.patch("src.selection.project_path",
                                   side_effect=env.fake_path):
                    code = run(["hydrate"])
            self.assertEqual(code, 1)
            self.assertIsNone(env.pending())
            self.assertEqual(env.inbox()["phase"], "scan")

    def test_hydrate_runs_after_auto_signal(self):
        """同一个放行信号机制：自动模式写下的信号能让 hydrate 放行。"""
        with tempfile.TemporaryDirectory() as tmp:
            env, _, _ = self._run(
                Path(tmp), _cfg(auto_summary=1),
                ["scan", "--date", DATE, "--no-open"])
            with mock.patch("src.main.load_settings",
                            return_value=_cfg(auto_summary=1)), \
                    mock.patch("src.main.project_path",
                               side_effect=env.fake_path), \
                    mock.patch("src.state.project_path",
                               side_effect=env.fake_path), \
                    mock.patch("src.selection.project_path",
                               side_effect=env.fake_path), \
                    mock.patch("src.main.article.fetch_content",
                               side_effect=lambda url, n: {
                                   # 正文按 url 区分，别让相似度去重把两篇并成一篇
                                   "content": f"{url} 的正文。" * 20,
                                   "content_chars": 200, "too_short": False}), \
                    mock.patch("src.main.prompt.build_pending",
                               side_effect=lambda inbox, kept, rk, bs, mp,
                               **kw:
                               env.fake_pending(rk, inbox, kept)):
                code = run(["hydrate"])
            self.assertEqual(code, 0)
            self.assertEqual(env.inbox()["phase"], "hydrated")
            self.assertIsNotNone(env.pending())
            self.assertEqual(env.pending()["article_count"], 2)


class TestAutoSummarySettings(unittest.TestCase):
    def test_spec_present_and_validated(self):
        keys = [s["key"] for s in settings_ui.specs()]
        self.assertIn("auto_summary", keys)
        self.assertEqual(settings_ui.validate({"auto_summary": 1}),
                         {"auto_summary": 1})
        self.assertEqual(settings_ui.validate({"auto_summary": "0"}),
                         {"auto_summary": 0})
        with self.assertRaises(RuntimeError):
            settings_ui.validate({"auto_summary": 2})


if __name__ == "__main__":
    unittest.main()
