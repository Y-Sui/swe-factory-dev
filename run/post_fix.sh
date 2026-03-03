#!/bin/bash
set -euo pipefail

python3 scripts/post_fix_failed_cases.py \
    --setup-dir internal-swe-bench-data/MiroMindAI__miroflow/setup_output_2026-03-03 \
    --instances-jsonl internal-swe-bench-data/MiroMindAI__miroflow/instances_selected_36.jsonl \
    --max-rounds 3 \


python3 scripts/post_fix_failed_cases.py \
    --setup-dir internal-swe-bench-data/MiroMindAI__MiroThinker/setup_output_2026-03-03 \
    --instances-jsonl internal-swe-bench-data/MiroMindAI__MiroThinker/instances_selected_24.jsonl \
    --max-rounds 3