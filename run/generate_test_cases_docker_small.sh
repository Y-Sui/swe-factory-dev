#!/bin/bash
# Small smoke test: run Stage II on a tiny subset of instances.
#
# Usage:
#   cd swe-factory && bash run/generate_test_cases_docker_small.sh

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
ROUND=3
NUM_PROCS=5
MAX_INSTANCES=2

REPOS=(
  "MiroMindAI__MiroThinker"
  "MiroMindAI__miroflow"
)

for REPO in "${REPOS[@]}"; do
  INSTANCE_FILE="$DATA_DIR/$REPO/instances.jsonl.all"

  if python - "$INSTANCE_FILE" <<'PY'
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
  python "$SCRIPT_DIR/get_version.py" \
    --instance_path "$INSTANCE_FILE" \
    --testbed "$SETUP_DIR" \
    --max-workers 10 \
    --in-place

  TASKS_MAP="$INSTANCE_FILE"
  OUT_DIR="$DATA_DIR/$REPO/setup_output_small"
  RESULT_DIR="$OUT_DIR/results"
  TASK_LIST="$OUT_DIR/task_list_small.txt"
  mkdir -p "$OUT_DIR" "$RESULT_DIR"

  export TASKS_MAP TASK_LIST MAX_INSTANCES
  python - <<'PY'
import json
import os

tasks_map = os.environ["TASKS_MAP"]
task_list = os.environ["TASK_LIST"]
max_instances = int(os.environ["MAX_INSTANCES"])

ids = []
with open(tasks_map, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if "instance_id" in obj:
            ids.append(obj["instance_id"])
        if len(ids) >= max_instances:
            break

with open(task_list, "w", encoding="utf-8") as f:
    f.write("\n".join(ids))
PY

  echo "=== Running Stage II for $REPO with $MODEL (small test) ==="
  python app/main.py swe-bench \
    --model "$MODEL" \
    --tasks-map "$TASKS_MAP" \
    --task-list-file "$TASK_LIST" \
    --num-processes "$NUM_PROCS" \
    --model-temperature 0.2 \
    --conv-round-limit "$ROUND" \
    --output-dir "$OUT_DIR" \
    --setup-dir "$SETUP_DIR" \
    --results-path "$RESULT_DIR" \
    --disable-run-test
done
