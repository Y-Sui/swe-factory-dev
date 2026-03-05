#!/bin/bash
# Stage II: Generate Dockerfiles + eval scripts + test files for all MiroMind repos.
#
# Prerequisites:
#   Run collect_issues_prs.sh first (Stage I) — it collects PRs, builds instances,
#   and adds version info via get_version.py.
#
# Supports incremental runs: instances with an existing status.json in the
# output directory are automatically skipped by main.py, so you can safely
# re-run this script with a larger --max-instances (or 0 = all) and only
# new instances will be processed.
#
# Usage:
#   bash run/generate_test_cases_docker.sh                   # default: first 50 instances per repo
#   bash run/generate_test_cases_docker.sh --max-instances 80
#   bash run/generate_test_cases_docker.sh --max-instances 0  # all instances
#   bash run/generate_test_cases_docker.sh --repos MiroMindAI__miroflow MiroMindAI__sd-torchtune

set -euo pipefail

# ── Defaults (override via CLI flags) ────────────────────────────────────────
MAX_INSTANCES=1      # 0 = all instances
MODEL="openai/gpt-5.2"
MODEL_SLUG="gpt-5.2"
ROUND=3
NUM_PROCS=5
ALL_REPOS=(
  "MiroMindAI__MiroThinker"
  "MiroMindAI__miroflow"
  "MiroMindAI__sd-torchtune"
)
REPOS=()  # populated below; empty = use ALL_REPOS

# ── Parse CLI args ───────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --max-instances) MAX_INSTANCES="$2"; shift 2 ;;
    --model)         MODEL="$2"; shift 2 ;;
    --model-slug)    MODEL_SLUG="$2"; shift 2 ;;
    --round)         ROUND="$2"; shift 2 ;;
    --num-procs)     NUM_PROCS="$2"; shift 2 ;;
    --repos)         shift; while [[ $# -gt 0 && ! "$1" =~ ^-- ]]; do REPOS+=("$1"); shift; done ;;
    *) echo "Unknown flag: $1"; exit 1 ;;
  esac
done
[[ ${#REPOS[@]} -eq 0 ]] && REPOS=("${ALL_REPOS[@]}")

# ── Paths ────────────────────────────────────────────────────────────────────
DATA_DIR="/data/yuansui/internal-swe-bench-data"
SETUP_DIR="testbed"

# Load env vars (OPENAI_KEY, OPENAI_API_BASE_URL, GITHUB_TOKEN, etc.)
set -a && source .env && set +a
export PYTHONPATH="$(pwd):${PYTHONPATH:-}"

# ── Step 0: Build base images in parallel (skipped if already present) ────────
echo "=== Building base images ==="
BUILD_PIDS=()
if ! docker image inspect swe-factory/miroflow:base &>/dev/null; then
  docker build -t swe-factory/miroflow:base -f docker/Dockerfile.miroflow . &
  BUILD_PIDS+=($!)
fi
if ! docker image inspect swe-factory/mirothinker:base &>/dev/null; then
  docker build -t swe-factory/mirothinker:base -f docker/Dockerfile.mirothinker . &
  BUILD_PIDS+=($!)
fi
if ! docker image inspect swe-factory/sd-torchtune:base &>/dev/null; then
  docker build --build-arg GITHUB_TOKEN="${GITHUB_TOKEN}" \
    -t swe-factory/sd-torchtune:base -f docker/Dockerfile.sd-torchtune . &
  BUILD_PIDS+=($!)
fi
for PID in "${BUILD_PIDS[@]}"; do
  wait "$PID" || { echo "Base image build failed (PID $PID)"; exit 1; }
done

# Helper: find the instances file. Prefer *_subset.jsonl, then plain instances_filter_*.jsonl.
find_instances_file() {
  local subset
  subset=$(ls "$DATA_DIR/$1"/instances_filter_*_subset.jsonl 2>/dev/null | head -1)
  if [ -n "$subset" ]; then echo "$subset"; return; fi
  ls "$DATA_DIR/$1"/instances_filter_*.jsonl 2>/dev/null | head -1
}

# ── Step 1: Sample instances & generate task list files ──────────────────────
# Output dir is fixed per model (no date suffix) so that status.json from
# previous runs persists and completed instances are automatically skipped.
for REPO in "${REPOS[@]}"; do
  TASKS_MAP=$(find_instances_file "$REPO")
  if [ -z "$TASKS_MAP" ]; then continue; fi

  OUT_DIR="$DATA_DIR/$REPO/setup_output_${MODEL_SLUG}"
  TASK_LIST="$OUT_DIR/task_list.txt"
  mkdir -p "$OUT_DIR" "$OUT_DIR/results"

  # Sample up to MAX_INSTANCES (0 = all), skipping already-completed ones in
  # the task list generation so the process slots go to new work.
  TASKS_MAP="$TASKS_MAP" TASK_LIST="$TASK_LIST" MAX_INSTANCES="$MAX_INSTANCES" OUT_DIR="$OUT_DIR" \
  python3 - <<'PY'
import json, os

tasks_map = os.environ["TASKS_MAP"]
task_list_path = os.environ["TASK_LIST"]
max_instances = int(os.environ["MAX_INSTANCES"])
out_dir = os.environ["OUT_DIR"]

# Read all instance IDs from the JSONL.
all_ids = []
with open(tasks_map, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if "instance_id" in obj:
            all_ids.append(obj["instance_id"])

total = len(all_ids)

# Apply sampling limit (0 = no limit).
if max_instances > 0:
    selected = all_ids[:max_instances]
else:
    selected = all_ids

# Filter out already-completed instances (have status.json) so main.py
# doesn't waste process slots loading/cloning repos that will be skipped.
pending = [
    iid for iid in selected
    if not os.path.exists(os.path.join(out_dir, iid, "status.json"))
]
already_done = len(selected) - len(pending)

print(f"  Total instances: {total}, selected: {len(selected)}, "
      f"already done: {already_done}, to run: {len(pending)}")

with open(task_list_path, "w", encoding="utf-8") as f:
    f.write("\n".join(pending))
PY
done

# ── Step 2: Run the multi-agent pipeline (parallel across repos) ─────────────
PIDS=()
for REPO in "${REPOS[@]}"; do
  TASKS_MAP=$(find_instances_file "$REPO")
  if [ -z "$TASKS_MAP" ]; then continue; fi

  OUT_DIR="$DATA_DIR/$REPO/setup_output_${MODEL_SLUG}"
  RESULT_DIR="$OUT_DIR/results"
  TASK_LIST="$OUT_DIR/task_list.txt"

  echo "=== Running Stage II for $REPO | model=$MODEL | max=$MAX_INSTANCES | rounds=$ROUND ==="
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

# Wait for all repos and fail if any errored.
FAIL=0
for PID in "${PIDS[@]}"; do
  wait "$PID" || FAIL=1
done
[ "$FAIL" -eq 0 ] || exit 1

echo "=== Done ==="
