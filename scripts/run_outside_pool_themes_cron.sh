#!/bin/bash
# 18:30 池外板块主线归纳 cron 入口（2026-10-09 改为主营推导模式）
# 用法: bash run_outside_pool_themes_cron.sh [YYYYMMDD] [--no-refill]  默认当天；补跑显式传交易日
# 依赖: 先由 18:30 cron 第1/2步产出 data/pool_derivation_result_YYYYMMDD.json（Agent 主营推理结果）
set -u
cd "$(dirname "$0")/.." || exit 1

DATE_ARG="${1:-$(date +%Y%m%d)}"
DATE_ARG=$(echo "$DATE_ARG" | tr -d '-')
NO_REFILL="${2:-}"
LOG="/tmp/outside_pool_themes_${DATE_ARG}.out"
PIDF="/tmp/outside_pool_themes_${DATE_ARG}.pid"
RESULT="data/pool_derivation_result_${DATE_ARG}.json"

# 已有进程则退出（避免重复）
if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
    echo "EXISTS $(cat "$PIDF") 已在运行"
    exit 0
fi

# 推理结果缺失 → 兜底（设计 3.3：全部独立候选 + 推导不可用标记）
if [ ! -f "$RESULT" ]; then
    INPUT="data/pool_derivation_input_${DATE_ARG}.json"
    if [ ! -f "$INPUT" ]; then
        echo "ERROR: $RESULT 与 $INPUT 均不存在（数据准备失败，无法兜底）"
        exit 1
    fi
    echo "⚠️ $RESULT 不存在，启用兜底：从 $INPUT 生成全部独立候选 result"
    export FALLBACK_INPUT="$INPUT"
    export FALLBACK_RESULT="$RESULT"
    python3 -c "
import json, os
with open(os.environ['FALLBACK_INPUT'], encoding='utf-8') as f:
    data = json.load(f)
boards = data.get('boards', [])
for b in boards:
    b['derived_theme'] = ''
data['derivation_unavailable'] = True
with open(os.environ['FALLBACK_RESULT'], 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print(f'  兜底 result 已生成: {len(boards)} 个板块全部独立')
"
fi

nohup python3 scripts/outside_pool_themes.py "$DATE_ARG" --cluster-from "$RESULT" $NO_REFILL > "$LOG" 2>&1 &
echo $! > "$PIDF"
echo "STARTED $!"
