"""Set up only this container's cgroup delegation, then start its loader."""
import os
from pathlib import Path
import sys

from .cgroups import setup


def main():
    setup()
    ptrace_scope = Path('/proc/sys/kernel/yama/ptrace_scope')
    if ptrace_scope.exists() and int(ptrace_scope.read_text()) > 2:
        raise RuntimeError('ptrace is disabled by the host; evaluator observers cannot run')
    os.execv(sys.executable, [sys.executable, '-B', '-m', 'loader.server', *sys.argv[1:]])


if __name__ == '__main__':
    main()
