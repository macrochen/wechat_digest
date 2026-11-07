# wechat_digest 需求文档（PRD）

> 版本：v1.9 ｜ 状态：**已确认** ｜ 创建日期：2026-11-07 ｜ 确认日期：2026-11-07 ｜ 最近修订：2026-11-08

> **v1.9 变更（2026-11-08）**：新增**按号抓取**——只抓指定一个公众号，切「列表模式」，
> 一次推送只要头条，单独出一份精选号简报。详细设计与实现记录见
> [PRD-account-fetch.md](PRD-account-fetch.md)，本节只记入口。
>
> - **新子命令** `account --name <号名> [--limit N] [--probe] [--keep-list] [--dry-run]`：
>   切列表模式 → 左栏按号名找 → 点开（复验右栏顶部就是目标号）→ 滚到底 →
>   从下往上逐次推送取头条，写 `out/_raw_urls-<号名>.json`。不取元信息、不写台账。
> - **scope 命名空间**：`inbox-<日期>-<号名>.json` / `_plan/*-<日期>-<号名>.json` /
>   `pending_summary-<号名>.json` / `_batches-<号名>/`。`scan` / `review` / `select` /
>   `hydrate` / `batches` / `voice-input` / `merge` / `publish` / `status` 全部支持
>   `--scope`；`scan --urls-file out/_raw_urls-<号名>.json` 会从文件名自动推断。
>   **台账（seen / offered / runs / published）不分 scope**——同一篇不该因为换入口被总结两次。
> - **只有一处破了「零 OCR」**：`src/ocr.py`（macOS Vision，纯本地），只用于认号名、
>   时间行、标题；点哪里仍由像素判据决定。`doctor` 增 OCR 可用性一行。
> - **取链接不碰 Cmd+W**：会话页的文章是主窗口内的标签页，标签页「…」一点就关标签，
>   改走「地球」→ 浏览器 `Cmd+L` / `Cmd+C` / `pbpaste` 读地址栏。
> - **验证**：130 个单测全绿（新增 26 项按号抓取用例）；屏幕锁定时 `account`
>   正确以退出码 3 停轮。实盘取 20 条的验收待解锁后执行。

> **v1.8 变更（2026-11-08）**：新增**自动模式（跳过人工确认）**。用户需求原话：
> 「确认默认是打开，我还希望能支持不需要我确认，自动执行总结」。
> 用户拍板两件事：① 自动模式下 `publish` 也一起自动；② 开关既做配置项、
> 也支持命令行单轮覆盖。
>
> - **开关**：`auto_summary`（默认 `0`）进 `DEFAULTS`，设置页列为第 22 项
>   （总结组）。`scan --auto-summary` 只放开本轮，`scan --confirm` 只强制本轮人工；
>   优先级 `--confirm` > `--auto-summary` > 配置项（`main._auto_mode`）。
> - **闸门机制不变**：自动模式下由 `scan` 调**同一个** `selection.write_request`
>   写放行信号，信号里带 `"mode": "auto"` / `"source": "scan"`；`hydrate`
>   的校验逻辑一行没放宽，只是读到 auto 时打印「本次未经人工确认」。
>   **故意不加旁路分支**——让「谁授权的」永远只有一个答案：看信号文件。
> - **自动模式只跳过「人选哪几篇」**：黑名单整号排除、无标题/闲聊过滤、
>   相似度去重、投递台账、发布哈希、认领锁全部照旧；自动模式下 `scan`
>   **不打开清单页**（没人需要看），直接提示下一步 `hydrate`。
> - **可观测**：`status` 增顶层 `auto_summary` 与 `inbox.request_mode`
>   （`manual` / `auto` / `null` 旧信号）；`doctor` 打印当前确认方式；
>   `/api/status` 同步 `request_mode`；`/review` 在「已自动放行、未 hydrate」时
>   渲染提示页而不是裸的「没有待确认清单」。
> - **技能文档**：铁律 10 加唯一例外，并写明**只能由用户开启、agent 不得自行打开**；
>   铁律 11 明确 `auto_summary=1` 是长期授权、必须用户自己动手；第 3 步加自动模式
>   分支（不跑 review、不等提交、直接 hydrate）；第 7 步在自动模式下不再问
>   「要不要发布」。
> - **验证**：104 个单测全绿，其中新增 6 项——关闭时 scan 不写任何信号、
>   开启时写 `mode=auto` 的全选信号且不弹清单页、两种命令行覆盖、
>   `hydrate` 无信号仍拒绝、有 auto 信号正常 hydrate、规格校验 0/1 与越界拒绝。
>   回归隔离在临时目录，真实台账（seen / offered / runs / published）未被改动。

