# PRD · 抓取完备性：让「看得见的头条一张都不漏」

> 版本：v1.1 ｜ 日期：2026-11-09 ｜ 状态：**用户已拍板「开始实施」，P0 / P1 / P2 代码已落地**
> 触发：用户本日反馈「一屏 4 条公众号文章，可能只抓了 3 篇或 2 篇」，并追问
> 「是不是跟滚动有关」。此前这个风险只写在 README 的「已知风险」里，**没有闭环**。
> 实施记录、与原设计的偏差、以及验收状态见文末「§10 实施记录」。

---

## 1. 目标与非目标

### 目标

| 编号 | 目标 | 可验证判据 |
| :--- | :--- | :--- |
| G1 | 信息流里**每一张看得见的头条卡**都被处理，不因图像猜测被静默跳过 | 同一位置连跑两次，链接集合完全一致；`coverage.missing == 0` |
| G2 | 滚动不提前收工、不跨整屏，窗口内内容**真的滚到边界** | `crossed_day=true` 或轮数上限才停；相邻屏有可测重叠 |
| G3 | 漏了能被发现、能被补，且**缺斤少两的简报发不出去** | 每屏台账 + 结束对账；缺口 >0 时 `scan` 被闸门拒绝 |
| G4 | 不改变现有安全语义 | 退出码 3 停轮、黑名单、放行信号、hydrate 校验、总结/发布链全部不动 |

### 非目标（已确认边界）

- **折叠「余下 N 篇」不抓、不统计**（用户 2026-11-09 拍板「完全不管次条」）。
  coverage 的分母只算屏幕上可见的**头条卡**，不把折叠次条算作缺口。
- 不改 `feed_days=2` 的两自然日窗口语义，不改按号抓取链（`account --name`）。
- 不改「放行信号」机制：`hydrate` 仍只认 `summary-request-*.json`，不加旁路。
- 本期**不做**「读微信本地消息库」（SQLCipher 解密 + TCC 权限），理由见 §7。

---

## 2. 实盘证据（本日 13:48 抓取 + 14:06 诊断图）

| 证据 | 数据 | 说明 |
| :--- | :--- | :--- |
| 抓取结果 | `grab --auto --append` 退出码 0，累计 10 条，本轮新增 7 条，滚 4 屏 | `crossed_day=false`，**没滚到窗口边界就停了** |
| 停止那一屏 | `out/diagnose-cards-1109.png` 上 4 张卡全是「15~16 小时前」 | 属昨天、**在 `feed_days=2` 窗口内**，却一篇没抓 |
| 识别层 | 程序认出 4 张（y≈234/360/505/631），人工数 6 张 | 顶部、底部各一张截断卡被排除 |
| 去重层 | 4 条互不相同的标题，`fps_close(容差 4)` 把「美股何时抄底？」与「忐忑，等周一」判成同一篇（汉明距离 3） | 误判率 1/6，短标题指纹后半段全 0，几乎无区分力 |
| 滚动层 | `gui.scroll(..., -12)` 连发 12 次**按行**滚轮事件，代码从不测量实际像素位移 | 见下表定量 |

卡片实测间距 126 / 145 / 126 px（均值 132），本屏卡跨度 397px，可见区 `y∈[110,832]`=722px：

| 单行像素（估） | 单次位移 | 下一屏还能看到本屏几张卡 |
| :--- | :--- | :--- |
| 20px | 240px | 3/4 |
| 33px | 396px | 2/4 |
| 40px | 480px | 1/4（临界） |
| 50px | 600px | **0/4 → 触发 `overlapped=False`** |

代码里 `overlapped` 检测到「跨过整屏」后**只打印一行警告、继续往下滚**，注释自己写着
「中间若有卡片就已漏抓」。

---

## 3. 根因分层

