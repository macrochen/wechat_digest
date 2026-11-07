# wechat_digest

从**本地微信客户端「公众号」信息流**取最新 20 篇文章 → 广告过滤与四层去重 → 由当前 WorkBuddy 会话的 Hy4 分批总结成**一份**中文简报 → 推到 ZenMark 阅读器。

需求文档：[docs/PRD.md](docs/PRD.md)（v1.0 已确认）

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
python3 -m src.main select --all                     # 4. 人工选文（或 --ids 1,3,5）
python3 -m src.main hydrate                          # 5. 取正文 + 相似度去重 + 待总结包
python3 -m src.main batches                          # 6. 看分批方案
#    Hy4 / 子代理按 prompts/summary.md 写 out/_batches/batch-N.md
python3 -m src.main merge | python3 -m src.main publish --stdin   # 7+8. 合并并推送
```

其它：`publish --dry-run`（不发请求）、`publish --check`（只读验令牌）、
`grab --dry-run`（只校验坐标不点击）、`scroll --amount -4`（滚动信息流）。

## 四层审核去重

1. **URL 级** `state/seen.json` — 抓过的链接不再抓；发布成功才写入，失败可原样重试
2. **正文相似度** — 字符 5-gram Jaccard ≥ 0.75 判为转载，只留先出现的一篇，记录写在 `out/_dedup_log.json`
3. **人工选文** — 扫完停在候选池，勾选（或 `--all` 明确放行）才进总结；清单带 `inbox_generated_at`，候选池被重扫覆盖后自动判过期并整份都要，不静默丢篇
4. **幂等 + 发布哈希 + 认领锁** — `state/runs.json` 命中即停；简报 SHA256 比对 `state/published.json`，内容没变不重推；`state/lock.json` 防两个进程同时跑同一批（20 分钟超时）

未勾选的篇目**不会**下载正文（`scan` 阶段 `phase=scan` 只有元信息）。

## 分批与总结

- 批内篇数 ≥15 就拆批，20 篇通常拆 2 批；标题相近的文章聚成一簇，**同事件簇不跨批**
- 子代理**并发上限 3**
- 提示词：`prompts/summary.md`（融合版，日报版骨架 + 插件版软文判断与 few-shot 风格）
- `merge` 会全局连续重编号，并拦下破折号 `——` 和残留占位符

## 令牌与隐私

ZenMark 令牌**运行时只读** `~/workspace/daily_digest_hub/wechat/config/sources.yaml` 的 `settings.publish.token`。本项目不复制、不打印、不提交该值；路径可在 `config/settings.json` 的 `token_source` 改。

## 界面变化怎么办（人介入）

GUI 固定用 `screencapture` + `sips` 缩到 1280×832 逻辑坐标 + `CGEventPost` 合成事件（本机没给辅助功能授权，这条路上一轮实测可用），**不做适配层、不自动重新识别**。

只要复制出来的不是 `mp.weixin.qq.com` 链接、或连着两篇拿到同一条链接（窗口没关掉 / 菜单位置变了），`grab` 立刻以退出码 3 报错，把已取到的链接留在 `out/_raw_urls_partial.json`，**不写任何台账**。请重新截图核对 `config/settings.json` 的 `ui.more_button` / `ui.copy_link` / `ui.close_window` 坐标后重跑。

## 与 daily_digest_hub 的关系

两个项目**各负责各的，不做任何跨项目去重或互查**。同一篇文章是否重复出现在两边简报里，由使用者自己控制。本项目只从对方 `sources.yaml` 读令牌，不读它的 `seen.json` / `_published.json`，也不往回写。

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
│   ├── prompt.py         # 待总结包 / 拆批 / 合并校验
│   └── publish.py        # ZenMark 推送
├── prompts/summary.md    # 融合提示词
├── config/settings.json  # 可调项（不含令牌）
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
