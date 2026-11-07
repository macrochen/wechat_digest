#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""待总结包 + 拆批 + 合并校验。

脚本不写总结。它把「融合提示词 + 正文 JSON」落成文件，交给 WorkBuddy 会话里的
Hy4 分批总结；子代理写回各批 md 后，再由 merge 命令合成一份汇总简报。
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path

from .config import batches_dir, pending_file, project_path
from .state import save_json


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def prompt_text() -> str:
    p = project_path("prompts", "summary.md")
    if not p.exists():
        raise RuntimeError(f"找不到融合提示词：{p}")
    return p.read_text(encoding="utf-8")


def _title_shingles(title: str, n: int = 3) -> set[str]:
    s = re.sub(r"[\s\W_]+", "", title or "")
    if not s:
        return set()
    if len(s) < n:
        return {s}
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _title_sim(a: str, b: str) -> float:
    x, y = _title_shingles(a), _title_shingles(b)
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


def _topic_clusters(articles: list[dict], threshold: float = 0.45,
                    max_cluster: int = 5):
    """把标题相近的文章聚成一簇，保证同事件簇不被切到不同批（铁律 6）。

    max_cluster 必须有上限：公众号标题常共享同一串栏目前后缀，
    不设上限时会把整批不相干文章吸成一个巨簇，反而拆不了批。
    日报规则里同一事件超过 5 篇本来也要拆两条，5 是合理上限。
    """
    remaining = list(articles)
    clusters: list[list[dict]] = []
    while remaining:
        seed = remaining.pop(0)
        cluster = [seed]
        changed = True
        while changed and remaining and len(cluster) < max_cluster:
            changed = False
            for a in list(remaining):
                if len(cluster) >= max_cluster:
                    break
                if any(_title_sim(a["title"], b["title"]) >= threshold
                       for b in cluster):
                    cluster.append(a)
                    remaining.remove(a)
                    changed = True
        clusters.append(cluster)
    clusters.sort(key=lambda c: min(x["index"] for x in c))
    return clusters


def make_batches(articles: list[dict], batch_size: int = 15,
                 max_parallel: int = 3) -> list[list[dict]]:
    """>= batch_size 就拆批，簇不跨批；20 篇通常得 2 批。"""
    if not articles:
        return []
    n_batches = max(1, math.ceil(len(articles) / batch_size))
    target = math.ceil(len(articles) / n_batches)
    clusters = _topic_clusters(articles)
    batches: list[list[dict]] = []
    current: list[dict] = []
    # 簇整体装入，累计到目标篇数才切，保证「同事件簇不跨批」
    for cluster in clusters:
        current.extend(cluster)
        if len(current) >= target:
            batches.append(current)
            current = []
    if current:
        # 尾部碎片并回上一批（只要不超 batch_size），避免为 2 篇单开一批
        if batches and len(batches[-1]) + len(current) <= batch_size:
            batches[-1].extend(current)
        else:
            batches.append(current)
    # 极端情况下批数超过并发上限，合并末尾几批而不是硬切事件簇
    while len(batches) > max_parallel and len(batches) > 1:
        batches[-2].extend(batches[-1])
        batches.pop()
    for b in batches:
        b.sort(key=lambda a: a["index"])
    return batches


def build_pending(inbox: dict, articles: list[dict], run_key: str,
                  batch_size: int, max_parallel: int,
                  scope: str = "") -> dict:
    """生成待总结包与各批输入文件。articles 需带 index/title/url/source/content。

    scope 决定落盘目录：信息流那份仍用 `out/_batches/`，按号抓取那份用
    `out/_batches-<号名>/`，两批互不覆盖（否则同一天跑两份会串）。
    """
    prompt = prompt_text()
    batches = make_batches(articles, batch_size, max_parallel)
    out_dir = project_path("out", batches_dir(scope))
    out_dir.mkdir(parents=True, exist_ok=True)
    # 清掉上一轮残留，避免合并时串进旧内容
    for old in out_dir.glob("batch-*.json"):
        old.unlink()
    for old in out_dir.glob("batch-*.md"):
        old.unlink()

    batch_info = []
    for i, batch in enumerate(batches, 1):
        f = out_dir / f"batch-{i}.json"
        f.write_text(json.dumps({
            "prompt": prompt,
            "batch_id": i,
            "batch_count": len(batches),
            "articles": batch,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        batch_info.append({
            "id": i,
            "file": str(f),
            "output": str(out_dir / f"batch-{i}.md"),
            "indices": [a["index"] for a in batch],
            "count": len(batch),
        })

    pending = {
        "run_key": run_key,
        "generated_at": _now(),
        "date": inbox["date"],
        "scope": scope,
        "account": inbox.get("account", ""),
        "prompt_file": str(project_path("prompts", "summary.md")),
        "prompt": prompt,
        "article_count": len(articles),
        "batch_size": batch_size,
        "max_parallel": max_parallel,
        "batches": batch_info,
        "articles": articles,
    }
    p = project_path("out", pending_file(scope))
    save_json(p, pending)
    return pending


_HEAD_RE = re.compile(r"^##\s*\[(\d+)\.\s", re.M)
_PLACEHOLDER_RE = re.compile(r"(TODO|TBD|待填|占位符|\{title\}|\{source\}|<待补)", re.I)


def merge_batches(pending: dict) -> str:
    """拼各批 md、全局连续重编号、校验破折号与占位符。"""
    parts: list[str] = []
    for b in pending["batches"]:
        f = Path(b["output"])
        if not f.exists():
            raise RuntimeError(
                f"缺第 {b['id']} 批的总结文件：{f}（子代理还没写完？）")
        text = f.read_text(encoding="utf-8").strip()
        if not text:
            raise RuntimeError(f"第 {b['id']} 批的总结文件是空的：{f}")
        parts.append(re.sub(r"\n+---\s*$", "", text).rstrip())

    merged = "\n\n---\n\n".join(parts)
    # 合并后统一连续编号，避免批内同事件合并造成跳号
    counter = [0]

    def _renumber(m):
        counter[0] += 1
        return f"## [{counter[0]}. "

    merged = _HEAD_RE.sub(_renumber, merged)

    problems = []
    if counter[0] == 0:
        problems.append("没找到任何 `## [N. ` 标题行，格式可能不对")
    # 破折号只查正文：标题必须原样照抄，原文标题里带 `——` 是允许的
    body = "\n".join(l for l in merged.splitlines() if not l.startswith("## "))
    if "——" in body:
        problems.append("正文里有破折号 `——`，按铁律必须去掉")
    ph = _PLACEHOLDER_RE.search(merged)
    if ph:
        problems.append(f"残留占位符：{ph.group(0)}")
    if problems:
        raise RuntimeError("合并校验没过：\n  - " + "\n  - ".join(problems))
    return merged.strip() + "\n"
