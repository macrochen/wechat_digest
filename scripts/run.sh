#!/usr/bin/env bash
# 调试入口：把命令转发给 CLI，自动切到项目根目录。
# 日常完整流程请用 WorkBuddy 的 wechat-digest skill，不用这个脚本。
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 -m src.main "$@"
