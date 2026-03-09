#!/usr/bin/env bash
# Run mini-swe-bench on a local JSON dataset.
# Usage: ./scripts/run_swebench.sh [model_name] [json_path] [output_dir] [extra_args...]
# Example: ./scripts/run_swebench.sh qwen/qwen3.5-35b-a3b /path/to/data.json ./results --slice 0:3
set -euo pipefail

cd /home/yuansui/mini-swe-agent

MODEL="${1:-qwen/qwen3.5-35b-a3b}"
JSON="${2:-/home/yuansui/swe-factory-dev/internal-swe-bench-data/results_v1_gpt_5_2_68_20260307_verified.json}"
MODEL_DIR="${MODEL//\//_}"
MODEL_DIR="${MODEL_DIR//./_}"
MODEL_DIR="${MODEL_DIR//-/_}"
OUTPUT="${3:-/data/yuansui/internal-swe-bench-data/results/${MODEL_DIR}}"
shift 2>/dev/null || true; shift 2>/dev/null || true; shift 2>/dev/null || true

python3 -m minisweagent.run.benchmarks.swebench \
  --subset "$JSON" \
  -c swebench.yaml \
  -c model.cost_tracking=ignore_errors \
  --model-class openrouter \
  -m "$MODEL" \
  -o "$OUTPUT" \
  -w 8 \
  "$@"


# ./scripts/run_swebench.sh "qwen/qwen3.5-35b-a3b" "$@"
# ./scripts/run_swebench.sh "anthropic/claude-opus-4.6" "$@"