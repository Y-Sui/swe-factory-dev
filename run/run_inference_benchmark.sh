#!/bin/bash
set -euo pipefail

DATASET="/data/yuansui/internal-swe-bench-data/results_v1_gpt_5_2_68_20260307_verified.json"

# Stage 1: Build images once
bash run/run_inference.sh \
    --dataset "$DATASET" --model "anthropic/claude-opus-4.6" --stage1-only

# # Stage 2: Run each model in parallel
# bash run/run_inference.sh \
#     --dataset "$DATASET" \
#     --model "qwen/qwen3.5-35b-a3b" \
#     --exp-name qwen35_35b_a3b_sweagent \
#     --skip-stage1 &

# bash run/run_inference.sh \
#     --dataset "$DATASET" \
#     --model "anthropic/claude-opus-4.6" \
#     --exp-name claude_opus_4_6_sweagent \
#     --skip-stage1 &

# wait
