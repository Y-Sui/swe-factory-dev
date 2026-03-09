#!/usr/bin/env bash
# Verify generated docker files and test files with gold patch F2P check
# Usage: ./run/run_evaluation.sh [extra_args...]
set -euo pipefail

DATASET="/home/yuansui/swe-factory-dev/internal-swe-bench-data/results_v1_gpt_5_2_68_20260307_verified.json"

python3 evaluation/run_evaluation.py \
    --dataset_path "$DATASET" \
    --mode benchmark \
    --run_id "all_f2p" \
    --output_path "run_instances" \
    "$@"
