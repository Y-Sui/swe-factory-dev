#!/bin/bash
# Build base Docker images in parallel (skipped if already present).
set -euo pipefail
set -a && source .env && set +a

build() { docker image inspect "$1" &>/dev/null || docker build "${@:2}" -t "$1" .; }

echo "=== Building base images ==="
build internal-swe-bench-miroflow:base     -f docker/Dockerfile.miroflow &
build internal-swe-bench-mirothinker:base  -f docker/Dockerfile.mirothinker &
build internal-swe-bench-sd-torchtune:base -f docker/Dockerfile.sd-torchtune --build-arg GITHUB_TOKEN="${GITHUB_TOKEN}" &
wait
