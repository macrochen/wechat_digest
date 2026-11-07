#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""人工选文清单。

对齐 daily_digest_hub 铁律 11：人没筛过 = 整份都要；清单过期（候选池被重扫
覆盖）自动忽略并打印提示，不当报错，更不静默丢篇。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .config import project_path
from .state import load_json, save_json


def plan_path(date: str) -> Path:
    return project_path("out", "_plan", f"selected-{date}.json")


def write_plan(inbox: dict, indices: list[int] | None,
               mode: str = "manual") -> dict:
    valid = [a["index"] for a in inbox["articles"]]
    chosen = valid if indices is None else [i for i in indices if i in valid]
    unknown = [] if indices is None else [i for i in indices if i not in valid]
    plan = {
        "date": inbox["date"],
        "inbox_generated_at": inbox["generated_at"],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": mode if indices is not None else "all",
        "selected": chosen,
        "unknown_indices": unknown,
    }
    p = plan_path(inbox["date"])
    save_json(p, plan)
    return plan


def read_plan(inbox: dict):
    """返回选中的 index 列表；None = 没筛过/清单过期，整份都要。"""
    p = plan_path(inbox["date"])
    if not p.exists():
        return None
    plan = load_json(p, {})
    if plan.get("inbox_generated_at") != inbox.get("generated_at"):
        print("· 选文清单已过期（候选池被重扫覆盖），自动忽略，整份都要", flush=True)
        return None
    selected = plan.get("selected")
    if not isinstance(selected, list):
        return None
    return selected
