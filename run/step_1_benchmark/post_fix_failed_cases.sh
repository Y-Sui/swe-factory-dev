#!/bin/bash
# Post-fix failed test generation cases. Runs after generate_test_cases_docker.sh.
set -euo pipefail
set -a && source .env && set +a
export PYTHONPATH="$(pwd):${PYTHONPATH:-}"
python3 scripts/post_fix_failed_cases.py "$@"
