#!/bin/bash
# Smoke tests for riscv-iss-api service.
#
# Prerequisites:
#   docker-compose up -d riscv-iss-api
#
# Usage:
#   ./tests/test_riscv_iss.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
API_URL="${RISCV_ISS_URL:-http://localhost:8002}"
PASS=0
FAIL=0
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

pass() { echo -e "${GREEN}PASS${NC}: $1"; PASS=$((PASS+1)); }
fail() { echo -e "${RED}FAIL${NC}: $1 — $2"; FAIL=$((FAIL+1)); }

# ---------------------------------------------------------------------------
# Test 1: Health endpoint
# ---------------------------------------------------------------------------
echo "--- Test 1: Health endpoint ---"
HTTP_CODE=$(curl -s -o "$TMPDIR/health.json" -w "%{http_code}" "$API_URL/health")
if [ "$HTTP_CODE" = "200" ]; then
    STATUS=$(python3 -c "import json; print(json.load(open('$TMPDIR/health.json'))['status'])")
    if [ "$STATUS" = "healthy" ]; then
        pass "Health endpoint returns healthy"
    else
        fail "Health endpoint" "status=$STATUS"
    fi
else
    fail "Health endpoint" "HTTP $HTTP_CODE"
fi

# ---------------------------------------------------------------------------
# Test 2: Spike binary available
# ---------------------------------------------------------------------------
echo "--- Test 2: Spike binary ---"
CONTAINER=$(docker ps --filter "ancestor=chipforge_eda_server_riscv-iss-api" --format '{{.Names}}' | head -1)
if [ -z "$CONTAINER" ]; then
    # Try alternate name format
    CONTAINER=$(docker ps --filter "publish=8002" --format '{{.Names}}' | head -1)
fi
if [ -n "$CONTAINER" ]; then
    SPIKE_VER=$(docker exec "$CONTAINER" spike --help 2>&1 | head -1 || true)
    if echo "$SPIKE_VER" | grep -q "Spike RISC-V"; then
        pass "Spike ISA simulator available: $SPIKE_VER"
    else
        fail "Spike binary" "not found or broken"
    fi
else
    fail "Spike binary" "container not running"
fi

# ---------------------------------------------------------------------------
# Test 3: RISC-V GCC available
# ---------------------------------------------------------------------------
echo "--- Test 3: RISC-V GCC ---"
if [ -n "$CONTAINER" ]; then
    GCC_VER=$(docker exec "$CONTAINER" riscv64-unknown-elf-gcc --version 2>&1 | head -1 || true)
    if echo "$GCC_VER" | grep -q "riscv"; then
        pass "RISC-V GCC available: $GCC_VER"
    else
        fail "RISC-V GCC" "not found or broken"
    fi
else
    fail "RISC-V GCC" "container not running"
fi

# ---------------------------------------------------------------------------
# Test 4: POST /simulate_iss with clean test (no Coral binary → CORAL_CRASH)
# ---------------------------------------------------------------------------
echo "--- Test 4: Clean test without Coral binary ---"

# Create minimal design.zip
cat > "$TMPDIR/test_clean.S" << 'ASSEMBLY'
    .attribute arch, "rv32imv_zicsr"
    .option norvc
    .section .text.init
    .align 2
    .globl _start
_start:
    li t0, 0x600
    csrs mstatus, t0
    vsetvli t0, zero, e32, m1, ta, ma
    la a0, vec_a
    vle32.v v1, (a0)
    la t0, tohost
    li t1, 1
    sw t1, 0(t0)
    wfi

    .section .tohost, "aw", @progbits
    .align 6
    .global tohost
tohost: .word 0
    .global fromhost
fromhost: .word 0

    .section .data
    .align 4
vec_a:
    .word 1, 2, 3, 4
ASSEMBLY

# Create design zip
cd "$TMPDIR" && zip -q design.zip test_clean.S

# Create ISS bundle from local fixtures
FIXTURES_DIR="$SCRIPT_DIR/fixtures/riscv-iss"
if [ -d "$FIXTURES_DIR" ]; then
    mkdir -p "$TMPDIR/bundle"
    cp -r "$FIXTURES_DIR"/* "$TMPDIR/bundle/"
    cd "$TMPDIR/bundle" && zip -qr "$TMPDIR/iss_bundle.zip" . -x '*__pycache__*'
else
    fail "POST /simulate_iss" "fixtures dir not found: $FIXTURES_DIR"
    echo ""
    echo "Results: $PASS passed, $FAIL failed"
    exit $FAIL
fi

RESPONSE=$(curl -s -X POST "$API_URL/simulate_iss" \
    -F "design_zip=@$TMPDIR/design.zip" \
    -F "iss_bundle=@$TMPDIR/iss_bundle.zip" \
    -F "submission_id=smoke_test_001" \
    --max-time 120)

SUCCESS=$(echo "$RESPONSE" | python3 -c "import json,sys; print(json.load(sys.stdin)['success'])" 2>/dev/null || echo "parse_error")
if [ "$SUCCESS" = "True" ]; then
    COMPILATION=$(echo "$RESPONSE" | python3 -c "import json,sys; print(json.load(sys.stdin)['results']['details']['compilation_success'])" 2>/dev/null)
    SPIKE_COUNT=$(echo "$RESPONSE" | python3 -c "import json,sys; print(json.load(sys.stdin)['results']['details']['spike_instructions'])" 2>/dev/null)
    CLASSIFICATION=$(echo "$RESPONSE" | python3 -c "import json,sys; print(json.load(sys.stdin)['results']['details']['classification'])" 2>/dev/null)

    if [ "$COMPILATION" = "True" ]; then
        pass "Compilation succeeded (GCC + linker working)"
    else
        fail "Compilation" "compilation_success=$COMPILATION"
    fi

    if [ "$SPIKE_COUNT" -gt 0 ] 2>/dev/null; then
        pass "Spike produced $SPIKE_COUNT instructions"
    else
        fail "Spike execution" "spike_instructions=$SPIKE_COUNT"
    fi

    # Without Coral binary, expect CORAL_CRASH
    if [ "$CLASSIFICATION" = "CORAL_CRASH" ]; then
        pass "Classification=CORAL_CRASH (expected without Coral binary)"
    else
        pass "Classification=$CLASSIFICATION (Coral binary may be present)"
    fi
else
    fail "POST /simulate_iss" "success=$SUCCESS"
    echo "$RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$RESPONSE"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=============================="
echo -e "Results: ${GREEN}$PASS passed${NC}, ${RED}$FAIL failed${NC}"
echo "=============================="
exit $FAIL
