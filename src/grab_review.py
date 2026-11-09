#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取明细：把某一轮 grab 抓到的**全部**链接摊开，标清每条的去向与重复情况。

为什么单独做一份
----------------
`/review` 选文清单只显示「本轮可总结的新文章」，被 seen（已总结过）、
offered（投递过没勾选）、黑名单、窗口外、无标题、元信息失败挡掉的那些都
看不见。用户想核对「是不是抓重复了」时，需要一张覆盖**全部**链接的表，
所以这里出一份只读明细给 `/grab` 页面用。

只读、只发本地文件请求：不取正文、不写 seen / offered / runs 台账、
不写放行信号、不触发任何总结。重复判定分三层：

1. URL 级：同一 URL 在本轮链接文件里出现多次；
2. 标题级：**不同 URL** 却标题相同（NFKC + 去空白 + casefold 后相等）；
3. 跨轮级：命中 `state/seen.json` 或 `state/offered.json`，说明以前抓过。

第 3 层是「感觉有重复」的主要来源 —— 信息流里同一篇文章会连着挂好几天，
抓取层每次都重新点开，靠 scan 的台账挡掉，并不会进简报两次。

`out_dir` 是给单测用的隔离口：scan 传候选池所在目录，测试就不会往真实
项目 `out/` 里写明细文件。
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from .config import inbox_matches, project_path
from .state import load_json

REVIEW_PREFIX = "_grab_review-"

STATUS_LABELS = {
    "in_inbox": "进本轮清单",
    "seen": "已总结过（跳过）",
    "offered": "已投递未选（跳过）",
    "blacklist": "黑名单排除",
    "notitle": "无标题/闲聊排除",
    "window_out": "窗口外排除",
    "meta_failed": "元信息失败",
    "unknown": "未归类",
}

# 判定优先级：先进清单，再看各类剔除，最后才是跨轮去重。
_STATUS_ORDER = ("in_inbox", "blacklist", "notitle", "window_out",
                 "meta_failed", "seen", "offered")


def _out(out_dir=None) -> Path:
    return Path(out_dir) if out_dir is not None else project_path("out")


def urls_path(scope: str = "", out_dir=None) -> Path:
    """本轮 grab 写下的原始链接文件。"""
    name = f"_raw_urls-{scope}.json" if scope else "_raw_urls.json"
    return _out(out_dir) / name


def review_path(date: str, scope: str = "", out_dir=None) -> Path:
    key = f"{date}-{scope}" if scope else date
    return _out(out_dir) / f"{REVIEW_PREFIX}{key}.json"


def ledger_path(date: str, out_dir=None) -> Path:
    return _out(out_dir) / f"_grab_ledger-{date}.json"


def _norm_title(text) -> str:
    s = unicodedata.normalize("NFKC", str(text or "")).strip().casefold()
    return re.sub(r"\s+", "", s)


def _read(path, fallback):
    try:
        return load_json(path, fallback)
    except (OSError, ValueError):
        return fallback


def _latest_inbox(date: str, scope: str, out: Path) -> dict:
    """本轮 scan 落下的候选池（带各分类明细）。"""
    files = sorted((f for f in out.glob("inbox-*.json")
                    if inbox_matches(f.name, scope)), reverse=True)
    for f in files:
        box = _read(f, {})
        if isinstance(box, dict) and box.get("date") == date:
            return box
    # 没有同一天的就用最新那份（例如 --date 指定的是过去某天）
    return _read(files[0], {}) if files else {}


def _meta_index(inbox: dict, seen: dict, offered: dict, out: Path) -> dict:
    """URL → 元信息。优先级：本轮 scan > offered > seen > 历史候选池。"""
    idx: dict = {}

    def put(url, item):
        if not url or not isinstance(item, dict):
            return
        cur = idx.setdefault(url, {"title": "", "source": "",
                                   "published_at": ""})
        for k in ("title", "source", "published_at"):
            v = item.get(k)
            if v and not cur[k]:
                cur[k] = str(v)

    for key in ("articles", "skipped_blacklist", "skipped_notitle",
                "skipped_notoday", "failed_meta"):
        for a in (inbox.get(key) or []):
            if isinstance(a, dict):
                put(a.get("url"), a)
    for u, v in (offered or {}).items():
        put(u, v)
    for u, v in (seen or {}).items():
        put(u, v)
    for f in sorted(out.glob("inbox-*.json")):
        box = _read(f, {})
        for a in (box.get("articles") or []) if isinstance(box, dict) else []:
            if isinstance(a, dict):
                put(a.get("url"), a)
    return idx


