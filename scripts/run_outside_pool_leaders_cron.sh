#!/bin/bash
# 18:45 池外候选主线个股筛选 cron 入口（2026-08-24 从18:00池外提示拆分独立）
# 依赖: 18:30 outside_pool_themes.py 已生成 data/outside_pool_themes_YYYYMMDD.json
# 用法: bash run_outside_pool_leaders_cron.sh [YYYYMMDD]  默认当天；补跑显式传交易日
set -u
cd "$(dirname "$0")/.." || exit 1

DATE_ARG="${1:-$(date +%Y%m%d)}"
DATE_ARG=$(echo "$DATE_ARG" | tr -d '-')
LOG="/tmp/outside_pool_leaders_${DATE_ARG}.out"
PIDF="/tmp/outside_pool_leaders_${DATE_ARG}.pid"

# 已有进程则退出（避免重复）
if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
    echo "EXISTS $(cat "$PIDF") 已在运行"
    exit 0
fi

nohup python3 scripts/outside_pool_leaders.py "$DATE_ARG" > "$LOG" 2>&1 &
echo $! > "$PIDF"
echo "STARTED $!"