#!/bin/bash
# Test whether already-generated artifacts (Dockerfile + eval.sh) pass F2P.
#
# This re-runs Docker build + tests on existing outputs WITHOUT calling
# the LLM agents.  Useful for validating that status=True is achievable.
#
# Usage:
#   cd swe-factory && bash run/test_f2p.sh
#   cd swe-factory && bash run/test_f2p.sh ../internal-swe-bench-data/MiroMindAI__MiroThinker/setup_output_small

set -euo pipefail

set -a && source .env && set +a
export PYTHONPATH="$(pwd):${PYTHONPATH:-}"

DATA_DIR="../internal-swe-bench-data"
TIMEOUT=1800
NUM_WORKERS=3

REPOS=(
  "MiroMindAI__MiroThinker"
  "MiroMindAI__miroflow"
  "MiroMindAI__sd-torchtune"
)

# If an explicit directory is passed, test only that one.
if [ $# -ge 1 ]; then
  echo "=== Testing F2P on $1 ==="
  python3 scripts/test_f2p_standalone.py \
    --output-dir "$1" \
    --timeout "$TIMEOUT" \
    --num-workers "$NUM_WORKERS"
  exit $?
fi

# Otherwise test all repos (setup_output_small first, then setup_output).
EXIT_CODE=0
for REPO in "${REPOS[@]}"; do
  for VARIANT in setup_output_small setup_output; do
    OUT_DIR="$DATA_DIR/$REPO/$VARIANT"
    if [ ! -d "$OUT_DIR" ]; then continue; fi

    echo ""
    echo "=========================================="
    echo "=== F2P test: $REPO / $VARIANT ==="
    echo "=========================================="
    python3 scripts/test_f2p_standalone.py \
      --output-dir "$OUT_DIR" \
      --timeout "$TIMEOUT" \
      --num-workers "$NUM_WORKERS" || EXIT_CODE=1
  done
done

exit $EXIT_CODE
