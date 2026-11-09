#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wechat_digest 命令行入口。

完整流程由 WorkBuddy 的 wechat-digest skill 编排（触发形态 A）：
  capture → (Hy4 读图给坐标) → grab → scan → [自动模式：scan 自己放行并接着
  hydrate；人工模式：review 清单页勾选并点「开始总结」] → hydrate →
  (Hy4 分批并发总结，写 batch-N.md) → merge → publish

  默认抓取窗口是「今天 + 昨天」共 2 个自然日（feed_days），默认自动放行
  （auto_summary=1）。没在清单页提交、且自动模式没写放行信号时，
  hydrate 一律拒绝执行 —— 闸门只有一个，看信号文件。
  本地控制台是「一个常驻服务、一个端口（默认 7777）」：/review 选文清单、
  /read 本地阅读、/settings 参数设置始终同址；提交后服务不退出。
  用户改动写 config/settings.local.json，优先级高于 config/settings.json。
  抓取黑名单 config/blacklist.json 在 scan 阶段按公众号名整号排除，
  读图给坐标时也跳过黑名单里的号，省掉无用的 GUI 点击。

也可以单独跑任一阶段做调试。所有命令都从项目根目录执行：
  cd ~/workspace/wechat_digest && python3 -m src.main --help
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import date as _Date, datetime, timedelta
from pathlib import Path

from . import (account, article, blacklist, grab, grab_review, gui, nav, ocr,
               prompt, publish, quality, review, selection)
from .config import (inbox_file, inbox_matches, load_settings,
                     pending_file, project_path)
from .state import (ClaimLock, Offered, Published, Runs, Seen, dedup_similar,
                    load_json, save_json)

EXIT_OK, EXIT_ERROR, EXIT_HUMAN = 0, 1, 3


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _inbox_path(date: str, scope: str = "") -> Path:
    """候选池路径。scope="" 是信息流那份，否则是按号抓取那份。"""
    return project_path("out", inbox_file(date, scope))


def _scope_of(inbox: dict, args) -> str:
    """这份候选池属于哪个 scope：以池子里记的为准，命令行只作兜底。"""
    return str(inbox.get("scope") or getattr(args, "scope", "") or "")


def _latest_inbox(scope: str = "") -> dict:
    files = sorted((p for p in project_path("out").glob("inbox-*.json")
                    if inbox_matches(p.name, scope)), reverse=True)
    if not files:
        hint = f"（scope={scope}）" if scope else ""
        raise RuntimeError(f"还没有候选池{hint}，请先跑 capture / grab / scan")
    return load_json(files[0], {})


def _run_key(date: str, urls: list[str], scope: str = "") -> str:
    """这批的幂等键。带 scope：同一天同一批 URL 的两份简报不算同一批。"""
    h = hashlib.sha256("\n".join(sorted(urls)).encode("utf-8")).hexdigest()[:8]
    return f"{date}-{scope}-{h}" if scope else f"{date}-{h}"


def _scope_from_urls_file(path: Path) -> str:
    """从 `out/_raw_urls-<号名>.json` 的文件名尾段推断 scope。

    按号抓取会写 `_raw_urls-<号名>.json`，紧接着 `scan --urls-file` 处理它。
    让用户再手敲一遍 `--scope <号名>` 既啰嗦又容易打错（打错就落到另一份
    候选池里，症状很隐蔽）。这里从文件名反推：`_raw_urls-EarlETF.json`
    → `EarlETF`；信息流那份 `_raw_urls.json` 没有尾段 → 空 scope。
    """
    stem = Path(path).stem                      # _raw_urls-EarlETF
    if not stem.startswith("_raw_urls-"):
        return ""
    return stem[len("_raw_urls-"):].strip()


def _auto_mode(cfg, args) -> bool:
    """本轮要不要跳过人工确认。

    优先级：--confirm（强制人工）> --auto-summary（本轮自动）> 配置项 auto_summary。
    只有 scan 会用它；hydrate 的闸门逻辑不看这个开关，只认放行信号文件本身。
    """
    if getattr(args, "confirm", False):
        return False
    flag = getattr(args, "auto_summary", None)
    if flag is not None:
        return bool(flag)
    return bool(int(cfg.get("auto_summary") or 0))


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
    ui = cfg["ui"]
    geo = gui.screen_geometry()
    disp = geo["display"]
    if disp:
        print(f"· 屏幕几何：逻辑 {disp['w']:g}x{disp['h']:g}"
              f"（{int(disp.get('displays') or 0)} 块活动屏）"
              f" / 全屏截图 {geo['raw'] or '未知'}")
    else:
        print(f"· 屏幕几何：无法判定（全屏截图 {geo['raw'] or '未知'}）")
    geo_on = int(ui.get("geometry_check", 1))
    _, geo_issues = gui.judge_geometry(
        expected_w=int(ui.get("logical_width", 1280)),
        expected_h=int(ui.get("logical_height", 832)),
        display=disp, raw_size=geo["raw"],
        tolerance=float(ui.get("geometry_tolerance", 0.01)))
    for it in geo_issues:
        print(f"  × {it['message']}")
    if geo_issues and geo_on:
        ok = False
    elif geo_issues:
        print("  · 几何自检已关闭，以上问题不会拦截抓取")
    print(f"· 几何自检：{'开启' if geo_on else '关闭'}"
          f"（期望 {ui.get('logical_width', 1280)}x{ui.get('logical_height', 832)}，"
          f"宽高比容差 {float(ui.get('geometry_tolerance', 0.01)):.2%}）")
    pf = project_path("prompts", "summary.md")
    print(f"· 融合提示词：{'有' if pf.exists() else '缺失'} {pf}")
    if not pf.exists():
        ok = False
    try:
        _, where = publish.read_token(cfg)
        print(f"· ZenMark 令牌：可读（来源 {where}，值不显示）")
    except publish.PublishError as exc:
        print(f"  × {exc}"); ok = False
    fs = int(cfg.get("feed_scope", 1))
    fd = max(1, int(cfg.get("feed_days", 2)))
    scope_desc = (f"最近 {fd} 个自然日（滚出窗口为止）" if fs
                  else "按篇数上限")
    print(f"· 抓取范围：{scope_desc}"
          f"（feed_scope={fs}，feed_days={fd}，"
          f"兜底上限 {cfg.get('max_articles')} 篇）")
    auto = int(cfg.get("auto_summary") or 0)
    auto_desc = ("自动放行（scan 出完候选池直接接着 hydrate，"
                 "不等清单页提交）" if auto
                 else "人工确认（清单页点「开始总结」才放行）")
    print(f"· 总结前确认：{auto_desc}"
          f"（auto_summary={auto}，可用 scan --confirm / --no-hydrate 单轮覆盖）")
    print(f"· UI 坐标：更多 {tuple(cfg['ui']['more_button'])} / 复制链接 "
          f"{tuple(cfg['ui']['copy_link'])} / 关闭 {tuple(cfg['ui']['close_window'])}")
    print(f"· 点击前标题行校准："
          f"{'开启' if cfg['ui'].get('title_probe', 1) else '关闭'}"
          f"（向下搜索 {cfg['ui'].get('title_search_down', 45)}px，"
          f"防点到公众号名）")
    # OCR 现在有两处用：按号抓取认号名/时间行，信息流认卡片推送时间判跨天。
    # 编不出来时按号抓取会直接失败；信息流那条链不受影响（只是判不了跨天，
    # 退回到篇数上限兜底）。
    ocr_ok = ocr.available()
    print(f"· 本地文字识别（macOS Vision）：{'可用' if ocr_ok else '不可用'}"
          f"（按号抓取认号名/时间行；信息流认卡片推送时间判跨天）")
    if not ocr_ok:
        print("  × 编不出 OCR 小工具（需要 swiftc / Xcode 命令行工具）；"
              "`account` 子命令会因此失败，信息流那条链不受影响"
              "（只是判不了跨天，退回篇数上限兜底）")
        ok = False
    print(f"· 列表模式坐标：左栏 x={tuple(ui.get('account_list_x', []))} / "
          f"行距 {ui.get('account_row_pitch')}px / "
          f"右栏起点 x={ui.get('account_pane_left')}")
    print(f"· 按号抓取默认取 {cfg.get('account_max_pushes', 20)} 次推送的头条"
          f"（一次推送多条只要第一条）")
    print("=== 自检结束（未发送任何网络请求、未改动台账）===")
    return EXIT_OK if ok else EXIT_ERROR


