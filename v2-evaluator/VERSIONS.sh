#!/bin/sh
# Output is stored as /opt/cf/VERSIONS.txt during the build and checked at startup.
set -eu
set -a
. /opt/cf/pins.env
set +a
verilator=$(verilator --version)
yosys=$(yosys -V)
python=$(python3.13 --version)
case "$verilator" in 'Verilator 5.038 '*g318b1c1a3*) ;; *) echo "Verilator parity mismatch: $verilator" >&2; exit 1;; esac
case "$yosys" in 'Yosys 0.56+197 (git sha1 d4c4b2106,'*) ;; *) echo "Yosys parity mismatch: $yosys" >&2; exit 1;; esac
test "$python" = "Python $PYTHON_VERSION"
test "$(cat /opt/cf/git/verilator)" = "$VERILATOR_COMMIT"
test "$(cat /opt/cf/git/yosys)" = "$YOSYS_COMMIT"
test "$(cat /opt/cf/git/yosys-abc)" = "$YOSYS_ABC_COMMIT"
printf 'verilator %s\nyosys %s\npython %s\n' "$verilator" "$yosys" "$python"
for tool in verilator yosys yosys-abc orfs cacti; do
    printf '%s.git %s\n' "$tool" "$(cat /opt/cf/git/$tool)"
done
printf 'docker %s\n' "$(docker --version)"
printf 'strace %s\n' "$(strace --version | head -n1)"
printf 'time %s\n' "$(/usr/bin/time --version | head -n1)"
printf 'ubuntu.snapshot %s\n' "$UBUNTU_SNAPSHOT"
python3.13 - <<'PY'
import ctypes, hashlib, json, os
from pathlib import Path
root = Path('/opt/cf/platform')
pin = json.loads((root/'backend-pin.json').read_text())
assert pin['commit'] == os.environ['DRAMSIM3_COMMIT']
assert pin['archive_sha256'] == os.environ['DRAMSIM3_SHA256']
source = Path(pin['source'])
assert hashlib.sha256((source/'src/controller.cc').read_bytes()).hexdigest() == pin['patched_controller_sha256'] == os.environ['DRAMSIM3_CONTROLLER_SHA256']
lib = ctypes.CDLL(pin['library'])
assert lib.cf_set_write_service_observer
for k in ('commit', 'archive_sha256', 'patched_controller_sha256'):
    print('dramsim3.'+k, pin[k])
PY
for component in orchestrator numerics workloads physical divisions reference-sw; do
    printf 'venv.%s %s\n' "$component" "$(/opt/cf/venv/$component/bin/python --version)"
    sha256sum "/opt/cf/locks/$component.lock"
done
sha256sum /opt/cf/nangate45.sha256
