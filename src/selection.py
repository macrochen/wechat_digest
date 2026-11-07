#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""人工选文清单。

对齐参考项目的选文铁律：人没在清单页提交 = 不总结。
选文清单决定「总结哪几篇」，提交信号决定「什么时候可以开始」，两者分工，
都按 inbox_generated_at 判过期（候选池被重扫覆盖后自动作废）。
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
    """返回选中的 index 列表；None = 没清单/清单过期，调用方必须拒绝放行。"""
    p = plan_path(inbox["date"])
    if not p.exists():
        return None
    plan = load_json(p, {})
    if plan.get("inbox_generated_at") != inbox.get("generated_at"):
        print("· 选文清单已过期（候选池被重扫覆盖），请在清单页重新勾选并提交",
              flush=True)
        return None
    selected = plan.get("selected")
    if not isinstance(selected, list):
        return None
    return selected


def request_path(date: str) -> Path:
    return project_path("out", "_plan", f"summary-request-{date}.json")


def write_request(inbox: dict, selected_count: int) -> dict:
    """人工在清单页点「开始总结」才写；这是唯一的放行信号。"""
    doc = {
        "date": inbox["date"],
        "inbox_generated_at": inbox["generated_at"],
        "run_key": inbox.get("run_key"),
        "requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "selected": selected_count,
        "_note": "没有这个文件，hydrate 一律拒绝执行；正文一个字都不放这里",
    }
    save_json(request_path(inbox["date"]), doc)
    return doc


def read_request(inbox: dict) -> dict | None:
    """返回提交信号；None = 没提交 / 信号过期，调用方必须拒绝放行。"""
    p = request_path(inbox["date"])
    if not p.exists():
        return None
    req = load_json(p, {})
    if req.get("inbox_generated_at") != inbox.get("generated_at"):
        print("· 提交信号已过期（候选池被重扫覆盖），请在清单页重新提交",
              flush=True)
        return None
    return req
