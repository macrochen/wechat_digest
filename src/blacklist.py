#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取黑名单：按公众号名整号排除。

数据文件 config/blacklist.json（Git 跟踪，故意不叫 *.local.json，因为它是
长期偏好、要跟项目走）：

    {"sources": ["中汇人寿"], "note": "..."}

也兼容「直接存一个字符串数组」的手写格式。

匹配规则（用户拍板）：规范化后**精确相等** —— NFKC 归一（全/半角统一）、
去掉所有空白、英文 casefold。不做包含匹配，避免「果壳」误伤「果壳网」。

命中只影响候选池：不进清单、不取正文、不总结、不推送；
**绝不写 seen / runs / published 台账**，也不等于人工提交授权。
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path

from .config import project_path

PATH: Path = project_path("config", "blacklist.json")
NOTE = ("一行一个公众号名；规范化后精确匹配（忽略空格 / 全半角 / 英文大小写）。"
        "命中后不进候选池、不取正文、不总结、不推送。")

MAX_ITEMS = 500
MAX_NAME_LEN = 100
MAX_TEXT_LEN = 20000


def normalize(name: str) -> str:
    """比较用的键：NFKC 归一 → 去所有空白 → casefold。"""
    if not isinstance(name, str):
        return ""
    s = unicodedata.normalize("NFKC", name)
    s = re.sub(r"\s+", "", s)
    return s.casefold()


def _clean(names) -> list[str]:
    """清洗成显示用的名字列表：去首尾空白、丢空行、按比较键去重、限长限量。"""
    if not isinstance(names, (list, tuple)):
        raise RuntimeError("黑名单必须是名字数组")
    out: list[str] = []
    keys: set[str] = set()
    for item in names:
        if not isinstance(item, str):
            raise RuntimeError("黑名单里每一项都必须是公众号名（字符串）")
        name = item.strip()
        if not name:
            continue
        if len(name) > MAX_NAME_LEN:
            raise RuntimeError(f"公众号名太长（>{MAX_NAME_LEN} 字）：{name[:20]}…")
        key = normalize(name)
        if not key or key in keys:
            continue
        keys.add(key)
        out.append(name)
    if len(out) > MAX_ITEMS:
        raise RuntimeError(f"黑名单最多 {MAX_ITEMS} 个公众号，当前 {len(out)} 个")
    return out


def load() -> list[str]:
    """读名单；文件不存在视为空名单。兼容对象格式与纯数组格式。"""
    if not PATH.exists():
        return []
    try:
        doc = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"黑名单读不了：{PATH.name}（{exc}）") from exc
    raw = doc.get("sources", []) if isinstance(doc, dict) else doc
    if not isinstance(raw, (list, tuple)):
        raise RuntimeError(f"黑名单格式不对：{PATH.name} 里 sources 应是数组")
    return _clean(raw)


def as_text(names: list[str]) -> str:
    return "\n".join(names)


def parse_text(text: str) -> list[str]:
    """把页面/命令行的多行文本拆成名字列表。"""
    if not isinstance(text, str):
        raise RuntimeError("黑名单内容必须是文本")
    if len(text) > MAX_TEXT_LEN:
        raise RuntimeError(f"黑名单内容太长（>{MAX_TEXT_LEN} 字）")
    return _clean(text.splitlines())


def save(names) -> list[str]:
    """原子写回；写前留 .bak。返回清洗后的名单（供页面回显）。"""
    cleaned = _clean(names)
    PATH.parent.mkdir(parents=True, exist_ok=True)
    doc = {"sources": cleaned, "note": NOTE}
    text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    if PATH.exists() and PATH.read_text(encoding="utf-8") == text:
        return cleaned
    if PATH.exists():
        Path(str(PATH) + ".bak").write_text(
            PATH.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = Path(str(PATH) + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, PATH)
    return cleaned


def is_blocked(source: str, names: list[str] | None = None) -> bool:
    """规范化后精确相等才命中；空来源一律不命中（宁可漏，不可误杀）。"""
    key = normalize(source or "")
    if not key:
        return False
    cur = load() if names is None else names
    return key in {normalize(n) for n in cur}


def filter_articles(articles: list[dict], names: list[str] | None = None):
    """拆成 (保留, 命中黑名单)；命中的原样带 url/title/source，便于留痕核对。"""
    cur = load() if names is None else names
    keys = {normalize(n) for n in cur}
    kept, blocked = [], []
    for a in articles:
        key = normalize(a.get("source") or "")
        if key and key in keys:
            blocked.append(a)
        else:
            kept.append(a)
    return kept, blocked


def add(names) -> tuple[list[str], bool]:
    """批量加入；返回 (新名单, 是否有变化)。"""
    cur = load()
    merged = save(cur + list(names))
    return merged, merged != cur


def remove(names) -> tuple[list[str], bool]:
    """批量移除；按规范化键比较，忽略空格/全半角/大小写差异。"""
    cur = load()
    drops = {normalize(n) for n in names}
    left = [n for n in cur if normalize(n) not in drops]
    result = save(left)
    return result, result != cur
