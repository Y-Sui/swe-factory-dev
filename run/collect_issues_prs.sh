#!/bin/bash
# Collect raw issue data from MiroMindAI/MiroThinker and MiroMindAI/miroflow
#
# Usage:
#   export GITHUB_TOKEN=<your_token>
#   cd swe-factory && bash run/collect_miro_issues.sh

set -euo pipefail

# Load environment variables
set -a
source "$(dirname "$0")/../.env"
set +a

SCRIPT_DIR="data_collection/collect"
DATA_DIR="../internal-swe-bench-data"
mkdir -p "$DATA_DIR"

# Step 1: Collect PRs
echo "=== Collecting PRs from MiroMindAI/MiroThinker ==="
mkdir -p "$DATA_DIR/MiroMindAI__MiroThinker"
python "$SCRIPT_DIR/print_pulls.py" MiroMindAI/MiroThinker "$DATA_DIR/MiroMindAI__MiroThinker/prs.jsonl" --mode omnigirl

echo "=== Collecting PRs from MiroMindAI/miroflow ==="
mkdir -p "$DATA_DIR/MiroMindAI__miroflow"
python "$SCRIPT_DIR/print_pulls.py" MiroMindAI/miroflow "$DATA_DIR/MiroMindAI__miroflow/prs.jsonl" --mode omnigirl

# Step 2: Build task instances
echo "=== Building instances for MiroMindAI/MiroThinker ==="
python "$SCRIPT_DIR/build_dataset.py" \
  "$DATA_DIR/MiroMindAI__MiroThinker/prs.jsonl" \
  "$DATA_DIR/MiroMindAI__MiroThinker/instances.jsonl" \
  --mode omnigirl --language python --cutoff_date "2026-02-25T23:59:59Z"

echo "=== Building instances for MiroMindAI/miroflow ==="
python "$SCRIPT_DIR/build_dataset.py" \
  "$DATA_DIR/MiroMindAI__miroflow/prs.jsonl" \
  "$DATA_DIR/MiroMindAI__miroflow/instances.jsonl" \
  --mode omnigirl --language python --cutoff_date "2026-02-25T23:59:59Z"

echo "=== Done ==="
