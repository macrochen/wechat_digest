#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""参数设置页的数据层：规格、校验、增量写回。

按本项目约定做了三处调整：
  1. 规格是唯一真源（SETTINGS_SPECS），前端按规格动态生成表单；
  2. 只提交改动过的键，写回 config/settings.local.json（Git 已忽略），
     主 config/settings.json 保持干净；写前 .bak、原子替换；
  3. 令牌绝不进页面：只回传「已配置 / 未配置」和来源描述，不回传值。

load_settings() 每次运行都重读文件，所以保存即生效、无需重启服务。
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from .config import DEFAULTS, ROOT, load_settings
from .publish import read_token

# key, 中文标签, 类型, 下限, 上限, 分组, 说明
# 类型：int / float / str / coords（长度 2 的整数数组）
_SETTINGS_SPECS = [
    ("max_articles", "抓取篇数上限", "int", 1, 200, "抓取",
     "每次从微信信息流取多少篇。是上限不是保证，实际篇数还受信息流和去重影响。"
     "命令行 scan --max-articles N 会覆盖本次运行。"),
    ("min_content_chars", "最短正文字数", "int", 0, 5000, "抓取",
     "正文短于这个字数，交给提示词标「内容无法总结」。"),

    ("dedup_threshold", "转载去重阈值", "float", 0.0, 1.0, "去重",
     "字符 5-gram Jaccard 相似度达到这个值就判为转载。真转载约 0.93，"
     "同事件各写各的约 0.37，0.75 比较安全。"),

    ("batch_size", "每批篇数上限", "int", 1, 50, "总结",
     "批内篇数达到这个数就拆批；同事件簇不会跨批。"),
    ("max_parallel", "子代理并发上限", "int", 1, 8, "总结",
     "同时派几个子代理写分批总结。"),
    ("auto_summary", "抓取后自动总结（跳过人工确认）", "int", 0, 1, "总结",
     "0=保持人工确认（默认）：候选池出好后在清单页勾选并点「开始总结」才放行；"
     "1=自动模式：scan 出完候选池就自动全选并代写放行信号，直接进 hydrate，"
     "清单页不再自动弹出。只跳过「人选哪几篇」——黑名单、无标题过滤、"
     "相似度去重、投递台账、发布哈希、认领锁全部照旧。"
     "单轮可用 scan --auto-summary / scan --confirm 覆盖，不改这里的值。"),

    ("publish_api", "上传接口地址", "str", None, None, "发布",
     "ZenMark 云端上传接口，需带完整路径。"),
    ("publish_title_prefix", "简报标题前缀", "str", None, None, "发布",
     "推送时标题形如「前缀 · 日期」。"),
    ("lock_ttl_minutes", "运行锁超时（分钟）", "int", 1, 240, "发布",
     "上一次异常中断留下的锁，超过这个分钟数自动失效。"),

    ("ui.logical_width", "逻辑宽度", "int", 800, 4000, "界面",
     "截图与点击坐标使用的逻辑分辨率宽度。"),
    ("ui.logical_height", "逻辑高度", "int", 600, 3000, "界面",
     "截图与点击坐标使用的逻辑分辨率高度。"),
    ("ui.geometry_check", "屏幕几何自检", "int", 0, 1, "界面",
     "1=开启（推荐）/ 0=关闭。每次 GUI 抓取前校验：显示器逻辑尺寸是否仍等于"
     "上面的宽高、全屏截图宽高比是否一致。外接显示器、iPad 随航、远程桌面"
     "接入都会让它们失效——失效时像素判据不报错，只会静默点错卡片，"
     "所以默认在点击前就按退出码 3 停下。"),
    ("ui.geometry_tolerance", "宽高比容差", "float", 0.0, 0.2, "界面",
     "全屏截图宽高比相对期望值的允许偏差，0.01 = 1%。1% 在 1280 宽上约等于 "
     "13px 的高度形变，已足以让 11~12px 的标题带判据失准。"),
    ("ui.more_button", "「…」按钮坐标", "coords", 0, 4000, "界面",
     "文章窗口右上角更多按钮，格式 [x, y]。"),
    ("ui.copy_link", "「复制链接」坐标", "coords", 0, 4000, "界面",
     "更多菜单里复制链接那一项，格式 [x, y]。只作兜底：程序优先按菜单"
     "结构定位（第 2 组第 2 项），认不出结构时才用这个坐标——文章被删除的"
     "页面没有这一项，会被判为跳过，不会盲点。"),
    ("ui.close_window", "关闭窗口备用坐标", "coords", 0, 4000, "界面",
     "当前 grab 用 Cmd+W 关多标签窗口，这个坐标只作备用。格式 [x, y]。"),
    ("ui.open_wait", "打开等待（秒）", "float", 0.0, 20.0, "界面",
     "点开文章后等页面加载的时间。"),
    ("ui.menu_wait", "菜单等待（秒）", "float", 0.0, 20.0, "界面",
     "点开「…」后等菜单出现的时间。"),
    ("ui.close_wait", "关闭等待（秒）", "float", 0.0, 20.0, "界面",
     "Cmd+W 之后等窗口关闭的时间；太小会拿到上一篇的链接。"),
    ("ui.click_wait", "点击间隔（秒）", "float", 0.0, 20.0, "界面",
     "连续两次点击之间的缓冲。"),
    ("ui.title_probe", "点击前标题行校准", "int", 0, 1, "界面",
     "1=开启（推荐）/ 0=关闭。每次点卡片前重新截图，把 y 吸附到深色加粗"
     "标题行，避免点到上方浅灰的公众号名而打开公众号主页。只改 y，"
     "不改坐标配置，也不做失败后重试。"),
    ("ui.title_search_down", "标题向下搜索范围（px）", "int", 0, 80, "界面",
     "标题行校准时在给定 y 下方多远找深色标题带；实测来源名比标题高约 "
     "30px，45 比较稳妥。设 0 相当于只认原坐标附近。"),
]

