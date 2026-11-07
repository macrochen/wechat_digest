# wechat_digest

从**本地微信客户端「公众号」信息流**取文章（默认 20 篇，可指定数量）→ **黑名单整号排除** + 广告过滤与四层去重 → **HTML 清单人工勾选并提交**（可用 `auto_summary` 开关跳过确认、抓完直接总结）→ 由当前 WorkBuddy 会话的 Hy4 分批总结成**一份**中文简报 → 推到 ZenMark 阅读器，并在本地服务里留一份可离线阅读的副本。

也可以**只抓指定的一个公众号**（切「列表模式」，一次推送只要头条），单独出一份精选号简报，见[按号抓取](#按号抓取只抓某一个号)。

需求文档：[docs/PRD.md](docs/PRD.md)（v1.0 已确认，顶部有 v1.2 ~ v1.6 变更说明）；
按号抓取：[docs/PRD-account-fetch.md](docs/PRD-account-fetch.md)

## 形态与运行原理

**日常用法**：在 WorkBuddy 里说一句「跑公众号简报」，由 `wechat-digest` skill 接管全流程。
skill 单一真源在 `~/.agents/skills/wechat-digest-skill/SKILL.md`，已软链到
`~/.workbuddy/skills/wechat-digest-skill`（新建软链后需重启 WorkBuddy 会话才注册）。

WorkBuddy 是 Electron 桌面应用，模型调用在客户端进程里；本机没有 headless CLI，也没有可携带提示词的深链。所以终端脚本**不能**同步调用 Hy4。分工是：

| 谁 | 干什么 |
| :--- | :--- |
| Python 脚本 | 截屏 / 合成点击取链接、抓元信息与正文、去重、拼提示词、合并校验、推 ZenMark |
| WorkBuddy 会话里的 Hy4 | 读截图给卡片坐标、按融合提示词分批写总结、派子代理并发 |
| 交接物 | `out/pending_summary.json`（提示词 + 正文 JSON）、`out/_batches/batch-N.md`（各批总结） |

总结**不调任何外部大模型 API**，零 API Key。

## 快速开始

```bash
cd ~/workspace/wechat_digest
python3 -m src.main doctor        # 自检：微信/屏幕/提示词/令牌，不发请求
python3 -m src.main status        # 看台账
```

完整流程由 skill 编排，也可以手工逐阶段调试：

```bash
python3 -m src.main capture                          # 1. 截屏，Hy4 读图给卡片坐标
python3 -m src.main grab --coords '560,275;560,400'  # 2. 批量复制链接
python3 -m src.main scan                             # 3. 补元信息，生成候选池（不取正文）
python3 -m src.main scan --auto-summary              # 3'. 自动模式：出完候选池直接放行，不用点提交
python3 -m src.main review                           # 4. 起 HTML 清单，等人工勾选并点「开始总结」
python3 -m src.main hydrate                          # 5. 取正文 + 相似度去重 + 待总结包
python3 -m src.main batches                          # 6. 看分批方案
#    Hy4 / 子代理按 prompts/summary.md 写 out/_batches/batch-N.md
python3 -m src.main merge | python3 -m src.main publish            # 7+8. 合并并推送
```

其它：`python3 -m src.main settings`（同一个常驻服务，落地设置页）、
`python3 -m src.main blacklist`（查看抓取黑名单）、
`python3 -m src.main account --name EarlETF`（按号抓取，见下节）、
`scan --max-articles 10`（本轮只抓 10 篇，默认 20）、
`review --no-open`（不自动开浏览器，仍是 7777 同一个服务）、`publish --dry-run`（不发请求）、
`publish --check`（只读验令牌）、`grab --dry-run`（只校验坐标不点击）、
`scroll --amount -4`（滚动信息流）、`status`（看 `inbox.submitted` 是否已提交）。

## 按号抓取（只抓某一个号）

信息流那条链是「点卡片 → 独立浮窗 → 「…」→ 复制链接」；单个公众号的会话页完全是
另一套界面：先把「公众号」页切到**列表模式**（一行一个号），点开目标号，右边是聊天式
时间线——**每次推送上面一行时间、下面一组卡片，组内第一条才是头条**（这就是「一天发了
多篇只要第一篇」的落点），**最新的一次推送在最底部**，所以先滚到底再一屏一屏往上翻。

```bash
python3 -m src.main account --name EarlETF --limit 20   # 取 20 次推送的头条
python3 -m src.main account --name EarlETF --probe      # 只看不点，排查定位
python3 -m src.main scan --urls-file out/_raw_urls-EarlETF.json   # scope 自动推断
python3 -m src.main hydrate --scope EarlETF
python3 -m src.main merge   --scope EarlETF --out out/summary-EarlETF.md
python3 -m src.main publish --scope EarlETF --file out/summary-EarlETF.md
```

- `--limit` 数的是**推送次数**，不是篇数（一次推送只取头条）。
- **scope 隔离**：`inbox-<日期>-<号名>.json`、`_plan/*-<日期>-<号名>.json`、
  `pending_summary-<号名>.json`、`_batches-<号名>/`；台账 `state/*.json` **只有一套**
  ——同一个链接在信息流批次里总结过，按号批次里照样跳过。
- 发布标题自动带号名（`… · EarlETF · <日期>`），因为它是一份精选号简报。
- **找不到号就停**（退出码 3）：左栏最多扫 `ui.account_scan_rounds` 屏（默认 8），
  扫完没命中不会退化成「点最像的那一行」。点开号之后还会复验右栏顶部就是目标号，
  两次不对即停——点偏一点就会取到别人家的 20 篇。
- **取链接走「地球」不碰「…」**：标签页右端的「…」菜单在鼠标按下时弹出、松开正好
  落在第一项，实测一点就把标签关掉。改成点「地球」（在浏览器打开）→ 微信自己关标签
  回列表页 → 浏览器 `Cmd+L` / `Cmd+C` / `pbpaste` 读地址栏。**全链路不按 Cmd+W**，
  从根本上避开「在主窗口按 Cmd+W 会把微信整个关掉」那个坑。
- **唯一的 OCR 在 `src/ocr.py`**（系统自带 Vision，zh-Hans，纯本地不联网）：
  只负责认字（左栏号名、时间行、标题），点哪里仍由像素判据决定。
  `doctor` 会打印「按号抓取用的本地文字识别：可用 / 不可用」，不可用说明缺 `swiftc`。
- 从左栏切进去的话，抓完自动切回卡片模式（`--keep-list` 可保留），免得下次跑信息流误判。


## 四层审核去重

1. **URL 级** `state/seen.json` — 抓过的链接不再抓；发布成功才写入，失败可原样重试
2. **正文相似度** — 字符 5-gram Jaccard ≥ 0.75 判为转载，只留先出现的一篇，记录写在 `out/_dedup_log.json`
3. **人工选文（强制闸门，可开关）** — `review` 确保 `127.0.0.1:7777` 上有一个常驻服务
   （`/review` / `/read` / `/settings` 三页同址，提交后服务不退出）；
   只有点了「开始总结」才写 `out/_plan/summary-request-<日期>.json` 信号，
   `hydrate` 硬检查它，没提交直接拒绝执行。清单与信号都带 `inbox_generated_at`，
   候选池被重扫覆盖后自动作废、需重新提交。口头「全部都要」**不算授权**，
   没有超时、没有静默放行。
   **自动模式例外且仅此一条**：配置 `auto_summary=1`（设置页可改 / 配置文件可写）
   时，`scan` 会替你把「全选清单 + 放行信号」一起写好，信号带 `"mode": "auto"`
   指纹，`hydrate` 照旧只认信号文件、逻辑一行不放宽，只是打印「未经人工确认」。
   详见[自动模式](#自动模式跳过人工确认)。
4. **幂等 + 发布哈希 + 认领锁** — `state/runs.json` 命中即停；简报 SHA256 比对 `state/published.json`，内容没变不重推；`state/lock.json` 防两个进程同时跑同一批（20 分钟超时）

未勾选的篇目**不会**下载正文（`scan` 阶段 `phase=scan` 只有元信息）；没提交则一篇正文都不会取。

## 自动模式（跳过人工确认）

不想每次去页面勾选，就让流程抓完直接总结。**默认关闭**，两种开法：

| 开法 | 生效范围 | 怎么用 |
| :--- | :--- | :--- |
| 配置项 `auto_summary=1` | 以后每次（长期） | 设置页「总结 → 抓取后自动总结」改成 1，或写 `config/settings.local.json` |
| `scan --auto-summary` | 只这一轮 | 平时保持人工确认，临时懒一次 |
| `scan --confirm` | 只这一轮（强制人工） | 配置里开着自动，但这次想自己挑 |

优先级：`--confirm` > `--auto-summary` > 配置项。

**它做了什么**：`scan` 建完候选池后，替你把「选文清单（全选）」和
「放行信号」一起写进 `out/_plan/`，信号里带 `"mode": "auto"`、`"source": "scan"`；
**不再自动弹出清单页**，直接提示下一步跑 `hydrate`。

**它没做什么**（这些闸门一个都不跳）：黑名单整号排除、无标题/闲聊过滤、
相似度去重、投递台账 `offered`、发布哈希、认领锁，以及 `hydrate` 对信号文件的
硬检查——自动模式不是 `hydrate` 里的旁路分支，只是「谁写了这个信号」不同。
故意保持单一路径，是为了事后只看一眼信号文件就知道这批是人工挑的还是自动放行的。

**怎么确认本轮是怎么放行的**：

```bash
python3 -m src.main status          # auto_summary：当前生效开关
                                    # inbox.request_mode：auto / manual / null（旧信号）
python3 -m src.main doctor          # 「总结前确认：人工确认 / 自动放行」
cat out/_plan/summary-request-$(date +%F).json   # 信号里的 mode / source
```

**风险提示**：自动模式下 `publish` 也随之自动执行（全流程零操作），
推上去的稿子没有人先看过；不满意就在落地页 `/read` 里回看，
或把 `auto_summary` 改回 0 恢复人工确认。自动模式**只影响本次扫描出来的这一批**，
已经提交过 / 已经 hydrate 的旧批次不会追溯。

## 抓取黑名单

订阅了但内容不关心的公众号，**整号排除**。名单唯一真源 `config/blacklist.json`
（会被 Git 跟踪，是长期偏好；不在被忽略的 `*.local.json` 里）：

```json
{"sources": ["中汇人寿"], "note": "..."}
```

- **规则**：规范化（NFKC 统一全半角、去所有空白、英文 casefold）后**精确相等**才命中；
  不做包含匹配，`中汇人寿` 不会误伤 `中汇人寿保险`；没解析到来源的一律不命中。
- **两层拦截**：读截图给坐标时，黑名单号的卡片直接跳过（不点开、不复制链接）；
  `scan` 拿到 `source` 后再做一次代码硬过滤。命中**不进候选池** → 不进清单、
  不取正文、不总结、不推送；候选池里记 `skipped_blacklist`，命令行打印排除篇数。
- **三种维护方式**：
  - 设置页 `/settings` 的「抓取黑名单」卡片，一行一个公众号名，独立「保存黑名单」按钮；
  - 终端 `python3 -m src.main blacklist`（查看）、`blacklist add 中汇人寿`（加入）、
    `blacklist remove 中汇人寿`（解除），可一次跟多个名字；
  - 直接编辑 `config/blacklist.json`（写盘前自动生成 `.bak`，已 Git 忽略）。
- **不动台账**：不写 seen / runs / published，不写提交信号，不等于授权总结；
  下次 `scan` 生效，已有候选池不追溯清理。「恢复默认」只清参数覆盖项，不动黑名单。

## 文章被删 / 没有「复制链接」

信息流里的文章可能已被发布者删除：点开仍会打开一个正常浮窗，但页面只有一句
「该内容已被发布者删除」，「…」菜单里**没有**「复制链接」。旧行为会退回配置里
写死的 `copy_link` 坐标盲点，剪贴板为空 → 判成界面异常 → 连续两张就整轮停下。

现在的处理：

- **认不出「复制链接」就不盲点**（结构要求：菜单第 2 组第 2 项）；
- 用 `feed_vision.looks_like_deleted_page` 判页面（近白底 + 居中黄色感叹号图标，
  纯像素无 OCR）→ 命中记 `_CardSkipped`：Esc 收菜单、关窗、**换下一张**，
  不进 `auto_fail_streak`；被跳过卡片的指纹记进 seen，滚动回来不再重复点；
- 自动抓取连续跳过 `ui.auto_skip_streak`（默认 4）张才停——整屏都没链接说明
  菜单结构变了，不是个别文章被删；
- 关掉跳过：设置页不便暴露，改 `config/settings.local.json` 写
  `{"ui": {"skip_deleted": 0}}`；判定门槛是 `ui.deleted_min_white`
  （近白占比，默认 0.70）与 `ui.deleted_min_yellow`（黄色图标像素，默认 800）。

## 参数设置页

### 谁来启动服务

**v1.7 起一个服务、一个端口**：选文清单 `/review`、本地阅读 `/read`、
参数设置 `/settings` 始终都是 `127.0.0.1:7777`，共用顶部导航。
候选池 `phase=scan` 且未提交时清单自动出现，提交后清单收起但**服务不退出**，
阅读页地址不变。端口上已有新版服务会直接复用，不再起第二个进程；
旧版服务或其它程序占着会明确报错，按提示释放端口，**不要换端口**。

| 场景 | 服务由谁启动 | 你要做什么 |
| --- | --- | --- |
| 跑「公众号简报」全流程 | **agent 用后台任务起或复用**常驻服务 | 只需在浏览器勾选文章、点「开始总结」（`auto_summary=1` 时连这一步都省掉）；服务不退出，**不用你手动启动任何东西** |
| 平时只想调参数 / 看往期简报 | **你在终端跑脚本**（或让 agent 起后台任务） | 改完点保存即写盘；用完按 Ctrl+C 关掉服务 |

终端里直接执行（自动选 `.venv` 里的 Python、自动开浏览器、重复运行提示复用）：

```bash
./scripts/start-ui.sh                 # 常驻服务，默认落地设置页，127.0.0.1:7777
./scripts/start-ui.sh --review        # 落地选文清单页（没待确认候选池时给提示页）
./scripts/start-ui.sh --port N        # 换端口（三页一起换，仍是同一个地址）
./scripts/start-ui.sh --no-open       # 不自动打开浏览器
```

等价的手工命令（两者都是同一个常驻服务，只是落地页不同）：

```bash
python3 -m src.main settings               # 落地设置页
python3 -m src.main review                 # 落地选文清单页
```

`review` / `settings` 都会先探测 7777：已有新版服务就复用并返回，不另起进程；
服务常驻，Ctrl+C 结束。**启动方式不改变闸门**：必须到清单页点「开始总结」
写出信号，`hydrate` 才放行（配置 `auto_summary=1` 时由 `scan` 代写信号，
见[自动模式](#自动模式跳过人工确认)）；agent 用 `status` 的 `inbox.submitted`
确认提交，不再依赖服务退出。

页面按 `src/settings_ui.py` 的规格动态生成 22 项参数，分抓取 / 去重 / 总结 /
发布 / 界面五组；含 `ui.*` 的按钮坐标、等待时长与屏幕几何自检开关。只提交改动过的键，带类型与范围
校验，写回 `config/settings.local.json`（Git 已忽略），写前 `.bak` 备份 + 原子替换；
「恢复默认」清掉覆盖项回到主配置。主 `config/settings.json` 不被页面改动。

优先级：`DEFAULTS` < `settings.json` < `settings.local.json`，每条命令重读配置，
**保存即生效、无需重启**；命令行参数更高（`scan --max-articles 10` 只覆盖本次）。
同一页下方还有独立的「抓取黑名单」编辑区（一行一个公众号名、单独保存，
写 `config/blacklist.json`），不属于这 22 项参数，「恢复默认」不会清空名单，
详见[抓取黑名单](#抓取黑名单)。

令牌内容不进页面，只显示是否已配置。**在设置页改参数不等于授权总结**——
唯一的例外是那个 `auto_summary` 开关：它表示「以后每次 scan 都自动放行」，
是明确的长期授权，改它之前页面会给出后果说明。

## 屏幕几何自检

抓取的每一个判据（标题带 11~12px、36x36 的「公众号」蓝方块、x=520..880 的
暗像素数）都建立在同一个前提上：**截图像素空间 == CGEvent 坐标空间 ==
`ui.logical_width` x `ui.logical_height`（默认 1280x832）**。

前提被破坏时程序不会报错，只会**静默点到别处**——表现为「点了但剪贴板为空」
或「开出公众号主页浮窗」，极难排查。所以每次 GUI 抓取前（`capture` / `nav` /
`grab` / `grab --auto` / `scroll`）都会做一次自检，约 150ms：

| 判据 | 拦下的情况 |
| --- | --- |
| 主显示器**逻辑尺寸**必须等于配置值 | 换了缩放档位（如 1280x800、1512x982）。CGEvent 按逻辑点发送，坐标整体偏移 |
| **全屏截图宽高比**与配置值偏差 ≤ `ui.geometry_tolerance`（默认 1%） | `sips -z` 是强制拉伸不是等比缩放；比例一偏，所有像素阈值失准 |
| 以上任一命中 | 打印实测值与三种修法，按**退出码 3** 停下等人 |

`ui.geometry_check=0` 可关掉自检（设置页「屏幕几何自检」）。

几个实测事实，改这块之前先看：

- **`CGDisplayPixelsWide` 不能用来推 backing scale。** 本机它报 1280x832 /
  scale 1.0，而 `screencapture` 实际拍出 **2560x1664**（缩放分辨率模式下它
  返回的是逻辑值）。拿它当基准只会稳定误报；**比例一致就够**，2x 等比缩放
  正是长期在用的正常路径。
- **`screencapture -x` 会把所有显示器拼成一张图。** 接第二块屏或 iPad 随航
  后，截图变成 2304x832 这类拼合图，比例判据会拦下它。
- **查不到信息一律放行**（没有 swiftc、调用失败都这样），交给后续真正的
  截图报错，不在前置检查里冒充失败。
- `python3 -m src.main doctor` 会打印当前几何实测值，排查时先看这一行。

## 本地阅读页

常驻服务起来后打开 `http://127.0.0.1:7777/read`，或点顶部导航「本地阅读」；
它与选文清单、参数设置同址，提交清单后服务不退出，地址始终不变。
发布简报时会在 `out/archive/` 额外留一份 `.md` 正文 + 同名 `.json` 元数据，
阅读页左侧按期列表、右侧渲染 Markdown（标题、公众号链接、段落、粗体、
分隔线、列表），纯本地、离线可用、不加载 CDN。

- **范围**：只显示今后发布时留存的副本；之前已推到 ZenMark 的简报不在列表里，
  也不从云端回拉。
- **普通版、语音播报版都归档**；语音版 `publish --no-ledger` 归档但不写台账，
  推送失败也归档并标「未推送」，原来的 `out/failed-*.md` 留底照旧保留。
- **纯只读**：不改 seen / runs / published、不写提交信号、不等于授权总结，
  也不影响推 ZenMark。归档写失败只警告，不会让发布失败。
- `publish --dry-run` / `--check` 不产生归档；同一份稿子重复发布只覆盖同一文件，
  不会堆重复条目。
- 渲染用标准库：先 HTML 转义、链接只放行 http(s)，`javascript:` 等降级成纯文本；
  文件名 id 白名单校验挡路径穿越。

## 分批与总结

- 批内篇数 ≥15 就拆批，默认 20 篇通常拆 2 批；标题相近的文章聚成一簇，**同事件簇不跨批**
- 子代理**并发上限 3**
- 提示词：`prompts/summary.md`（融合版，日报版骨架 + 插件版软文判断与 few-shot 风格）
- `merge` 会全局连续重编号，并拦下破折号 `——` 和残留占位符

## 令牌与隐私

ZenMark 令牌在**本项目内独立配置一份**，与 `daily_digest_hub` 完全分开、互不读取：

```bash
cp config/secrets.example.json config/secrets.local.json
# 编辑 config/secrets.local.json，填入 zenmark_token
chmod 600 config/secrets.local.json
```

- `*.local.json` 已被 `.gitignore` 忽略，真值不会进 Git；仓库里只有不含真值的模板
- 运行时读 `config/settings.json` 里 `token_file` 指向的文件，可用环境变量 `ZENMARK_API_TOKEN` 覆盖（换机器或临时调试用）
- 令牌不打印、不写日志；本机已配置好，用 `python3 -m src.main doctor` 验证
- **本项目不读取 `daily_digest_hub` 的任何文件**，对方改目录、改令牌都不影响这边

## 界面变化怎么办（人介入）

GUI 固定用 `screencapture` + `sips` 缩到 1280×832 逻辑坐标 + `CGEventPost` 合成事件（本机没给辅助功能授权，这条路上一轮实测可用），**不做适配层、不自动重新识别**。

只要复制出来的不是 `mp.weixin.qq.com` 链接、或连着两篇拿到同一条链接（窗口没关掉 / 菜单位置变了），`grab` 立刻以退出码 3 报错，把已取到的链接留在 `out/_raw_urls_partial.json`，**不写任何台账**。请重新截图核对 `config/settings.json` 的 `ui.more_button` / `ui.copy_link` / `ui.close_window` 坐标后重跑。

## 与 daily_digest_hub 的关系

两个项目**各负责各的，不做任何跨项目去重或互查**，配置也各自独立。同一篇文章是否重复出现在两边简报里，由使用者自己控制。令牌各存一份：`daily_digest_hub` 用它自己的 `sources.yaml`，本项目用 `config/secrets.local.json`；**互不读取、不往回写**，任何一边换令牌或搬目录都不影响另一边。

## 目录结构

```text
wechat_digest/
├── src/                  # 全部标准库，零第三方依赖
│   ├── main.py           # CLI 路由
│   ├── config.py         # 配置加载
│   ├── gui.py            # 截屏 + CGEventPost + 剪贴板
│   ├── winpos.py         # 窗口几何（谁挡着微信、主窗口在哪）
│   ├── nav.py            # 自动进「公众号」页、卡片流 / 列表模式形态识别与互切
│   ├── feed_vision.py    # 标题带像素识别、卡片指纹、删除页判定
│   ├── grab.py           # 微信取链接（失败即人介入）
│   ├── ocr.py            # 本地文字识别（macOS Vision，只服务按号抓取）
│   ├── account.py        # 按号抓取：列表模式找号 → 会话页取头条链接
│   ├── article.py        # 元信息 / 正文 / 噪音清洗
│   ├── state.py          # 四层去重台账
│   ├── selection.py      # 选文清单
│   ├── blacklist.py      # 抓取黑名单（匹配 / 增删 / 过滤）
│   ├── quality.py        # 无标题 / 闲聊类文章过滤
│   ├── prompt.py         # 待总结包 / 拆批 / 合并校验
│   ├── publish.py        # ZenMark 推送
│   ├── archive.py        # 发布留底 + 受限 Markdown 渲染
│   ├── settings_ui.py    # 参数设置页数据层
│   └── review.py         # 本地服务：清单 / 设置 / 阅读三页
├── prompts/summary.md    # 融合提示词
├── config/settings.json          # 可调项（不含令牌）
├── config/blacklist.json         # 抓取黑名单（Git 跟踪）
├── config/secrets.example.json   # 令牌模板（不含真值，提交到 Git）
├── config/secrets.local.json     # 实际令牌（Git 忽略，权限 600）
├── docs/PRD.md
├── tests/
├── state/                # 台账（Git 忽略）
└── out/                  # 候选池 / 待总结包 / 批次 / 失败留底（Git 忽略）
```

## 环境说明

**纯 Python 3 标准库，无需 pip install，不依赖 `.venv`。** 脚手架创建 `.venv` 时 `ensurepip` 报错、清理又被系统安全策略拦住，本项目改用零依赖实现绕开（PRD 决策 10）。残留的 `.venv/` 已被 `.gitignore` 忽略，不用管它。

跑测试：

```bash
python3 -m unittest discover -s tests -v
```
