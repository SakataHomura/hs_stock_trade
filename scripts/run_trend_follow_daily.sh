#!/bin/bash
# =============================================================
# run_trend_follow_daily.sh — 每日16:30 主线级+板块级趋势跟随（闭环）
# 调用 fetch_trend.py：从 industry_themes 表读当日主线 → 概念映射代表股
# → 合成主线指数K线 → 趋势上下文 → 主线级交易建议；同时输出板块级趋势。
# 输出文件: data/trend_follow_YYYYMMDD.md
#
# 用法: bash run_trend_follow_daily.sh [YYYY-MM-DD]   (默认最新板块文件)
# =============================================================
set -u

BASE="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS="${BASE}/scripts"
OUT_DIR="${BASE}/data"
DATE_ARG="${1:-}"

if [ -n "${DATE_ARG}" ]; then
    LABEL=$(echo "${DATE_ARG}" | tr -d '-')
else
    PLATE_FILE=$(ls -t "${OUT_DIR}/plate"/plate_*.md 2>/dev/null | head -1)
    if [ -z "${PLATE_FILE}" ]; then
        PLATE_FILE=$(ls -t "${BASE}/data/plate"/plate_*.md 2>/dev/null | head -1)
    fi
    if [ -z "${PLATE_FILE}" ]; then
        echo "❌ 未找到任何板块统计文件" >&2
        exit 1
    fi
    LABEL=$(basename "${PLATE_FILE}" .md | sed 's/plate_//')
fi

OUT_FILE="${OUT_DIR}/trend_follow_${LABEL}.md"

echo "[$(date '+%F %T')] 趋势跟随分析 ${LABEL}，输出: ${OUT_FILE}"

{
    echo "# 📈 主线级+板块级趋势跟随 ${LABEL}"
    echo ""
    echo "> 生成时间：$(date '+%Y-%m-%d %H:%M:%S')"
    echo "> 数据来源：industry_themes 表（主线）+ 腾讯财经（K线）"
    echo ""
} > "${OUT_FILE}"

if [ -n "${DATE_ARG}" ]; then
    cd "${SCRIPTS}" && python3 fetch_trend.py --date "${DATE_ARG}" >> "${OUT_FILE}" 2>&1
else
    cd "${SCRIPTS}" && python3 fetch_trend.py >> "${OUT_FILE}" 2>&1
fi
RC=$?

echo "" >> "${OUT_FILE}"
if [ ${RC} -eq 0 ]; then
    echo "[$(date '+%F %T')] ✅ 趋势跟随完成，输出: ${OUT_FILE}" | tee -a "${OUT_FILE}"
else
    echo "[$(date '+%F %T')] ❌ 趋势跟随执行失败 (exit=${RC})，输出: ${OUT_FILE}" | tee -a "${OUT_FILE}"
fi
exit ${RC}