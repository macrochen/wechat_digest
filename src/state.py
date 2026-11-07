#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四层审核去重台账：seen / 相似度 / 幂等 runs / 发布哈希 / 认领锁。

纯本地 JSON，原子写。所有状态只在「成功」时落盘；GUI 抓取失败不写任何台账。
参考已验证日报流水线的 seen.json / runs.json / published.json 与认领锁做法。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from .config import ensure_dirs, project_path


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_json(path: Path, default=None):
    if default is None:
        default = {}
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, data) -> None:
    """先写临时文件再原子替换，避免中途退出留下半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- 第 1 层：URL

class Seen:
    def __init__(self):
        ensure_dirs()
        self.path = project_path("state", "seen.json")
        self.data = load_json(self.path, {})

    def has(self, url: str) -> bool:
        return url in self.data

    def mark(self, articles: list[dict]) -> None:
        for a in articles:
            self.data[a["url"]] = {
                "title": a.get("title", ""),
                "source": a.get("source", ""),
                "at": _now(),
            }
        save_json(self.path, self.data)


# ---------------------------------------------------------------- 第 2 层：正文相似度

def shingles(text: str, n: int = 5) -> set[str]:
    """字符 5-gram 集合，先去掉所有空白，抗排版差异。"""
    s = re.sub(r"\s+", "", text or "")
    if not s:
        return set()
    if len(s) < n:
        return {s}
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def dedup_similar(articles: list[dict], threshold: float = 0.75):
    """批内跨号转载去重，保留先出现的那篇。返回 (kept, dropped)。"""
    kept: list[dict] = []
    kept_shingles: list[set[str]] = []
    dropped: list[dict] = []
    for a in articles:
        sh = shingles(a.get("content", ""))
        hit = None
        if sh:
            for i, other in enumerate(kept_shingles):
                score = jaccard(sh, other)
                if score >= threshold:
                    hit = (kept[i], score)
                    break
        if hit:
            dropped.append({
                "url": a["url"], "title": a.get("title", ""),
                "kept_url": hit[0]["url"], "score": round(hit[1], 4),
                "reason": "正文相似度超过阈值，判定为转载",
            })
        else:
            kept.append(a)
            kept_shingles.append(sh)
    return kept, dropped


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 第 4 层：幂等 / 发布 / 锁

class Runs:
    """同一批候选只跑一次。命中 done 输出「已在 XX 跑过，本次跳过」。"""

    def __init__(self):
        ensure_dirs()
        self.path = project_path("state", "runs.json")
        self.data = load_json(self.path, {})

    def done(self, run_key: str) -> dict | None:
        rec = self.data.get(run_key) or {}
        return rec if rec.get("status") == "done" else None

    def begin(self, run_key: str, info: dict) -> None:
        if self.data.get(run_key, {}).get("status") == "done":
            return
        self.data[run_key] = {"status": "running", "started_at": _now(), **info}
        save_json(self.path, self.data)

    def finish(self, run_key: str, info: dict) -> None:
        rec = self.data.get(run_key) or {}
        rec.update({"status": "done", "finished_at": _now(), **info})
        self.data[run_key] = rec
        save_json(self.path, self.data)


class Published:
    """简报正文哈希，内容没变不重推。"""

    def __init__(self):
        ensure_dirs()
        self.path = project_path("state", "published.json")
        self.data = load_json(self.path, {})

    def same(self, run_key: str, digest: str) -> dict | None:
        rec = self.data.get(run_key) or {}
        return rec if rec.get("hash") == digest else None

    def mark(self, run_key: str, digest: str, info: dict) -> None:
        self.data[run_key] = {"hash": digest, "at": _now(), **info}
        save_json(self.path, self.data)


class ClaimLock:
    """防两个进程同时总结同一批。超时自动失效（默认 20 分钟）。"""

    def __init__(self, ttl_minutes: int = 20):
        ensure_dirs()
        self.path = project_path("state", "lock.json")
        self.ttl = ttl_minutes

    def _expired(self, rec: dict) -> bool:
        try:
            at = datetime.strptime(rec["at"], "%Y-%m-%d %H:%M:%S")
            return datetime.now() - at > timedelta(minutes=self.ttl)
        except (KeyError, ValueError):
            return True

    def acquire(self, run_key: str) -> bool:
        rec = load_json(self.path, {})
        if rec and not self._expired(rec) and rec.get("pid") != os.getpid():
            return False
        save_json(self.path, {"run_key": run_key, "pid": os.getpid(),
                             "at": _now()})
        return True

    def owner(self) -> dict:
        return load_json(self.path, {})

    def release(self) -> None:
        if self.path.exists():
            self.path.unlink()
