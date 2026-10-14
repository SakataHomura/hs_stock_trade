#!/bin/bash
# =============================================================
# run_trend_leaders_daily.sh — 每日16:45 主线龙头股分析（主线维度）
# 板块和上榜股票只作为股票池来源，按 active 主线（industry_themes）聚类输出龙头。
# 由 fetch_theme_leaders.py 完成：主线读取 → 股票池构建 → 均线照妖镜验证 → 主线聚类报告。
#
# 用法: bash run_trend_leaders_daily.sh [YYYYMMDD]   (默认最新板块文件)
# =============================================================
set -u

BASE="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS="${BASE}/scripts"
DATA_DIR="${BASE}/data/plate"
OUT_DIR="${BASE}/data"
DATE_ARG="${1:-}"

# ---------- 1. 定位板块文件 ----------
if [ -n "${DATE_ARG}" ]; then
    # 兼容 2026-08-19 与 20260819 两种日期格式
    COMPACT_DATE=$(echo "${DATE_ARG}" | tr -d '-')
    PLATE_FILE="${DATA_DIR}/plate_${COMPACT_DATE}.md"
    LABEL="${COMPACT_DATE}"
else
    PLATE_FILE=$(ls -t "${DATA_DIR}"/plate_*.md 2>/dev/null | head -1)
    if [ -z "${PLATE_FILE}" ]; then
        echo "❌ 未找到任何板块统计文件 (${DATA_DIR}/plate_*.md)"
        exit 1
    fi
    LABEL=$(basename "${PLATE_FILE}" .md | sed 's/plate_//')
fi

OUT_FILE="${OUT_DIR}/theme_leaders_${LABEL}.md"

if [ ! -f "${PLATE_FILE}" ]; then
    echo "❌ 板块文件不存在: ${PLATE_FILE}"
    exit 1
fi
echo "[$(date '+%F %T')] 板块文件: ${PLATE_FILE}"
echo "[$(date '+%F %T')] 输出文件: ${OUT_FILE}"

# ---------- 2. 主线维度龙头分析 ----------
echo "[$(date '+%F %T')] 开始主线龙头分析（仅 active 主线）..."
cd "${SCRIPTS}" && python3 fetch_theme_leaders.py "${LABEL}" --top-n 10 --min-ltsz 300 > "${OUT_FILE}" 2>> "${OUT_DIR}/theme_leaders_${LABEL}.err"
RC=$?
if [ ${RC} -ne 0 ]; then
    echo "❌ fetch_theme_leaders.py 失败 (rc=${RC})，诊断见 ${OUT_DIR}/theme_leaders_${LABEL}.err"
    exit 4
fi

# ---------- 3. 主线龙头精筛（五层量价漏斗，每主线 5-8 只 + 可执行清单） ----------
echo "[$(date '+%F %T')] 开始主线龙头精筛（量价/买点/稳定性/动量/赔率五层漏斗）..."
REFINE_OUT="${OUT_DIR}/refine_theme_leaders_${LABEL}.md"
cd "${SCRIPTS}" && python3 refine_theme_leaders.py "${LABEL}" > "${REFINE_OUT}" 2>> "${OUT_DIR}/refine_theme_leaders_${LABEL}.err"
RC=$?
if [ ${RC} -ne 0 ]; then
    echo "❌ refine_theme_leaders.py 失败 (rc=${RC})（主报告已生成，精筛失败不阻断）"
else
    echo "[$(date '+%F %T')] ✅ 精筛完成，输出: ${REFINE_OUT}（含 '精筛汇总' 段）"
    echo ""
    tail -12 "${REFINE_OUT}"
fi

# ---------- 4. 完成 ----------
echo "[$(date '+%F %T')] ✅ 全部完成，输出: ${OUT_FILE} + ${REFINE_OUT}"
echo ""
echo "===== 主线龙头报告摘要（${OUT_FILE}） ====="
tail -8 "${OUT_FILE}"