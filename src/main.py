#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wechat_digest 命令行入口。

完整流程由 WorkBuddy 的 wechat-digest skill 编排（触发形态 A）：
  capture → (Hy4 读图给坐标) → grab → scan → select → hydrate
  → (Hy4 分批并发总结，写 batch-N.md) → merge → publish

也可以单独跑任一阶段做调试。所有命令都从项目根目录执行：
  cd ~/workspace/wechat_digest && python3 -m src.main --help
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from . import article, grab, gui, prompt, publish, selection
from .config import load_settings, project_path
from .state import (ClaimLock, Published, Runs, Seen, dedup_similar,
                    load_json, save_json)

EXIT_OK, EXIT_ERROR, EXIT_HUMAN = 0, 1, 3


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _inbox_path(date: str) -> Path:
    return project_path("out", f"inbox-{date}.json")


def _latest_inbox() -> dict:
    files = sorted(project_path("out").glob("inbox-*.json"), reverse=True)
    if not files:
        raise RuntimeError("还没有候选池，请先跑 capture / grab / scan")
    return load_json(files[0], {})


def _run_key(date: str, urls: list[str]) -> str:
    h = hashlib.sha256("\n".join(sorted(urls)).encode("utf-8")).hexdigest()[:8]
    return f"{date}-{h}"


# ---------------------------------------------------------------- 子命令

def cmd_doctor(cfg, args) -> int:
    print("=== wechat_digest 环境自检 ===")
    ok = True
    print(f"· 平台：{sys.platform}")
    if sys.platform != "darwin":
        print("  × 只能在 macOS 跑 GUI 抓取"); ok = False
    print(f"· 微信进程：{'运行中' if gui.wechat_running() else '未运行'}")
    locked = gui.screen_locked()
    print(f"· 屏幕锁定：{locked if locked is not None else '无法判定（不误报）'}")
    if locked is True:
        ok = False
    pf = project_path("prompts", "summary.md")
    print(f"· 融合提示词：{'有' if pf.exists() else '缺失'} {pf}")
    if not pf.exists():
        ok = False
    try:
        _, where = publish.read_token(cfg)
        print(f"· ZenMark 令牌：可读（来源 {where}，值不显示）")
    except publish.PublishError as exc:
        print(f"  × {exc}"); ok = False
    print(f"· 每批上限 {cfg['batch_size']} 篇 / 子代理并发 ≤{cfg['max_parallel']}"
          f" / 相似度阈值 {cfg['dedup_threshold']}")
    print(f"· UI 坐标：更多 {tuple(cfg['ui']['more_button'])} / 复制链接 "
          f"{tuple(cfg['ui']['copy_link'])} / 关闭 {tuple(cfg['ui']['close_window'])}")
    print("=== 自检结束（未发送任何网络请求、未改动台账）===")
    return EXIT_OK if ok else EXIT_ERROR


def cmd_capture(cfg, args) -> int:
    result = grab.capture(cfg, args.out)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_grab(cfg, args) -> int:
    coords = grab.parse_coords(args.coords)
    result = grab.grab(cfg, coords, args.limit, args.dry_run, args.append)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_scroll(cfg, args) -> int:
    grab.preflight()
    gui.scroll(args.x, args.y, args.amount)
    print(f"· 已滚动（amount={args.amount}），请重新 capture 读卡片坐标")
    return EXIT_OK


def cmd_scan(cfg, args) -> int:
    """从 raw urls 建候选池：只取元信息，不下载正文。"""
    raw_path = Path(args.urls_file).expanduser() if args.urls_file else \
        project_path("out", "_raw_urls.json")
    if not raw_path.exists():
        raise RuntimeError(f"找不到链接文件：{raw_path}，请先跑 grab")
    raw = load_json(raw_path, {})
    urls = raw.get("urls") if isinstance(raw, dict) else raw
    urls = list(dict.fromkeys(urls or []))[:cfg["max_articles"]]
    if not urls:
        print("· 没有链接可处理")
        return EXIT_OK

    date = args.date or _today()
    run_key = _run_key(date, urls)
    runs = Runs()
    done = runs.done(run_key)
    if done:
        print(f"· 已在 {done.get('finished_at') or done.get('started_at')} "
              f"跑过，本次跳过（run_key={run_key}）")
        print("  没有产生第二份简报，也没有调用 ZenMark 接口")
        return EXIT_OK

    seen = Seen()
    fresh = [u for u in urls if not seen.has(u)]
    skipped = [u for u in urls if seen.has(u)]
    if skipped:
        print(f"· URL 级去重：跳过已抓过的 {len(skipped)} 篇", file=sys.stderr)
    if not fresh:
        print("· 这批全都抓过了，没有新文章")
        return EXIT_OK

    def _meta(u):
        try:
            return article.fetch_meta(u)
        except Exception as exc:  # noqa: BLE001
            return {"url": u, "error": str(exc)}

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(_meta, fresh))
    articles, failed = [], []
    for i, r in enumerate(results, 1):
        if r.get("error"):
            failed.append(r)
        else:
            r["index"] = i
            articles.append(r)

    inbox = {
        "date": date, "generated_at": _now(), "phase": "scan",
        "run_key": run_key,
        "max_articles": cfg["max_articles"],
        "articles": articles,
        "failed_meta": failed,
        "skipped_seen": skipped,
        "_note": "phase=scan：只有元信息，没有正文；hydrate 之后才取正文",
    }
    p = _inbox_path(date)
    save_json(p, inbox)
    print(f"✓ 候选池：{p}")
    print(f"  新文章 {len(articles)} 篇 / 元信息失败 {len(failed)} 篇"
          f" / 已抓过 {len(skipped)} 篇")
    for a in articles:
        print(f"  {a['index']:>2}. {a.get('published_at','时间未知')}  "
              f"{a.get('source','?')}  《{a['title']}》")
    print("· 下一步：select --all 或 select --ids 1,3,5（不选则默认整份都要）")
    return EXIT_OK