GROUPS = ["抓取", "去重", "总结", "发布", "界面"]


def specs() -> list[dict]:
    """返回规格给前端；不含任何令牌值。"""
    return [{
        "key": key, "label": label, "kind": kind,
        "lo": lo, "hi": hi, "group": group, "hint": hint,
    } for key, label, kind, lo, hi, group, hint in _SETTINGS_SPECS]


def local_path() -> Path:
    return ROOT / "config" / "settings.local.json"


def bak_path() -> Path:
    return ROOT / "config" / "settings.local.json.bak"


def read_local() -> dict:
    p = local_path()
    if not p.exists():
        return {}
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"本地覆盖配置读不了：{p.name}（{exc}）") from exc
    if not isinstance(doc, dict):
        raise RuntimeError(f"本地覆盖配置格式不对：{p.name} 应是 JSON 对象")
    return doc


def _get(obj: dict, dotted: str):
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _put(obj: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    cur = obj
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _drop(obj: dict, dotted: str) -> None:
    parts = dotted.split(".")
    cur = obj
    for part in parts[:-1]:
        if not isinstance(cur, dict) or not isinstance(cur.get(part), dict):
            return
        cur = cur[part]
    cur.pop(parts[-1], None)


def _prune(obj: dict) -> None:
    """删掉清空的嵌套字典，避免留下 "ui": {}。"""
    for k in list(obj):
        if isinstance(obj[k], dict):
            _prune(obj[k])
            if not obj[k]:
                del obj[k]


def current_state() -> dict:
    """页面首屏数据：规格 + 实际生效值 + 主配置值 + 覆盖状态 + 令牌状态。"""
    cfg = load_settings()
    main_path = ROOT / "config" / "settings.json"
    main = json.loads(main_path.read_text(encoding="utf-8")) if main_path.exists() else {}
    # 必须深拷贝：下面 _put 会改 base["ui"] 的嵌套值，不能污染全局 DEFAULTS
    base = copy.deepcopy(DEFAULTS)
    # 主配置合并进 DEFAULTS，作为「恢复默认」的落点
    for key in [s[0] for s in _SETTINGS_SPECS]:
        val = _get(main, key)
        if val is not None:
            _put(base, key, val)

    local = read_local()
    overrides = [s[0] for s in _SETTINGS_SPECS if _get(local, s[0]) is not None]
    try:
        _, source = read_token(cfg)
        token = {"configured": True, "source": source}
    except Exception:  # noqa: BLE001 - 只回传布尔状态，绝不回传令牌或异常里的值
        token = {"configured": False, "source": "未配置"}

    return {
        "ok": True,
        "specs": specs(),
        "groups": GROUPS,
        "values": {s[0]: _get(cfg, s[0]) for s in _SETTINGS_SPECS},
        "defaults": {s[0]: _get(base, s[0]) for s in _SETTINGS_SPECS},
        "overrides": overrides,
        "token": token,
        "local_file": local_path().name,
        "main_file": main_path.name,
    }


def _coerce(kind: str, lo, hi, val, key: str):
    if kind == "bool":
        if not isinstance(val, bool):
            raise ValueError(f"{key} 必须是 true / false")
        return val
    if kind in ("int", "float"):
        if isinstance(val, bool) or not isinstance(val, (int, float, str)):
            raise ValueError(f"{key} 必须是数字")
        try:
            fv = float(val)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{key} 必须是数字，收到 {val!r}") from exc
        if kind == "int":
            if fv != int(fv):
                raise ValueError(f"{key} 必须是整数")
            fv = int(fv)
        if lo is not None and (fv < lo or fv > hi):
            raise ValueError(f"{key} 应在 {lo} ~ {hi} 之间")
        return fv
    if kind == "str":
        sv = str(val).strip()
        if not sv:
            raise ValueError(f"{key} 不能为空")
        return sv
    if kind == "coords":
        if (not isinstance(val, (list, tuple)) or len(val) != 2
                or any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in val)):
            raise ValueError(f"{key} 必须是 [x, y] 两个整数")
        pair = [int(x) for x in val]
        if any(x < lo or x > hi for x in pair):
            raise ValueError(f"{key} 坐标应在 {lo} ~ {hi} 之间")
        return pair
    raise ValueError(f"{key} 类型不认识：{kind}")


