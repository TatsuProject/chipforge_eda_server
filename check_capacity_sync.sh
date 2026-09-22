#!/usr/bin/env bash
# The three copies of capacity.py must be byte-identical. Each service's docker build context is
# its own directory, so the module is vendored rather than imported from the repo root -- which is
# exactly how two copies of one rule start to disagree. This is the check that stops it.
set -u
cd "$(dirname "$0")"
ok=1
for c in verilator-api/capacity.py openlane-api/capacity.py; do
  cmp -s capacity.py "$c" || { echo "DRIFT: $c differs from capacity.py"; ok=0; }
done
[ $ok = 1 ] && echo "capacity.py: all copies identical" || exit 1