def cmd_select(cfg, args) -> int:
    inbox = _latest_inbox()
    if args.all:
        indices, mode = None, "all"
    elif args.ids:
        indices = [int(x) for x in args.ids.split(",") if x.strip()]
        mode = "manual"
    else:
        raise RuntimeError("请给 --all 或 --ids 1,3,5")
    plan = selection.write_plan(inbox, indices, mode)
    print(f"✓ 选文清单：{selection.plan_path(inbox['date'])}")
    print(f"  选中 {len(plan['selected'])} 篇 / 共 {len(inbox['articles'])} 篇"
          f"（模式 {plan['mode']}）")
    return EXIT_OK


def cmd_hydrate(cfg, args) -> int:
    """按选文清单取正文 → 相似度去重 → 生成待总结包。"""
    inbox = _latest_inbox()
    if inbox.get("phase") != "scan":
        raise RuntimeError("候选池 phase 不是 scan，可能已经处理过了")
    run_key = inbox["run_key"]
    runs = Runs()
    done = runs.done(run_key)
    if done:
        print(f"· 已在 {done.get('finished_at') or done.get('started_at')} "
              f"跑过，本次跳过（run_key={run_key}）")
        return EXIT_OK

    lock = ClaimLock(cfg["lock_ttl_minutes"])
    if not lock.acquire(run_key):
        owner = lock.owner()
        raise RuntimeError(
            f"这批已被另一个进程认领（pid={owner.get('pid')}, "
            f"at={owner.get('at')}）。等它跑完或锁过期（{cfg['lock_ttl_minutes']} 分钟）再试")

    chosen = selection.read_plan(inbox)
    all_articles = inbox["articles"]
    source_articles = all_articles if chosen is None else [
        a for a in all_articles if a["index"] in chosen]
    # 浅拷贝：正文只进待总结包，不回写候选池（候选池永远只有元信息）
    picked = [dict(a) for a in source_articles]
    if not picked:
        print("· 没有选中任何文章")
        return EXIT_OK
    print(f"· 取正文：{len(picked)}/{len(all_articles)} 篇", file=sys.stderr)

    def _hydrate(a):
        try:
            a.update(article.fetch_content(a["url"], cfg["min_content_chars"]))
        except Exception as exc:  # noqa: BLE001
            a.update({"content": "", "content_chars": 0,
                      "too_short": True, "fetch_error": str(exc)})
        return a

    with ThreadPoolExecutor(max_workers=3) as pool:
        hydrated = list(pool.map(_hydrate, picked))

    kept, dropped = dedup_similar(hydrated, cfg["dedup_threshold"])
    if dropped:
        save_json(project_path("out", "_dedup_log.json"),
                  {"run_key": run_key, "at": _now(), "dropped": dropped})
        print(f"· 相似度去重：合并 {len(dropped)} 篇转载", file=sys.stderr)
        for d in dropped:
            print(f"    {d['score']:.2f} 《{d['title']}》→ 保留 {d['kept_url']}",
                  file=sys.stderr)

    pending = prompt.build_pending(inbox, kept, run_key,
                                   cfg["batch_size"], cfg["max_parallel"])
    runs.begin(run_key, {"articles": len(kept),
                         "batches": len(pending["batches"])})
    inbox["phase"] = "hydrated"
    save_json(_inbox_path(inbox["date"]), inbox)

    print(f"✓ 待总结包：{project_path('out', 'pending_summary.json')}")
    print(f"  {len(kept)} 篇拆成 {len(pending['batches'])} 批"
          f"（每批 ≤{cfg['batch_size']} 篇，子代理并发 ≤{cfg['max_parallel']}）")
    for b in pending["batches"]:
        print(f"  批 {b['id']}: {b['count']} 篇 index={b['indices']}")
        print(f"    输入 {b['file']}")
        print(f"    输出 {b['output']}")
    return EXIT_OK


