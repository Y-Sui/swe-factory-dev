#!/usr/bin/env bash
# Remove miromindai__* Docker images (keeps base images and other users' images).
# Usage: ./scripts/cleanup_docker_images.sh [--force]
set -euo pipefail

IMAGES=$(docker images --format '{{.Repository}}:{{.Tag}}' | grep '^miromindai__' || true)
[ -z "$IMAGES" ] && echo "No miromindai__* images found." && exit 0

echo "Found $(echo "$IMAGES" | wc -l) miromindai__* images."
if [[ "${1:-}" == "--force" ]]; then
    echo "$IMAGES" | xargs -r docker rmi 2>&1
else
    echo "$IMAGES"
    echo "Run with --force to remove."
fi
