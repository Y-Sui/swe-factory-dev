#!/bin/bash
# Combine dataset + model predictions + eval results into a single JSON per model.
#
# Usage: bash run/combine_results.sh

set -euo pipefail

DATASET="internal-swe-bench-data/results_v1_gpt_5_2_68_20260307_verified.json"

# Opus
python3 scripts/combine_results.py \
    --dataset "$DATASET" \
    --preds "internal-swe-bench-data/results/anthropic_claude_opus_4_6/preds.json" \
    --eval-dir "eval_output/opus_eval/anthropic__claude-opus-4.6/"

# Qwen
python3 scripts/combine_results.py \
    --dataset "$DATASET" \
    --preds "internal-swe-bench-data/results/qwen_qwen3_5_35b_a3b/preds.json" \
    --eval-dir "eval_output/qwen_eval/qwen__qwen3.5-35b-a3b/"