| 层 | 位置 | 怎么漏 | 与主犯关系 |
| :--- | :--- | :--- | :--- |
| R1 去重 | `grab_auto` 的 `seen_fps` + `fps_close` | 不同文章被判「已点过」直接 `continue`，**永久漏** | 主犯 |
| R2 停止 | `if new_links == 0: break` | 一屏没新货就收工，与「是否滚到窗口边界」无关 | 被 R1 触发 |
| R3 滚动 | `gui.scroll(-12)` 行数制、`overlapped` 只警告 | 位移不可测、跨整屏不补扫 | 独立路径 |
| R4 识别 | `find_card_titles` 的 `min_y=110`、`weak_min_gap=90` | 截断卡 / 紧邻强标题的短标题被丢，靠重叠屏兜底 | R3 失守则永久漏 |
| R5 守卫 | `_guard_title` | 一次截图时机不对就「拒绝盲点」，无重试、无缺口记录 | 制造缺口 |
| R6 结构 | 折叠「余下 N 篇」 | 从不展开 | 非目标，不计缺口 |
| R7 对账 | 全链路缺失 | 不知道「这屏该有几篇」，漏了无声，简报照发 | 让前 6 条无法被发现 |

**因果链**：R1 误判 → 新卡被跳过 → `new_links=0` → **R2 提前收工** → 窗口内昨天内容根本没滚到。
所以单修滚动不够（R1 照样在屏内吞卡），单修去重也不够（R2/R3/R4 仍在）。

---

## 4. 设计原则

> **任何跳过都必须有内容级凭证（URL / 标题），不靠图像猜测；
> 不因一屏没新货就停；结束必须对账，缺口可见、可补扫、可拦截。**

---

## 5. 方案

### P0 · 止血（`src/grab.py` 主循环，风险最低、先做）

| # | 改动 | 具体做法 |
| :--- | :--- | :--- |
| P0-1 | `seen_fps` 降级为**同屏去重点** | 每屏开始时清空；跨屏一律重新点开，用 `obtained` 中的 URL 集合去重。指纹只保留「同屏内防止 `_guard_title` 把同一行吸两次」的用途。宁可多花几秒点开，绝不靠猜跳过。 |
| P0-2 | 删掉 `new_links == 0 → break` | 停止只认三种：① `_crossed_day` 判到窗口边界；② `len(obtained) >= limit`；③ `rounds >= auto_rounds`。新增 `stalled_rounds`：连续 K 屏无新增**不收工**，改做补偿滚动并记入台账。 |
| P0-3 | `overlapped=False` 升级为强制动作 | 从「打印警告」改成「自动回滚半屏 + 重新截图补扫」，补扫结果计入该屏 `results`。 |
| P0-4 | `_guard_title` 失败重试 | 隔 0.5s 重截图重试 1~2 次；仍失败写入 `missing[]` 而不是无声丢弃。 |

### P1 · 完备性闭环（这才是「彻底」）

**每屏台账** `out/_grab_ledger-<date>.json`：

```jsonc
{
  "date": "2026-11-09", "run_id": "...", "feed_days": 2,
  "screens": [
    {
      "round": 1, "shot": "out/_shots/screen-001.png",
      "cards": [{"y": 234.0, "x": 680.0, "peak": 121, "fp": "..."}],
      "results": [{"card_y": 234.0, "outcome": "linked", "url": "https://mp.weixin.qq.com/s/..."}],
      "stamps": ["4小时前"], "displacement_px": 268, "overlap_ok": true,
      "new_urls": 2, "stalled": false
    }
  ],
  "coverage": {"seen": 20, "linked": 18, "no_link": 2, "missing": 0}
}
```

`outcome` 枚举与缺口口径（**关键边界**）：

| outcome | 含义 | 计入缺口 |
| :--- | :--- | :--- |
| `linked` | 成功复制到公众号链接 | 否 |
| `duplicate` | 重新点开后发现 URL 已在 `obtained` | 否 |
| `no_link` / `deleted` | 广告卡、被删文章，本来就没有链接 | **否**（不是漏，是内容本身没有） |
| `error` | 守卫拒点 / 菜单认不出 / 恢复失败 | **是** |

`coverage.missing = seen - linked - duplicate - no_link - deleted`，即 `error` 的条数。
也就是说：**缺口只统计「本该拿到却没拿到」的卡**，广告和删文不会让闸门误报。

**缺口清单** `out/_grab_missing-<date>.json`：每条记 `round`、`y`、`fp`、`reason`、`retry_count`，供 `--resume` 定位。

