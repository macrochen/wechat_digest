#!/usr/bin/env bash
# 一键启动本地 UI 服务（只绑 127.0.0.1）。
#
# v1.7 起全项目只有「一个常驻服务、一个端口」：默认 7777。
#   http://127.0.0.1:7777/review    选文清单（候选池处于 scan 且未提交时自动出现）
#   http://127.0.0.1:7777/read      本地阅读
#   http://127.0.0.1:7777/settings  参数设置
# 在清单页点「开始总结」后服务**不会退出**，阅读页继续同址可用；Ctrl+C 结束。
#
# 什么时候需要你手动启动？
#   · 平时只想调参数或看往期简报 → 终端跑一下，服务常驻。
#   · 跑「公众号简报」全流程时不用管：agent 会复用这个服务（没起才代起一个），
#     你只要在 /review 勾选文章并点「开始总结」。
#   · 重复运行本脚本不会再起第二个进程，会提示复用现有服务。
#
# 用法：
#   ./scripts/start-ui.sh                    # 默认落地设置页，端口 7777
#   ./scripts/start-ui.sh --review          # 落地选文清单页
#   ./scripts/start-ui.sh --port N          # 换端口（三页一起换，仍是同一个地址）
#   ./scripts/start-ui.sh --no-open         # 不自动打开浏览器
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

MODE="settings"
PORT="7777"
NO_OPEN="0"

usage() {
  cat <<'EOF'
用法：start-ui.sh [--review] [--port 端口] [--no-open]

  默认        起常驻服务并打开设置页；三页始终同一个端口
  --review    起常驻服务并打开选文清单页（没有待确认候选池时给提示）
  --port N    指定端口，默认 7777；改动后 /review、/read、/settings 一起换
  --no-open   不自动打开浏览器
  -h/--help   显示本帮助

  选文清单 / 本地阅读 / 参数设置是同一个服务，提交后服务不退出。
  在设置页改参数不等于授权总结，必须到选文清单点「开始总结」。
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --review)  MODE="review"; shift ;;
    --port)
      [[ $# -ge 2 ]] || { echo "× --port 后面要跟端口号" >&2; exit 2; }
      PORT="$2"; shift 2 ;;
    --no-open) NO_OPEN="1"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "× 未知参数：$1" >&2; usage; exit 2 ;;
  esac
done

[[ "$PORT" =~ ^[0-9]+$ ]] || { echo "× 端口号必须是数字：$PORT" >&2; exit 2; }
(( PORT >= 1 && PORT <= 65535 )) || { echo "× 端口超出范围：$PORT" >&2; exit 2; }

# 优先用项目虚拟环境，避免系统 python3 缺依赖（终端 PATH 可能指向别的解释器）。
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="$(command -v python3 || true)"
  [[ -n "$PY" ]] || { echo "× 找不到 python3，请先创建 .venv 或安装 Python 3" >&2; exit 1; }
fi

# 端口已被新版常驻服务占着就复用，别再起第二个进程让端口分家。
# 旧版服务（没有选文清单）或其它程序占着则停下来提示，绝不换端口凑合。
LSOF="$(command -v lsof || true)"
[[ -n "$LSOF" ]] || LSOF="/usr/sbin/lsof"
CURL="$(command -v curl || true)"
[[ -n "$CURL" ]] || CURL="/usr/bin/curl"

if [[ -x "$LSOF" ]] && \
   "$LSOF" -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  if [[ -x "$CURL" ]] && \
     "$CURL" --noproxy '*' -fsS --max-time 1 \
       "http://127.0.0.1:$PORT/api/status" 2>/dev/null | \
       grep -q '"app"'; then
    echo "· 端口 $PORT 上已有本项目常驻服务，直接复用，不另起第二个进程。"
    echo "  选文清单：http://127.0.0.1:$PORT/review"
    echo "  本地阅读：http://127.0.0.1:$PORT/read"
    echo "  参数设置：http://127.0.0.1:$PORT/settings"
    exit 0
  fi
  echo "× 端口 $PORT 被旧版本地服务或其它程序占用。"
  echo "  如果是本项目的旧版设置页服务，请在它的终端按 Ctrl+C 结束后重跑本脚本。"
  echo "  查占用：  $LSOF -nP -iTCP:$PORT -sTCP:LISTEN"
  echo "  不要换端口：选文清单 / 本地阅读 / 参数设置必须同址。"
  exit 1
fi

echo "· 项目目录：$ROOT"
echo "· 解释器：  $PY"
if [[ "$MODE" == "review" ]]; then
  PAGE="review"
  echo "· 模式：    落地选文清单页（三页同一端口，提交后服务不退出）"
else
  PAGE="settings"
  echo "· 模式：    落地设置页（三页同一端口，改动保存即写 config/settings.local.json）"
fi
echo "· 地址：    http://127.0.0.1:$PORT/$PAGE"
echo "· 结束：    在这个窗口按 Ctrl+C"
echo

if [[ "$NO_OPEN" == "1" ]]; then
  exec "$PY" -m src.main "$MODE" --port "$PORT" --no-open
else
  exec "$PY" -m src.main "$MODE" --port "$PORT"
fi
