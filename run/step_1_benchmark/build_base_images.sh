#!/bin/bash
# Build base Docker images (skipped if already present).
set -euo pipefail
set -a && source .env && set +a

echo "=== Building base images ==="
docker image inspect swe-factory/miroflow:base &>/dev/null \
  || docker build -t swe-factory/miroflow:base -f docker/Dockerfile.miroflow . &
docker image inspect swe-factory/mirothinker:base &>/dev/null \
  || docker build -t swe-factory/mirothinker:base -f docker/Dockerfile.mirothinker . &
docker image inspect swe-factory/sd-torchtune:base &>/dev/null \
  || docker build --build-arg GITHUB_TOKEN="${GITHUB_TOKEN}" \
       -t swe-factory/sd-torchtune:base -f docker/Dockerfile.sd-torchtune . &
wait