def cmd_batches(cfg, args) -> int:
    pending = load_json(project_path("out", "pending_summary.json"), {})
    if not pending:
        raise RuntimeError("还没有待总结包，请先跑 hydrate")
    print(json.dumps({
        "run_key": pending["run_key"],
        "prompt_file": pending["prompt_file"],
        "max_parallel": pending["max_parallel"],
        "batches": pending["batches"],
    }, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_merge(cfg, args) -> int:
    pending = load_json(project_path("out", "pending_summary.json"), {})
    if not pending:
        raise RuntimeError("还没有待总结包，请先跑 hydrate")
    merged = prompt.merge_batches(pending)
    print(f"· 合并 {len(pending['batches'])} 批完成，"
          f"共 {len(prompt._HEAD_RE.findall(merged))} 条", file=sys.stderr)
    if args.out:
        Path(args.out).expanduser().write_text(merged, encoding="utf-8")
        print(f"· 同时写入 {args.out}", file=sys.stderr)
    sys.stdout.write(merged)
    return EXIT_OK


def cmd_publish(cfg, args) -> int:
    if args.check:
        return publish.publish(cfg, "", check=True)
    if args.file:
        text = Path(args.file).expanduser().read_text(encoding="utf-8")
    else:
        text = publish.read_stdin()
    return publish.publish(cfg, text, title=args.title, dry_run=args.dry_run)


def cmd_status(cfg, args) -> int:
    inbox = None
    try:
        inbox = _latest_inbox()
    except RuntimeError:
        pass
    pending = load_json(project_path("out", "pending_summary.json"), {})
    batches = []
    for b in pending.get("batches", []):
        batches.append({"id": b["id"], "count": b["count"],
                        "written": Path(b["output"]).exists()})
    data = {
        "seen": len(Seen().data),
        "runs": Runs().data,
        "published": Published().data,
        "lock": ClaimLock().owner(),
        "inbox": None if not inbox else {
            "date": inbox["date"], "run_key": inbox["run_key"],
            "phase": inbox["phase"],
            "articles": len(inbox["articles"]),
            "generated_at": inbox["generated_at"],
        },
        "pending": None if not pending else {
            "run_key": pending["run_key"],
            "articles": pending["article_count"],
            "batches": batches,
        },
    }
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return EXIT_OK


# ---------------------------------------------------------------- CLI

def build_parser():
    ap = argparse.ArgumentParser(
        prog="wechat_digest",
        description="从本地微信公众号信息流生成简报并推到 ZenMark")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="环境自检（不发请求、不改台账）")

    p = sub.add_parser("capture", help="激活微信并截一张逻辑坐标图")
    p.add_argument("--out", default=None, help="截图输出路径")

    p = sub.add_parser("grab", help="按卡片坐标批量复制文章链接")
    p.add_argument("--coords", required=True,
                   help="卡片中心坐标，形如 '560,275;560,400'")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--append", action="store_true",
                   help="分多屏抓时，把本轮链接合并进已有的 _raw_urls.json")
    p.add_argument("--dry-run", action="store_true", help="只校验环境与坐标，不点击")

    p = sub.add_parser("scroll", help="滚动信息流")
    p.add_argument("--x", type=float, default=700)
    p.add_argument("--y", type=float, default=450)
    p.add_argument("--amount", type=int, default=-4)

    p = sub.add_parser("scan", help="按链接补元信息，生成候选池（不取正文）")
    p.add_argument("--urls-file", default=None)
    p.add_argument("--date", default=None)

    p = sub.add_parser("select", help="人工选文")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true", help="整份都要")
    g.add_argument("--ids", help="逗号分隔的编号，如 1,3,5")

    sub.add_parser("hydrate", help="取正文 + 相似度去重 + 生成待总结包")
    sub.add_parser("batches", help="打印分批方案与输入输出路径")

    p = sub.add_parser("merge", help="合并各批总结，输出到 stdout（可管道给 publish）")
    p.add_argument("--out", default=None, help="可选：同时写一份文件")

    p = sub.add_parser("publish", help="推到 ZenMark（默认从 stdin 读简报）")
    p.add_argument("--file", default=None, help="从文件读简报")
    p.add_argument("--title", default=None)
    p.add_argument("--dry-run", action="store_true", help="不发送请求、不改台账")
    p.add_argument("--check", action="store_true", help="只读验令牌")

    sub.add_parser("status", help="查看台账与批次进度")
    return ap


HANDLERS = {
    "doctor": cmd_doctor, "capture": cmd_capture, "grab": cmd_grab,
    "scroll": cmd_scroll, "scan": cmd_scan, "select": cmd_select,
    "hydrate": cmd_hydrate, "batches": cmd_batches, "merge": cmd_merge,
    "publish": cmd_publish, "status": cmd_status,
}


def run(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_settings()
    try:
        return HANDLERS[args.cmd](cfg, args)
    except grab.GrabAborted as exc:
        print(f"× 需要人介入：\n  {exc}", file=sys.stderr)
        return EXIT_HUMAN
    except (RuntimeError, publish.PublishError, OSError) as exc:
        print(f"× {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(run())