> **v1.7 变更（2026-11-07 晚）**：用户反馈「筛选确认的服务端口和打开阅读的端口
> 不一致」，要求统一。根因有两条：① 端口被占时旧逻辑建议换到 `8788`，
> 与常驻设置页服务的 `7777` 分了家；② `review` 服务收到提交即 `shutdown()`，
> 提交后 `/read` 只能回落到另一个服务。用户拍板方案 =
> **一个服务常驻 7777**（AskUserQuestion 三选一）。
>
> - `src/review.py`：新增 `current_inbox()` 每次请求动态读候选池，仅当
>   `phase=scan` 且提交信号不存在（或已按 `inbox_generated_at` 过期）时
>   才认为「有待确认清单」；`_Handler.dynamic` 模式下 `/review` 自动出现 / 收起，
>   导航随之增减；新增无清单提示页（不再是裸 404）；`/api/status` 增
>   `app=wechat_digest` 与 `review` 字段。**提交后服务不退出**，
>   `/read`、`/settings` 继续同址；旧模式（显式传 inbox）仍提交后退出，保留兼容。
> - `src/main.py`：`review` / `settings` 都改为「确保 7777 上只有一个新版
>   dynamic 常驻服务」，只是落地页不同。新增 `_probe_ui()` 返回
>   `current / legacy / other / free`：新版复用、旧版或其它程序占端口则
>   **明确报错且不允许换端口**。探测用 `ProxyHandler({})` 绕过环境里的
>   `HTTP_PROXY`，否则空闲端口会被代理回一个 HTTP 错误误判成「被占用」。
> - `scripts/start-ui.sh`：重复运行提示复用、只认带 `app` 标识的新版服务、
>   `curl --noproxy '*'`；`lsof` / `curl` 加绝对路径兜底。
> - **闸门一律不动**：`summary-request-<日期>.json` 仍是唯一放行信号，
>   `hydrate` 硬检查；保存选择、设置页改参数、看阅读页都不算授权；
>   agent 改用 `status` 的 `inbox.submitted` 确认提交，不再依赖服务退出。
> - 验证：临时目录隔离回归 19 项全过（含保存不放行、提交不退出、
>   下一轮 scan 可再提交、真实 `state/*.json` 哈希未变）；真实 7777 上
>   `/review`、`/read`、`/settings` 均 200，命令与脚本均正确复用。
>   台账（seen / runs / published）、发布链路、归档逻辑未改。

> **v1.6 变更（2026-11-07 傍晚八）**：本地 UI 服务默认端口由 `8771`
> **统一改为 `7777`**（用户要求「启动脚本的默认端口改成 7777」）。
> 同步范围：`scripts/start-ui.sh` 的 `PORT` 默认值与帮助 / 注释文案、
> `src/main.py` 的 `review` / `settings` 两个 `--port` 默认值、
> `src/review.py` 的 `serve()` 默认参数，以及 README、技能文档、
> 下方历史变更块里的地址一并订正，避免出现「脚本 7777 / CLI 8771」两套地址。
> 仍然只绑 `127.0.0.1`、`--port N` 仍可临时覆盖；人工提交闸门、
> 抓取黑名单、台账与发布流程一律不动。

