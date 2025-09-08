#!/usr/bin/env python3
import argparse
import json
import subprocess
import re
import sys
from pathlib import Path

# Regex for extracting functionality score from sim output
FUNC_RE = re.compile(r'FUNC_SCORE:\s*([0-9]*\.?[0-9]+)')

def run_cmd(cmd, cwd):
    """Run shell command and capture output"""
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return result.returncode, result.stdout + result.stderr

def extract_func_score(output: str):
    """Parse FUNC_SCORE=X.X from simulator output"""
    m = FUNC_RE.search(output)
    if m:
        try:
            val = float(m.group(1))
            return max(0.0, min(1.0, val))  # clamp 0..1
        except Exception:
            return None
    return None

def main():
    parser = argparse.ArgumentParser(description="Evaluator: run Verilator simulation and compute functionality score")
    parser.add_argument("--design", required=True, help="Path to extracted design directory")
    parser.add_argument("--top", required=True, help="Top module name")
    parser.add_argument("--resources", help="Optional evaluator resources (where TB files are)")
    args = parser.parse_args()

    workdir = Path(args.design)

    # Look for rtl.f anywhere under design/
    filelist = None
    for path in workdir.rglob("rtl.f"):
        filelist = path
        break

    if not filelist:
        print(json.dumps({
            "success": False,
            "error_message": f"rtl.f not found under {workdir}"
        }))
        sys.exit(1)

    # ---------------------------
    # Collect evaluator TB files
    # ---------------------------
    tb_files = []
    if args.resources:
        resources_dir = Path(args.resources)
        # collect any .v / .sv files in evaluator resources
        tb_files = list(resources_dir.rglob("*.v")) + list(resources_dir.rglob("*.sv"))

    # ---------------------------
    # Step 1: Verilator compile
    # ---------------------------
    verilator_cmd = [
        "verilator",
        "--timing", "--binary", "-Wall",
        "--Wno-fatal",
        "--top-module", args.top,
        "--cc", "--exe",
        "--Wno-PROCASSWIRE", "--Wno-SYNCASYNCNET", "--Wno-BLKSEQ",
        "--Wno-WIDTHTRUNC", "--Wno-UNUSEDSIGNAL", "--Wno-UNUSEDPARAM",
        "-CFLAGS", "-std=c++17",
        "-f", str(filelist),
    ] + [str(f) for f in tb_files] + [
        "+define+VIVADO_SIM", "+define+USE_SRAM", "+define+tracer",
        "--trace"
    ]

    rc, out_compile = run_cmd(verilator_cmd, cwd=filelist.parent)
    if rc != 0:
        print(json.dumps({
            "success": False,
            "error_message": "Verilator compile failed",
            "verilator_log": out_compile
        }))
        sys.exit(1)

    # ---------------------------
    # Step 2: Make build
    # ---------------------------
    makefile = f"V{args.top}.mk"
    rc, out_make = run_cmd(["make", "-C", "obj_dir", "-j", "-f", makefile], cwd=filelist.parent)
    if rc != 0:
        print(json.dumps({
            "success": False,
            "error_message": "Make failed when building simulation",
            "verilator_log": out_make
        }))
        sys.exit(1)

    # ---------------------------
    # Step 3: Run simulation
    # ---------------------------
    exe = filelist.parent / f"obj_dir/V{args.top}"
    rc, sim_out = run_cmd([str(exe)], cwd=filelist.parent)
    if rc != 0:
        print(json.dumps({
            "success": False,
            "error_message": "Simulation failed",
            "verilator_log": sim_out
        }))
        sys.exit(1)

    # ---------------------------
    # Extract functionality score
    # ---------------------------
    func_score = extract_func_score(sim_out)

    result = {
        "success": True,
        "functionality_score": func_score if func_score is not None else 0.0,
        "details": {
            "note": "Evaluator ran Verilator simulation",
            "design": str(workdir),
            "top_module": args.top,
            "resources_used": bool(args.resources),
            "tb_files": [str(f) for f in tb_files],
        },
        "simulation_output": sim_out
    }

    print(json.dumps(result))
    sys.exit(0)

if __name__ == "__main__":
    main()
