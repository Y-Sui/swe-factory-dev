#!/bin/bash
# Quick test run: collect PRs and build instances for MiroMindAI/miroflow only.
#
# Usage:
#   cd swe-factory-dev && bash run/collect_miroflow_test.sh

set -euo pipefail

set -a
source "$(dirname "$0")/../.env"
set +a

SCRIPT_DIR="data_collection/collect"
DATA_DIR="/data/yuansui/internal-swe-bench-data/MiroMindAI__miroflow"
mkdir -p "$DATA_DIR"

echo "=== Step 1: Collecting PRs from MiroMindAI/miroflow ==="
python3 "$SCRIPT_DIR/print_pulls.py" MiroMindAI/miroflow "$DATA_DIR/prs.jsonl" --mode omnigirl

echo "=== Step 2: Building instances ==="
python3 "$SCRIPT_DIR/build_dataset.py" \
  "$DATA_DIR/prs.jsonl" \
  "$DATA_DIR" \
  --mode omnigirl --language python \
  --cutoff_date "2026-12-31T23:59:59Z"

# echo "=== Step 3: Refining problem statements ==="
# python3 "$SCRIPT_DIR/refine_problem_statements.py" "$DATA_DIR"

echo "=== Done. Output in $DATA_DIR ==="
