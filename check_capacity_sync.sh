#!/usr/bin/env bash
# The vendored modules (capacity.py, deadline.py) must be byte-identical in all three places. Each
# service's docker build context is its own directory, so the modules are vendored rather than
# imported from the repo root -- which is exactly how two copies of one rule start to disagree. This
# is the check that stops it.
set -u
cd "$(dirname "$0")"
ok=1
for m in capacity.py deadline.py; do
  for c in "verilator-api/$m" "openlane-api/$m"; do
    cmp -s "$m" "$c" || { echo "DRIFT: $c differs from $m"; ok=0; }
  done
done
[ $ok = 1 ] && echo "capacity.py, deadline.py: all copies identical" || exit 1
