#!/bin/sh
# Legacy builder guarantees sequential stages. Run through cfjob on shared laptops.
set -eu
cd "$(dirname "$0")"
. ./pins.env
grep -qx "ARG BASE=$BASE" Dockerfile
grep -qx "ARG CERT_BASE=$CERT_BASE" Dockerfile
df -h /
DOCKER_BUILDKIT=0 docker build --no-cache --force-rm -t "${1:-chipforge_eda_server-v2-evaluator:2.0}" .
