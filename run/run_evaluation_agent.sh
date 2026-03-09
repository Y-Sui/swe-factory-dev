#!/usr/bin/env bash
# Evaluate agent predictions against the benchmark
# Usage: ./run/run_evaluation_agent.sh <preds_dir> <run_id> [extra_args...]
set -euo pipefail

PREDS_DIR="${1:?Usage: $0 <preds_dir> <run_id>}"
RUN_ID="${2:?Usage: $0 <preds_dir> <run_id>}"
DATASET="/home/yuansui/swe-factory-dev/internal-swe-bench-data/results_v1_gpt_5_2_68_20260307_verified.json"
OUTPUT="./eval_output"
shift 2

python3 evaluation/run_evaluation.py \
    --dataset_path "$DATASET" \
    --mode evaluate \
    --preds_dir "$PREDS_DIR" \
    --run_id "$RUN_ID" \
    --output_path "$OUTPUT" \
    --max_workers 10 \
    "$@"

# Examples:
# ./run/run_evaluation_agent.sh /home/yuansui/mini-swe-agent/results/anthropic_claude_opus_4_6 opus_eval
# ./run/run_evaluation_agent.sh /home/yuansui/mini-swe-agent/results/qwen_qwen3_5_35b_a3b qwen_eval
