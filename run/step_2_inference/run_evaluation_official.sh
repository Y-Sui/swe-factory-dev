#!/usr/bin/env bash
# Evaluate using official swebench harness.
# Usage: ./scripts/run_evaluation_official.sh <preds_path> <run_id> [extra_args...]
set -euo pipefail

cd /home/yuansui/mini-swe-agent


PREDS="${1:?Usage: $0 <preds_path> <run_id>}"
RUN_ID="${2:?Usage: $0 <preds_path> <run_id>}"
DATASET="/home/yuansui/swe-factory-dev/internal-swe-bench-data/results_v1_gpt_5_2_68_20260307_verified.json"
shift 2

# Convert dict preds -> jsonl (official swebench expects list/jsonl)
CONVERTED=$(mktemp /tmp/preds_XXXXXX.jsonl)
python3 -c "
import json
p = json.load(open('$PREDS'))
items = p.values() if isinstance(p, dict) else p
n = 0
with open('$CONVERTED', 'w') as f:
    for v in items:
        if v.get('model_patch'):
            f.write(json.dumps(v) + '\n')
            n += 1
print(f'{n} predictions written (skipped empty patches)')
"

python3 -m swebench.harness.run_evaluation \
  --dataset_name "$DATASET" \
  --predictions_path "$CONVERTED" \
  --max_workers 4 \
  --run_id "$RUN_ID" \
  "$@"

rm -f "$CONVERTED"

# ./scripts/run_evaluation_official.sh ./results/anthropic_claude_opus_4_6/preds.json opus_eval