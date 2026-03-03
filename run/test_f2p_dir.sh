#!/bin/bash
# Run F2P validation on a specific output directory.
#
# Usage:
#   cd swe-factory-dev && bash run/test_f2p_dir.sh <output_dir>
#
# Example:
#   bash run/test_f2p_dir.sh internal-swe-bench-data/MiroMindAI__miroflow/setup_output_2026-03-03

set -euo pipefail

set -a && source .env && set +a
export PYTHONPATH="$(pwd):${PYTHONPATH:-}"

TIMEOUT=1800
NUM_WORKERS=3

if [ $# -lt 1 ]; then
  echo "Usage: $0 <output_dir>"
  exit 1
fi

OUTPUT_DIR="$1"

echo "=== F2P test: $OUTPUT_DIR ==="
python3 scripts/test_f2p_standalone.py \
  --output-dir "$OUTPUT_DIR" \
  --timeout "$TIMEOUT" \
  --num-workers "$NUM_WORKERS"
