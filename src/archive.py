#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""简报本地归档 + 只读渲染，供 /read 阅读页使用。

只把**已经生成好**的 Markdown 存一份到 out/archive/，不参与发布、
不读写 seen / runs / published 台账、不碰人工提交闸门。
渲染用标准库做受限 Markdown：先 HTML 转义，链接只放行 http(s)，
不引第三方库、不加载 CDN，保证本地服务离线可用。
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime
from pathlib import Path

from .config import project_path

ARCHIVE_DIR = project_path("out", "archive")

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_HR_RE = re.compile(r"^(?:-{3,}|\*{3,})$")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def save(text: str, *, title: str, kind: str = "digest",
         status: str = "published", run_key: str | None = None,
         zenmark_id: str | None = None, articles: int | None = None,
         date: str | None = None) -> dict:
    """写一份归档（.md 正文 + .json 元数据），返回元数据。

    文件名按内容/批次派生，所以同一份稿子重复发布只会覆盖同一个文件，
    不会在阅读页里堆出重复条目。
    kind: digest（普通版） / voice（语音播报版）
    status: published（已推送）/ failed（推送失败，仍可在本地读）
    """
    date = date or datetime.now().strftime("%Y-%m-%d")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if kind == "voice":
        stem = f"voice-{date}-{digest[:8]}"
    else:
        stem = _UNSAFE_RE.sub("-", run_key or f"manual-{date}")
    if status != "published":
        stem = "failed-" + stem
    stem = stem[:120]

    meta = {
        "id": stem,
        "title": title,
        "kind": kind,
        "status": status,
        "created_at": _now(),
        "date": date,
        "run_key": run_key,
        "zenmark_id": zenmark_id,
        "articles": articles,
        "sha256": digest,
        "chars": len(text),
    }
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(ARCHIVE_DIR / f"{stem}.md", text)
    _atomic_write(ARCHIVE_DIR / f"{stem}.json",
                  json.dumps(meta, ensure_ascii=False, indent=2))
    return meta


def list_items() -> list[dict]:
    """按时间倒序列出可用归档；坏文件与缺正文的元数据直接跳过。"""
    if not ARCHIVE_DIR.exists():
        return []
    items = []
    for p in ARCHIVE_DIR.glob("*.json"):
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict) or meta.get("id") != p.stem:
            continue
        if not (ARCHIVE_DIR / f"{p.stem}.md").exists():
            continue
        items.append(meta)
    return sorted(items,
                  key=lambda m: (m.get("created_at", ""), m.get("id", "")),
                  reverse=True)


def load(item_id: str) -> str | None:
    """读归档正文；id 只吃安全文件名，挡住 ../ 之类的路径穿越。"""
    if not item_id or not _ID_RE.fullmatch(item_id):
        return None
    p = ARCHIVE_DIR / f"{item_id}.md"
    if not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def _inline(raw: str) -> str:
    """先转义整行，再只把白名单链接还原成 <a>。"""
    esc = html.escape(raw, quote=False)

    def link(m: re.Match) -> str:
        label, url = m.group(1), m.group(2)
        if not re.match(r"^https?://", url, re.I):
            return label  # javascript: / data: 等一律降级成纯文本
        # 整行已经转义过一次，先把 &amp; 还原再转义属性，避免二次转义
        raw_url = url.replace("&amp;", "&")
        return (f'<a href="{html.escape(raw_url, quote=True)}" '
                f'target="_blank" rel="noopener noreferrer">{label}</a>')

    out = _LINK_RE.sub(link, esc)
    return _BOLD_RE.sub(r"<strong>\1</strong>", out)


def render_markdown(text: str) -> str:
    """渲染本项目简报会用到的 Markdown 子集：标题 / 链接 / 粗体 /
    段落 / 列表 / 分隔线。其它语法按纯文本显示，不执行任何 HTML。
    """
    out: list[str] = []
    para: list[str] = []
    bullets: list[str] = []

    def flush() -> None:
        if para:
            out.append("<p>" + "<br>".join(para) + "</p>")
            para.clear()
        if bullets:
            out.append("<ul>" + "".join(f"<li>{b}</li>" for b in bullets)
                       + "</ul>")
            bullets.clear()

    for line in text.replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if not s:
            flush()
            continue
        if _HR_RE.fullmatch(s):
            flush()
            out.append("<hr>")
            continue
        m = _HEAD_RE.match(s)
        if m:
            flush()
            level = min(len(m.group(1)), 6)
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            continue
        if s.startswith(("- ", "* ")):
            if para:
                flush()
            bullets.append(_inline(s[2:]))
            continue
        if bullets:
            flush()
        para.append(_inline(s))
    flush()
    return "\n".join(out)