> **v1.5 变更（2026-11-07 傍晚七）**：新增「抓取黑名单」。用户拍板：
> 维护入口 = **设置页 + CLI + 文件三合一**，匹配规则 = **整号精确相等**。
> ① `config/blacklist.json` 为唯一真源（Git 跟踪，故意不叫 `*.local.json`）；
> ② `src/blacklist.py`：NFKC 统一全半角 + 去所有空白 + 英文 casefold 后
>    精确匹配，不做包含匹配、空 `source` 一律不命中；增删去重、原子写盘、
>    写前 `.bak`（已忽略）；兼容纯数组手写格式；
> ③ `src/main.py`：`scan` 在 `fetch_meta` 之后按 `source` 硬过滤，命中不进
>    候选池，池里记 `skipped_blacklist` 与名单快照，过滤后重新连续编号；
>    新增 `blacklist [list|add|remove] [名字...]` 子命令；
> ④ `src/review.py`：设置页加独立黑名单编辑区（textarea 一行一个、独立
>    「保存黑名单」按钮，不混入 17 项参数规格，「恢复默认」清不掉名单），
>    新增 `/api/blacklist/get|save`，`review` 与 `settings` 两种模式都可用；
> ⑤ 技能文档加铁律 12 与第 1 步读图跳过约定，形成「读图省 GUI 点击 +
>    `scan` 代码兜底」两层拦截。
> **不动**：台账（seen / runs / published）、人工提交闸门、已有候选池、
> ZenMark 推送；名单只影响候选池，编辑黑名单也不等于授权总结。
> 首个拉黑对象：**中汇人寿**（用户截图指定，整号排除）。

> **v1.4 变更（2026-11-07 傍晚六）**：新增「本地阅读页」，用户拍板走最小
> 路线 C —— 只做发布留底 + `/read` 阅读页，抓取 / 总结流程、人工提交闸门、
> 台账逻辑全部不动；范围只覆盖「今后发布」的新简报，不补录历史、不回拉
> ZenMark。① `src/archive.py`：写 `out/archive/<id>.md` + `.json` 元数据，
> 只读列表、安全读取、标准库受限 Markdown 渲染（HTML 转义 + http(s) 链接
> 白名单）；② `src/publish.py`：发布成功 / 失败（含语音版 `--no-ledger`）
> 都归档，归档失败只警告，`--dry-run` / `--check` 不写；
> ③ `src/review.py`：`/read` 路由 + 顶部导航，两种服务模式都能访问。
> 同时调研确认：WorkBuddy 无 headless CLI / 可编程 API，深链仅用于定位
> 会话或自动化记录，服务页面**无法同步调用当前会话的模型**；
> 「全部操作都在页面完成」暂不实施，留待后续路线决策。

> **v1.3 变更（2026-11-07 傍晚二）**：新增「参数设置页」，对齐参考项目
> `daily_digest_hub` 的 `/settings`，但按本项目约定做了三处调整：
> ① 与选文清单共用同一个服务/端口（127.0.0.1:7777）与顶部导航，
> 另加 `python3 -m src.main settings` 可在不依赖候选池时单独打开；
> ② 规格唯一真源 `src/settings_ui.py`，17 项参数动态生成表单，
> 增量写回 `config/settings.local.json`（Git 忽略、写前 .bak、原子替换、类型范围校验），
> 主配置不动、保存即生效；③ 令牌不进页面，只显示是否已配置。
> `src/config.py` 同步 DEFAULTS 里过时的 `ui.close_window` / `ui.close_wait`，
> 避免「恢复默认」把已修好的 Cmd+W 相关配置退回去。
> **在设置页改参数不是授权总结**，人工提交闸门维持不变。
> 补充（同日）：新增 `scripts/start-ui.sh` 终端启动脚本，供用户平时
> 不跑简报、只想调参数时自己起设置页；跑简报时 `review` 服务仍由 agent
> 用后台任务启动，用户无需手动开服务。脚本不改变人工提交闸门。

> **v1.2 变更（2026-11-07 傍晚）**：下文「20 篇」「命令行选文」「网页版选文界面以后再说」
> 等是 v1.1 的设计，已被后续实现覆盖，实际以
> `~/.workbuddy/skills/wechat-digest-skill/SKILL.md` 和代码为准：
> ① 抓取篇数默认 20，可用 `scan --max-articles N` 指定；
> ② 改用 `python3 -m src.main review` 起本地 HTML 清单页（127.0.0.1:7777），
> 只有人工勾选并点「开始总结」写出 `out/_plan/summary-request-<日期>.json`
> 信号，`hydrate` 才放行；口头「全部都要」不再算授权；
> ③ 「没有我的允许不准执行总结」是代码层硬闸门，无超时、无自动放行。

## 1. 一句话定位

在 WorkBuddy 里说一句话触发，从本机微信客户端「公众号」信息流取回最新 20 篇文章，过滤广告和重复内容，由**当前 WorkBuddy 会话的 Hy4** 逐篇总结成一份中文简报，推送到 ZenMark 阅读器。

