#!/usr/bin/env python3
"""fetch.py URL SHA256 DEST -- download at image-build time and refuse anything but the pinned bytes."""
import hashlib
import sys
import time
import urllib.request

url, want, dest = sys.argv[1:4]
for attempt in range(5):
    try:
        h = hashlib.sha256()
        with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
            while chunk := r.read(1 << 20):
                h.update(chunk)
                f.write(chunk)
        break
    except OSError as e:
        print(f"fetch {url}: {e}; retry {attempt + 1}", file=sys.stderr)
        time.sleep(5 * (attempt + 1))
else:
    raise SystemExit(f"fetch failed: {url}")
if h.hexdigest() != want:
    raise SystemExit(f"sha256 mismatch for {url}: got {h.hexdigest()}, pinned {want}")
