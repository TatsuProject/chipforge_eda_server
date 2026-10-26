#!/usr/bin/env bash
# Install the evaluator named in .env (CF_V2_EVALUATOR_ZIP; its signature is the same path + .sshsig) and wait
# until it is ready. Run by `make start`; nothing to do when CF_V2_EVALUATOR_ZIP is empty. Safe to repeat: an
# evaluator that is already ready is not installed again.
set -euo pipefail

env_value() {
  local v
  v=$(grep -E "^$1=" .env 2>/dev/null | tail -n 1 | cut -d= -f2-) || true
  v=${v%\"}; v=${v#\"}; v=${v%\'}; v=${v#\'}
  printf '%s' "$v"
}

ZIP=$(env_value CF_V2_EVALUATOR_ZIP)
[ -n "$ZIP" ] || exit 0
ZIP=${ZIP/#\~/$HOME}
SIG="$ZIP.sshsig"
for f in "$ZIP" "$SIG"; do
  [ -f "$f" ] || { echo "evaluator: $f not found (CF_V2_EVALUATOR_ZIP in .env)" >&2; exit 1; }
done

HOST=$(env_value EDA_BIND_ADDRESS)
case "$HOST" in ""|0.0.0.0) HOST=127.0.0.1 ;; esac
URL=${CF_EDA_URL:-http://$HOST:8080}
AUTH=()
TOKEN=$(env_value V2_ADMIN_TOKEN)
[ -z "$TOKEN" ] || AUTH=(-H "Authorization: Bearer $TOKEN")

SHA=$(sha256sum "$ZIP" | cut -d' ' -f1)
phase() { curl -sS "$URL/v2/evaluator/$SHA" 2>/dev/null | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("phase") or "")
except Exception: print("")'; }

for _ in $(seq 60); do curl -fsS "$URL/health" >/dev/null 2>&1 && break; sleep 2; done
curl -fsS "$URL/health" >/dev/null 2>&1 || { echo "evaluator: the gateway at $URL does not answer" >&2; exit 1; }

if [ "$(phase)" = ready ]; then
  echo "Evaluator ready: $SHA"
  exit 0
fi
echo "Installing evaluator $SHA (first time: about 3 min plus a ~315 MB download)"
curl -fsS "${AUTH[@]}" -F "evaluator_zip=@$ZIP" -F "evaluator_sig=@$SIG" "$URL/v2/evaluator" >/dev/null

last=""
deadline=$((SECONDS + 3 * 3600))
while [ $SECONDS -lt $deadline ]; do
  p=$(phase)
  [ "$p" = "$last" ] || { [ -z "$p" ] || echo "  $p"; last=$p; }
  case "$p" in
    ready) echo "Evaluator ready: $SHA"; exit 0 ;;
    failed)
      echo "evaluator: install failed. Details: curl -sS $URL/v2/health (install_failures names the log)" >&2
      curl -sS "$URL/v2/evaluator/$SHA" >&2; echo >&2
      exit 1 ;;
  esac
  sleep 10
done
echo "evaluator: still not ready after 3 h; check curl -sS $URL/v2/evaluator/$SHA" >&2
exit 1
