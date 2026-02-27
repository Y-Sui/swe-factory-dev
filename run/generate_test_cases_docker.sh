#!/bin/bash
# Stage II: Generate Dockerfiles + eval scripts for MiroMindAI/MiroThinker and MiroMindAI/miroflow
#
# Prerequisites:
#   1. Run collect_miro_issues.sh first (Stage I)
#   2. Fill in OPENROUTER_API_KEY and OPENAI_KEY in .env
#
# Usage:
#   cd swe-factory && bash run/setup_miro_envs.sh

set -euo pipefail

# Load env vars (OPENAI_KEY, OPENAI_API_BASE_URL, GITHUB_TOKEN, etc.)
set -a && source .env && set +a

# Ensure the app package is importable
export PYTHONPATH="$(pwd):${PYTHONPATH:-}"

SCRIPT_DIR="data_collection/collect"
DATA_DIR="../internal-swe-bench-data"
SETUP_DIR="testbed"
# MODEL="anthropic/claude-sonnet-4.5"
MODEL="google/gemini-2.5-flash"
ROUND=5
NUM_PROCS=8

REPOS=(
  "MiroMindAI__MiroThinker"
  "MiroMindAI__miroflow"
)

# Step 1: Add version info to instances (required by Stage II)
for REPO in "${REPOS[@]}"; do
  INSTANCE_FILE="$DATA_DIR/$REPO/instances.jsonl.all"

  if python3 - "$INSTANCE_FILE" <<'PY'
import json
import sys
path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if "version" in obj:
                sys.exit(0)
            break
except FileNotFoundError:
    pass
sys.exit(1)
PY
  then
    echo "=== Versions already exist in $INSTANCE_FILE, skipping ==="
    continue
  fi

  echo "=== Getting versions for $REPO ==="
  python3 "$SCRIPT_DIR/get_version.py" \
    --instance_path "$INSTANCE_FILE" \
    --testbed "$SETUP_DIR" \
    --max-workers 10 \
    --in-place
done

# Step 2: Run the multi-agent env setup (Dockerfile + eval.sh generation)
for REPO in "${REPOS[@]}"; do
  TASKS_MAP="$DATA_DIR/$REPO/instances.jsonl.all"
  OUT_DIR="$DATA_DIR/$REPO/setup_output"
  RESULT_DIR="$DATA_DIR/$REPO/setup_output/results"
  mkdir -p "$OUT_DIR" "$RESULT_DIR"

  echo "=== Running Stage II for $REPO with $MODEL ==="
  python3 app/main.py swe-bench \
    --model "$MODEL" \
    --tasks-map "$TASKS_MAP" \
    --num-processes "$NUM_PROCS" \
    --model-temperature 0.2 \
    --conv-round-limit "$ROUND" \
    --output-dir "$OUT_DIR" \
    --setup-dir "$SETUP_DIR" \
    --results-path "$RESULT_DIR" \
    --disable-run-test
done

echo "=== Done ==="
