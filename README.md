# wechat_digest

> 从本地微信客户端抓取订阅公众号最新文章，经过滤去重后生成中文简报，并推送到 ZenMark 阅读器

> ⚠️ 当前状态：工程骨架已建，**需求文档待确认**。业务代码尚未开始。

## 项目简介

从本地微信客户端抓取订阅公众号最新文章，经过滤去重后生成中文简报，并推送到 ZenMark 阅读器

## 目录结构

```text
wechat_digest/
├── src/                # 核心业务源代码
│   ├── __init__.py
│   └── main.py         # 业务主入口（当前为占位）
├── tests/              # 单元测试与测试用例
│   ├── __init__.py
│   └── test_main.py
├── scripts/            # 运维脚本、自动化工具或辅助脚本
├── docs/               # 架构方案、设计文档及 API 文档
│   └── PRD.md          # 需求文档（先确认它，再写代码）
├── assets/             # 静态资源、图像或多媒体文件
├── requirements.txt    # 核心依赖清单
├── .gitignore          # Git 忽略配置
└── README.md           # 项目工程说明文档
```

## 开发顺序（不要跳步）

1. 填写 `docs/PRD.md`，把功能范围和"不做什么"写清楚。
2. 找用户确认。确认前不写业务代码、不装业务依赖。
3. 用户确认后，PRD 版本改 v1.0，单独提交一次。
4. 按 PRD 的"必须有"清单开始写代码。

## 环境准备与快速开始

本项目遵循虚拟环境隔离规范，严禁使用系统全局 Python 环境。

### 1. 激活虚拟环境

```bash
# 进入项目目录
cd ~/workspace/wechat_digest

# 激活本地虚拟环境 (Mac/Linux)
source .venv/bin/activate

# 激活本地虚拟环境 (Windows)
# .venv\Scripts\activate
```

### 2. 安装依赖

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 3. 运行项目

```bash
python src/main.py
```

### 4. 运行单元测试

```bash
python -m unittest discover -s tests
```
