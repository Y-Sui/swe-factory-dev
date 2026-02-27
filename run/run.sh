#!/bin/bash
set -euo pipefail

MODEL="deepseek/deepseek-chat-v3-0324"
# "google/gemini-2.5-flash-preview"
# "gpt-4.1-mini"
REPO_NAME="SetupBench-lite"
BASE_TASK_DIR="data_collection/collect/${REPO_NAME}"
TASKS_MAP="${BASE_TASK_DIR}/merged_instances_versions.jsonl"
SETUP_DIR="testbed"
ROUND=5
NUM_PROCS=5
TEMP=0.2
BATCH_COUNT=1
# we split SweSetupBench-lite into 17 batches, each batch contains 40 raw issue instances.
for f in  "$TASKS_MAP"; do
  if [ ! -f "$f" ]; then
    echo "❌ Missing file: $f"
    exit 1
  fi
done

cleanup() {
  docker ps -a -q | xargs -r docker rm -f || true
  docker image prune -af || true
  rm -rf "$SETUP_DIR"
}

for idx in $(seq 17 $BATCH_COUNT); do
  TASK_LIST_FILE="${BASE_TASK_DIR}/batch_${idx}.txt"
  if [ ! -f "$TASK_LIST_FILE" ]; then
    echo "⚠️  Skipping missing ${TASK_LIST_FILE}"
    continue
  fi

  # cleanup

  OUT_DIR="output_test1/${REPO_NAME}/${MODEL}/round_${ROUND}_batch_${idx}"
  RESULT_DIR="output_test1/${REPO_NAME}/${MODEL}/results"
  mkdir -p "$OUT_DIR"

  echo "▶️  Running batch_${idx} with normal mode"

  python3 app/main.py swe-bench \
    --model "$MODEL" \
    --tasks-map "$TASKS_MAP" \
    --task-list-file "$TASK_LIST_FILE" \
    --num-processes "$NUM_PROCS" \
    --model-temperature "$TEMP" \
    --conv-round-limit "$ROUND" \
    --output-dir "$OUT_DIR" \
    --setup-dir "$SETUP_DIR" \
    --results-path "$RESULT_DIR"
done
