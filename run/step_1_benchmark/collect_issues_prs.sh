#!/bin/bash
# Collect PRs, build instances, and refine problem statements for all target repos.
#
# Usage:
#   cd swe-factory-dev && bash run/step_1_benchmark/collect_issues_prs.sh

set -euo pipefail

set -a
source "$(dirname "$0")/../.env"
set +a

SCRIPT_DIR="data_collection/collect"
DATA_DIR="/data/yuansui/internal-swe-bench-data"
CUTOFF_DATE="2026-12-31T23:59:59Z"


# create output directories for each repo (if not exist)
mkdir -p "$DATA_DIR/MiroMindAI__MiroThinker"
mkdir -p "$DATA_DIR/MiroMindAI__sd-torchtune"
mkdir -p "$DATA_DIR/MiroMindAI__miroflow"

# ── Step 1: Collect PRs and issues ─────────────────────────────────────────────
echo "=== [MiroThinker] Step 1: Collecting PRs ==="
python3 "$SCRIPT_DIR/print_pulls.py" MiroMindAI/MiroThinker "$DATA_DIR/MiroMindAI__MiroThinker/prs.jsonl" --mode omnigirl
echo "=== [sd-torchtune] Step 1: Collecting PRs ==="
python3 "$SCRIPT_DIR/print_pulls.py" MiroMindAI/sd-torchtune "$DATA_DIR/MiroMindAI__sd-torchtune/prs.jsonl" --mode omnigirl
echo "=== [miroflow] Step 1: Collecting PRs ==="
python3 "$SCRIPT_DIR/print_pulls.py" MiroMindAI/miroflow "$DATA_DIR/MiroMindAI__miroflow/prs.jsonl" --mode omnigirl

# ── Step 2: Build instances from PRs ───────────────────────────────────────────
echo "=== [MiroThinker] Step 2: Building instances ==="
python3 "$SCRIPT_DIR/build_dataset.py" \
  "$DATA_DIR/MiroMindAI__MiroThinker/prs.jsonl" \
  "$DATA_DIR/MiroMindAI__MiroThinker" \
  --mode omnigirl --language python --cutoff_date "$CUTOFF_DATE"

echo "=== [miroflow] Step 2: Building instances ==="
python3 "$SCRIPT_DIR/build_dataset.py" \
  "$DATA_DIR/MiroMindAI__miroflow/prs.jsonl" \
  "$DATA_DIR/MiroMindAI__miroflow" \
  --mode omnigirl --language python --cutoff_date "$CUTOFF_DATE"

echo "=== [sd-torchtune] Step 2: Building instances ==="
python3 "$SCRIPT_DIR/build_dataset.py" \
  "$DATA_DIR/MiroMindAI__sd-torchtune/prs.jsonl" \
  "$DATA_DIR/MiroMindAI__sd-torchtune" \
  --mode omnigirl --language python --cutoff_date "$CUTOFF_DATE"

# ── Step 2.5: Filter benchmark-worthy instances ────────────────────────────────
echo "=== [MiroThinker] Step 2.5: Filtering benchmark-worthy instances ==="
python3 "$SCRIPT_DIR/filter_benchmark_worthy.py" "$DATA_DIR/MiroMindAI__MiroThinker"

echo "=== [miroflow] Step 2.5: Filtering benchmark-worthy instances ==="
python3 "$SCRIPT_DIR/filter_benchmark_worthy.py" "$DATA_DIR/MiroMindAI__miroflow"

echo "=== [sd-torchtune] Step 2.5: Filtering benchmark-worthy instances ==="
python3 "$SCRIPT_DIR/filter_benchmark_worthy.py" "$DATA_DIR/MiroMindAI__sd-torchtune"


# ── Step 3: Add version info to instances (in-place) ─────────────────────────
export PYTHONPATH="$(cd "$(dirname "$0")/.."; pwd):${PYTHONPATH:-}"
python3 "$SCRIPT_DIR/get_version.py" --data-dir "$DATA_DIR"

# ── Step 4: Refine problem statements ─────────────────────────────────────────
echo "=== [MiroThinker] Step 4: Refining problem statements ==="
python3 "$SCRIPT_DIR/refine_problem_statements.py" "$DATA_DIR/MiroMindAI__MiroThinker"
echo "=== [miroflow] Step 4: Refining problem statements ==="
python3 "$SCRIPT_DIR/refine_problem_statements.py" "$DATA_DIR/MiroMindAI__miroflow"
echo "=== [sd-torchtune] Step 4: Refining problem statements ==="
python3 "$SCRIPT_DIR/refine_problem_statements.py" "$DATA_DIR/MiroMindAI__sd-torchtune"

# ── Step 4.5: Quality check & auto-fix problem statements ────────────────────
echo "=== [MiroThinker] Step 4.5: Quality-checking problem statements ==="
python3 "$SCRIPT_DIR/filter_ps_quality.py" "$DATA_DIR/MiroMindAI__MiroThinker"
echo "=== [miroflow] Step 4.5: Quality-checking problem statements ==="
python3 "$SCRIPT_DIR/filter_ps_quality.py" "$DATA_DIR/MiroMindAI__miroflow"
echo "=== [sd-torchtune] Step 4.5: Quality-checking problem statements ==="
python3 "$SCRIPT_DIR/filter_ps_quality.py" "$DATA_DIR/MiroMindAI__sd-torchtune"



echo "=== All repos processed. Output in $DATA_DIR ==="
