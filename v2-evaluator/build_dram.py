#!/usr/bin/env python3
"""Pinned source + explicit service observer overlay; public tool build, root selected by CF_DRAM_ROOT."""

import os
import hashlib
import json
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

PINS = dict(line.split('=', 1) for line in Path(__file__).with_name('pins.env').read_text().splitlines()
            if line and not line.startswith('#'))
PIN = PINS['DRAMSIM3_COMMIT']
ARCHIVE_SHA256 = PINS['DRAMSIM3_SHA256']
ROOT = Path(os.environ.get('CF_DRAM_ROOT', '/opt/cf/platform'))

def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    archive = ROOT / f'DRAMsim3-{PIN}.tar.gz'
    if not archive.exists():
        for attempt in range(1, 6):
            try:
                urllib.request.urlretrieve(f'https://codeload.github.com/umd-memsys/DRAMsim3/tar.gz/{PIN}', archive)
                break
            except OSError as exc:
                archive.unlink(missing_ok=True)
                if attempt == 5:
                    raise
                print(f'DRAMsim3 download failed ({exc}); retry {attempt}', file=sys.stderr)
                time.sleep(10 * attempt)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != ARCHIVE_SHA256:
        raise ValueError('pinned source archive hash mismatch')
    src = ROOT / f'DRAMsim3-{PIN}'
    if not src.exists():
        with tarfile.open(archive) as f:
            f.extractall(ROOT, filter='data')
    p = src / 'src/controller.cc'
    original = p.read_text()
    if 'cf_set_write_service_observer' not in original:
        updated = original.replace('namespace dramsim3 {', '''// Evaluator overlay: observe actual WRITE command service, never early ACK.
static void (*cf_observer)(uint64_t, uint64_t) = nullptr;
extern "C" void cf_set_write_service_observer(void (*fn)(uint64_t, uint64_t)) {
    cf_observer = fn;
}
namespace dramsim3 {''', 1)
        needle = '        auto wr_lat = clk_ - it->second.added_cycle + config_.write_delay;'
        assert updated.count(needle) == 1
        updated = updated.replace(needle, '''        if (cf_observer) cf_observer(cmd.hex_addr, clk_ + config_.write_delay);
''' + needle)
        p.write_text(updated)
    expected = PINS['DRAMSIM3_CONTROLLER_SHA256']
    if hashlib.sha256(p.read_bytes()).hexdigest() != expected:
        raise ValueError('observer overlay hash mismatch')
    build = ROOT / 'backend-build'
    subprocess.run(['cmake', '-S', str(src), '-B', str(build), '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_POLICY_VERSION_MINIMUM=3.5'], check=True)
    subprocess.run(['cmake', '--build', str(build), '--target', 'dramsim3', '-j', '2'], check=True)
    (ROOT / 'backend-pin.json').write_text(json.dumps({
        'commit': PIN, 'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
        'patched_controller_sha256': hashlib.sha256(p.read_bytes()).hexdigest(),
        'overlay': 'build_backend.py: observer at WRITE issue + config.write_delay',
        'source': str(src), 'library': str(src / 'libdramsim3.so')}, indent=2) + '\n')
    print('BACKEND_BUILD_PASS', src)
if __name__ == '__main__':
    main()