def cmd_capture(cfg, args) -> int:
    result = grab.capture(cfg, args.out)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_nav(cfg, args) -> int:
    """自动进「公众号」信息流，并输出本屏卡片的可点坐标。"""
    result = nav.nav(cfg, out=args.out)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_grab(cfg, args) -> int:
    limit = args.limit if args.limit is not None else cfg["max_articles"]
    if limit <= 0:
        raise RuntimeError("抓取篇数必须大于 0")
    resume = bool(getattr(args, "resume", False))
    no_gate = bool(getattr(args, "no_gate", False))
    # --resume 是「从头重抓补齐」，必须合并已有链接，不能覆盖掉上一轮的成果。
    append = bool(args.append) or resume
    if args.auto:
        result = grab.grab_auto(cfg, limit, dry_run=args.dry_run,
                                append=append, resume=resume,
                                no_gate=no_gate)
    else:
        if not args.coords:
            raise RuntimeError("要么给 --coords，要么加 --auto 让程序自己找卡片")
        coords = grab.parse_coords(args.coords)
        result = grab.grab(cfg, coords, limit, args.dry_run, append)
    # 每屏明细已经写进 out/_grab_ledger-<date>.json，终端只打摘要。
    shown = {k: v for k, v in result.items() if k != "screens"}
    print(json.dumps(shown, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_scroll(cfg, args) -> int:
    grab.preflight(cfg)
    gui.scroll(args.x, args.y, args.amount)
    print(f"· 已滚动（amount={args.amount}），请重新 capture 读卡片坐标")
    return EXIT_OK


def _window_dates(target: str, days: int) -> list[str]:
    """抓取窗口里的日期（YYYY-MM-DD），从新到旧；含基准日往前 N-1 天。

    按**自然日**、北京时间 00:00 分界：2026-11-09 + 2 天 =
    ["2026-11-09", "2026-11-08"]。日期格式不合法时退回「只认这一天」。
    """
    n = max(1, int(days))
    try:
        end = _Date.fromisoformat(target)
    except (TypeError, ValueError):
        return [str(target)]
    return [(end - timedelta(days=i)).isoformat() for i in range(n)]


def _filter_recent_days(articles: list[dict], target: str,
                        days: int = 2) -> tuple[list[dict], list[dict]]:
    """把「不在最近 N 个自然日窗口内」的文章剔出去，返回 (留下的, 被剔掉的)。

    判据是服务端给的 `published_at`（北京时间，精确到分），不是抓取层
    那个 OCR 猜的推送时间 —— 后者只用来决定「滚到哪儿停」。

    **认不出发布时间的留下**：宁可多留一篇（它可能就在窗口内），也不误删。
    N=1 等价于原来的「只留当天」。
    """
    allowed = set(_window_dates(target, days))
    keep, out = [], []
    for a in articles:
        pub = str(a.get("published_at") or "").strip()
        day = pub[:10] if len(pub) >= 10 else ""
        if day and day not in allowed:
            out.append(a)
        else:
            keep.append(a)
    return keep, out


def _grab_gate_blocks(date: str, scope: str, ignore: bool = False,
                      *, verbose: bool = True) -> bool:
    """grab → scan 的完备性闸门：有未解除的缺口就返回 True 拦住 scan。

    只管信息流那条链：按号抓取（scope 非空 / --urls-file）是另一套数据源，
    不受 grab_auto 的缺口影响。单轮 `--ignore-grab-gate` 会打印告警留痕。
    """
    if scope:
        return False
    if ignore:
        if verbose:
            print("· 本轮用 --ignore-grab-gate 绕过完备性闸门"
                  "（未做缺口校验，已知风险）", flush=True)
        return False
    from . import grab_ledger
    gate = grab_ledger.read_gate(date)
    if not gate:
        return False
    cov = gate.get("coverage") or {}
    if verbose:
        print(f"⛔ 抓取完备性闸门：{date} 这轮抓取还缺 "
              f"{cov.get('missing', '?')} 篇，scan 已拒绝执行。")
        print(f"   缺口清单：{gate.get('missing_file')}")
        print("   先跑 python3 -m src.main grab --auto --resume 补齐；"
              "确需跳过用 scan --ignore-grab-gate（本轮留痕）")
    return True


def cmd_scan(cfg, args) -> int:
    """从 raw urls 建候选池：只取元信息，不下载正文。

    默认（auto_summary=1）自动放行：候选池落盘后写「全选清单 + 放行信号」
    （mode=auto），不弹清单页，**接着直接跑 hydrate** 取正文 —— 抓完即总结，
    不等人工确认。`--confirm` 压回人工确认（等清单页提交）、`--no-hydrate`
    只出候选池、`--days N` 只改本次抓取窗口。
    """
    raw_path = Path(args.urls_file).expanduser() if args.urls_file else \
        project_path("out", "_raw_urls.json")
    if not raw_path.exists():
        raise RuntimeError(f"找不到链接文件：{raw_path}，请先跑 grab")
    raw = load_json(raw_path, {})
    urls = raw.get("urls") if isinstance(raw, dict) else raw
    limit = args.max_articles
    if limit is None:
        limit = cfg["max_articles"]
    if limit <= 0:
        raise RuntimeError("抓取篇数必须大于 0")
    urls = list(dict.fromkeys(urls or []))[:limit]
    if not urls:
        print("· 没有链接可处理")
        return EXIT_OK

    date = args.date or _today()
    scope = str(getattr(args, "scope", "") or "")
    if not scope and args.urls_file:
        scope = _scope_from_urls_file(raw_path)   # 按号抓的那份自己带号名
        if scope:
            print(f"· 从链接文件名推断 scope={scope}（按号抓取的那份）")
    key = f"{date}-{scope}" if scope else date
    # 完备性闸门：信息流那条链（无 scope、非 --urls-file）才检查。
    # 按号抓取是另一套数据源，不受 grab_auto 的缺口影响。
    if _grab_gate_blocks(date, scope,
                         getattr(args, "ignore_grab_gate", False)):
        return EXIT_ERROR
    run_key = _run_key(date, urls, scope)
    day_arg = getattr(args, "days", None)
    feed_days = max(1, int(day_arg if day_arg is not None
                           else cfg.get("feed_days", 2)))
    window = _window_dates(date, feed_days)
    window_start = window[-1]
    runs = Runs()
    done = runs.done(run_key)
    if done:
        print(f"· 已在 {done.get('finished_at') or done.get('started_at')} "
              f"跑过，本次跳过（run_key={run_key}）")
        print("  没有产生第二份简报，也没有调用 ZenMark 接口")
        return EXIT_OK

    seen = Seen()
    offered = Offered()
    use_offered = not getattr(args, "include_offered", False)
    fresh, skipped_seen, skipped_offered = [], [], []
    for u in urls:
        if seen.has(u):
            skipped_seen.append(u)
        elif use_offered and offered.has(u, run_key):
            skipped_offered.append(u)
        else:
            fresh.append(u)
    if skipped_seen:
        print(f"· URL 级去重：跳过已总结过的 {len(skipped_seen)} 篇", file=sys.stderr)
    if skipped_offered:
        print(f"· 投递级去重：跳过上次进过清单但没勾选总结的 "
              f"{len(skipped_offered)} 篇", file=sys.stderr)
        print("  想看的话加 `scan --include-offered`，这批老文章会重新进清单",
              file=sys.stderr)
    if not fresh:
        if skipped_offered:
            print("· 这批全都投递过（没勾选总结的也算看过），没有新文章")
        else:
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
    for r in results:
        if r.get("error"):
            failed.append(r)
        else:
            articles.append(r)

    # 黑名单硬闸门：拿到 nickname 之后立刻剔除，绝不进清单 / 正文 / 总结。
    # 不写 seen / runs / published，只在候选池里留痕便于核对。
    names = blacklist.load()
    articles, blocked = blacklist.filter_articles(articles, names)
    if blocked:
        print(f"· 黑名单整号排除：跳过 {len(blocked)} 篇", file=sys.stderr)
        for a in blocked:
            print(f"    - {a.get('source','?')}  《{a.get('title','?')}》",
                  file=sys.stderr)
    # 无标题闸门：闲聊类文章一般没有独立标题，元信息里标题位塞的是正文
    # 首段、公众号名还可能是空的。这类内容信息价值低，还会把语音稿的
    # 条目标题撑坏，所以跟黑名单同档处理：不进清单、不写台账。
    articles, chatty = quality.filter_articles(articles)
    if chatty:
        print(f"· 无标题/闲聊排除：跳过 {len(chatty)} 篇", file=sys.stderr)
        for a in chatty:
            print(f"    - {(a.get('source') or '（无公众号名）')}  "
                  f"{a.get('reason')}", file=sys.stderr)
    # 「最近 N 天全部」模式：只留抓取窗口内推送的文章。
    # **必须排在 offered.mark 之前** —— 否则窗口外的文章被记成「投递过」，
    # 下次重扫会被 skipped_offered 挡掉，就再也补不回来了。
    # 字段名 skipped_notoday 沿袭旧版（正文 / 清单页 / 台账都可能读它），
    # 现在装的是「不在这 N 天窗口内」的文章。
    skipped_notoday: list[dict] = []
    if int(cfg.get("feed_scope", 1)) and not scope:
        articles, skipped_notoday = _filter_recent_days(articles, date,
                                                        feed_days)
        if skipped_notoday:
            print(f"· 不在抓取窗口（{window_start} ~ {date}，共 {feed_days} 天）"
                  f"的文章：跳过 {len(skipped_notoday)} 篇", file=sys.stderr)
            for a in skipped_notoday:
                print(f"    - {a.get('published_at','?')}  "
                      f"{a.get('source','?')}  《{a.get('title','?')}》",
                      file=sys.stderr)
    # 过滤后重新连续编号，清单页的 data-i / select --ids 依赖它
    for i, a in enumerate(articles, 1):
        a["index"] = i

    inbox = {
        "date": date, "generated_at": _now(), "phase": "scan",
        "run_key": run_key,
        # key = 落盘用的路径键（带 scope）；account = 号名，发布标题要用
        "scope": scope, "key": key, "account": scope,
        "max_articles": limit,
        # 抓取窗口（含基准日往前 feed_days 个自然日），可观测：
        "feed_days": feed_days,
        "window_start": window_start, "window_end": date,
        "articles": articles,
        "failed_meta": failed,
        "skipped_seen": skipped_seen,
        "skipped_offered": skipped_offered,
        "skipped_blacklist": blocked,
        "skipped_notitle": chatty,
        "skipped_notoday": skipped_notoday,
        "blacklist": names,
        "_note": "phase=scan：只有元信息，没有正文；hydrate 之后才取正文",
    }
    p = _inbox_path(date, scope)
    save_json(p, inbox)
    # 抓取明细（`/grab` 页的数据源）：必须排在 offered.mark **之前**，
    # 否则本轮刚进清单的文章会被写进台账、被误算成「跨轮重复」。
    # 纯读盘 + 写自己的明细文件，失败只警告，绝不影响 scan 主流程。
    try:
        rp, rv = grab_review.write(date, scope, out_dir=p.parent)
        s = rv["summary"]
        print(f"· 本轮文章明细：{rp.name}（页面只列进本轮清单的 "
              f"{s['in_inbox']} 篇，已处理过的不会出现）")
    except Exception as exc:  # noqa: BLE001 - 展示层不能拖垮抓取链路
        print(f"· 抓取明细没写成功（不影响本轮）：{exc}", file=sys.stderr)
    # 真正进了清单的才算「投递过」：黑名单 / 元信息失败的不记，将来还能重抓
    if articles:
        offered.mark(articles, run_key, date)
    if scope:
        print(f"✓ 候选池（scope={scope}）：{p}")
    else:
        print(f"✓ 候选池：{p}")
    print(f"  新文章 {len(articles)} 篇 / 元信息失败 {len(failed)} 篇"
          f" / 已总结过 {len(skipped_seen)} 篇"
          f" / 已投递未选 {len(skipped_offered)} 篇"
          f" / 黑名单排除 {len(blocked)} 篇"
          + (f" / 窗口外 {len(skipped_notoday)} 篇" if skipped_notoday else ""))
    if not articles:
        if skipped_notoday and not failed and not blocked and not chatty:
            print(f"· 这批链接没有一篇落在抓取窗口（{window_start} ~ {date}）"
                  "内，已全部剔出。要出更早那天的简报就加 "
                  "--date <那天> --days 1；想放宽窗口就加 --days 3。")
        else:
            print("· 本轮没有可总结的文章（可能被黑名单排完或元信息全失败），出不了清单")
    for a in articles:
        print(f"  {a['index']:>2}. {a.get('published_at','时间未知')}  "
              f"{a.get('source','?')}  《{a['title']}》")
    print(f"· 本轮抓取上限 {limit} 篇（实际取到 {len(articles)} 篇）"
          + ("；已按最近 " + str(feed_days) + " 天（"
             + window_start + " ~ " + date + "）过滤" if skipped_notoday else ""))

    # 自动模式：由 scan 代写「选文清单 + 放行信号」，hydrate 的校验逻辑一字不改。
    # 不加旁路分支，是为了让「谁授权的」永远只有一个答案——看信号文件。
    auto = _auto_mode(cfg, args)
    if articles and auto:
        plan = selection.write_plan(inbox, None, "auto")
        req = selection.write_request(inbox, len(plan["selected"]),
                                      mode="auto", source="scan")
        print(f"⚙ 自动模式：已自动放行 {len(plan['selected'])} 篇"
              f"（{req['requested_at']}，未经人工确认）", flush=True)
        print("  只跳过「人选哪几篇」：黑名单 / 无标题过滤 / 相似度去重 / "
              "投递台账一律照旧")
        if getattr(args, "no_hydrate", False):
            print("· 本轮 --no-hydrate：停在候选池；下一步 "
                  "python3 -m src.main hydrate（信号已写好，不用去清单页）")
            return EXIT_OK
        # 抓完直接总结：同一条命令里接着取正文。这里**不是**绕过闸门，
        # hydrate 仍然只读刚写下的那个信号文件，校验一行没放宽。
        print("· 抓完直接总结：接着跑 hydrate 取正文（闸门仍只认上面那个信号）",
              flush=True)
        return cmd_hydrate(cfg, args)

    if not articles:
        return EXIT_OK  # 上面已经说清「本轮没有可总结的文章」，不再提下一步
    if not getattr(args, "no_open", False):
        _open_listing(getattr(args, "port", 7777) or 7777, landing="review",
                      scope=scope)
    print("· 下一步：在清单页勾选后点「开始总结」才放行；没有提交不会执行任何总结")
    return EXIT_OK


def cmd_account(cfg, args) -> int:
    """按号抓取：切列表模式 → 左栏按号名找 → 点开 → 滚到底 → 逐次推送取头条。

    只负责「拿到链接」，不取元信息、不建候选池、不写任何台账。
    拿到之后照常跑 `scan --urls-file out/_raw_urls-<号名>.json`（它会从文件名
    推断 scope），后面 hydrate / 总结 / 发布全部与信息流那条链一样。
    """
    name = (args.name or "").strip()
    if not name:
        raise RuntimeError("请用 --name 指定公众号名（要和左栏显示的一致）")
    if args.probe:
        data = account.probe(cfg)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        print(f"\n· 现场截图：{data['image']}")
        return EXIT_OK
    limit = args.limit
    if limit is None:
        # 按号抓取走自己的配置：信息流那条链改成「最近 N 天全部」不影响这里
        # 「最近 N 次推送的头条」的原语义。
        limit = int(cfg.get("account_max_pushes", 20))
    print(f"=== 按号抓取：{name}（最多 {limit} 次推送的头条）===")
    print("  步骤：切列表模式 → 左栏找号 → 点开 → 滚到底 → 从下往上逐条取链接")
    result = account.grab_account(
        cfg, name, limit,
        dry_run=args.dry_run,
        # 从卡片流进来的话，抓完顺手切回去，省得下次跑信息流要手工切
        restore_card=not args.keep_list)
    print(json.dumps({k: v for k, v in result.items() if k != "urls"},
                     ensure_ascii=False, indent=2))
    if result.get("dry_run"):
        return EXIT_OK
    if result["count"] == 0:
        print("× 一条链接都没取到（滚到底了也没识别出推送），请跑 "
              "`account --name <号名> --probe` 看看这一屏，或手工确认版面")
        return EXIT_HUMAN
    for i, u in enumerate(result["urls"], 1):
        print(f"  {i:>2}. {u}")
    print(f"\n✓ 链接已写入：{result['file']}")
    print(f"· 下一步：python3 -m src.main scan --urls-file {result['file']}")
    print(f"  它会自动认出 scope={account.slug(name)}，"
          f"生成 inbox-<日期>-{account.slug(name)}.json")
    return EXIT_OK


def _probe_ui(port: int) -> tuple[str, dict | None]:
    """探测端口：current=新版常驻服务 / legacy=旧版设置页 / other / free。

    旧版服务只提供 /settings 与 /read，没有 /review；绝不能复用它，
    否则端口虽然一致，选文清单却打不开，问题反而更隐蔽。
    """
    import urllib.error
    import urllib.request
    # 环境里常有 HTTP_PROXY，直连 127.0.0.1 必须绕过代理，
    # 否则空闲端口会被代理回一个 HTTP 错误，被误判成"被其它程序占用"。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(
                f"http://127.0.0.1:{port}/api/status", timeout=0.6) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError:
        return "other", None
    except Exception:  # noqa: BLE001 - 连接不上就当端口空闲
        return "free", None
    if not isinstance(data, dict) or not data.get("ok"):
        return "other", None
    return ("current" if data.get("app") == "wechat_digest" else "legacy"), data


def _open_listing(port: int, *, landing: str = "review",
                  scope: str = "") -> None:
    """把清单页推到浏览器：服务在跑就开新标签，没跑就先在后台起一个。

    scan 结束后自动调用，用户不用再去终端里找地址。
    起不来不是致命错误（候选池已经落盘），只提示、不抛异常。
    """
    base = f"http://127.0.0.1:{port}"
    # 清单页地址总要带 ?scope=（空也要带）：常驻服务据此决定读哪份候选池，
    # 少这个参数就会打开服务启动时那个 scope 的清单。
    url = (review.review_url(base, scope) if landing == "review"
           else f"{base}/{landing}")
    state, _ = _probe_ui(port)
    if state == "current":
        print(f"→ 已在浏览器打开：{url}", flush=True)
        webbrowser.open(url)
        return
    if state in ("legacy", "other"):
        print(f"· 端口 {port} 上是旧版服务或其它程序，没有自动打开页面；"
              f"请在它的终端按 Ctrl+C，然后跑 `python3 -m src.main review`",
              file=sys.stderr)
        return

    root = Path(__file__).resolve().parent.parent
    try:
        # 起服务统一用 `review`（它就是那个常驻服务，三页同址），
        # 落地页由下面的 webbrowser 决定，别让它再开一个标签
        subprocess.Popen(
            [sys.executable, "-m", "src.main", "review",
             "--port", str(port), "--no-open"] + (["--scope", scope] if scope else []),
            cwd=str(root), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except OSError as exc:
        print(f"· 后台起本地服务失败（{exc}），请手动跑 "
              f"`python3 -m src.main {landing}`", file=sys.stderr)
        return
    # 等端口就绪再开页面，否则浏览器会抢在 bind 之前打一个空页面
    for _ in range(40):
        time.sleep(0.15)
        if _probe_ui(port)[0] == "current":
            print(f"→ 已自动起本地服务并打开：{url}", flush=True)
            webbrowser.open(url)
            return
    print(f"· 本地服务启动较慢，稍等后手动打开：{url}", file=sys.stderr)


def _service_alive(port: int) -> bool:
    """端口上是否已有**新版**常驻服务（供回归脚本与调用方使用）。"""
    return _probe_ui(port)[0] == "current"


def _ensure_ui_service(args, landing: str) -> int:
    """确保默认端口上只有一个新版常驻服务，并给出三页的统一地址。"""
    base = f"http://127.0.0.1:{args.port}"
    scope = getattr(args, "scope", "") or ""
    # 清单页要带 scope（空也要带），否则常驻服务会打开服务启动时那个 scope 的池子
    page = (review.review_url(base, scope) if landing == "review"
            else f"{base}/{landing}")
    state, _ = _probe_ui(args.port)
    if state == "current":
        print(f"· {args.port} 上已有本地服务，直接复用，不另起第二个进程")
        print(f"  选文清单：{base}/review | 本地阅读：{base}/read | "
              f"参数设置：{base}/settings")
        if landing == "review":
            if scope:
                print(f"  本次看的是 scope={scope} 那份（按号抓取的精选简报）")
            print("  勾选后点「开始总结」才算授权；提交后服务不退出")
        if not args.no_open:
            # 复用时也要把页面推到浏览器，否则命令看着像什么都没发生
            webbrowser.open(page)
            print(f"→ 已在浏览器打开：{page}", flush=True)
        return EXIT_OK
    if state == "legacy":
        raise RuntimeError(
            f"端口 {args.port} 上是旧版本地服务（只有设置页 / 阅读页，没有选文清单）。"
            "\n  请在它的终端按 Ctrl+C 结束，然后重跑本命令；"
            "\n  不要换端口，否则选文和阅读又会分到两个地址。")
    if state == "other":
        raise RuntimeError(
            f"端口 {args.port} 被其它程序占用，请先释放它；三页必须同址，不要换端口。")
    review.serve(None, args.port, not args.no_open,
                 dynamic=True, landing=landing, scope=scope)
    return EXIT_OK


def cmd_review(cfg, args) -> int:
    """打开选文清单（常驻服务）；提交后服务不退出，阅读页继续同址可用。

    agent 用 status 的 inbox.submitted 确认页面提交，不再依赖服务退出。
    """
    return _ensure_ui_service(args, "review")


def cmd_settings(cfg, args) -> int:
    """打开参数设置页（同一个常驻服务），Ctrl+C 结束。"""
    return _ensure_ui_service(args, "settings")


def cmd_blacklist(cfg, args) -> int:
    """查看 / 增删抓取黑名单；名单是配置文件，不写任何台账。"""
    action, names = args.action, args.names or []
    if action == "list":
        cur = blacklist.load()
        print(f"· 抓取黑名单（{blacklist.PATH.name}）：{len(cur)} 个公众号")
        for i, n in enumerate(cur, 1):
            print(f"  {i:>2}. {n}")
        if not cur:
            print("  （空名单，所有公众号都会正常进入候选池）")
        print("· 规则：规范化后精确相等（忽略空格 / 全半角 / 英文大小写）；"
              "命中不进清单、不取正文、不总结、不推送")
        return EXIT_OK
    if not names:
        raise RuntimeError(f"blacklist {action} 后面至少要跟一个公众号名")
    cur, changed = (blacklist.add(names) if action == "add"
                    else blacklist.remove(names))
    verb = ("已加入" if action == "add" else "已移除") if changed else "没有变化"
    print(f"{verb}：{blacklist.PATH.name} 现在 {len(cur)} 个公众号")
    for i, n in enumerate(cur, 1):
        print(f"  {i:>2}. {n}")
    print("· 下次 scan 生效；已在候选池里的旧条目不会追溯清理")
    return EXIT_OK


def cmd_offered(cfg, args) -> int:
    """查看 / 回填 / 清空投递台账（进过清单就算看过，下次 scan 过滤掉）。"""
    st = Offered()
    if args.action == "clear":
        n = len(st.data)
        save_json(st.path, {})
        print(f"· 已清空投递台账（{n} 条）：下次 scan 旧文章会重新进清单")
        return EXIT_OK
    if args.action == "backfill":
        added, used = st.backfill(project_path("out"))
        print(f"✓ 投递台账回填：新增 {added} 条，共 {len(st.data)} 条")
        print(f"  读过：{'、'.join(used) if used else '（out/ 下没有历史文件）'}")
        print("· 只补 URL 不补正文；补进来的老文章下次 scan 会被跳过")
        return EXIT_OK
    data = st.stats()
    print(f"· 投递台账（{st.path.name}）：{data['total']} 条已投递文章")
    if not st.data:
        print("  （空台账，所有抓到的文章都会进清单）")
        return EXIT_OK
    for i, (url, rec) in enumerate(
            sorted(st.data.items(), key=lambda kv: kv[1].get("last_at", ""),
                   reverse=True), 1):
        print(f"  {i:>3}. {rec.get('last_at','?')}  "
              f"{rec.get('source') or '?'}  《{rec.get('title') or url}》")
    print("· 规则：进过清单就算看过，下次 scan 不再投递；"
          "要重新看加 `scan --include-offered`")
    return EXIT_OK


def cmd_select(cfg, args) -> int:
    inbox = _latest_inbox(getattr(args, "scope", "") or "")
    if args.all:
        indices, mode = None, "all"
    elif args.ids:
        indices = [int(x) for x in args.ids.split(",") if x.strip()]
        mode = "manual"
    else:
        raise RuntimeError("请给 --all 或 --ids 1,3,5")
    plan = selection.write_plan(inbox, indices, mode)
    print(f"✓ 选文清单：{selection.plan_path(selection.inbox_key(inbox))}")
    print(f"  选中 {len(plan['selected'])} 篇 / 共 {len(inbox['articles'])} 篇"
          f"（模式 {plan['mode']}）")
    return EXIT_OK


def cmd_hydrate(cfg, args) -> int:
    """按选文清单取正文 → 相似度去重 → 生成待总结包。"""
    inbox = _latest_inbox(getattr(args, "scope", "") or "")
    scope = _scope_of(inbox, args)
    if inbox.get("phase") != "scan":
        raise RuntimeError("候选池 phase 不是 scan，可能已经处理过了")
    run_key = inbox["run_key"]
    runs = Runs()
    done = runs.done(run_key)
    if done:
        print(f"· 已在 {done.get('finished_at') or done.get('started_at')} "
              f"跑过，本次跳过（run_key={run_key}）")
        return EXIT_OK

    chosen = selection.read_plan(inbox)
    if chosen is None:
        raise RuntimeError(
            "没有有效的选文清单：请先在清单页勾选并点「开始总结」"
            "（python3 -m src.main review）。不自动整份总结")
    if not chosen:
        raise RuntimeError("选文清单里一篇都没勾中，不执行总结")
    req = selection.read_request(inbox)
    if req is None:
        raise RuntimeError(
            "还没在清单页点「开始总结」：缺少提交信号，"
            "不执行取正文、不生成总结、不发布")
    if req.get("mode") == "auto":
        # 自动模式的放行信号也是 scan 走同一个 write_request 写的，
        # 这里只把「没人看过这份清单」这件事说清楚，校验一行不放宽。
        print(f"⚙ 自动模式放行：{req['requested_at']} 由 "
              f"{req.get('source', 'scan')} 代写信号，"
              f"本次 {len(chosen)} 篇未经人工确认", flush=True)
    else:
        print(f"· 已收到人工提交：{req['requested_at']}，"
              f"选中 {len(chosen)} 篇，放行后续流程", flush=True)
    lock = ClaimLock(cfg["lock_ttl_minutes"])
    if not lock.acquire(run_key):
        owner = lock.owner()
        raise RuntimeError(
            f"这批已被另一个进程认领（pid={owner.get('pid')}, "
            f"at={owner.get('at')}）。等它跑完或锁过期（{cfg['lock_ttl_minutes']} 分钟）再试")

    all_articles = inbox["articles"]
    source_articles = [a for a in all_articles if a["index"] in chosen]
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
                                   cfg["batch_size"], cfg["max_parallel"],
                                   scope=scope)
    runs.begin(run_key, {"articles": len(kept),
                         "batches": len(pending["batches"])})
    inbox["phase"] = "hydrated"
    save_json(_inbox_path(inbox["date"], scope), inbox)

    print(f"✓ 待总结包：{project_path('out', pending_file(scope))}")
    print(f"  {len(kept)} 篇拆成 {len(pending['batches'])} 批"
          f"（每批 ≤{cfg['batch_size']} 篇，子代理并发 ≤{cfg['max_parallel']}）")
    for b in pending["batches"]:
        print(f"  批 {b['id']}: {b['count']} 篇 index={b['indices']}")
        print(f"    输入 {b['file']}")
        print(f"    输出 {b['output']}")
    return EXIT_OK


def cmd_batches(cfg, args) -> int:
    pending = load_json(project_path("out", pending_file(args.scope)), {})
    if not pending:
        raise RuntimeError("还没有待总结包，请先跑 hydrate")
    print(json.dumps({
        "run_key": pending["run_key"],
        "prompt_file": pending["prompt_file"],
        "max_parallel": pending["max_parallel"],
        "batches": pending["batches"],
    }, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_voice_input(cfg, args) -> int:
    """把待总结包按篇数切成若干期，每期生成一个语音稿输入包。

    为什么必须拆：语音稿一期正文有字数上限（默认 1800 字），一条 200-350 字，
    15 篇以上就装不下 30 篇的量。硬塞的结果是每条被压成一句话，听着像报菜名。
    所以按篇数分批发给子代理，一期一份 `voice-<日期>-partN.md`，
    每期各自 `publish --no-ledger`，标题带「（N/M）」。
    """
    pending = load_json(project_path("out", pending_file(args.scope)), {})
    if not pending:
        raise RuntimeError("还没有待总结包，请先跑 hydrate")
    articles = pending.get("articles") or []
    if not articles:
        raise RuntimeError("待总结包里没有文章")
    per = int(args.per_issue or cfg.get("voice_per_issue", 10))
    per = max(1, per)
    max_chars = int(args.max_chars or cfg.get("voice_max_chars", 1800))
    date = pending.get("date") or _today()

    scope = str(getattr(args, "scope", "") or "")
    prompt_text = project_path("prompts", "summary_voice.md").read_text(
        encoding="utf-8")
    wordlist_text = project_path("prompts", "voice_wordlist.md").read_text(
        encoding="utf-8")
    out_dir = project_path("out", "_voice")
    out_dir.mkdir(parents=True, exist_ok=True)

    chunks = [articles[i:i + per] for i in range(0, len(articles), per)]
    if args.groups:
        # 手工分期：同一件事的多篇（比如一波美股暴跌里七八篇）要放进同一期，
        # 子代理才可能把它们合并成一条讲，而不是每期都重复讲一遍行情。
        by_index = {int(a["index"]): a for a in articles}
        chunks = []
        used: set[int] = set()
        for grp in str(args.groups).split(";"):
            grp = grp.strip()
            if not grp:
                continue
            idxs = [int(x) for x in grp.replace("，", ",").split(",") if x.strip()]
            missing = [i for i in idxs if i not in by_index]
            if missing:
                raise RuntimeError(f"手工分期里有不存在的 index：{missing}")
            dup = [i for i in idxs if i in used]
            if dup:
                raise RuntimeError(f"手工分期里 index 重复出现：{dup}")
            used.update(idxs)
            chunks.append([by_index[i] for i in idxs])
        left = [a["index"] for a in articles if int(a["index"]) not in used]
        if left:
            raise RuntimeError(f"手工分期漏掉了 {len(left)} 篇：{left}"
                               "（每篇都必须落在某一期，不许悄悄丢）")
    total = len(chunks)
    written = []
    for idx, chunk in enumerate(chunks, 1):
        part = (f"voice-{date}-{scope}-part{idx}.md" if scope
                else f"voice-{date}-part{idx}.md")
        pack = {
            "prompt": prompt_text,
            "wordlist": wordlist_text,
            "articles": chunk,
            "date": date,
            "issue": idx,
            "issues": total,
            "max_chars": max_chars,
            "out_path": str(project_path("out", part)),
            "note": (f"这是第 {idx}/{total} 期，只写本期这 {len(chunk)} 篇，"
                     "不要提到别的期、不要说「未完待续」。"),
        }
        path = out_dir / f"voice-input-{idx:02d}.json"
        path.write_text(json.dumps(pack, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        written.append({"issue": idx, "articles": len(chunk),
                        "input": str(path), "out": pack["out_path"]})
    print(json.dumps({
        "date": date, "run_key": pending.get("run_key"),
        "articles": len(articles), "per_issue": per,
        "max_chars": max_chars, "issues": written,
    }, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_merge(cfg, args) -> int:
    pending = load_json(project_path("out", pending_file(args.scope)), {})
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
    return publish.publish(cfg, text, title=args.title,
                           dry_run=args.dry_run, no_ledger=args.no_ledger,
                           scope=args.scope)


def cmd_status(cfg, args) -> int:
    inbox = None
    try:
        inbox = _latest_inbox(args.scope)
    except RuntimeError:
        pass
    pending = load_json(project_path("out", pending_file(args.scope)), {})
    batches = []
    for b in pending.get("batches", []):
        batches.append({"id": b["id"], "count": b["count"],
                        "written": Path(b["output"]).exists()})
    req = selection.read_request(inbox) if inbox else None
    data = {
        "seen": len(Seen().data),
        "offered": len(Offered().data),
        "runs": Runs().data,
        "published": Published().data,
        "lock": ClaimLock().owner(),
        # 当前生效的确认方式与抓取窗口：agent 用它决定「等人点提交」还是
        # 「直接往下跑」，以及这批覆盖哪几天
        "auto_summary": int(cfg.get("auto_summary") or 0),
        "feed_days": max(1, int(cfg.get("feed_days", 2))),
        "inbox": None if not inbox else {
            "date": inbox["date"], "run_key": inbox["run_key"],
            "feed_days": inbox.get("feed_days"),
            "window_start": inbox.get("window_start"),
            "window_end": inbox.get("window_end"),
            "scope": inbox.get("scope") or "",
            "phase": inbox["phase"],
            "submitted": req is not None,
            "request_mode": (req or {}).get("mode"),
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

    p = sub.add_parser("nav",
                       help="自动点「公众号」入口进信息流，输出本屏卡片坐标")
    p.add_argument("--out", default=None, help="把结果另存为 JSON")

    p = sub.add_parser("grab", help="按卡片坐标批量复制文章链接")
    p.add_argument("--coords", nargs="?", default=None,
                   help="卡片中心坐标，形如 '560,275;560,400'；用 --auto 时不需要")
    p.add_argument("--auto", action="store_true",
                   help="全自动：自己点「公众号」入口进信息流、逐张点开、滚动翻页")
    p.add_argument("--limit", type=int, default=None,
                   help="本轮最多复制几条链接，默认取 settings.json 的 "
                        "max_articles（窗口全部模式下只是兜底上限）")
    p.add_argument("--append", action="store_true",
                   help="分多屏抓时，把本轮链接合并进已有的 _raw_urls.json")
    p.add_argument("--dry-run", action="store_true", help="只校验环境与坐标，不点击")
    p.add_argument("--resume", action="store_true",
                   help="带上轮缺口清单从头重抓补齐（隐含 --append）；"
                        "重新对账 K=0 才解除完备性闸门")
    p.add_argument("--no-gate", dest="no_gate", action="store_true",
                   help="本轮不写完备性阻断标记（调试用，缺口照旧记进台账）")

    p = sub.add_parser(
        "account",
        help="按公众号名抓取：切列表模式 → 左栏找号 → 点开 → 从下往上取头条链接")
    p.add_argument("--name", required=False, default=None,
                   help="公众号名，要和列表模式左栏显示的一致（如 EarlETF）")
    p.add_argument("--limit", type=int, default=None,
                   help="最多取几次推送的头条，默认取 settings.json 的 "
                        "account_max_pushes")
    p.add_argument("--probe", action="store_true",
                   help="只看不点：打印当前形态、左栏号名、右栏头条，排查定位用")
    p.add_argument("--keep-list", dest="keep_list", action="store_true",
                   help="抓完停在列表模式（默认：从卡片流进来就切回卡片流）")
    p.add_argument("--dry-run", action="store_true",
                   help="只校验环境与配置，不切换界面、不点击")

    p = sub.add_parser("scroll", help="滚动信息流")
    p.add_argument("--x", type=float, default=700)
    p.add_argument("--y", type=float, default=450)
    p.add_argument("--amount", type=int, default=-4)

    p = sub.add_parser("scan", help="按链接补元信息，生成候选池（不取正文）")
    p.add_argument("--urls-file", default=None)
    p.add_argument("--date", default=None)
    p.add_argument("--max-articles", type=int, default=None,
                   help="本轮抓取篇数上限，默认取 settings.json 的 max_articles"
                        "（窗口全部模式下只是兜底上限）")
    p.add_argument("--days", type=int, default=None,
                   help="本轮抓取窗口：含 --date（默认今天）往前共 N 个自然日，"
                        "默认取 settings.json 的 feed_days（2 = 今天 + 昨天）；"
                        "给 1 就退回「只抓当天」。只改 scan 层过滤，"
                        "GUI 滚动停止读配置里的 feed_days")
    p.add_argument("--include-offered", action="store_true",
                   help="连上次进过清单但没勾选的文章也一起进清单（默认过滤掉）")
    p.add_argument("--port", type=int, default=7777,
                   help="候选池出好后自动打开清单页用哪个端口，默认 7777")
    p.add_argument("--no-open", action="store_true",
                   help="出完候选池不自动打开浏览器清单页")
    p.add_argument("--auto-summary", dest="auto_summary",
                   action="store_const", const=1, default=None,
                   help="本轮跳过人工确认（默认就是自动）：出完候选池自动全选、"
                        "代写放行信号并接着 hydrate（只覆盖本次，不改配置）")
    p.add_argument("--confirm", action="store_true",
                   help="本轮强制人工确认，压掉默认/配置里的 auto_summary=1")
    p.add_argument("--no-hydrate", dest="no_hydrate", action="store_true",
                   help="自动模式下只出候选池和放行信号，不接着跑 hydrate"
                        "（分步排查用；闸门不变）")
    p.add_argument("--ignore-grab-gate", dest="ignore_grab_gate",
                   action="store_true",
                   help="单轮绕过抓取完备性闸门（有缺口也放行，会打印告警留痕）")

    p = sub.add_parser("review", help="生成 HTML 清单页，等人工勾选并提交")
    p.add_argument("--port", type=int, default=7777)
    p.add_argument("--no-open", action="store_true", help="不自动打开浏览器")

    p = sub.add_parser("settings", help="起参数设置页（不依赖候选池，Ctrl+C 结束）")
    p.add_argument("--port", type=int, default=7777)
    p.add_argument("--no-open", action="store_true", help="不自动打开浏览器")

    p = sub.add_parser("blacklist",
                       help="查看 / 增删抓取黑名单（按公众号名整号排除）")
    p.add_argument("action", nargs="?", default="list",
                   choices=("list", "add", "remove"),
                   help="list=查看（默认），add=加入，remove=移除")
    p.add_argument("names", nargs="*",
                   help="公众号名；add / remove 时必填，可一次跟多个")

    p = sub.add_parser("offered",
                       help="查看 / 回填 / 清空投递台账（进过清单就算看过）")
    p.add_argument("action", nargs="?", default="list",
                   choices=("list", "backfill", "clear"),
                   help="list=查看（默认），backfill=从历史文件补录，clear=清空")
    p.add_argument("--port", type=int, default=7777)  # 与其它子命令统一，未使用

    p = sub.add_parser("select", help="人工选文（低级命令，正常走 review）")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true",
                   help="整份都选（只写清单，不放行总结；正常应走 review）")
    g.add_argument("--ids", help="逗号分隔的编号，如 1,3,5")

    sub.add_parser("hydrate", help="取正文 + 相似度去重 + 生成待总结包")
    sub.add_parser("batches", help="打印分批方案与输入输出路径")
    p = sub.add_parser("voice-input",
                       help="把待总结包按篇数切成若干期，生成语音稿输入包")
    p.add_argument("--per-issue", type=int, default=None,
                   help="每期最多几篇（默认取配置 voice_per_issue）")
    p.add_argument("--max-chars", type=int, default=None,
                   help="每期正文上限（默认取配置 voice_max_chars）")
    p.add_argument("--groups", default=None,
                   help="手工分期：用分号隔开每期，逗号隔开该期的 index，"
                        "如 \"21,25,26;12,13,14\"。给了就不按篇数硬切，"
                        "用来把同一件事的文章放进同一期")

    p = sub.add_parser("merge", help="合并各批总结，输出到 stdout（可管道给 publish）")
    p.add_argument("--out", default=None, help="可选：同时写一份文件")

    p = sub.add_parser("publish", help="推到 ZenMark（默认从 stdin 读简报）")
    p.add_argument("--file", default=None, help="从文件读简报")
    p.add_argument("--title", default=None)
    p.add_argument("--no-ledger", action="store_true",
                   help="旁路发布：不查去重、不写台账（语音播报版用）")
    p.add_argument("--dry-run", action="store_true", help="不发送请求、不改台账")
    p.add_argument("--check", action="store_true", help="只读验令牌")

    p = sub.add_parser("grab-review",
                       help="生成/刷新抓取明细（全部链接的去向与重复情况，供 /grab 页查看）")
    p.add_argument("--date", default=None, help="默认今天")
    p.add_argument("--scope", default="", help="按号抓取的命名空间")

    sub.add_parser("status", help="查看台账与批次进度")

    # scope 命名空间：按号抓取出来的那份候选池与信息流那份各走各的路
    # （inbox-<日期>-<号名>.json、pending_summary-<号名>.json、_batches-<号名>/），
    # 靠这个后缀区分。台账（seen/offered/runs/published）**不**区分 scope，
    # 同一个链接在信息流批次里总结过，按号批次里照样跳过。
    for name in ("scan", "review", "select", "hydrate", "batches",
                 "voice-input", "merge", "publish", "status"):
        sub.choices[name].add_argument(
            "--scope", default="",
            help="按号抓取的命名空间，如 EarlETF。scan --urls-file "
                 "out/_raw_urls-<号名>.json 时会自动从文件名推断，一般不用手填")
    return ap


def cmd_grab_review(cfg, args) -> int:
    """刷新 `/grab` 页的数据源。只读台账，不取正文、不放行总结。"""
    date = args.date or _today()
    scope = str(getattr(args, "scope", "") or "")
    p, data = grab_review.write(date, scope)
    s = data["summary"]
    print(f"✓ 抓取明细：{p}")
    print(f"  抓到 {s['total']} 条 / 唯一 URL {s['unique_urls']} 条"
          f" / URL 重复组 {s['url_dup_groups']} / 标题重复组 {s['title_dup_groups']}"
          f" / 跨轮重复 {s['cross_round']} 条 / 进本轮清单 {s['in_inbox']} 条")
    for k, v in sorted((s["counts"] or {}).items()):
        print(f"    - {grab_review.STATUS_LABELS.get(k, k)}: {v} 条")
    print(f"· 打开 http://127.0.0.1:7777/grab?scope={scope or ''} 查看")
    return EXIT_OK


HANDLERS = {
    "doctor": cmd_doctor, "capture": cmd_capture, "nav": cmd_nav,
    "grab": cmd_grab, "account": cmd_account,
    "scroll": cmd_scroll, "scan": cmd_scan, "review": cmd_review,
    "settings": cmd_settings, "blacklist": cmd_blacklist,
    "offered": cmd_offered,
    "select": cmd_select,
    "hydrate": cmd_hydrate, "batches": cmd_batches, "merge": cmd_merge,
    "voice-input": cmd_voice_input,
    "publish": cmd_publish, "status": cmd_status,
    "grab-review": cmd_grab_review,
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
