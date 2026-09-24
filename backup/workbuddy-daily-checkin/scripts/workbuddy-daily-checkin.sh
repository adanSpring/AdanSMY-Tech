#!/bin/bash
# WorkBuddy 每日自动签到 · 包装脚本
# 被 WorkBuddy 自动化以固定命令名调用，已加入 ~/.workbuddy/settings.json
# 的 sandbox.excludedCommands，使其能读取本机登录态文件
# （该路径在沙箱内置 denyRead 列表中，普通命令读不到）。
PY="/Users/ajiang/.workbuddy/binaries/python/versions/3.13.12/bin/python3"
exec "$PY" "$HOME/.workbuddy/scripts/workbuddy_checkin.py" "$@"
