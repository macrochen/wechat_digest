#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取完备性台账：每屏结果、coverage 对账、缺口清单、grab → scan 硬闸门。

设计见 docs/PRD-抓取完备性.md 的 P1。这里只做**纯数据 + 文件 IO**，
不碰 GUI、不碰 state/ 下的 seen / offered / runs 台账，方便单测直接覆盖。

缺口口径（最容易误报的地方）
--------------------------
`missing` 只统计 outcome == "error"（守卫拒点、菜单认不出、恢复失败等
「本该拿到却没拿到」）。广告卡 / 被删文章（no_link / deleted）、
重复点开（duplicate / duplicate_screen）都**不算缺口**，否则闸门天天误报，
会把自动链路彻底堵死。

闸门与现有「放行信号」同构：都是 out/_plan/ 下的一个小 JSON；
hydrate 的校验一行没改，这里新增的只是 grab → scan 的前置一道。
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from .config import project_path
from .state import load_json, save_json

OUTCOMES = ("linked", "duplicate", "duplicate_screen", "ad_skipped",
            "no_link", "deleted", "error")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def compute_coverage(screens: list[dict]) -> dict:
    """把每屏的 results 汇总成「看到 N / 拿到 M / 无链接 S / 缺 K」。

    `seen` 是处理过的卡片**次数**（跨屏重叠会重复计数，所以另外给
    `unique_cards` 作参考）；闸门只看 `missing`，它等于 error 的条数，
    不受重复计数影响。    恒等式：
        linked + duplicate + ad + no_link + deleted + missing == seen

    `ad` 是**点击前就被拦掉的广告卡**：既不是缺口，也不算「没拿到链接」，
    它本来就不该被点（2026-11-09 用户铁律：广告卡绝对不能点）。
    """
    counts: Counter = Counter()
    fps: set[str] = set()
    for screen in screens or []:
        for c in screen.get("cards", []):
            if c.get("fp"):
                fps.add(c["fp"])
        for r in screen.get("results", []):
            counts[str(r.get("outcome") or "error")] += 1
    # 未知 outcome 一律当 error，绝不静默吞掉
    for key in list(counts):
        if key not in OUTCOMES:
            counts["error"] += counts.pop(key)
    duplicate = counts["duplicate"] + counts["duplicate_screen"]
    coverage = {
        "seen": sum(counts.values()),
        "linked": counts["linked"],
        "duplicate": duplicate,
        "ad": counts["ad_skipped"],
        "no_link": counts["no_link"],
        "deleted": counts["deleted"],
        "missing": counts["error"],
        "screens": len(screens or []),
        "unique_cards": len(fps),
    }
    assert (coverage["linked"] + coverage["duplicate"] + coverage["ad"] +
            coverage["no_link"] + coverage["deleted"] +
            coverage["missing"] == coverage["seen"]), "coverage 口径不闭合"
    return coverage


def ledger_path(date: str) -> Path:
    return project_path("out", f"_grab_ledger-{date}.json")


def missing_path(date: str) -> Path:
    return project_path("out", f"_grab_missing-{date}.json")


def gate_path(date: str) -> Path:
    return project_path("out", "_plan", f"grab-gate-{date}.json")


def write_ledger(date: str, screens: list[dict], coverage: dict,
                 *, stop_reason: str = "", feed_days: int = 2,
                 resume: bool = False) -> Path:
    doc = {
        "date": date,
        "written_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "feed_days": feed_days,
        "stop_reason": stop_reason,
        "resume": resume,
        "screens": screens,
        "coverage": coverage,
    }
    p = ledger_path(date)
    save_json(p, doc)
    return p


def read_ledger(date: str) -> dict | None:
    p = ledger_path(date)
    return load_json(p, None) if p.exists() else None


def write_missing(date: str, items: list[dict]) -> Path:
    p = missing_path(date)
    save_json(p, {"date": date,
                  "written_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  "count": len(items), "items": items})
    return p


def read_missing(date: str) -> list[dict]:
    p = missing_path(date)
    if not p.exists():
        return []
    doc = load_json(p, {})
    items = doc.get("items") if isinstance(doc, dict) else None
    return items if isinstance(items, list) else []


def write_gate(date: str, coverage: dict, *, reason: str = "") -> dict:
    doc = {
        "date": date,
        "written_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "reason": reason or "抓取存在缺口（coverage.missing > 0）",
        "coverage": coverage,
        "missing_file": str(missing_path(date)),
        "_note": "有这个文件就不许 scan：先跑 grab --resume 补齐，"
                 "或单轮 scan --ignore-grab-gate 绕过（会留痕）",
    }
    save_json(gate_path(date), doc)
    return doc


def read_gate(date: str) -> dict | None:
    p = gate_path(date)
    if not p.exists():
        return None
    doc = load_json(p, None)
    return doc if isinstance(doc, dict) else None


def clear_gate(date: str) -> bool:
    """缺口补齐后解除阻断。返回是否真的删掉过一个文件。"""
    p = gate_path(date)
    if not p.exists():
        return False
    p.unlink()
    return True


def summary_line(coverage: dict) -> str:
    return (f"· 抓取对账：看到 {coverage['seen']} 张 / 拿到 "
            f"{coverage['linked']} 条 / 重复 {coverage['duplicate']} 张 / "
            f"广告拦下 {coverage.get('ad', 0)} 张 / "
            f"无链接 {coverage['no_link'] + coverage['deleted']} 张 / "
            f"缺 {coverage['missing']} 篇")