**硬闸门（本次按推荐值定，用户可一句话改）**

- `coverage.missing > 0` → 写 `out/_plan/grab-gate-<date>.json`（阻断标记），
  `cmd_scan` 启动时读它，**存在且未解除就拒绝执行**，提示 `grab --resume`；
  自动链路因此不会拿一份缺斤少两的候选池去总结、发布。
- `grab --resume`：读缺口清单，按台账里的屏号 + 位移回滚到对应位置补扫，
  只补缺失卡；补齐后自动删除阻断标记，流程继续。
- 与现有「放行信号」机制**同构**：新增的是 `grab → scan` 的前置闸门，
  `hydrate` 的校验一行不改。
- 逃生口：`scan --ignore-grab-gate`（单轮绕过，打印醒目告警并留痕），
  防止闸门误报把流程彻底堵死。
- **按号抓取链不受影响**：`scan --urls-file` / 带 scope 时不检查该闸门。

**若要把策略改成「补一次，补不齐也发」或「只出清单不补」**：只需在
`grab_auto` 结束时改「是否写 gate」这一个分支，其余台账与 coverage 全部复用；
配置键 `grab_gate_policy` 取值 `block`（默认）/ `warn` / `off`。

### P2 · 识别加固 + 位移可测

| # | 改动 | 具体做法 |
| :--- | :--- | :--- |
| P2-1 | **位移测量** | 新增 `feed_vision.measure_shift(prev_png, cur_png)`：取上一屏一条标题带（x∈标题列、h≈18px）的灰度条带，在下一屏沿 y 轴 ±0.8 屏高滑窗做 SAD，最小差即 `displacement_px`。复用现有纯 Python `decode_png`，**不引入 OpenCV / numpy 等新依赖**。 |
| P2-2 | **目标位移 + 迭代修正** | 目标步长 = `min(2 × 本屏中位卡间距, 320px)`（实测约 265px），保证至少 2 张卡重叠。按 `ui.scroll_amount` 发行数 → 测位移 → 若 `> max_step` 则按实测比例反向发行数迭代修正（≤3 次）；仍不达标记 `overlap_risky` 并强制补扫。 |
| P2-3 | 重叠判据收紧 | 从「任意一张卡对上就算」改成「上一屏**最后一张**必须出现在下一屏前部」，杜绝跨掉中间几张仍判通过。 |
| P2-4 | 弱带回收动态化 | `weak_min_gap` 从固定 90 改为 `0.6 × 本屏中位卡间距`，避免紧邻强标题的短标题被当小字丢掉。 |
| P2-5 | 截断卡登记 | 顶部/底部只露一部分的卡记 `partial`，明确交给重叠屏覆盖；不再静默丢弃。 |

### P3 · 本期不做

折叠次条展开（推翻已确认边界）、微信本地消息库直读（需 SQLCipher 解密 +
TCC 全盘访问权限，schema 逆向工作量与合规风险都大，作为终局备选保留）。

---

## 6. CLI 与产物变化

```bash
python3 -m src.main grab --auto                 # 结束打印「看到 N / 拿到 M / 无链接 S / 缺 K」
python3 -m src.main grab --auto --resume        # 按缺口清单补扫，补齐解除闸门
python3 -m src.main grab --auto --no-gate       # 单轮不写阻断标记（调试用）
python3 -m src.main scan                        # 有未解除闸门时拒绝，提示 --resume
python3 -m src.main scan --ignore-grab-gate     # 单轮绕过（留痕告警）
```

`grab_auto` 返回值新增 `coverage`、`ledger`、`missing_file`、`gate` 四个字段；
原有 `mode / count / new_this_round / skipped / file / urls / rounds / crossed_day /
feed_scope / feed_days` 全部保留，`cmd_grab` 的 JSON 打印格式兼容。

新增配置键（全部写进 `DEFAULTS`，设置页规格相应 +N 项）：
`ui.scroll_max_step_px=320`、`ui.scroll_shift_retries=3`、`ui.title_retry=2`、
`ui.stalled_rounds=3`、`grab_gate_policy="block"`。

---

## 7. 验收标准

