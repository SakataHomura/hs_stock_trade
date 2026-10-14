#!/bin/bash
# 18:00 监控池主线龙头分析 cron 入口（环节6，2026-08-22 脚本化升级）
# 用法: bash run_faded_watch_leaders_cron.sh [YYYYMMDD]  默认当天；补跑显式传交易日
set -u
cd "$(dirname "$0")/.." || exit 1

DATE_ARG="${1:-$(date +%Y%m%d)}"
DATE_ARG=$(echo "$DATE_ARG" | tr -d '-')
LOG="/tmp/faded_watch_leaders_${DATE_ARG}.out"
PIDF="/tmp/faded_watch_leaders_${DATE_ARG}.pid"

# 已有进程则退出（避免重复）
if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
    echo "EXISTS $(cat "$PIDF") 已在运行"
    exit 0
fi

nohup python3 scripts/fetch_faded_watch_leaders.py "$DATE_ARG" > "$LOG" 2>&1 &
echo $! > "$PIDF"
echo "STARTED $!"