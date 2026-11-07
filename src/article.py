#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公众号文章的网络读取与清洗。

GUI 只负责把 mp.weixin.qq.com 链接递出来；标题、公众号名、发布时间、正文
全部在这里补齐（上一轮已验证公开页可读）。

- scan 阶段调 fetch_meta：只解析元信息，**不保存正文**（未勾选的篇目不下载正文）
- hydrate 阶段调 fetch_content：抓正文并清推广噪音
"""

from __future__ import annotations

import html
import re
import subprocess
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")
CN = timezone(timedelta(hours=8))

# 常见噪音行，照搬已验证日报流水线的 JUNK 规则
JUNK = re.compile(
    r"^(免责声明|版权|转载|点击|关注|扫码|长按|广告|本文|编辑|来源[:：]|"
    r"Tags?[:：]|标签[:：]|作者\s*$|阅读原文|分享|点赞|在看|写下你的留言|"
    r"About the Author|Please enable JavaScript|comments powered by|"
    r"跳转微信打开|点个在看|都看到这里了|还不快快点赞|还不快点赞|"
    r"点个赞|转发一下|欢迎转发|长按识别|往期回顾|更多精彩|"
    r"股市复盘|刘备教授\s*$|猫笔刀\s*$)"
)


class FetchError(RuntimeError):
    pass


def fetch_html(url: str, timeout: int = 20) -> str:
    """curl 优先（沙箱里 urllib 会被本地代理挡），失败再退 urllib。"""
    cmd = ["curl", "-sS", "-L", "-m", str(timeout), "-A", UA, url]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
        if p.returncode == 0 and p.stdout:
            return p.stdout.decode("utf-8", "ignore")
        err = (p.stderr or b"").decode("utf-8", "ignore").strip()[:160]
    except (OSError, subprocess.SubprocessError) as exc:
        err = str(exc)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "ignore")
    except Exception as exc:  # noqa: BLE001 - 两种通道都失败才抛
        raise FetchError(f"页面读不到：{url}（curl: {err}；urllib: {exc}）")


def _match(patterns: list[str], text: str) -> str:
    for p in patterns:
        m = re.search(p, text, re.S | re.I)
        if m:
            val = re.sub(r"<[^>]+>", "", m.group(1))
            return html.unescape(val).strip()
    return ""


def _parse_time(text: str) -> str:
    """从源码里找 10 位 ct 时间戳或日期串，统一输出北京时间 YYYY-MM-DD HH:MM。"""
    m = re.search(r'var\s+ct\s*=\s*["\'](\d{10})["\']', text)
    if not m:
        m = re.search(r'["\']?publish_time["\']?\s*[:=]\s*["\']?(\d{10})', text)
    if m:
        return datetime.fromtimestamp(int(m.group(1)), CN).strftime(
            "%Y-%m-%d %H:%M")
    m = re.search(r'var\s+createTime\s*=\s*["\']([\d\-: ]+)["\']', text)
    if not m:
        m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{2}:\d{2})?", text)
    if m:
        if len(m.groups()) >= 3 and "年" in m.group(0):
            return (f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                    f" {m.group(4) or '00:00'}")
        return m.group(1).strip()
    return ""


_BLOCK = {"p", "div", "section", "br", "li", "h1", "h2", "h3", "h4",
          "h5", "h6", "blockquote", "tr", "ul", "ol", "hr"}


class _ContentExtractor(HTMLParser):
    """只收 #js_content 里的文字，跳过 script/style，块级标签补换行。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.capture = False
        self.div_depth = 0
        self.skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if not self.capture and d.get("id") == "js_content":
            self.capture = True
            self.div_depth = 1
            return
        if not self.capture:
            return
        if tag in ("script", "style"):
            self.skip += 1
            return
        if tag == "div":
            self.div_depth += 1
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if not self.capture:
            return
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
            return
        if tag == "div":
            self.div_depth -= 1
            if self.div_depth <= 0:
                self.capture = False
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.capture and not self.skip and data.strip():
            self.parts.append(data)


def extract_content(page: str) -> str:
    ex = _ContentExtractor()
    try:
        ex.feed(page)
    except Exception:  # noqa: BLE001 - HTML 残缺时也尽量返回已收集内容
        pass
    return "\n".join(ex.parts)


def strip_junk(text: str) -> str:
    """逐行清掉推广/导航噪音，重排成空行分段。"""
    lines = [ln.strip().replace("\u3000", " ").replace("\xa0", " ")
             for ln in text.split("\n")]
    lines = [re.sub(r"[ \t]+", " ", ln) for ln in lines]
    lines = [ln for ln in lines if ln and not JUNK.match(ln)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def fetch_meta(url: str) -> dict:
    """只取标题 / 公众号名 / 发布时间，不返回正文。"""
    page = fetch_html(url)
    title = _match([
        # 微信页面里是 var msg_title = '标题'.html(false); 后面不是分号，
        # 旧正则会一路吞到下一个引号，把 msg_desc / msg_cdn_url 混进标题。
        # 用 [^"']+ 禁止跨引号，就能只取标题本身。
        r'var\s+msg_title\s*=\s*["\']([^"\']+)["\']',
        r'"msg_title"\s*:\s*"([^"]+)"',
        r'<h1[^>]*rich_media_title[^>]*>(.*?)</h1>',
        r'<meta\s+property="og:title"\s+content="([^"]+)"',
        r"<title>(.*?)</title>",
    ], page)
    source = _match([
        r'var\s+nickname\s*=\s*["\']([^"\']+)["\']',
        r'"nickname"\s*:\s*"([^"]+)"',
        r'id="js_name"[^>]*>(.*?)<',
        r'var\s+user_name\s*=\s*["\']([^"\']+)["\']',
    ], page)
    published_at = _parse_time(page)
    if not title:
        raise FetchError(f"页面里没解析到标题，可能是验证页：{url}")
    return {"url": url, "title": title, "source": source,
            "published_at": published_at}


def fetch_content(url: str, min_chars: int = 50) -> dict:
    """取正文并清洗。太短不丢弃，交给提示词标「内容无法总结」。"""
    page = fetch_html(url)
    content = strip_junk(extract_content(page))
    return {
        "content": content,
        "content_chars": len(content),
        "too_short": len(content) < min_chars,
    }
