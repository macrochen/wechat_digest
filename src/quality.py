#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""候选池的质量闸门：把「没有正经标题」的条目挡在清单之外。

由来
----
用户反馈：公众号里这类「大周末的，和你们聊几句……」的闲聊文，一般**没有
独立标题**，微信返回的元信息里标题位往往塞进了正文首段，公众号名也可能
是空的。这类内容信息价值低，还会把语音稿的条目标题撑坏（标题里带换行）。

判定只看元信息，不看正文（`scan` 阶段本来就不取正文）：

* `source` 空 —— 连公众号名都没有，多半是这个号的设置问题；
* `title` 空；
* `title` 里有换行 —— 拿整段正文当标题了；
* `title` 超过 `max_title_chars`（默认 80）—— 正常公众号标题不会这么长。

命中就不进候选池，也不写任何台账，下次还能重抓（跟黑名单是同一档处理）。
"""

from __future__ import annotations

MAX_TITLE_CHARS = 80


def why_not_title(article: dict, *, max_title_chars: int = MAX_TITLE_CHARS
                  ) -> str:
    """返回命中原因（空字符串表示没问题）。"""
    title = (article.get("title") or "").strip()
    source = (article.get("source") or "").strip()
    if not source:
        return "公众号名为空"
    if not title:
        return "标题为空"
    if "\n" in title or "\r" in title:
        return "标题里带换行（整段正文被当成标题）"
    if len(title) > max_title_chars:
        return f"标题超过 {max_title_chars} 字"
    return ""


def filter_articles(articles: list[dict], *,
                    max_title_chars: int = MAX_TITLE_CHARS
                    ) -> tuple[list[dict], list[dict]]:
    """拆成 (保留, 剔除)。剔除项会补上 `reason` 方便在候选池里核对。"""
    kept: list[dict] = []
    dropped: list[dict] = []
    for a in articles:
        reason = why_not_title(a, max_title_chars=max_title_chars)
        if reason:
            dropped.append({**a, "reason": reason})
        else:
            kept.append(a)
    return kept, dropped