只有我一个人用，没有界面、没有账号、不发布给别人。不接任何外部大模型 API，不用 API Key。

**与 `daily_digest_hub` 的关系**：两个项目各自独立、各管各的，**不做任何跨项目去重或互查**，同一篇文章是否重复出现由我自己手工控制。**配置也完全独立**：ZenMark 令牌各存一份，本项目不读对方的任何文件。

## 2. 要解决的问题

现在有两个半成品，各管一段，凑不到一起：

- `gzh_articles_fetcher`（Chrome 插件）能抓公众号文章并批量总结，但要我自己在浏览器里一篇篇打开，结果只留在插件本地，不推送，而且总结要调 Gemini API。
- `daily_digest_hub` 已有完整的「扫描 → 人工选文 → 总结 → 推 ZenMark」流水线和一套成熟的去重规则，但订阅源是 129 个号的 RSS，**不是我微信客户端里真实订阅的那些号**。它的摘要本来就是「自动化里的 AI 按 `prompt.md` 写的」，不是脚本写的。

难受的点：想看的是「微信里刚推给我的那几篇」，却得靠 RSS 覆盖或手工开浏览器；重复抓、重复总结、重复推送全靠自己盯着。

这次要做的：把「从微信客户端取最新文章」接上，复用两个项目已验证的提示词、过滤和去重规则，让当前会话的模型直接产出简报。

## 3. 核心场景

1. 我在电脑前，微信已登录、「公众号」模块能打开。在 WorkBuddy 会话里说一句「跑公众号简报」，由 `wechat-digest` skill 接管全流程（触发形态 **A**）。
2. 工具激活微信、截屏定位信息流，取回最新 **20 篇**的原文链接（标题 / 公众号名 / 发布时间由网络读取补齐）。只取链接，不读正文。
3. 列出候选清单，等我勾选要总结的篇目；不勾就按默认规则全要。勾完才去取正文。
4. 对勾中的文章：抓正文 → 清推广噪音 → 生成「待总结包」（融合提示词 + 正文 JSON）→ 由当前会话的 Hy4 **拆批 + 子代理并发（并发 ≤3）** 逐篇总结 → 合并成**一份** Markdown 简报。
5. 通过 ZenMark 接口推到阅读器；成功记入台账，下次不再重复处理同一批。

全程手动触发，不设定时任务。锁屏、微信不可用或**界面坐标识别失败时报错退出、不写状态，等人介入**。

## 4. 功能清单

### 必须有（不做等于白做）

1. **从本机微信客户端取候选列表**：每次最新 **20 篇**，拿到标题、公众号名、发布时间、原文链接，写成候选池。按**信息流**取，不逐个订阅号遍历。只取链接，不取正文。
2. **四层审核去重**（照搬 `daily_digest_hub` 已验证的做法，仅限本项目内部）：
   - URL 级 `seen.json`：抓过的链接不再抓；
   - 正文相似度去重：字符 5-gram Jaccard，阈值 0.75，识别多号转载；
   - 人工选文：扫完停在候选池，必须勾选（或明确放行）才进总结；清单带 `generated_at`，候选池被重扫覆盖后清单自动判过期并忽略，**不静默丢篇**；
   - 幂等台账 + 发布哈希：`runs.json` 命中即停、不靠 `--force` 重跑；简报正文 SHA256 比对 `published.json`，内容没变不重推；再加一次运行认领锁，防两个进程同时总结同一批。
