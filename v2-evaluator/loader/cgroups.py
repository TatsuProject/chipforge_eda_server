"""Delegated cgroup-v2 scopes, with no systemd daemon or host-wide mutations."""
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import fcntl
import os
from pathlib import Path
import re
import sys
import uuid

CGROUP = Path('/sys/fs/cgroup')


def current():
    for line in Path('/proc/self/cgroup').read_text().splitlines():
        if line.startswith('0::'):
            return CGROUP / line[3:].lstrip('/')
    raise RuntimeError('cgroup v2 is required')


def parent():
    value = os.environ.get('CF_CGROUP_PARENT')
    if not value:
        raise RuntimeError('CF_CGROUP_PARENT missing; run the container entrypoint')
    root = Path(value).resolve()
    here = current().resolve()
    if root == CGROUP or not root.is_relative_to(CGROUP) or not here.is_relative_to(root):
        raise RuntimeError('scope parent must be the current delegated container subtree')
    return root


@contextmanager
def scope_lock():
    # Serialize mkdir -> join with the reaper: a new, not-yet-joined scope is empty.
    with open(os.environ.get('CF_SCOPE_LOCK', '/run/cfscope.lock'), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def memory(value):
    if value in ('infinity', 'max'):
        return 'max'
    m = re.fullmatch(r'(\d+)([KMGTPE]?)(?:B)?', value, re.I)
    if not m:
        raise ValueError('invalid memory limit')
    return str(int(m[1]) * 1024 ** (' KMGTPE'.index(m[2].upper()) if m[2] else 0))


def properties(values):
    out = {'memory.swap.max': '0', 'memory.oom.group': '0'}
    for key, value in values.items():
        if key in ('MemoryMax', 'MemorySwapMax'):
            out['memory.max' if key == 'MemoryMax' else 'memory.swap.max'] = memory(value)
        elif key == 'OOMPolicy' and value == 'continue':
            pass
        elif key == 'CPUQuota':
            try:
                n = Decimal(value.removesuffix('%'))
                if not value.endswith('%') or not n.is_finite() or n <= 0:
                    raise ValueError('invalid CPUQuota')
                out['cpu.max'] = f'{max(1, int(n * 1000))} 100000'
            except InvalidOperation:
                raise ValueError('invalid CPUQuota') from None
        elif key == 'AllowedCPUs':
            cpus = value.replace(' ', ',')
            if not re.fullmatch(r'\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*', cpus):
                raise ValueError('invalid AllowedCPUs')
            if any(int(a) > int(b) for a, b in re.findall(r'(\d+)-(\d+)', cpus)):
                raise ValueError('invalid AllowedCPUs range')
            out['cpuset.cpus'] = cpus
        else:
            raise ValueError('unsupported scope property: ' + key)
    return out


def parse(argv):
    values, unit, flags = {}, None, set()
    i = 0
    while i < len(argv):
        arg = argv[i]
        i += 1
        if arg == '--':
            break
        if not arg.startswith('-'):
            i -= 1
            break
        if arg in ('--user', '--scope', '-q', '--quiet'):
            flags.add(arg)
        elif arg in ('-p', '--property', '--unit'):
            if i == len(argv):
                raise ValueError('missing option argument')
            value = argv[i]
            i += 1
            if arg == '--unit':
                unit = value
            else:
                k, sep, v = value.partition('=')
                if not sep or k in values:
                    raise ValueError('invalid or duplicate property')
                values[k] = v
        elif arg.startswith('--unit='):
            unit = arg.split('=', 1)[1]
        elif arg.startswith('--property=') or (arg.startswith('-p') and len(arg) > 2):
            value = arg.split('=', 1)[1] if arg.startswith('--') else arg[2:]
            k, sep, v = value.partition('=')
            if not sep or k in values:
                raise ValueError('invalid or duplicate property')
            values[k] = v
        else:
            raise ValueError('unsupported systemd-run option: ' + arg)
    if '--scope' not in flags or not argv[i:]:
        raise ValueError('--scope and a command are required')
    if unit and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', unit):
        raise ValueError('invalid unit name')
    name = unit.removesuffix('.scope') if unit else uuid.uuid4().hex
    if not name.startswith('cf-'):
        name = 'cf-' + name
    return name + '.scope', properties(values), argv[i:]


def setup():
    root = current()
    if root == CGROUP:
        raise RuntimeError('refusing to delegate the host cgroup root')
    # Restart: the entrypoint starts back in Docker's parent, or in the service leaf.
    if root.name == 'service.scope':
        root = root.parent
    leaf = root / 'service.scope'
    leaf.mkdir(exist_ok=True)
    for pid in (root / 'cgroup.procs').read_text().split():
        try:
            (leaf / 'cgroup.procs').write_text(pid)
        except ProcessLookupError:
            pass
    needed = {'memory', 'cpu', 'cpuset'}
    if not needed <= set((root / 'cgroup.controllers').read_text().split()):
        raise RuntimeError('memory, cpu and cpuset must be delegated by Docker')
    (root / 'cgroup.subtree_control').write_text('+memory +cpu +cpuset')
    (leaf / 'memory.max').write_text(memory(os.environ.get('CF_V2_SERVICE_MEMORY_MAX', '5G')))
    (leaf / 'memory.swap.max').write_text('0')
    os.environ['CF_CGROUP_PARENT'] = str(root)
    os.environ['CF_SCOPE_BACKEND'] = 'cfscope'
    return root


def reap(root=None):
    root = root or parent()
    removed = 0
    with scope_lock():
        for scope in root.glob('cf-*.scope'):
            try:
                scope.rmdir()  # The kernel refuses populated groups; never kill to clean up.
                removed += 1
            except OSError:
                pass
    return removed


def main(argv=None):
    try:
        name, limits, command = parse(sys.argv[1:] if argv is None else argv)
    except ValueError as exc:
        print('cfscope: ' + str(exc), file=sys.stderr)
        return 2
    try:
        root = parent()
        with scope_lock():
            scope = root / name
            scope.mkdir()  # Named scopes are exclusive, never attach to another job.
            try:
                for key, value in limits.items():
                    (scope / key).write_text(value)
                (scope / 'cgroup.procs').write_text(str(os.getpid()))
            except BaseException:
                scope.rmdir()
                raise
        os.environ['CF_SCOPE_BACKEND'] = 'cfscope'
        os.execvp(command[0], command)
    except FileNotFoundError:
        print('cfscope: executable or cgroup file missing', file=sys.stderr)
        return 127
    except PermissionError:
        print('cfscope: executable or cgroup permission denied', file=sys.stderr)
        return 126
    except (OSError, RuntimeError) as exc:
        print('cfscope: ' + str(exc), file=sys.stderr)
        return 1