def _status_sets(inbox: dict) -> dict:
    def urls(key):
        found = set()
        for a in (inbox.get(key) or []):
            if isinstance(a, dict):
                if a.get("url"):
                    found.add(a["url"])
            elif isinstance(a, str):
                found.add(a)
        return found

    return {
        "in_inbox": urls("articles"),
        "blacklist": urls("skipped_blacklist"),
        "notitle": urls("skipped_notitle"),
        "window_out": urls("skipped_notoday"),
        "meta_failed": urls("failed_meta"),
        "seen": urls("skipped_seen"),
        "offered": urls("skipped_offered"),
    }


def build(date: str, scope: str = "", *, out_dir=None) -> dict:
    """生成一份抓取明细（纯读盘，不写任何台账）。"""
    out = _out(out_dir)
    raw = _read(urls_path(scope, out_dir), {})
    urls = raw.get("urls") or [] if isinstance(raw, dict) else list(raw or [])
    inbox = _latest_inbox(date, scope, out)
    seen = _read(project_path("state", "seen.json"), {})
    offered = _read(project_path("state", "offered.json"), {})
    ledger = _read(ledger_path(date, out_dir), {})
    coverage = (ledger.get("coverage") or {}) if isinstance(ledger, dict) else {}

    meta = _meta_index(inbox, seen, offered, out)
    sets = _status_sets(inbox)
    url_counts = Counter(urls)

    rows = []
    for i, u in enumerate(urls, 1):
        m = meta.get(u) or {}
        title = m.get("title") or "（未取到元信息）"
        status = "unknown"
        for key in _STATUS_ORDER:
            if u in sets[key]:
                status = key
                break
        rows.append({
            "index": i,
            "url": u,
            "title": title,
            "source": m.get("source") or "",
            "published_at": m.get("published_at") or "",
            "status": status,
            "status_label": STATUS_LABELS[status],
            "url_dup": url_counts[u] > 1,
            "cross_round": (u in (seen or {}) or u in (offered or {})),
            "title_dup": False,   # 下面按规范化标题回填
        })

    # 标题重复：**不同 URL** 共享同一标题才算，同一个 URL 出现两次已经由
    # url_dup 记了，别重复计数。
    by_title = defaultdict(set)
    for r in rows:
        key = _norm_title(r["title"])
        if key and not r["title"].startswith("（未取到"):
            by_title[key].add(r["url"])
    dup_groups = []
    for key, group in by_title.items():
        if len(group) < 2:
            continue
        for r in rows:
            if r["url"] in group:
                r["title_dup"] = True
        dup_groups.append({
            "title": next(r["title"] for r in rows if r["url"] in group),
            "urls": sorted(group),
            "count": len(group),
        })

    counts = Counter(r["status"] for r in rows)
    return {
        "date": date,
        "scope": scope,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "run_key": inbox.get("run_key") or "",
        "phase": inbox.get("phase") or "",
        "inbox_count": len(inbox.get("articles") or []),
        "summary": {
            "total": len(urls),
            "unique_urls": len(set(urls)),
            "url_dup_groups": sum(1 for c in url_counts.values() if c > 1),
            "title_dup_groups": len(dup_groups),
            "cross_round": sum(1 for r in rows if r["cross_round"]),
            "in_inbox": counts.get("in_inbox", 0),
            "counts": dict(counts),
            "ledger": coverage,
        },
        "dup_groups": dup_groups,
        "articles": rows,
    }


def write(date: str, scope: str = "", *, out_dir=None):
    """落盘到 out/_grab_review-<date>[-<scope>].json，返回 (路径, 明细)。"""
    data = build(date, scope, out_dir=out_dir)
    p = review_path(date, scope, out_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return p, data
