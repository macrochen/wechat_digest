#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""人工选文清单。

对齐参考项目的选文铁律：人没在清单页提交 = 不总结。
选文清单决定「总结哪几篇」，提交信号决定「什么时候可以开始」，两者分工，
都按 inbox_generated_at 判过期（候选池被重扫覆盖后自动作废）。

自动模式（配置 auto_summary=1 或 scan --auto-summary）下，这两份文件由 scan
代写，信号里带 `"mode": "auto"` / `"source": "scan"`；放行规则没有第二条——
永远只认这个信号文件，模式不同只影响 hydrate 打印哪一句。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .config import project_path
from .state import load_json, save_json


def inbox_key(inbox: dict) -> str:
    """候选池的「路径键」。

    信息流那份 = 日期（`2026-11-08`）；按号抓取那份 = 日期 + 号名
    （`2026-11-08-EarlETF`）。选文清单与放行信号都跟着这个键走，所以
    同一天的两份简报各用各的，不会互相覆盖、也不会互相认领。
    """
    return str(inbox.get("key") or inbox["date"])


def plan_path(key: str) -> Path:
    return project_path("out", "_plan", f"selected-{key}.json")


def write_plan(inbox: dict, indices: list[int] | None,
               mode: str = "manual") -> dict:
    valid = [a["index"] for a in inbox["articles"]]
    chosen = valid if indices is None else [i for i in indices if i in valid]
    unknown = [] if indices is None else [i for i in indices if i not in valid]
    plan = {
        "date": inbox["date"],
        "inbox_generated_at": inbox["generated_at"],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": mode,
        "selected": chosen,
        "unknown_indices": unknown,
    }
    p = plan_path(inbox_key(inbox))
    save_json(p, plan)
    return plan


def read_plan(inbox: dict):
    """返回选中的 index 列表；None = 没清单/清单过期，调用方必须拒绝放行。"""
    p = plan_path(inbox_key(inbox))
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


def request_path(key: str) -> Path:
    return project_path("out", "_plan", f"summary-request-{key}.json")


def write_request(inbox: dict, selected_count: int, *,
                  mode: str = "manual", source: str = "review") -> dict:
    """放行信号。

    mode=manual 只有人工在清单页点「开始总结」才写；
    mode=auto 是 scan 在自动模式下代写的（source=scan），hydrate 的校验完全不变，
    只是读到 auto 时会明确打印「本次未经人工确认」，留痕可追。
    """
    doc = {
        "date": inbox["date"],
        "inbox_generated_at": inbox["generated_at"],
        "run_key": inbox.get("run_key"),
        "requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "selected": selected_count,
        "mode": mode,
        "source": source,
        "_note": ("自动模式：由 scan 代写放行信号，未经人工确认；正文一个字都不放这里"
                  if mode == "auto" else
                  "没有这个文件，hydrate 一律拒绝执行；正文一个字都不放这里"),
    }
    save_json(request_path(inbox_key(inbox)), doc)
    return doc


def read_request(inbox: dict) -> dict | None:
    """返回提交信号；None = 没提交 / 信号过期，调用方必须拒绝放行。"""
    p = request_path(inbox_key(inbox))
    if not p.exists():
        return None
    req = load_json(p, {})
    if req.get("inbox_generated_at") != inbox.get("generated_at"):
        print("· 提交信号已过期（候选池被重扫覆盖），请在清单页重新提交",
              flush=True)
        return None
    return req
