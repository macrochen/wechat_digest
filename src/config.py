#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""路径与运行配置。

所有可调项集中在 config/settings.json。注意：令牌**不在**这里，
运行时才从项目内独立的 config/secrets.local.json 只读加载。
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    # 每次从微信信息流取多少篇
    "max_articles": 20,
    # 字符 5-gram Jaccard 阈值，>= 视为转载（实测真转载约 0.93）
    "dedup_threshold": 0.75,
    # 批内篇数达到这个数就拆批（对齐参考项目的拆批规则）
    "batch_size": 15,
    # 子代理并发上限（对齐铁律 5）
    "max_parallel": 3,
    # 正文短于这个字数，交给提示词标「内容无法总结」
    "min_content_chars": 50,
    # 项目内独立令牌文件（Git 忽略、权限 600），相对项目根；可用环境变量覆盖
    "token_file": "config/secrets.local.json",
    "publish_api": "https://zenmark-bxx.pages.dev/api/cloud/upload",
    "publish_title_prefix": "公众号简报",
    # 运行认领锁超时（分钟）
    "lock_ttl_minutes": 20,
    # 微信公众号阅读窗口的 UI 坐标（1280x832 逻辑坐标，上一轮实测）
    # 界面变了就改这里，或按报错提示人介入；脚本不自动重新识别。
    "ui": {
        "logical_width": 1280,
        "logical_height": 832,
        "more_button": [1258, 101],
        "copy_link": [1110, 300],
        "close_window": [88, 108],
        "open_wait": 2.5,
        "menu_wait": 0.8,
        "close_wait": 1.0,
        "click_wait": 0.45,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings() -> dict:
    """读配置；文件缺失时用默认值。"""
    path = ROOT / "config" / "settings.json"
    cfg = dict(DEFAULTS)
    if path.exists():
        try:
            cfg = _deep_merge(cfg, json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"× 配置文件读不了：{path}（{exc}）")
    tf = Path(cfg["token_file"]).expanduser()
    cfg["token_file"] = str(tf if tf.is_absolute() else ROOT / tf)
    return cfg


def project_path(*parts: str) -> Path:
    return ROOT.joinpath(*parts)


def ensure_dirs() -> None:
    for name in ("state", "out", "out/_plan", "out/_batches"):
        project_path(name).mkdir(parents=True, exist_ok=True)
