import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time
import uuid

import pytest

from loader import cgroups

SHIM = (Path(shutil.which('systemd-run')) if os.environ.get('CF_CGROUP_PARENT')
        else Path(__file__).resolve().parents[1] / 'cfscope')


@pytest.mark.parametrize('value,want', [('96M', str(96 << 20)), ('5G', str(5 << 30)), ('0','0'), ('1024','1024'), ('infinity','max')])
def test_memory_units(value, want):
    assert cgroups.memory(value) == want


def test_flags_and_limits():
    name, props, cmd = cgroups.parse(['--user', '--scope', '--quiet', '--unit=x', '-p', 'MemoryMax=96M',
        '--property=CPUQuota=150%', '-pAllowedCPUs=0-2 4', '-p', 'OOMPolicy=continue', '--', 'echo', '-n'])
    assert name == 'cf-x.scope' and cmd == ['echo','-n']
    assert props == {'memory.max': str(96 << 20), 'memory.swap.max':'0', 'memory.oom.group':'0',
                     'cpu.max':'150000 100000', 'cpuset.cpus':'0-2,4'}


@pytest.mark.parametrize('args', [
    ['--scope','--unknown','true'], ['--scope','-p','MemoryMax=oops','true'],
    ['--scope','-p','OOMPolicy=kill','true'], ['--scope','-p','CPUQuota=nan%','true'],
    ['--scope','-p','AllowedCPUs=4-2','true'], ['--scope','--unit','../../bad','true'],
    ['--scope','-p','MemoryMax=5G','-p','MemoryMax=6G','true'], ['true'], ['--scope'],
])
def test_unsupported_options_fail_loudly(args, capsys):
    assert cgroups.main(args) == 2
    assert 'cfscope:' in capsys.readouterr().err


@pytest.fixture
def delegated():
    if not os.environ.get('CF_CGROUP_PARENT'):
        pytest.skip('kernel cgroup checks run inside the delegated evaluator container')
    parent = cgroups.parent()
    assert os.access(parent, os.W_OK)
    return parent


def scope_command(command, *flags):
    return [sys.executable, str(SHIM), '--user', '--scope', '-q', *flags, *command]


def test_kernel_limits_exec_pid_swap_cpu_and_reaper(delegated):
    unit = 'test-' + uuid.uuid4().hex
    cpu = min(os.sched_getaffinity(0))
    script = "import os,sys; print(os.getpid(),flush=True); sys.stdin.buffer.read(1);sys.exit(37)"
    p = subprocess.Popen(scope_command([sys.executable, '-c', script], '--unit', unit,
        '-p', 'MemoryMax=64M', '-p', 'MemorySwapMax=0', '-p', 'CPUQuota=50%', '-p', f'AllowedCPUs={cpu}'),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert int(p.stdout.readline()) == p.pid  # exec, no resident shim/waiter.
        scope = delegated / ('cf-' + unit + '.scope')
        assert (scope / 'memory.max').read_text().strip() == str(64 << 20)
        assert (scope / 'memory.swap.max').read_text().strip() == '0'
        assert (scope / 'cpu.max').read_text().strip() == '50000 100000'
        assert (scope / 'cpuset.cpus').read_text().strip() == str(cpu)
        assert os.sched_getaffinity(p.pid) == {cpu}
        cgroups.reap(delegated)
        assert scope.exists()  # Populated scopes cannot be removed.
        p.communicate('x', timeout=10)
        assert p.returncode == 37
        cgroups.reap(delegated)
        assert not scope.exists()
    finally:
        if p.poll() is None: p.kill(); p.wait()


def test_kernel_oom_counted_and_cap_enforced(delegated):
    # Keep the observer small; prefer the allocator for OOM so it can report kernel evidence.
    script = '''import json, os, subprocess, sys
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', "open('/proc/self/oom_score_adj','w').write('500'); x=bytearray(256<<20)"])
rc = child.wait()
g = next(l[3:] for l in Path('/proc/self/cgroup').read_text().splitlines() if l.startswith('0::'))
p = Path('/sys/fs/cgroup')/g.lstrip('/')
events = dict((k,int(v)) for k,v in (l.split() for l in (p/'memory.events').read_text().splitlines()))
print(json.dumps(dict(rc=rc,events=events,limit=(p/'memory.max').read_text().strip(),swap=(p/'memory.swap.max').read_text().strip(),peak=int((p/'memory.peak').read_text()))))
'''
    p = subprocess.run(scope_command([sys.executable, '-c', script], '-p', 'MemoryMax=64M',
        '-p', 'MemorySwapMax=0', '-p', 'OOMPolicy=continue'), capture_output=True, text=True, timeout=20)
    assert p.returncode == 0, p.stderr
    result = json.loads(p.stdout)
    assert result['rc'] == -signal.SIGKILL
    assert result['events']['oom'] > 0 and result['events']['oom_kill'] > 0
    assert result['limit'] == str(64 << 20) and result['swap'] == '0'
    assert result['peak'] <= (65 << 20)


def test_kernel_signal_and_missing_executable_exit_codes(delegated):
    p = subprocess.run(scope_command([sys.executable, '-c', 'import os,signal;os.kill(os.getpid(),signal.SIGTERM)']))
    assert p.returncode == -signal.SIGTERM
    p = subprocess.run(scope_command(['cf-nonexistent-executable']), capture_output=True)
    assert p.returncode == 127
