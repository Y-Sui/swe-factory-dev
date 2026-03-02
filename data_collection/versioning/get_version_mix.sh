#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<EOF
Usage: $0 <instance_file> <output_dir> [testbed_dir]

  <instance_file>  Original task instance file (.json or .jsonl)
  <output_dir>     Directory to store intermediate and final results
  [testbed_dir]    (Optional) Temporary directory for git-clone, defaults to ./testbed
EOF
  exit 1
}

# Check arguments
if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
  usage
fi

INSTANCE="$1"
OUTDIR="$2"
# Use the third argument if it exists, otherwise default to ./testbed
TESTBED="${3:-./testbed}"

echo "🔧 Using testbed directory: $TESTBED"
echo "🔧 Using output directory: $OUTDIR"

mkdir -p "$TESTBED" "$OUTDIR"

# 1. Extract by-github versions
echo "👉 1. Getting by-github versions..."
python3 get_versions.py \
    --instances_path "$INSTANCE" \
    --num_workers 100 \
    --retrieval_method github \
    --output_dir "$OUTDIR"

# 2. Extract by-git versions
echo "👉 2. Getting by-git versions..."
python3 get_versions_by_git.py \
    --instance_path "$INSTANCE" \
    --testbed "$TESTBED" \
    --max_workers 100 \
    --output_dir "$OUTDIR" \
    --last_stage_output_dir "$OUTDIR"

# 3. Merge into the final version
echo "👉 3. Merging both results into the final version..."
python3 merge_final_data.py "$OUTDIR"

echo "✅ All done. Results are saved in $OUTDIR"