#!/bin/sh
# Run without network or host mounts: docker run --rm --network none IMAGE /opt/cf/smoke.sh
set -eu
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
cd "$work"
cat > tiny.sv <<'SV'
module tiny(input [3:0] a, input [3:0] b, output [3:0] y);
  assign y = a + b;
endmodule
SV
cat > main.cpp <<'CPP'
#include "Vtiny.h"
int main() {
    Vtiny top;
    top.a = 3; top.b = 5; top.eval();
    return top.y == 8 ? 0 : 1;
}
CPP
verilator --lint-only --top-module tiny tiny.sv
verilator --cc --exe --build -j 2 --top-module tiny tiny.sv main.cpp
./obj_dir/Vtiny
yosys -Q -p 'read_verilog tiny.sv; synth -top tiny; check -assert; stat'
yosys-abc -c 'help' >/dev/null
cp -a /opt/cf/cacti/tech_params /opt/cf/cacti/cache.cfg .
/opt/cf/cacti/cacti -infile cache.cfg > cacti.log
test -s cache.cfg.out
python3.13 - <<'PY'
import ctypes, json
p=json.load(open('/opt/cf/platform/backend-pin.json'))
assert ctypes.CDLL(p['library']).cf_set_write_service_observer
PY
echo C1_SMOKE_PASS
