#!/bin/bash
# =============================================================
# run_trend_follow_daily_cron.sh — Hermes Cron 16:30 入口
# 职责：nohup 启动趋势跟随后台任务，立即返回（不阻塞 cron wrapper）
# 后台任务完成信号: 输出文件 data/trend_follow_YYYYMMDD.md 生成
# 用法: bash run_trend_follow_daily_cron.sh [YYYY-MM-DD]
# =============================================================
set -u

BASE="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS="${BASE}/scripts"
LOG_PREFIX="/tmp/trend_follow_daily"
DATE_ARG="${1:-$(date +%Y-%m-%d)}"
OUT_LOG="${LOG_PREFIX}_${DATE_ARG}.out"
ERR_LOG="${LOG_PREFIX}_${DATE_ARG}.err"
PID_FILE="${LOG_PREFIX}_${DATE_ARG}.pid"

# 防止重复运行
if [ -f "${PID_FILE}" ] && kill -0 "$(cat ${PID_FILE})" 2>/dev/null; then
    echo "⚠️ 已有趋势跟随任务在运行 (PID=$(cat ${PID_FILE}))，跳过本次启动"
    exit 0
fi

echo "[$(date '+%F %T')] 启动趋势跟随分析 ${DATE_ARG}" >> "${OUT_LOG}"

# nohup 后台执行，脱离 cron wrapper 进程组
nohup bash "${SCRIPTS}/run_trend_follow_daily.sh" "${DATE_ARG}" >> "${OUT_LOG}" 2>> "${ERR_LOG}" &
PID=$!
echo "${PID}" > "${PID_FILE}"
echo "[$(date '+%F %T')] 已启动 PID=${PID}" >> "${OUT_LOG}"
echo "STARTED ${PID}"