def validate(changes: dict) -> dict:
    """只接受规格内的键；返回清洗后的值。任一不合法就整体拒绝。"""
    if not isinstance(changes, dict):
        raise RuntimeError("changes 必须是对象")
    lookup = {s[0]: s for s in _SETTINGS_SPECS}
    cleaned: dict = {}
    errors: list[str] = []
    for key, val in changes.items():
        spec = lookup.get(key)
        if spec is None:
            errors.append(f"不认识设置项：{key}")
            continue
        try:
            cleaned[key] = _coerce(spec[2], spec[3], spec[4], val, key)
        except (ValueError, TypeError) as exc:
            errors.append(str(exc))
    if errors:
        raise RuntimeError("；".join(errors))
    return cleaned


def _atomic_write(text: str) -> None:
    p = local_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        bak_path().write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = Path(str(p) + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def save(changes: dict) -> dict:
    """增量写回本地覆盖层；只动提交过来的键。"""
    cleaned = validate(changes)
    doc = read_local()
    for key, val in cleaned.items():
        _put(doc, key, val)
    # 没变就不写盘
    new_text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    if local_path().exists() and local_path().read_text(encoding="utf-8") == new_text:
        return {"ok": True, "changed": [], "note": "内容没变，没有写盘"}
    _atomic_write(new_text)
    return {"ok": True, "changed": sorted(cleaned), "note": "已写回，下次命令即生效"}


def reset() -> dict:
    """恢复默认 = 从本地覆盖层清掉所有规格键，回落到主配置 / DEFAULTS。"""
    doc = read_local()
    removed = []
    for spec in _SETTINGS_SPECS:
        if _get(doc, spec[0]) is not None:
            _drop(doc, spec[0])
            removed.append(spec[0])
    _prune(doc)
    if not removed:
        return {"ok": True, "removed": [], "note": "本来就没有覆盖项"}
    if doc:
        _atomic_write(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    elif local_path().exists():
        bak_path().write_text(local_path().read_text(encoding="utf-8"), encoding="utf-8")
        local_path().unlink()
    return {"ok": True, "removed": removed, "note": "已回到主配置的值"}