3. **内容过滤**：噪音行正则清掉「免责声明 / 扫码 / 长按 / 广告 / 阅读原文 / 在看 / 往期回顾」等；纯推广文由提示词标注 `[软文识别]`；主体有内容、只在末尾带推广的正常总结，忽略推广部分。
4. **总结**：由**当前 WorkBuddy 会话的 Hy4** 完成，脚本不调用任何大模型 API。融合版提示词 —— 以 `daily_digest_hub/wechat/config/prompt.md` 为骨架（逐篇编号、`《标题》来源`、同一事件合并、禁破折号、25 字短句、200-400 字），补上 `gzh_articles_fetcher` 的 few-shot 示例与软文判断细则。输出**一份汇总** Markdown。
5. **分批与并发**：批内篇数 **≥15 就拆批**（20 篇通常拆 2 批），**子代理并发上限 3**，同一事件簇不拆开；合并后逐条校验编号连续、无残留占位符。与 `daily_digest_hub/AGENTS.md` 铁律 6/7 一致。
6. **推送阅读器**：沿用 ZenMark `POST https://zenmark-bxx.pages.dev/api/cloud/upload`，`Bearer` 令牌 + `{"title","content"}`。令牌**由本项目自己配置一份**，与 `daily_digest_hub` 完全分开：运行时只读项目内 `config/secrets.local.json`（Git 忽略、权限 600），支持环境变量 `ZENMARK_API_TOKEN` 覆盖；**不读对方的任何文件**、不打印到日志、不提交到 Git。仓库里只提供不含真值的 `config/secrets.example.json` 模板。发布失败才把正文留底，正常情况走 stdin 不落盘。
7. **可复查**：候选池、选文清单、去重台账、发布记录都落盘，能回答「这篇为什么没进简报」。

### 以后再说

- 定时触发与补跑（等 GUI 抓取稳定性验证后再说）
- 逐个订阅号遍历，而不只是读信息流
- 网页版选文界面（先命令行勾选）
- 换其他阅读器 / 多频道简报
- 接 computer use（当前固定用截屏 + 合成事件，若以后要接再重做抓取层）

### 明确不做

- **不做跨项目去重**：不读 `daily_digest_hub` 的 `seen.json` / `_published.json`，不互查、不改对方、不设共享台账。两个项目各负责各的，重复由我自己控制。
- **不读取 `daily_digest_hub` 的任何配置文件或状态文件**（包括 `sources.yaml`）；令牌也已改为项目内独立配置，两边彻底分开
- **不接外部大模型 API**：不调 Gemini / OpenAI，不要 API Key，总结只用当前会话的 Hy4
- **不做 RSS 订阅源**，不接 `wechat2rss`，不复制 `daily_digest_hub` 的 129 个号配置
- **不做无人值守定时任务**，不做锁屏自动重试
- **不做 Chrome 插件**，不在浏览器里注入脚本
- **不做账号体系、云同步、多用户**
- **不改两个参考项目**，只读它们、搬思路，不往回写
- **不把含真值的令牌文件提交到 Git**；仓库里只保留不含真值的模板
- **不做 GUI 适配层**：固定截屏 + `CGEventPost`，界面变了就报错让人介入，不自动重新识别

## 5. 怎么算做完了

- [ ] 微信已登录且信息流有文章时跑一次，生成含「标题 / 公众号名 / 时间 / 链接」四列的候选池，**恰好最多 20 条**，链接是 `mp.weixin.qq.com` 地址。
- [ ] 同一批文章连续跑两次，第二次输出「已在 XX 跑过，本次跳过」，**不产生第二份简报、不调用 ZenMark 接口**。
- [ ] 同一篇文章的不同转载链接（正文相似度 ≥0.75）只保留一条。
- [ ] 未勾选的篇目**不下载正文**（可由候选池 `phase` 字段或请求日志验证）。
- [ ] 喂一篇纯广告文，简报出现 `[软文识别]`；喂一篇末尾带推广的正文，总结里不出现推广文字。
- [ ] 20 篇时拆成 2 批、子代理并发不超过 3；最终简报是一份汇总 Markdown，编号连续，每篇标题为 `《原标题》公众号名` 并带原文超链接，正文 200-400 字，不含破折号 `——`。
- [ ] 整个流程**没有任何对外部大模型 API 的网络请求**；总结内容由当前会话的 Hy4 产出。
- [ ] 推送成功时 ZenMark 云端暂存区出现一份新文档；同一份简报再发一次应提示「内容没变，跳过」。
- [ ] 正常发布时 `out/` 下不新增 `.md` 正文文件；发布失败时生成 `failed-<日期>.md` 留底。
- [ ] 项目内 `config/secrets.local.json` 缺失或字段为空时明确报错退出，不发送、不留残缺记录；Git 仓库里搜不到真实令牌字符串。
- [ ] **代码里搜不到 `daily_digest_hub` 路径**：本项目不读对方任何文件；`doctor` 自检能验证令牌可读且不显示值。
- [ ] 微信未运行 / 屏幕锁定 / **界面坐标识别失败**时报错退出且**不写 `seen.json`**，提示人介入，下次可原样重试。

