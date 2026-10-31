#!/bin/sh
# Keep the reviewed image tool parity check; evaluator code is installed at runtime.
set -eu
/opt/cf/VERSIONS.sh > /tmp/cf-versions.txt
diff -u /opt/cf/VERSIONS.txt /tmp/cf-versions.txt
# Explicit commands are tools-image diagnostics, not evaluator service launches.
if [ "$#" -gt 0 ]; then exec "$@"; fi
cd /opt/cf
exec python3 -B -m loader.entrypoint
