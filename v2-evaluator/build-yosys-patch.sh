#!/bin/sh
# Rebuild only Yosys; preserve all other shipped tools, including ABC bytes.
set -eu
cd "$(dirname "$0")"
runtime=chipforge_eda_server-v2-evaluator:2.0-r63
expected=sha256:e3b79874d55d0c476890ecdcbc5dd1df046230b44e0738e1315634d9b41f8425
actual=$(docker image inspect "$runtime" --format '{{.Id}}')
[ "$actual" = "$expected" ] || { echo "Unexpected runtime: $actual" >&2; exit 1; }
export DOCKER_BUILDKIT=1
exec nice -n 10 docker build --pull=false --progress=plain \
    -f Dockerfile.yosys-patch -t "${1:-chipforge/v2-evaluator:2.0.5-rc-yosys}" .