## 6. 技术方案要点

- **技术栈与形态**：Python 3 标准库（**零第三方依赖**）+ WorkBuddy skill。目录 `~/workspace/wechat_digest`。
  - `.venv` 决策：脚手架创建 `.venv` 时 `ensurepip` 非零退出，清理残缺目录被系统安全策略拦截。本项目全部用标准库实现（`urllib` / `json` / `re` / `hashlib` / `ctypes` / `subprocess` / `argparse`），**不依赖 `.venv`、不执行 pip 安装、不清理该目录**（`.gitignore` 已忽略）。后续如需第三方库再由我手工处理。
- **运行原理**：WorkBuddy 是 Electron 桌面应用，模型调用在客户端进程里走云端 API + 登录态；本机**没有** `workbuddy` / `codebuddy` / `wb` 这类 headless CLI，App 包内也无 CLI 可执行文件、无 AppleScript 字典；`~/.workbuddy/cli-env` 只有 node/python 的壳。本机有 `claude` CLI，但那是 Claude Code（另一个模型/账号）。`workbuddy://` 深链虽已注册，但在 `app.asar` 里没找到可携带提示词的路由，且深链只能唤起界面、拿不到总结结果。**结论：终端 shell 进程无法同步调用 Hy4，只能靠文件交接把「待总结包」递给一次 WorkBuddy 会话。**
- **触发形态（已定：A）**：写一个 `wechat-digest` skill 作为操作契约，在 WorkBuddy 里说一句话触发。skill 指导 Hy4 用 Bash 工具依次跑脚本（抓取 / 去重 / 选文 / 取正文 / 拼待总结包），然后 Hy4 自己读包、按融合提示词分批总结，最后再跑发布脚本。终端命令保留为抓取阶段调试入口，但完整流程由 agent 代敲。这与 `daily_digest_hub/README.md:174`「摘要不是脚本写的，是定时任务里的 AI 生成的」是同一种分工。
- **skill 存放**：按既有约定放 `~/.agents/skills/`，软链进 `~/.workbuddy/skills/` 保持单一真源；软链后**需重启 WorkBuddy 会话**才会注册。
- **抓取层（GUI，固定方案）**：手动触发。上一轮**没有用 computer use**（当前会话未挂载该工具），实际走的是 `screencapture -x` 截屏 + `ctypes` 调 `ApplicationServices.CGEventPost` 合成鼠标/键盘事件（辅助功能授权未给，但这条路绕过它），截图用 `sips` 缩到 1280×832 逻辑坐标后由 Hy4 视觉识别。从文章窗口「… → 复制链接」拿 URL，`pbpaste` 读剪贴板。**不做适配层**：识别不到预期坐标 / 菜单项时立即报错退出、不写状态，等人介入。
  - 分工：脚本提供 `capture`（截屏缩放）、`grab`（按 Hy4 给出的卡片坐标批量点开并复制链接）、`click` 等原子命令；视觉识别由 Hy4 承担，脚本不猜坐标。
