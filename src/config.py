#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""路径与运行配置。

优先级：DEFAULTS < config/settings.json（项目定稿） <
config/settings.local.json（参数设置页写的用户覆盖层，Git 已忽略）。
令牌**不在**这些文件里，运行时才从项目内独立的 config/secrets.local.json
只读加载。每次 CLI 命令都重新读盘，所以设置页保存即生效、无需重启服务。
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    # 每次从微信信息流取多少篇
    "max_articles": 20,
    # 抓完要不要人工在清单页确认再总结。
    #   0 = 必须人工提交（默认，闸门照旧）
    #   1 = scan 出完候选池就自动全选、代写放行信号，直接进 hydrate
    # 只跳过「人选哪几篇」，黑名单 / 无标题 / 相似度去重 / 台账一律照旧。
    # 命令行 scan --auto-summary / scan --confirm 可只覆盖本次运行。
    "auto_summary": 0,
    # 字符 5-gram Jaccard 阈值，>= 视为转载（实测真转载约 0.93）
    "dedup_threshold": 0.75,
    # 批内篇数达到这个数就拆批（对齐参考项目的拆批规则）
    "batch_size": 15,
    # 子代理并发上限（对齐铁律 5）
    "max_parallel": 3,
    # 正文短于这个字数，交给提示词标「内容无法总结」
    "min_content_chars": 50,
    # 项目内独立令牌文件（Git 忽略、权限 600），相对项目根；可用环境变量覆盖
    "token_file": "config/secrets.local.json",
    "publish_api": "https://zenmark-bxx.pages.dev/api/cloud/upload",
    "publish_title_prefix": "公众号简报",
    # 运行认领锁超时（分钟）
    "lock_ttl_minutes": 20,
    # 语音播报版：一篇期装不下 30 篇，按篇数分期，每期各自推送一次。
    # per_issue 是每期最多几篇，max_chars 是每期正文上限（不含标题）。
    "voice_per_issue": 10,
    "voice_max_chars": 1800,
    # 微信公众号阅读窗口的 UI 坐标（1280x832 逻辑坐标，上一轮实测）
    # 界面变了就改这里，或按报错提示人介入；脚本不自动重新识别。
    "ui": {
        "logical_width": 1280,
        "logical_height": 832,
        # 抓取前校验屏幕几何：逻辑尺寸是否仍是 logical_width x logical_height、
        # 全屏截图宽高比是否一致（外接屏 / iPad 随航 / 远程桌面接入都会让它
        # 们失效）。1=自检并在不符时按退出码 3 停下，0=跳过。
        "geometry_check": 1,
        # 宽高比允许的相对偏差。1% 在 1280 宽上约等于 13px 的高度形变，
        # 已经足以让 11~12px 的标题带判据失准，所以默认卡在 1%。
        "geometry_tolerance": 0.01,
        "more_button": [1258, 101],
        "copy_link": [1110, 300],
        "close_window": [76, 108],
        "open_wait": 2.5,
        "menu_wait": 0.8,
        "close_wait": 1.8,
        "click_wait": 0.45,
        # 点击前标题行校准：卡片第一行是浅灰公众号名（点到会打开主页），
        # 第二行才是深色加粗标题。每次点击前重截图，把 y 吸附到标题行。
        # 1=开启 / 0=关闭；搜索半径是逻辑像素，阈值是 RGB 上限。
        "title_probe": 1,
        "title_search_down": 45,
        "title_search_up": 20,
        "title_dark_threshold": 120,
        "title_min_dark": 10,
        # 标题带的「够黑」门槛：真标题峰值 88~169，「余下 N 篇 / 时间」这类
        # 小字带只有 ~20，光靠高度分不开，再加一道峰值门槛。
        "title_min_peak": 45,
        "title_min_height": 6,
        "title_max_height": 22,
        # 自动导航（点左侧「公众号」图标进信息流）：最多点几次、每次等多久。
        # 图标位置靠像素识别，不需要 OCR；点完必须数到 ≥2 个标题才算成功。
        # 不写死等待秒数，而是轮询到出现卡片为止（超时才失败）。
        "nav_tries": 3,
        "nav_load_timeout": 8.0,
        "nav_load_poll": 0.8,
        # 微信窗口被完全挡住时的兜底：点它自己的菜单栏（「窗口」那一格）再 Esc。
        # 只有确认前台 App 是微信才会点，否则跳过。
        "menu_window": [255, 14],
        # 自动抓取（grab --auto）：一张卡片算「标题」的门槛（峰值 100+ 才是
        # 加粗标题，卡片里的「余下 N 篇 / 时间」只有 18~37），以及起始 y。
        "card_min_peak": 55,
        # 起始 y：原来 200，会把信息流顶部第一张卡片（标题约在 y=144）
        # 整张漏掉。110 以下只剩标题栏/「公众号」页头，不会再往下误检。
        "card_min_y": 110,
        # 短标题回收（漏点修复）：八九个字的标题峰值只有 20~40，纯峰值门槛
        # 会把它当小字噪声扔掉。峰值落在这个区间、且离强标题 ≥ gap 的，
        # 只要左边缘和卡片标题对齐就收回来。
        "card_weak_min_peak": 26,
        "card_weak_min_gap": 90,
        "card_left_align_tol": 14,
        # 跨屏认同一行的指纹容差（十六进制字符数，越大越宽松）
        "card_fp_max_diff": 4,
        # 「…」菜单里识别文字行的门槛：菜单项字号小、深色像素少，
        # 分组间距按实测：项间距约 27px，分组之间约 44px。
        "menu_min_dark": 6,
        "menu_group_gap": 35,
        # 一屏抓完就向下滚动；连续几张失败就停（广告卡片偶发一张不算）。
        "auto_rounds": 6,
        "auto_fail_streak": 2,
        # 文章被发布者删除 / 页面没有「复制链接」时：1=跳过换下一张（默认），
        # 0=按界面异常停下等人。这类是**内容**问题，跳过不计入上面的
        # auto_fail_streak；但连续跳过 auto_skip_streak 张仍然要停（那多半
        # 不是个别文章被删，而是菜单结构整体失效）。
        "skip_deleted": 1,
        "auto_skip_streak": 4,
        # 「该内容已被发布者删除」空页判定门槛：近白像素占比、黄色图标像素数。
        # 实测空页白占比 ~0.95、黄色图标约 4900 像素；纯文字白底文章没有
        # 黄色图标，带黄色配图的文章不是白底，两道门都过不了。
        "deleted_min_white": 0.70,
        "deleted_min_yellow": 800,
        "scroll_x": 700,
        "scroll_y": 650,
        "scroll_amount": -12,
        "scroll_wait": 1.4,
        # ---------------- 按号抓取（公众号「列表模式」） ----------------
        # 2026-11-08 在真实 1280x832 上逐项量出来的值，改界面就要重新量。
        # 列表模式：左栏是「一行一个号」，右栏是该号的会话页。
        # 面板分界：左栏 x 318..618，右栏 620..1280。
        "account_list_x": [318, 618],
        # 右栏（会话页）左边界，用来区分「这行字属于左栏还是右栏」
        "account_pane_left": 620,
        # 左栏是否存在的判据：分界线那竖条 (x≈616..623, y 150..700) 里
        # 非白像素的占比。列表模式实测 98.8%~100%，卡片流 17%~32%，
        # 文章页 30% —— 0.90 两边都留足余量。
        "account_left_ratio": 0.90,
        # 号名文字的水平范围（点击就落在这段里；星标在 x≈600，必须避开）
        "account_name_x": [345, 560],
        # 左栏行距 68px、首行号名中心 y≈110（实测 110/178/246/314/382…）
        "account_row_first_y": 110,
        "account_row_pitch": 68,
        "account_row_pitch_tol": 14,
        # 右栏：文章标题左边界 x≈778；底部固定菜单栏（本站精华/保险推荐）
        # 从 y≈790 开始，标题不能落在它下面
        "account_title_x": [760, 1200],
        # 标题行的左边界上限：右栏标题一律左对齐在 x≈778~790；卡片右上角的
        # 位置标签（「• clash：美国」）OCR 读成 x≈1145~1202，靠左边界就能拦住。
        "account_title_left_max": 870,
        "account_footer_y": 782,
        # 头条标题的字号上限（OCR 行高）。真标题实测 h=14~20；
        # 大图推送的**封面宣传语**会被读成 h=28~36 的大字，比真标题更靠上，
        # 必须用字号把它筛掉，否则会点到封面图上。整组都超大时才退回 30。
        "account_title_max_h": 24,
        "account_title_hard_max": 30,
        # 模式切换按钮。**两个位置不一样，别混用**：
        #   卡片模式 → 点右上角 (1249,60) 切列表
        #   列表模式 → 点中栏表头 (593,60) 切回卡片
        # 列表模式下 (1249,60) 正好是「公众号主页」人像图标，点错会开主页。
        "account_toggle_to_list": [1249, 60],
        "account_toggle_to_card": [593, 60],
        # 标签栏按钮**不设坐标**：不同版本/不同形态（全屏标签页 vs 独立浮窗）
        # 位置都不一样，还可能出现/不出现「地球」。取链接走「⋯ 菜单 → 复制链接」，
        # 按钮位置由 account._toolbar_row() + _more_button() 现算。
        # 只留三个等待时间（秒）。
        "account_menu_wait": 1.0,      # 点「⋯」后等菜单弹出
        "account_copy_wait": 0.8,      # 点「复制链接」后等剪贴板写入
        "account_close_wait": 1.4,     # 点标签「×」后等列表页回来
        # 右栏滚动：从底部（最新）往旧翻
        "account_scroll_x": 900,
        "account_scroll_y": 480,
        "account_scroll_step": 8,
        # 连续几次滚动内容不变才算到底 / 到顶
        "account_bottom_confirm": 2,
        # 找号时左栏最多滚几屏（找不到就停，不无限滚）
        "account_scan_rounds": 8,
        # 点开文章等多久（文章可能是主窗口内的标签页，也可能是独立浮窗）
        "account_open_wait": 3.0,
        # 右栏标题带判据（沿用信息流那套：真标题峰值 80+，正文灰字过不了）
        "account_min_peak": 45,
        "account_title_min_height": 6,
        "account_title_max_height": 24,
        # 找号的名字匹配：规范化后精确相等；OCR 偶尔认错，允许近似匹配的
        # 相似度门槛（0 = 关掉近似匹配，只认精确相等）
        "account_name_ratio": 0.86,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings() -> dict:
    """读配置；主配置缺失用默认值，用户覆盖层缺失则跳过。"""
    cfg = dict(DEFAULTS)
    for name in ("settings.json", "settings.local.json"):
        path = ROOT / "config" / name
        if not path.exists():
            continue
        try:
            override = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"× 配置文件读不了：{path}（{exc}）")
        if not isinstance(override, dict):
            raise SystemExit(f"× 配置文件格式不对，应为 JSON 对象：{path}")
        cfg = _deep_merge(cfg, override)
    tf = Path(cfg["token_file"]).expanduser()
    cfg["token_file"] = str(tf if tf.is_absolute() else ROOT / tf)
    return cfg


