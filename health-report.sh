#!/usr/bin/env bash
# ChipForge validator health report. Read-only: it changes nothing, and prints no key, token or .env value.
#
#   cd chipforge_eda_server && git pull && sudo bash health-report.sh
#
# Writes the report to the screen and to chipforge-health-<UTC time>.txt; send that file to the ChipForge team.
# Covers: the machine (CPU, RAM, load, disk), the EDA server (versions, evaluator, watchdog, cleanups, unfinished
# evaluations, how long each stage took in the last evaluations), and the validator neuron (version, recent
# designs, weights, errors). sudo is needed to read the evaluator's data directory.
set -u
cd "$(dirname "$0")"
ROOT=$(grep -E '^CF_V2_ROOT=' .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"' ")
ROOT=${ROOT:-/var/lib/chipforge-v2}
EDA=${EDA_URL:-http://127.0.0.1:8080}
OUT="chipforge-health-$(date -u +%Y%m%d-%H%M%S).txt"
TOKEN=$(grep -E '^V2_ADMIN_TOKEN=' .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"' ")
AUTH=(); [ -n "$TOKEN" ] && AUTH=(-H "Authorization: Bearer $TOKEN")

section() { printf '\n===== %s =====\n' "$1"; }

report() {
  echo "ChipForge validator health report, $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
  [ "$(id -u)" -ne 0 ] && echo "NOTE: not run with sudo, the evaluator's data could not be read: run 'sudo bash health-report.sh'"

  section "Machine"
  echo "CPU:      $(lscpu 2>/dev/null | sed -n 's/^Model name:[[:space:]]*//p' | head -1)"
  echo "Threads:  $(nproc 2>/dev/null)   (sockets $(lscpu 2>/dev/null | sed -n 's/^Socket(s):[[:space:]]*//p'), cores/socket $(lscpu 2>/dev/null | sed -n 's/^Core(s) per socket:[[:space:]]*//p'), threads/core $(lscpu 2>/dev/null | sed -n 's/^Thread(s) per core:[[:space:]]*//p'))"
  echo "Clock:    $(grep -m1 MHz /proc/cpuinfo 2>/dev/null | sed 's/.*: //') MHz now, max $(lscpu 2>/dev/null | sed -n 's/^CPU max MHz:[[:space:]]*//p') MHz"
  echo "Hypervisor: $(lscpu 2>/dev/null | sed -n 's/^Hypervisor vendor:[[:space:]]*//p')"
  free -g 2>/dev/null | sed -n '1,2p'
  echo "Load:     $(cut -d' ' -f1-3 /proc/loadavg) (1, 5, 15 min)   uptime: $(uptime -p 2>/dev/null)"
  echo "Top CPU users:"; ps -eo pcpu,pmem,etime,comm --sort=-pcpu 2>/dev/null | head -8
  df -h / "$ROOT" 2>/dev/null | awk '!seen[$0]++'

  section "Docker"
  docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' 2>/dev/null | grep -iE 'chipforge|eda|cf-|validator|gateway' || echo "(docker not reachable)"

  section "EDA server"
  echo "Repository: $(git log -1 --format='%h %cd' --date=short 2>/dev/null)   VERSION file: $(cat gateway/VERSION 2>/dev/null)"
  curl -s -m 10 "$EDA/v2/health" | python3 -c '
import json, sys
try:
    h = json.load(sys.stdin)
except Exception:
    print("GET /v2/health: no answer (is the EDA server running?)"); sys.exit()
print("eda_version:", h.get("eda_version"), "| ready:", h.get("ready"), "| active evaluator:", str(h.get("active_evaluator"))[:16])
print("install failures:", h.get("install_failures") or "none")
d = h.get("disk") or {}
print("disk: %s GiB free of %s GiB, evaluator versions on disk: %s" % (d.get("free_gib"), d.get("total_gib"), d.get("evaluator_versions")))
print("last cleanup of old evaluators:", h.get("last_cleanup") or "none yet")
dc = h.get("last_design_cleanup")
print("last cleanup of finished designs:", ("%s, %s design(s), freed %s GiB" % (dc.get("at"), len(dc.get("designs") or []), dc.get("freed_gib"))) if dc else "none yet (EDA server older than 1.0.3, or nothing finished since it started)")
w = h.get("watchdog")
print("watchdog:", ("on" if w.get("enabled") else "OFF") + ", last pass " + str(w.get("last_pass")) if w else "missing (EDA server older than 1.0.2: update it)")
for c in (w or {}).get("recent_cancellations") or []:
    print("   cancelled %s at %s by %s: %s" % (c.get("request_id", "")[:48], c.get("at"), c.get("by"), c.get("reason")))
'
  echo "Unfinished evaluations (GET /v2/jobs):"
  curl -s -m 10 "${AUTH[@]}" "$EDA/v2/jobs" | python3 -c '
import json, sys
try:
    jobs = json.load(sys.stdin).get("jobs")
except Exception:
    jobs = None
if jobs is None:
    print("   not available (EDA server older than 1.0.2, or a token is required)")
for j in jobs or []:
    print("   %-60s %-8s sent %s  started %s  deadline %s" % (j["request_id"][:60], j["phase"], j.get("sent_at"), j.get("started_at"), j.get("deadline")))
if jobs == []:
    print("   none")
'

  section "Evaluator data (needs sudo)"
  if [ -r "$ROOT/loader/http.sqlite" ]; then
    du -sh "$ROOT"/* 2>/dev/null | sort -rh | head -8
    echo "Design folders still on disk:"
    for d in "$ROOT"/state/*/components/*/wp-o "$ROOT"/state/*/runs/jobs; do
      [ -d "$d" ] && du -sh "$d"/sub_* 2>/dev/null | sed "s#$ROOT/state/##"
    done | head -20
    echo "Last 12 evaluation requests (loader):"
    python3 - "$ROOT" <<'EOF'
import sqlite3, sys, datetime
root = sys.argv[1]
db = sqlite3.connect(f"file:{root}/loader/http.sqlite?mode=ro", uri=True)
for rid, phase, created, fwd, err in db.execute(
        "SELECT request_id, phase, created, forwarded, error FROM jobs ORDER BY created DESC LIMIT 12"):
    t = datetime.datetime.fromtimestamp(created, datetime.timezone.utc).strftime("%m-%d %H:%M")
    print(f"   {t}  {phase:<10} {rid[:64]:<64} {err or ''}")
EOF
    echo "Stages of the last evaluations (workers.log; times UTC):"
    python3 - "$ROOT" <<'EOF'
import glob, os, re, sys
from collections import OrderedDict
root = sys.argv[1]
logs = sorted(glob.glob(f"{root}/state/*/workers.log"), key=os.path.getmtime)
if not logs:
    print("   no workers.log found"); sys.exit()
line_re = re.compile(r"^\[(\d\d:\d\d:\d\d)\] (sub_[0-9a-f]+): (.*)$")
runs = OrderedDict()
for path in logs[-2:]:
    for line in open(path, errors="replace"):
        m = line_re.match(line.strip())
        if m:
            runs.setdefault(m.group(2), []).append((m.group(1), m.group(3)))
for sub, events in list(runs.items())[-4:]:
    first, last = events[0][0], events[-1][0]
    retries = sum("system error" in e for _, e in events)
    print(f"   {sub}: {first} -> {last}, {len(events)} events, {retries} system-error retries")
    for t, e in events:
        e = re.sub(r"\(.*", "", e).strip()            # stage and verdict only, never scores
        if any(k in e for k in ("passed", "failed", "error", "timed out", "cancel", "rejected", "evaluation root")):
            print(f"      {t} {e[:110]}")
EOF
  else
    echo "Cannot read $ROOT (run with sudo, or set CF_V2_ROOT in .env)"
  fi

  section "Validator neuron"
  V=$(docker ps --format '{{.Names}}' 2>/dev/null | grep -iE 'validator' | grep -viE 'eda|gateway|evaluator' | head -1)
  if [ -n "$V" ]; then
    echo "Container: $V ($(docker ps --filter name="^$V$" --format '{{.Status}}'); its logs start when it last started)"
    docker exec "$V" python -c "import chipforge; print('neuron version:', chipforge.__version__)" 2>/dev/null || echo "neuron version: unknown"
    LOGS=$(docker logs --since 24h "$V" 2>&1)
  else
    P=$(pgrep -af 'neurons/validator' | head -1)
    echo "Not in Docker; process: ${P:-not found}"
    LOGS=$( (pm2 logs --nostream --lines 3000 2>/dev/null; journalctl --since '24 hours ago' -u '*validator*' 2>/dev/null) | cat)
  fi
  echo "Designs in the last 24 h:"
  echo "$LOGS" | grep -E "Batch split-|Evaluated |timed out|VALIDATOR_EDA|KILL SWITCH|Cancelled EDA" | tail -15 | cut -c1-200
  echo "Weights set in the last 24 h: $(echo "$LOGS" | grep -c 'Weights set on chain')   last: $(echo "$LOGS" | grep 'Weights set on chain' | tail -1 | cut -c1-19)"
  echo "Errors in the last 24 h: $(echo "$LOGS" | grep -c ' - ERROR - ')"
  echo "$LOGS" | grep ' - ERROR - ' | sed -E 's/^[0-9-]+ [0-9:]+ - //' | cut -c1-160 | sort | uniq -c | sort -rn | head -8
}

report 2>&1 | tee "$OUT"
echo
echo "Saved to $(pwd)/$OUT - send this file to the ChipForge team."
