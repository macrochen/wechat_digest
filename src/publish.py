#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把汇总简报推到 ZenMark 阅读器。

接口说明（参考项目的实现思路，运行时不读取任何外部项目文件）：
  POST <api>   Authorization: Bearer <token>
  body {"title": ..., "content": <markdown>}
  返回 {"success": true, "item": {"id": "cloud_xxx"}}

令牌从**本项目内** config/secrets.local.json 运行时只读（Git 忽略、权限 600），
可用环境变量 ZENMARK_API_TOKEN 覆盖。不读其它项目任何文件、不打印、不提交 Git。
正文走 stdin，正常情况不落盘；只有发布失败才留 out/failed-<日期>.md。
"""

from __future__ import annotations

import json
import os
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


def read_token(cfg: dict):
    """返回 (token, 来源描述)。只读本项目配置，绝不读外部项目。

    优先级：环境变量 ZENMARK_API_TOKEN > config/secrets.local.json。
    """
    env = os.environ.get("ZENMARK_API_TOKEN", "").strip()
    if env:
        return env, "环境变量 ZENMARK_API_TOKEN"

    p = Path(cfg["token_file"])
    if not p.exists():
        raise PublishError(
            f"找不到项目内令牌文件：{p}\n"
            "  请执行 cp config/secrets.example.json config/secrets.local.json "
            "并填入 zenmark_token")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PublishError(f"令牌文件读不了：{p}（{exc}）")
    if not isinstance(data, dict):
        raise PublishError(f"{p} 格式不对，应为 JSON 对象")
    token = str(data.get("zenmark_token") or data.get("token") or "").strip()
    if not token:
        raise PublishError(f"{p} 里没有 zenmark_token，不发送、不留残缺记录")
    return token, p.name


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
    token, where = read_token(cfg)  # 只读项目内配置，绝不打印值

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
