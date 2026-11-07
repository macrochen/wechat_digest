#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把汇总简报推到 ZenMark 阅读器。

接口对齐 daily_digest_hub/scripts/publish.py：
  POST <api>   Authorization: Bearer <token>
  body {"title": ..., "content": <markdown>}
  返回 {"success": true, "item": {"id": "cloud_xxx"}}

令牌**运行时只读** daily_digest_hub/wechat/config/sources.yaml 的
settings.publish.token，不复制进本项目、不打印、不写日志、不提交 Git。
正文走 stdin，正常情况不落盘；只有发布失败才留 out/failed-<日期>.md。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

from .config import project_path
from .state import ClaimLock, Published, Runs, Seen, load_json, sha256_text

TIMEOUT = 60  # 该接口实测 1~20 秒都有，30 秒会误判


class PublishError(RuntimeError):
    pass


def read_token(source: str) -> str:
    """不依赖 PyYAML，只按缩进定位 settings.publish.token。"""
    p = Path(source)
    if not p.exists():
        raise PublishError(
            f"找不到令牌来源文件：{p}\n"
            "  请检查 config/settings.json 的 token_source 路径")
    publish_indent = None
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if publish_indent is None:
            if re.match(r"^publish\s*:", s):
                publish_indent = indent
            continue
        if indent <= publish_indent:
            break
        m = re.match(r"^token\s*:\s*(.+)$", s)
        if m:
            token = m.group(1).split(" #")[0].strip().strip("\"'").strip()
            if token:
                return token
    raise PublishError(
        f"在 {p} 里没找到 settings.publish.token，不发送、不留残缺记录")


def _headers(token: str, json_body: bool = True) -> dict:
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if json_body:
        h["Content-Type"] = "application/json; charset=utf-8"
    return h


def _post_curl(url, payload, headers) -> str:
    cmd = ["curl", "-sS", "-L", "-m", str(TIMEOUT), "-X", "POST", url]
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    cmd += ["--data-binary", "@-"]
    p = subprocess.run(cmd, input=json.dumps(payload, ensure_ascii=False).encode(),
                       capture_output=True, timeout=TIMEOUT + 10)
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError("curl 失败：" + (p.stderr or b"").decode(
            "utf-8", "ignore").strip()[:160])
    return p.stdout.decode("utf-8", "ignore")


def _post_urllib(url, payload, headers) -> str:
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "ignore")


def _try_post(url, payload, headers):
    errors = []
    for fn, name in ((_post_curl, "curl"), (_post_urllib, "urllib")):
        try:
            raw = fn(url, payload, headers)
            data = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
            continue
        item = data.get("item") or {}
        if data.get("success") and item.get("id"):
            return True, item["id"]
        return False, data.get("error") or json.dumps(
            data, ensure_ascii=False)[:200]
    return False, " | ".join(errors)


def check_token(api: str, token: str):
    """只读验令牌：拿云端清单，不上传任何东西。"""
    list_url = api[:-len("/upload")] if api.endswith("/upload") else api
    cmd = ["curl", "-sS", "-L", "-m", "30"]
    for k, v in _headers(token, json_body=False).items():
        cmd += ["-H", f"{k}: {v}"]
    cmd.append(list_url)
    p = subprocess.run(cmd, capture_output=True, timeout=40)
    if p.returncode != 0 or not p.stdout:
        return False, (p.stderr or b"").decode("utf-8", "ignore").strip()[:160]
    try:
        data = json.loads(p.stdout.decode("utf-8", "ignore"))
        files = data.get("files")
        if isinstance(files, list):
            return True, len(files)
    except ValueError:
        pass
    return False, p.stdout.decode("utf-8", "ignore")[:200]


def publish(cfg: dict, text: str, title: str | None = None,
            dry_run: bool = False, check: bool = False) -> int:
    api = cfg["publish_api"]
    source = cfg["token_source"]
    token = read_token(source)  # 只读，绝不打印值
    where = Path(source).name

    if check:
        ok, res = check_token(api, token)
        if not ok:
            print(f"× 令牌或接口有问题：{res}")
            return 1
        print(f"✓ 令牌可用（来源：{where}，值不显示）")
        print(f"  接口：{api}")
        print(f"  云端共 {res} 个文件")
        return 0

    if not text.strip():
        print("× 没有内容可发（stdin 是空的）")
        return 1

    pending_path = project_path("out", "pending_summary.json")
    pending = load_json(pending_path, {})
    run_key = pending.get("run_key") or "manual-" + datetime.now().strftime(
        "%Y-%m-%d")
    date = pending.get("date") or datetime.now().strftime("%Y-%m-%d")

    runs = Runs()
    done = runs.done(run_key)
    if done:
        print(f"· 已在 {done.get('finished_at') or done.get('started_at')} 跑过，"
              f"本次跳过（run_key={run_key}）")
        print("  没有产生第二份简报，也没有调用 ZenMark 接口")
        return 0

    digest = sha256_text(text)
    pub = Published()
    same = pub.same(run_key, digest)
    if same:
        print(f"· 内容没变，跳过（上次已上传：{same.get('id')}）")
        return 0

    final_title = title or f"{cfg['publish_title_prefix']} · {date}"
    print(f"→ 准备发布：{final_title}")
    print(f"  正文 {len(text)} 字 / {text.count(chr(10)) + 1} 行")
    print(f"  令牌来源：{where}（值不显示）")
    print(f"  接口：{api}")
    print(f"  正文哈希：{digest}")

    if dry_run:
        print("· dry-run：没有发送任何请求，也没有改动台账")
        return 0

    ok, res = _try_post(api, {"title": final_title, "content": text},
                        _headers(token))
    if not ok:
        fallback = project_path("out", f"failed-{date}.md")
        fallback.parent.mkdir(parents=True, exist_ok=True)
        fallback.write_text(text, encoding="utf-8")
        print(f"× 发布失败：{res}")
        print(f"  正文已留底：{fallback}")
        print(f"  修好后重发：cat {fallback} | python3 -m src.main publish --stdin")
        return 1

    pub.mark(run_key, digest, {"id": res, "title": final_title})
    runs.finish(run_key, {"id": res, "articles": pending.get("article_count", 0)})
    articles = pending.get("articles") or []
    if articles:
        Seen().mark(articles)  # 成功后才记 seen，失败下次可原样重试
    ClaimLock(cfg["lock_ttl_minutes"]).release()
    print(f"✓ 已上传：{res}")
    print(f"  已记入 seen / runs / published，下次同批会直接跳过")
    return 0


def read_stdin() -> str:
    if sys.stdin.isatty():
        return ""
    return sys.stdin.read()
