"""RANKED #8: Yosys peepopt `shiftadd` miscompiles a constant index that wreduce turned into a signed
1-bit $add (`res[(c*4+3)%4]` -> offset -8 instead of +24), deleting live logic (a memory macro in sa16x16v).
Set CF_YOSYS_FIXED=1 for the patched evaluator image; leave unset for the old baseline."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

YOSYS = os.environ.get('CF_YOSYS', 'yosys')
pytestmark = pytest.mark.skipif(shutil.which(YOSYS) is None, reason='requires Yosys')
FIXED = os.environ.get('CF_YOSYS_FIXED') == '1'   # a Yosys carrying upstream a74edc9be

HERE = Path(__file__).resolve().parent
COARSE = ('synth -top top -run :coarse; proc; opt_expr; opt_clean; check; opt -nodffe -nosdff; fsm; opt; '
          'wreduce; peepopt {sel}; opt_clean; alumacc; share; opt; memory -nomap; opt_clean; '
          'synth -top top -run fine:')
GUARD = 't:$add t:$sub %u %n'   # proposed: peepopt on everything except $add/$sub = no shiftadd


def yosys(script, tmp_path):
    (tmp_path / 's.ys').write_text(script)
    r = subprocess.run([YOSYS, '-q', '-s', 's.ys', '-l', 's.log'], cwd=tmp_path, capture_output=True, text=True)
    log = (tmp_path / 's.log').read_text()
    if r.returncode and 'proof did fail' not in log:
        pytest.fail(f'Yosys exited {r.returncode}: {r.stderr}\n{log[-3000:]}')
    return log


def equiv(flow, tmp_path):
    src = HERE / 'shiftadd_signed_const.sv'
    log = yosys(f'read_verilog -sv {src}\nprep -top top\nrename top gold\ndesign -stash gold\n'
                f'read_verilog -sv {src}\nhierarchy -top top\n{flow}\nrename top gate\ndesign -stash gate\n'
                'design -copy-from gold -as gold gold\ndesign -copy-from gate -as gate gate\n'
                'miter -equiv -flatten -make_outputs gold gate miter\nsat -verify -prove trigger 0 miter\n',
                tmp_path)
    return 'SUCCESS' in log


def macros(flow, tmp_path):
    log = yosys(f'read_verilog -sv {HERE / "macro_dropped.sv"}\nhierarchy -top top\n{flow}\nselect -count t:bb\n',
                tmp_path)
    return int(re.search(r'(\d+) objects', log)[1])


def test_plain_synth(tmp_path):   # pinned d4c4b2106 miscompiles; with a74edc9be it must be exact
    assert equiv('synth -top top', tmp_path) is FIXED
    assert macros('synth -top top', tmp_path) == (4 if FIXED else 3)


def test_expanded_script_matches_synth(tmp_path):
    assert equiv(COARSE.format(sel=''), tmp_path) is FIXED


@pytest.mark.parametrize('check', [equiv, macros])
def test_guarded_peepopt_is_correct(check, tmp_path):
    out = check(COARSE.format(sel=GUARD), tmp_path)
    assert out is True if check is equiv else out == 4