| # | 验收项 | 判据 |
| :--- | :--- | :--- |
| A1 | 离线夹具 | 用 `out/diagnose-cards-1109.png` 的 4 条标题断言：改动后主循环**不会**因 fp 相似跳过任何一张（现版本会误跳 1 对） |
| A2 | 停止条件 | 构造「一屏 0 新增」的 mock，断言主循环继续滚动而非 break；连续 `stalled_rounds` 屏才做补偿滚动 |
| A3 | 重叠闭环 | mock `overlapped=False`，断言发生回滚补扫且该屏 `results` 被补齐 |
| A4 | coverage 口径 | `linked + duplicate + no_link + deleted + missing == seen`；广告/删文**不**抬高 missing |
| A5 | 闸门读写 | missing>0 写 gate → `scan` 拒绝；`--resume` 补齐 → gate 删除 → `scan` 放行；`--ignore-grab-gate` 留痕 |
| A6 | 位移测量 | 用人工平移过的两张截图（已知位移 100/250/500px）断言 `measure_shift` 误差 ≤8px |
| A7 | 实盘可重复性 | 同一信息流位置连跑两次，URL 集合完全一致（顺序可不同），且 `coverage.missing == 0` |
| A8 | 回归 | 现有 **219 项单测全绿**，新增用例覆盖 A1~A6 |

---

## 8. 风险与回滚

| 风险 | 影响 | 缓解 |
| :--- | :--- | :--- |
| 跨屏一律重点，重复开窗变多 | 两天窗口耗时从 16~24 分钟增至约 25~35 分钟 | 目标步长调到 2 张卡重叠，减少总屏数；`ui.scroll_amount` 可调 |
| 位移测量在「顶部插入新文章」时失配 | 测得位移偏差 | 取多条标题带做中位数；失配记 `overlap_risky` 并强制补扫，不静默通过 |
| 闸门误报堵住自动链路 | 简报发不出去 | `grab_gate_policy=warn` / `--ignore-grab-gate` 单轮逃生，且闸门只在信息流链生效 |
| `grab --resume` 回到旧位置后界面已变 | 补扫仍失败 | 补扫上限 + 明确报错退出码 3，按铁律停轮等人介入，不无限重试 |
| GUI 操作风险 | 误关主窗口等 | 退出码 3 停轮、`nav.recover` 形态复验、Cmd+W 守卫**一律不动** |

**回滚**：改动集中在 `src/grab.py`、`src/feed_vision.py`、`src/nav.py`、
`src/main.py`、`src/selection.py`（gate 读写）、`src/config.py`、
`src/settings_ui.py` 与新增测试，可用一次 `git` 回退完整撤销；
台账 / gate 都是 `out/` 下的新文件，不写 `state/` 台账、不动 `offered.json`。

---

## 9. 实施顺序（**待用户过目确认后才开始**）

1. **P0**：改主循环四项 → 补单测 → 全量回归 → 实盘跑一轮看还漏不漏。
2. **P1**：ledger / coverage / gate / `--resume` → 补单测 → 实盘验证 K=0。
3. **P2**：位移测量与识别加固 → 补单测 → A7 连跑两次可重复性验收。
4. 同步 `README.md`「已知风险」条目改为「已闭环」、主 PRD 追加 v1.13 变更记录、
   技能文档 `SKILL.md` 补充 `grab --resume` / 闸门说明；更新工作区记忆。

> 每一步都单独跑全量单测；任何一步实盘出现退出码 3，按铁律立即停轮，
> 不用 `--force`、不盲点、不猜坐标。

---

## 10. 实施记录（2026-11-09 15:00 起）

用户回复「开始实施」后按 P0 → P1 → P2 依次落地，**未动**放行信号、
hydrate 校验、黑名单、总结提示词、发布链、按号抓取链、折叠次条边界。

### 已落地

