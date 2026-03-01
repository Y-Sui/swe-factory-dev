#!/bin/bash
# Small smoke test: run Stage II on a tiny subset of instances using grok-4.1-fast.
#
# Usage:
#   cd swe-factory && bash run/generate_test_cases_docker_small_grok41.sh

set -euo pipefail

# Load env vars (OPENAI_KEY, OPENAI_API_BASE_URL, GITHUB_TOKEN, etc.)
set -a && source .env && set +a

# Ensure the app package is importable
export PYTHONPATH="$(pwd):${PYTHONPATH:-}"

SCRIPT_DIR="data_collection/collect"
DATA_DIR="../internal-swe-bench-data"
SETUP_DIR="testbed"
MODEL="x-ai/grok-4.1-fast"
MODEL_SLUG="grok-4.1-fast"
ROUND=3
NUM_PROCS=5
MAX_INSTANCES=1

REPOS=(
  "MiroMindAI__MiroThinker"
  "MiroMindAI__miroflow"
  "MiroMindAI__sd-torchtune"
)

# Step 1: Add version info to instances (modifies file in-place)
for REPO in "${REPOS[@]}"; do
  INSTANCE_FILE=$(ls "$DATA_DIR/$REPO"/instances_all_*.jsonl 2>/dev/null | head -1)
  if [ -z "$INSTANCE_FILE" ]; then
    echo "=== No instances_all file found for $REPO, skipping ==="
    continue
  fi

  if python3 - "$INSTANCE_FILE" <<'PY'
import json, sys
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

# Step 2: Prepare task lists, then run all repos in parallel.
for REPO in "${REPOS[@]}"; do
  TASKS_MAP=$(ls "$DATA_DIR/$REPO"/instances_all_*.jsonl 2>/dev/null | head -1)
  if [ -z "$TASKS_MAP" ]; then continue; fi
  OUT_DIR="$DATA_DIR/$REPO/setup_output_small_${MODEL_SLUG}"
  TASK_LIST="$OUT_DIR/task_list_small.txt"
  mkdir -p "$OUT_DIR" "$OUT_DIR/results"

  TASKS_MAP="$TASKS_MAP" TASK_LIST="$TASK_LIST" MAX_INSTANCES="$MAX_INSTANCES" \
  python3 - <<'PY'
import json, os
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
done

PIDS=()
for REPO in "${REPOS[@]}"; do
  TASKS_MAP=$(ls "$DATA_DIR/$REPO"/instances_all_*.jsonl 2>/dev/null | head -1)
  if [ -z "$TASKS_MAP" ]; then continue; fi
  OUT_DIR="$DATA_DIR/$REPO/setup_output_small_${MODEL_SLUG}"
  RESULT_DIR="$OUT_DIR/results"
  TASK_LIST="$OUT_DIR/task_list_small.txt"

  echo "=== Running Stage II for $REPO with $MODEL (small test) ==="
  python3 app/main.py swe-bench \
    --model "$MODEL" \
    --tasks-map "$TASKS_MAP" \
    --task-list-file "$TASK_LIST" \
    --num-processes "$NUM_PROCS" \
    --model-temperature 0.2 \
    --conv-round-limit "$ROUND" \
    --output-dir "$OUT_DIR" \
    --setup-dir "$SETUP_DIR" \
    --results-path "$RESULT_DIR" &
  PIDS+=($!)
done

FAIL=0
for PID in "${PIDS[@]}"; do
  wait "$PID" || FAIL=1
done
[ "$FAIL" -eq 0 ] || exit 1

echo "=== Done ==="
