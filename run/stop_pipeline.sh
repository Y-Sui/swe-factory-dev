#!/bin/bash
# Stop all running Stage II pipeline processes for the current user.
#
# Usage:
#   bash run/stop_pipeline.sh          # kill all swe-bench processes
#   bash run/stop_pipeline.sh gpt-5.2  # kill only gpt-5.2 processes

set -euo pipefail

MODEL_FILTER="${1:-}"

if [ -n "$MODEL_FILTER" ]; then
  PATTERN="app/main.py swe-bench.*${MODEL_FILTER}"
else
  PATTERN="app/main.py swe-bench"
fi

# Show matching processes before killing.
PROCS=$(ps aux | grep -E "$PATTERN" | grep -v grep | grep "$(whoami)" || true)

if [ -z "$PROCS" ]; then
  echo "No running pipeline processes found."
  exit 0
fi

echo "Found processes:"
echo "$PROCS"
echo ""

pkill -u "$(whoami)" -f "$PATTERN" || true
echo "All matching processes killed."
