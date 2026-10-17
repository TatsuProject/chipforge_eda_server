#!/bin/sh
# Run via cfjob on resource-constrained hosts. Legacy builder serializes the stages.
set -eu
cd "$(dirname "$0")"
df -h /
DOCKER_BUILDKIT=0 docker build --no-cache --force-rm -t "${1:-cf-compiler:v2.1-public}" .
