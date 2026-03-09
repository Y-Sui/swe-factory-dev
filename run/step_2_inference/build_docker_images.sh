#!/usr/bin/env bash
# Build Docker images from a JSON data file.
# Tags each image for both mini-swe-agent and swe-factory evaluation.
# Usage: ./scripts/build_docker_images.sh <path_to_json> [workers]
set -euo pipefail

JSON_FILE="${1:?Usage: $0 <path_to_json> [workers]}"
WORKERS="${2:-20}"

python3 -c "
import json, subprocess, sys
from concurrent.futures import ThreadPoolExecutor

def build(item):
    iid = item['instance_id']
    dockerfile = item.get('dockerfile', '')
    if not dockerfile:
        return f'[SKIP] {iid}: no dockerfile'
    id_lower = iid.lower()
    id_docker = iid.replace('__', '_1776_').lower()
    img_agent = f'swebench/sweb.eval.x86_64.{id_docker}:latest'
    img_eval = f'setup.{id_lower}:latest'
    # Check if already built
    r = subprocess.run(['docker', 'image', 'inspect', img_agent], capture_output=True)
    if r.returncode == 0:
        # Ensure eval tag also exists
        subprocess.run(['docker', 'tag', img_agent, img_eval], capture_output=True)
        return f'[EXISTS] {iid}'
    r = subprocess.run(['docker', 'build', '-t', img_agent, '-t', img_eval, '-'],
                       input=dockerfile.encode(), capture_output=True)
    if r.returncode == 0:
        return f'[OK] {iid}'
    return f'[FAIL] {iid}: {r.stderr.decode()[-200:]}'

data = json.load(open('$JSON_FILE'))
with ThreadPoolExecutor(max_workers=$WORKERS) as pool:
    for result in pool.map(build, data):
        print(result, flush=True)
"
