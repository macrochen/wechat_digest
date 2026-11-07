# wechat_digest

从**本地微信客户端「公众号」信息流**取文章（默认 20 篇，可指定数量）→ **黑名单整号排除** + 广告过滤与四层去重 → **HTML 清单人工勾选并提交** → 由当前 WorkBuddy 会话的 Hy4 分批总结成**一份**中文简报 → 推到 ZenMark 阅读器，并在本地服务里留一份可离线阅读的副本。

需求文档：[docs/PRD.md](docs/PRD.md)（v1.0 已确认，顶部有 v1.2 ~ v1.6 变更说明）

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
python3 -m src.main review                           # 4. 起 HTML 清单，等人工勾选并点「开始总结」
python3 -m src.main hydrate                          # 5. 取正文 + 相似度去重 + 待总结包
python3 -m src.main batches                          # 6. 看分批方案
#    Hy4 / 子代理按 prompts/summary.md 写 out/_batches/batch-N.md
python3 -m src.main merge | python3 -m src.main publish            # 7+8. 合并并推送
```

其它：`python3 -m src.main settings`（同一个常驻服务，落地设置页）、
`python3 -m src.main blacklist`（查看抓取黑名单）、
`scan --max-articles 10`（本轮只抓 10 篇，默认 20）、
`review --no-open`（不自动开浏览器，仍是 7777 同一个服务）、`publish --dry-run`（不发请求）、
`publish --check`（只读验令牌）、`grab --dry-run`（只校验坐标不点击）、
`scroll --amount -4`（滚动信息流）、`status`（看 `inbox.submitted` 是否已提交）。

## 四层审核去重

1. **URL 级** `state/seen.json` — 抓过的链接不再抓；发布成功才写入，失败可原样重试
2. **正文相似度** — 字符 5-gram Jaccard ≥ 0.75 判为转载，只留先出现的一篇，记录写在 `out/_dedup_log.json`
3. **人工选文（强制闸门）** — `review` 确保 `127.0.0.1:7777` 上有一个常驻服务
   （`/review` / `/read` / `/settings` 三页同址，提交后服务不退出）；
   只有点了「开始总结」才写 `out/_plan/summary-request-<日期>.json` 信号，
   `hydrate` 硬检查它，没提交直接拒绝执行。清单与信号都带 `inbox_generated_at`，
   候选池被重扫覆盖后自动作废、需重新提交。口头「全部都要」**不算授权**，
   没有超时、没有自动放行
4. **幂等 + 发布哈希 + 认领锁** — `state/runs.json` 命中即停；简报 SHA256 比对 `state/published.json`，内容没变不重推；`state/lock.json` 防两个进程同时跑同一批（20 分钟超时）

未勾选的篇目**不会**下载正文（`scan` 阶段 `phase=scan` 只有元信息）；没提交则一篇正文都不会取。

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

## 参数设置页

### 谁来启动服务

**v1.7 起一个服务、一个端口**：选文清单 `/review`、本地阅读 `/read`、
参数设置 `/settings` 始终都是 `127.0.0.1:7777`，共用顶部导航。
候选池 `phase=scan` 且未提交时清单自动出现，提交后清单收起但**服务不退出**，
阅读页地址不变。端口上已有新版服务会直接复用，不再起第二个进程；
旧版服务或其它程序占着会明确报错，按提示释放端口，**不要换端口**。

| 场景 | 服务由谁启动 | 你要做什么 |
| --- | --- | --- |
| 跑「公众号简报」全流程 | **agent 用后台任务起或复用**常驻服务 | 只需在浏览器勾选文章、点「开始总结」；服务不退出，**不用你手动启动任何东西** |
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
写出信号，`hydrate` 才放行；agent 用 `status` 的 `inbox.submitted` 确认提交，
不再依赖服务退出。

页面按 `src/settings_ui.py` 的规格动态生成 17 项参数，分抓取 / 去重 / 总结 /
发布 / 界面五组；含 `ui.*` 的按钮坐标与等待时长。只提交改动过的键，带类型与范围
校验，写回 `config/settings.local.json`（Git 已忽略），写前 `.bak` 备份 + 原子替换；
「恢复默认」清掉覆盖项回到主配置。主 `config/settings.json` 不被页面改动。

优先级：`DEFAULTS` < `settings.json` < `settings.local.json`，每条命令重读配置，
**保存即生效、无需重启**；命令行参数更高（`scan --max-articles 10` 只覆盖本次）。
同一页下方还有独立的「抓取黑名单」编辑区（一行一个公众号名、单独保存，
写 `config/blacklist.json`），不属于这 17 项参数，「恢复默认」不会清空名单，
详见[抓取黑名单](#抓取黑名单)。

令牌内容不进页面，只显示是否已配置。**在设置页改参数不等于授权总结**，
闸门仍然只认清单页的「开始总结」。

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
│   ├── grab.py           # 微信取链接（失败即人介入）
│   ├── article.py        # 元信息 / 正文 / 噪音清洗
│   ├── state.py          # 四层去重台账
│   ├── selection.py      # 选文清单
│   ├── blacklist.py      # 抓取黑名单（匹配 / 增删 / 过滤）
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
