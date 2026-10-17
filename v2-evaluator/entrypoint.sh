#!/bin/sh
# C1 PLACEHOLDER ONLY: C2 supplies cgroup nesting; C6 supplies the evaluator zip loader.
# There is no HTTP service yet. Public model/data assets belong to the later install phase.
set -eu
/opt/cf/VERSIONS.sh > /tmp/cf-versions.txt
diff -u /opt/cf/VERSIONS.txt /tmp/cf-versions.txt
cat /opt/cf/VERSIONS.txt
if [ "$#" -gt 0 ]; then exec "$@"; fi
echo 'C1 tools image only: loader and cgroup scope support are pending.' >&2
exec sleep infinity