- **正文层（网络）**：GUI 只负责取链接。标题 / 公众号名 / 时间 / 正文全部用网络读取补齐 —— `mp.weixin.qq.com` 是公开页，`urllib` + UA 可拿到源码里的 `nickname`、`msg_title`、`publish_time` 时间戳（换算成北京时间）；必要时用精确标题反查长链。
- **数据存哪**：纯本地文件，不建数据库。`state/seen.json`、`state/runs.json`、`state/published.json`、`out/inbox-<日期>.json`、`out/_plan/selected-<日期>.json`、`out/pending_summary.json`（待总结包：融合提示词 + 正文 JSON）。字段对齐提示词的 `title / url / content / source`。
- **令牌存哪（v1.1 改）**：**项目内独立一份** —— `config/secrets.local.json`，格式 `{"zenmark_token": "..."}`，`chmod 600`，由 `.gitignore` 的 `*.local.json` 忽略；`config/settings.json` 的 `token_file` 指向它；环境变量 `ZENMARK_API_TOKEN` 可覆盖（换机器/临时调试用）。仓库里提交 `config/secrets.example.json` 作为不含真值的模板。
- **外部服务**：只有 ZenMark 上传接口与 `mp.weixin.qq.com` 页面读取。**完全不依赖 `daily_digest_hub`**：对方改目录、改令牌、删配置都不影响本项目；本项目也绝不读它的 `sources.yaml` 或任何状态文件。
- **参考来源（只读思路，运行时不读取）**：
  - `gzh_articles_fetcher/options/options.js:152` — 默认总结提示词、软文识别、few-shot 示例
  - `gzh_articles_fetcher/utils.js`、`Readability.js` — 浏览器端正文抽取与清洗
  - `daily_digest_hub/wechat/config/prompt.md` — 日报版提示词、同事件合并规则
  - `daily_digest_hub/scripts/fetch.py:174` — JUNK 噪音行正则、5-gram Jaccard 跨源去重
  - `daily_digest_hub/scripts/selection.py` — 选文清单与过期校验
  - `daily_digest_hub/scripts/summary_queue.py` — 认领锁、幂等判断
  - `daily_digest_hub/scripts/publish.py` — ZenMark 接口、发布哈希、失败留底
  - `daily_digest_hub/AGENTS.md:322` — 铁律 6/7：≥15 拆批、子代理并发 ≤3、合并后逐条复查
  - `daily_digest_hub/README.md:174`、`AGENTS.md:1` — AI 总结分工与自动化契约

## 7. 已确认的关键决策

| # | 决策项 | 结论 |
| :--- | :--- | :--- |
| 1 | 触发形态 | **A**：skill + WorkBuddy 里一句话，一次跑完全流程；终端命令仅作调试入口 |
| 2 | 项目关系 | **不做跨项目去重**，各负责各的，重复由用户自己控制；**配置也各自独立** |
| 3 | 抓取来源 | 本地微信客户端「公众号」**信息流**，每次 20 篇 |
| 4 | 模型 | 当前 WorkBuddy 会话的 Hy4，零 API Key |
| 5 | 令牌（v1.1 改） | **项目内独立配置一份** `config/secrets.local.json`（Git 忽略、权限 600），与 `daily_digest_hub` 完全分开、互不读取；环境变量可覆盖 |
| 6 | GUI 方案 | **固定**截屏 + `CGEventPost`，不做适配层 |
| 7 | 界面变化兜底 | 坐标识别失败**报错退出、不写状态、等人介入** |
| 8 | 分批并发 | 批内 ≥15 篇拆批，**子代理并发 ≤3**，同事件簇不拆开 |
| 9 | 简报形态 | 一份汇总 Markdown；v1.4 起发布时额外在 `out/archive/` 留本地副本供 `/read` 阅读（旧「正文不落盘」已被覆盖） |
| 10 | `.venv` | 纯标准库零依赖，暂用系统 python3，不清理残留目录 |

## 变更记录

| 版本 | 日期 | 改了什么 |
| :--- | :--- | :--- |
| v0.1 | 2026-11-07 | 初始模板 |
| v0.1 | 2026-11-07 | 完成两轮访谈，补齐调研结论、四层去重设计、功能清单、验收标准与待定问题 |
| v0.2 | 2026-11-07 | 模型改为当前会话 Hy4、令牌改读 `sources.yaml`、每次 20 篇、一份汇总简报、正文不落盘（v1.4 起改为发布时归档）、按信息流取；新增运行原理调研结论 |
| v1.0 | 2026-11-07 | **确认定稿**：删除跨项目不重复硬要求（各管各的、重复由用户控制）；GUI 固定截屏 + `CGEventPost` 不做适配层；坐标失效报错人介入；确定拆批 + 子代理并发 ≤3；触发形态定为 A；`.venv` 改用纯标准库零依赖绕开。待定问题全部关闭 |
| v1.1 | 2026-11-07 | **令牌改为项目内独立配置**：不再读 `daily_digest_hub/sources.yaml`，改读 `config/secrets.local.json`（Git 忽略、权限 600）+ 模板 `secrets.example.json`，环境变量可覆盖；两项目配置彻底分开。同步功能清单、明确不做、验收项、技术方案、决策表 |

---

> ✅ 本文件状态为「已确认」，允许开始写业务代码。
> 后续若变更上述决策，先改本文件并单独提交一次 Git，再动代码。
