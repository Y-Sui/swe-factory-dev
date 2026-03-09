#!/bin/bash
# Stage II: Generate Dockerfiles + eval scripts + test files for all MiroMind repos.
set -euo pipefail
set -a && source .env && set +a
export PYTHONPATH="$(pwd):${PYTHONPATH:-}"
bash "$(dirname "$0")/build_base_images.sh"
python3 scripts/generate_task_list.py run "$@"