def project_path(*parts: str) -> Path:
    return ROOT.joinpath(*parts)


# ---------------------------------------------------------------- scope
# 「单独出一份简报」的落点：所有按日期命名的产物都要分命名空间，否则同一天
# 跑两份（信息流 20 篇 + 某个号精选）会互相覆盖。
#
#   scope = ""            → 信息流那份，文件名**保持原样**（不破坏历史产物）
#   scope = "EarlETF"     → out/inbox-2026-11-08-EarlETF.json 等
#
# 台账（seen / offered / runs / published）**不分区**：同一篇文章不该因为
# 换了入口就被总结两次。
def inbox_file(date: str, scope: str = "") -> str:
    return f"inbox-{date}-{scope}.json" if scope else f"inbox-{date}.json"


def inbox_matches(name: str, scope: str = "") -> bool:
    """这个 inbox 文件名属不属于 scope（防止两份简报互相认领）。"""
    if not (name.startswith("inbox-") and name.endswith(".json")):
        return False
    stem = name[len("inbox-"):-len(".json")]
    if scope:
        return stem.endswith("-" + scope)
    # 信息流那份：日期就是全部（YYYY-MM-DD），带后缀的都算别人的
    return len(stem) == 10 and stem[4] == "-" and stem[7] == "-"


def pending_file(scope: str = "") -> str:
    return f"pending_summary-{scope}.json" if scope else "pending_summary.json"


def batches_dir(scope: str = "") -> str:
    return f"_batches-{scope}" if scope else "_batches"


def ensure_dirs() -> None:
    for name in ("state", "out", "out/_plan", "out/_batches"):
        project_path(name).mkdir(parents=True, exist_ok=True)