| 层 | 文件 | 内容 |
| :--- | :--- | :--- |
| P0 | `src/grab.py` | `_open_and_copy_retry`（守卫拒点重试，内容问题不重试）；`seen_fps` 全局去重换成**同屏 y 距离去重 + 跨屏 URL 去重**；删 `new_links==0 → break` 改 `stalled_rounds`；`_overlap_with` 无重叠时 `_scroll_back_half()` 回滚补扫；每张卡记 `screens[].results` |
| P1 | `src/grab_ledger.py`（新） | `compute_coverage` / ledger / 缺口清单 / gate 读写 / `summary_line`；`grab._persist_ledger` 单独抽出便于单测隔离 |
| P1 | `src/main.py` | `--resume` / `--no-gate` / `scan --ignore-grab-gate`；`_grab_gate_blocks()` 只拦信息流链；`cmd_grab` 终端只打摘要（屏明细在 ledger 里） |
| P2 | `src/feed_vision.py` | `measure_shift()` 纯 Python profile + L1 滑窗，无新依赖；`find_card_titles` 增加 `dynamic_weak_gap` 开关 |
| P2 | `src/grab.py` | `_target_step()`（2 倍中位卡间距，上限 320px）；`_scroll_with_measure()` 实测位移并迭代微调；`_overlap_with` 收紧为「上屏最后一张在前部」或「≥2 张匹配」 |
| 配置 | `src/config.py` / `src/settings_ui.py` | 新增 `grab_gate_policy`、`title_retry(_wait)`、`stalled_rounds`、`scroll_max_step_px`、`scroll_shift_retries`、`card_dynamic_weak_gap`；设置页 **25 → 30 项** |
| 测试 | `tests/test_grab_completeness.py`（新） | P0 9 例、P1 11 例、P2 15 例；另把 3 个旧测试底板适配新主循环（单屏用例 `max_rounds=1` / 固定屏） |

### 与原设计的偏差（如实记录）

1. **`--resume` 降级为「从头重抓补齐」**，不是 PRD §5 写的「按台账屏号 + 位移回到旧位置」。
   微信会往信息流顶部插新文章，GUI 滚动位置不可复现，按旧位移回滚并不比
   重抓更可靠。现在 resume 带着上轮缺口清单从头跑一遍，重新对账，
   **K=0 才解除闸门**；多花几分钟换确定性。代码注释里也写明了这个取舍。
2. **P2-5 截断卡只做台账登记**，没改 `find_card_titles` 的返回结构：
   每屏 `screens[].cards` 已记首尾卡的 x/y/fp，缺口可见但不改变点击行为 ——
   把「只露半张」的卡也拿去点会大量产生 `error`，反而让闸门误报。
3. **P2-4 动态弱带判据默认关闭**（`ui.card_dynamic_weak_gap=0`）。逻辑已实现
   （0.6 × 本屏中位卡间距、至少 40px），但它会改变识别结果，需先实盘跑一轮
   核对有没有误收小字，再在设置页改成 1。关掉时行为与旧版完全一致。
4. **coverage 的 `seen` 是「处理次数」而非去重后的卡数**（跨屏重叠会重复
   计数），另给 `unique_cards` 作参考。闸门只看 `missing`（= error 条数），
   不受重复计数影响；恒等式 `linked + duplicate + no_link + deleted + missing == seen` 有断言。

### 验收状态

| 阶段 | 全量单测 | 说明 |
| :--- | :--- | :--- |
| 改造前基线 | 219 OK | — |
| P0 完成 | **228 OK** | 新增 9 例；3 个旧底板因主循环语义变更做了适配 |
| P1 完成 | **239 OK** | 新增 11 例；确认测试不污染真实 `out/`（无 ledger / gate 残留） |
| P2 完成 | **未能跑最终回归** | 系统临时目录被安全策略清空，`tempfile.TemporaryDirectory()` 在既有用例的 `setUp` 里抛 `FileNotFoundError`，70 个既有用例连带报错（与代码无关，同一批用例 P1 阶段 239 OK）。已把 P2 的 15 例写成**不写任何文件、不依赖 tempfile**（直接 patch `decode_png` 注入内存像素），待环境恢复后执行 `python3 -m unittest discover -s tests -q` 复核（预期 254 项）。 |

**A7 实盘可重复性仍未验收**：需要在微信信息流同一位置连跑两次
`grab --auto --append`，比对链接集合完全一致且 `coverage.missing == 0`。
这一步要操作 GUI 约 20~35 分钟，且会真实写 `out/` 台账与闸门，
按铁律不在未确认时自动跑。

