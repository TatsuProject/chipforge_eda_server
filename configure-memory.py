#!/usr/bin/env python3
"""Set a conservative default once; preserve explicit operator configuration."""
from pathlib import Path
import re

HEADROOM = 4 << 30  # OS, Docker, gateway and loader reserve

def configure(path=Path('.env'), meminfo=Path('/proc/meminfo')):
    text = path.read_text() if path.exists() else ''
    pattern = r'^CF_V2_EVALUATOR_MEMORY_LIMIT=(.*)$'
    match = re.search(pattern, text, re.M)
    if match and match.group(1).strip():
        return
    total = int(re.search(r'^MemTotal:\s+(\d+) kB', meminfo.read_text(), re.M)[1]) * 1024
    limit = total - HEADROOM
    if limit < (1 << 30):
        raise SystemExit('At least 5 GiB host RAM is required to reserve 4 GiB headroom')
    line = f'CF_V2_EVALUATOR_MEMORY_LIMIT={limit}'
    text = re.sub(pattern, line, text, flags=re.M) if match else text.rstrip() + '\n' + line + '\n'
    path.write_text(text)
    print(f'Evaluator ceiling: {limit} bytes; reserved headroom: {HEADROOM} bytes')

if __name__ == '__main__':
    configure()
