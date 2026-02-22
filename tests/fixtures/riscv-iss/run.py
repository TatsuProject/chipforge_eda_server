#!/usr/bin/env python3
"""
CoralNPU Bug Bounty Evaluator for ChipForge Challenge 0011

Miners submit .S assembly files that expose bugs in Google's CoralNPU RTL.
This script:
1. Validates the .S submission (size, structure, safety)
2. Compiles to ELF and converts to memory images
3. Runs Spike (reference ISA sim) to get golden trace
4. Runs Coral (pre-built Vtb_top) to get DUT trace
5. Compares traces with false-positive filtering
6. Returns JSON: bug_found, fingerprint, score

Dual-linker approach:
  - Coral ELF: linked at 0x0 (ITCM) / 0x10000 (DTCM)
  - Spike ELF: linked at 0x80000000 / 0x80010000 (avoids Spike's built-in devices)
  - Spike trace is post-processed: PCs and PC-relative register values are
    adjusted by subtracting the offset so they match Coral's address space.
"""

import os
import sys
import json
import hashlib
import subprocess
import argparse
import csv
from pathlib import Path


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_S_FILE_SIZE = 100 * 1024       # 100 KB
MAX_SIM_CYCLES = 200000            # Maximum simulation cycles for Coral
SPIKE_TIMEOUT = 30                 # Spike execution timeout (seconds)
CORAL_TIMEOUT = 120                # Coral execution timeout (seconds)
COMPILE_TIMEOUT = 60               # Compilation timeout (seconds)
HANG_THRESHOLD = 0.50              # If Coral < 50% of Spike instructions, it's a hang

# Spike ELF is linked at 0x80000000 to avoid Spike's built-in devices at 0x0-0x2000.
# All Spike PCs and PC-relative register values are adjusted by this offset.
SPIKE_PC_OFFSET = 0x80000000

# Tool paths (overridable via environment)
RISCV_GCC = os.environ.get("RISCV_GCC", "riscv64-unknown-elf-gcc")
RISCV_OBJCOPY = os.environ.get("RISCV_OBJCOPY", "riscv64-unknown-elf-objcopy")
SPIKE_BIN = os.environ.get("SPIKE_BIN", "spike")
CORAL_BIN = os.environ.get("CORAL_BIN", "/opt/coralnpu/bin/Vtb_top")

# ISA and ABI for compilation
RISCV_ISA = "rv32imf_zicsr_zve32f"
RISCV_ABI = "ilp32f"

# Spike ISA string: use full V extension (implies VLEN=128, ELEN=64)
# We use full V rather than zve32f because zve32f may enforce stricter
# constraints that don't match CoralNPU's actual behavior. The full V
# extension is more permissive and matches our verif environment.
SPIKE_ISA = "rv32imfv_zicsr"


def log(msg):
    """Print log message to stderr (stdout reserved for JSON output)"""
    print(f"[BugBounty] {msg}", file=sys.stderr)


def run_cmd(cmd, cwd=None, timeout=600, capture=True, env=None):
    """Run shell command with timeout"""
    log(f"Running: {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    try:
        result = subprocess.run(
            cmd,
            shell=isinstance(cmd, str),
            cwd=cwd,
            timeout=timeout,
            capture_output=capture,
            text=True,
            env=env,
        )
        return result
    except subprocess.TimeoutExpired:
        log(f"Command timed out after {timeout}s")
        return None
    except Exception as e:
        log(f"Command failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Step 1: Validate .S file
# ---------------------------------------------------------------------------

def find_s_file(design_dir):
    """Find .S assembly file in miner's submission directory.

    Returns (path, error_detail) — error_detail is None on success,
    or a string listing what was found instead.
    """
    design_path = Path(design_dir)

    s_files = list(design_path.glob("*.S")) + list(design_path.glob("*.s"))
    s_files += list(design_path.glob("*/*.S")) + list(design_path.glob("*/*.s"))

    if not s_files:
        # List what IS in the submission so miner knows what went wrong
        all_files = [str(p.relative_to(design_path)) for p in design_path.rglob("*") if p.is_file()]
        if all_files:
            file_list = ", ".join(all_files[:20])
            if len(all_files) > 20:
                file_list += f" ... and {len(all_files) - 20} more"
            detail = (f"No .S or .s assembly file found in submission. "
                      f"Files found: [{file_list}]. "
                      f"Your design.zip must contain a .S assembly file (e.g., test.S, bug.S).")
        else:
            detail = ("Submission directory is empty. "
                      "Your design.zip must contain a .S assembly file (e.g., test.S, bug.S).")
        return None, detail

    for f in s_files:
        if f.stem.lower().startswith(('test', 'bug', 'exploit', 'submission')):
            return f, None

    return s_files[0], None


def validate_s_file(s_file):
    """Validate .S assembly file for safety and correctness."""
    s_path = Path(s_file)

    if not s_path.exists():
        return False, f"File not found: {s_file}"

    file_size = s_path.stat().st_size
    if file_size > MAX_S_FILE_SIZE:
        return False, (f"File too large: {file_size:,} bytes (max {MAX_S_FILE_SIZE // 1024} KB). "
                       f"Reduce your assembly file size.")
    if file_size == 0:
        return False, "Assembly file is empty (0 bytes). Submit a valid .S file with RISC-V instructions."

    try:
        content = s_path.read_text()
    except UnicodeDecodeError:
        return False, ("File is not valid text/UTF-8. "
                       "Submit a plain-text .S assembly file, not a binary or compiled ELF.")
    except Exception as e:
        return False, f"Cannot read file: {e}"

    for directive in ['#include', '.include']:
        if directive in content:
            return False, (f"File contains '{directive}' directive. "
                           f"Submissions must be self-contained — all code in a single .S file, "
                           f"no external includes.")

    if '_start' not in content:
        return False, ("Missing '_start' entry point. Your .S file must define a '_start' label. "
                       "Example:\n  .globl _start\n_start:\n  <your code>")

    if '.text' not in content:
        return False, ("Missing '.text' section. Your .S file must have a .text section. "
                       "Example:\n  .section .text.init\n  .align 2\n  .globl _start\n_start:")

    return True, None


# ---------------------------------------------------------------------------
# Step 2: Compile .S to ELF and memory images
# ---------------------------------------------------------------------------

def compile_s_to_elf(s_file, link_ld, output_dir, stem_suffix=""):
    """Compile .S file to ELF using RISC-V GCC."""
    s_path = Path(s_file)
    elf_path = Path(output_dir) / f"{s_path.stem}{stem_suffix}.elf"

    cmd = [
        RISCV_GCC,
        "-march=" + RISCV_ISA,
        "-mabi=" + RISCV_ABI,
        "-nostdlib",
        "-nostartfiles",
        "-T", str(link_ld),
        "-o", str(elf_path),
        str(s_file),
    ]

    result = run_cmd(cmd, timeout=COMPILE_TIMEOUT)
    if result is None:
        return None, (f"Compilation timed out after {COMPILE_TIMEOUT}s. "
                      f"Your assembly file may be too large or contain recursive macros.")
    if result.returncode != 0:
        # Clean up GCC error: strip the full path prefix to show only the filename
        raw_err = (result.stderr or "").strip()
        # Replace absolute paths with just the filename for readability
        clean_err = raw_err.replace(str(s_file), s_path.name)
        if len(clean_err) > 2000:
            clean_err = clean_err[:2000] + "\n... (truncated)"
        log(f"Compilation failed: {clean_err}")
        return None, f"GCC compilation failed:\n{clean_err}"

    if not elf_path.exists():
        return None, "Compilation produced no output (ELF file not created)."

    return str(elf_path), None


def elf_to_mem(elf_path, output_dir):
    """Convert ELF to hex memory images for Coral simulation.

    Produces:
    - <name>.mem: instruction memory (ITCM, 0x0000_0000 - 0x0000_FFFF)
    - <name>.data.mem: data memory (DTCM, 0x0001_0000 - 0x0001_FFFF)
    """
    elf = Path(elf_path)
    stem = elf.stem
    out = Path(output_dir)
    itcm_size = 64 * 1024
    dtcm_size = 64 * 1024

    itcm_bin = out / f"{stem}_itcm.bin"
    dtcm_bin = out / f"{stem}_dtcm.bin"

    # Extract code sections
    cmd_itcm = [
        RISCV_OBJCOPY, "-O", "binary",
        "--only-section=.text", "--only-section=.rodata",
        str(elf_path), str(itcm_bin),
    ]
    result = run_cmd(cmd_itcm, timeout=30)
    if result is None or result.returncode != 0:
        log("Failed to extract ITCM binary")
        return None, None

    # Extract data sections
    cmd_dtcm = [
        RISCV_OBJCOPY, "-O", "binary",
        "--only-section=.data", "--only-section=.sdata",
        "--only-section=.tohost", "--only-section=.bss",
        str(elf_path), str(dtcm_bin),
    ]
    result = run_cmd(cmd_dtcm, timeout=30)
    if result is None or result.returncode != 0:
        dtcm_bin.write_bytes(b'')

    imem_path = out / f"{stem}.mem"
    dmem_path = out / f"{stem}.data.mem"

    _bin_to_memhex(itcm_bin, imem_path, itcm_size)
    _bin_to_memhex(dtcm_bin, dmem_path, dtcm_size)

    return str(imem_path), str(dmem_path)


def _bin_to_memhex(bin_path, mem_path, total_size):
    """Convert raw binary to Verilog hex memory format."""
    data = bytearray(total_size)

    if bin_path.exists() and bin_path.stat().st_size > 0:
        with open(bin_path, 'rb') as f:
            raw = f.read()
            data[:len(raw)] = raw[:total_size]

    with open(mem_path, 'w') as f:
        for i in range(0, total_size, 4):
            word = int.from_bytes(data[i:i+4], byteorder='little')
            f.write(f"{word:08x}\n")


# ---------------------------------------------------------------------------
# Step 3: Run Spike (reference simulator)
# ---------------------------------------------------------------------------

def run_spike(elf_path, output_dir):
    """Run Spike ISA simulator to produce reference trace.

    The ELF is linked at 0x80000000 to avoid Spike's built-in device overlap.
    """
    spike_log = Path(output_dir) / "spike.log"

    isa_str = SPIKE_ISA.replace("_z", "_Z")

    cmd = [
        SPIKE_BIN,
        f"--isa={isa_str}",
        "-l", "--log-commits",
        f"--log={spike_log}",
        str(elf_path),
    ]

    result = run_cmd(cmd, timeout=SPIKE_TIMEOUT)

    if result is None:
        log("Spike timed out (possible infinite loop in test)")
        if spike_log.exists() and spike_log.stat().st_size > 0:
            return str(spike_log)
        return None

    if not spike_log.exists() or spike_log.stat().st_size == 0:
        log("Spike produced no output")
        if result:
            log(f"Spike stderr: {result.stderr[:500]}")
        return None

    return str(spike_log)


def _adjust_spike_value(hex_value, offset):
    """Adjust a Spike register value by subtracting the PC offset.

    For scalar values (8 hex chars) that fall in the Spike address range
    (>= offset), subtract offset to map back to Coral's address space.
    This handles auipc and derived address computations.

    Vector values (>8 hex chars) are left unchanged.
    """
    if len(hex_value) > 8:
        # Vector register — don't adjust
        return hex_value
    try:
        val = int(hex_value, 16)
        if val >= offset:
            val -= offset
            return f"{val:08x}"
    except ValueError:
        pass
    return hex_value


def _filter_and_adjust_spike_csv(raw_csv, filtered_csv, min_pc, offset):
    """Filter Spike bootrom and adjust PCs and register values.

    1. Remove instructions with PC < min_pc (Spike bootrom)
    2. Subtract offset from all PCs
    3. Subtract offset from scalar register values >= offset
    4. Renumber instructions from 1
    """
    count = 0
    with open(raw_csv, 'r') as fin, open(filtered_csv, 'w') as fout:
        reader = csv.DictReader(fin)
        writer = csv.DictWriter(fout, fieldnames=reader.fieldnames)
        writer.writeheader()

        for row in reader:
            try:
                pc = int(row['pc'], 16)
            except (ValueError, KeyError):
                continue

            if pc < min_pc:
                continue

            count += 1
            row['order'] = str(count)

            # Adjust PC
            adjusted_pc = pc - offset
            row['pc'] = f"{adjusted_pc:08x}"

            # Adjust scalar register values
            rd_val = row.get('rd_value', '')
            if rd_val and rd_val != '-':
                row['rd_value'] = _adjust_spike_value(rd_val, offset)

            writer.writerow(row)

    return count


def convert_spike_trace(spike_log, output_dir, scripts_dir):
    """Convert Spike log to CSV, filter bootrom, and adjust addresses.

    Returns (csv_path, num_instructions) or (None, 0) on failure.
    """
    raw_csv_path = Path(output_dir) / "spike_raw.csv"
    csv_path = Path(output_dir) / "spike.csv"
    script = Path(scripts_dir) / "spike_trace_to_csv.py"

    if not script.exists():
        log(f"Spike trace converter not found: {script}")
        return None, 0

    cmd = [sys.executable, str(script), str(spike_log), "-o", str(raw_csv_path)]
    result = run_cmd(cmd, timeout=60)

    if result is None or result.returncode != 0:
        log("Spike trace conversion failed")
        return None, 0

    if not raw_csv_path.exists():
        return None, 0

    # Filter bootrom (PC < SPIKE_PC_OFFSET) and adjust all addresses
    num_instr = _filter_and_adjust_spike_csv(
        raw_csv_path, csv_path,
        min_pc=SPIKE_PC_OFFSET,
        offset=SPIKE_PC_OFFSET,
    )
    log(f"Spike: filtered bootrom + adjusted addresses, kept {num_instr} instructions")

    return str(csv_path), num_instr


# ---------------------------------------------------------------------------
# Step 4: Run Coral (DUT)
# ---------------------------------------------------------------------------

def run_coral(imem_path, dmem_path, output_dir, coral_bin=None):
    """Run Coral RTL simulation (pre-built Vtb_top)."""
    if coral_bin is None:
        coral_bin = CORAL_BIN

    if not Path(coral_bin).exists():
        log(f"Coral binary not found: {coral_bin}")
        return None

    trace_log = Path(output_dir) / "coral.log"

    cmd = [
        str(coral_bin),
        f"+TEST_FILE={imem_path}",
        f"+TRACE_FILE={trace_log}",
        f"+TIMEOUT={MAX_SIM_CYCLES}",
    ]

    if dmem_path and Path(dmem_path).exists():
        cmd.append(f"+DATA_FILE={dmem_path}")

    result = run_cmd(cmd, cwd=str(output_dir), timeout=CORAL_TIMEOUT)

    if result is None:
        log("Coral simulation timed out")
        if trace_log.exists() and trace_log.stat().st_size > 0:
            return str(trace_log)
        return None

    if not trace_log.exists() or trace_log.stat().st_size == 0:
        log("Coral produced no trace output")
        return None

    return str(trace_log)


def convert_coral_trace(coral_log, output_dir, scripts_dir):
    """Convert Coral trace log to CSV.

    Returns (csv_path, num_instructions) or (None, 0) on failure.
    """
    csv_path = Path(output_dir) / "coral.csv"
    script = Path(scripts_dir) / "coral_trace_to_csv.py"

    if not script.exists():
        log(f"Coral trace converter not found: {script}")
        return None, 0

    cmd = [sys.executable, str(script), str(coral_log), "-o", str(csv_path)]
    result = run_cmd(cmd, timeout=60)

    if result is None or result.returncode != 0:
        log("Coral trace conversion failed")
        return None, 0

    if not csv_path.exists():
        return None, 0

    with open(csv_path, 'r') as f:
        num_instr = sum(1 for _ in f) - 1
    return str(csv_path), max(0, num_instr)


# ---------------------------------------------------------------------------
# Step 5: Compare traces
# ---------------------------------------------------------------------------

def compare_traces(spike_csv, coral_csv, scripts_dir):
    """Compare Spike and Coral traces using compare_traces.py.

    No --spike-pc-offset needed because we already adjusted PCs and values
    in _filter_and_adjust_spike_csv().
    """
    script = Path(scripts_dir) / "compare_traces.py"

    if not script.exists():
        log(f"Trace comparator not found: {script}")
        return None

    cmd = [
        sys.executable, str(script),
        str(spike_csv), str(coral_csv),
        "--max-errors", "20",
    ]
    result = run_cmd(cmd, timeout=120)

    if result is None:
        log("Trace comparison timed out")
        return None

    output = (result.stdout or '') + (result.stderr or '')

    comparison = {
        'returncode': result.returncode,
        'match': 0,
        'mismatch': 0,
        'waw_tracer_bugs': 0,
        'mask_tail_diffs': 0,
        'cascade_diffs': 0,
        'spike_loop': 0,
        'store_tracer_bugs': 0,
        'output': output,
    }

    for line in output.split('\n'):
        line = line.strip()
        if line.startswith('Match:'):
            try:
                comparison['match'] = int(line.split(':')[1].strip())
            except (ValueError, IndexError):
                pass
        elif line.startswith('Mismatch:'):
            try:
                comparison['mismatch'] = int(line.split(':')[1].strip())
            except (ValueError, IndexError):
                pass
        elif 'WAW tracer bugs:' in line:
            try:
                comparison['waw_tracer_bugs'] = int(line.split(':')[1].strip().split()[0])
            except (ValueError, IndexError):
                pass
        elif 'Mask tail (spec-legal):' in line:
            try:
                comparison['mask_tail_diffs'] = int(line.split(':')[1].strip().split()[0])
            except (ValueError, IndexError):
                pass
        elif 'Mask tail cascades:' in line:
            try:
                comparison['cascade_diffs'] = int(line.split(':')[1].strip().split()[0])
            except (ValueError, IndexError):
                pass
        elif 'Spike loop-back:' in line:
            try:
                comparison['spike_loop'] = int(line.split(':')[1].strip().split()[0])
            except (ValueError, IndexError):
                pass
        elif 'Store tracer bugs:' in line:
            try:
                comparison['store_tracer_bugs'] = int(line.split(':')[1].strip().split()[0])
            except (ValueError, IndexError):
                pass

    return comparison


def extract_first_mismatch(spike_csv, coral_csv):
    """Extract the first real divergence point for bug fingerprinting."""
    try:
        with open(spike_csv, 'r') as f:
            spike = list(csv.DictReader(f))
        with open(coral_csv, 'r') as f:
            coral = list(csv.DictReader(f))
    except Exception:
        return None, None

    min_len = min(len(spike), len(coral))

    for i in range(min_len):
        s = spike[i]
        c = coral[i]

        if s['pc'] != c['pc']:
            return c['pc'], c['inst']

        if s['inst'] != c['inst']:
            return c['pc'], c['inst']

    if len(spike) != len(coral):
        shorter = spike if len(spike) < len(coral) else coral
        if shorter:
            last = shorter[-1]
            return last['pc'], last['inst']

    return None, None


# ---------------------------------------------------------------------------
# Step 6: Classify result and build fingerprint
# ---------------------------------------------------------------------------

def _coral_reached_wfi(coral_log_path):
    """Check if Coral reached WFI by scanning the raw log for the WFI encoding.

    Coral trace lines end with WFI (0x10500073) when the test completes normally.
    The CSV excludes WFI, so we check the raw log directly.
    """
    try:
        with open(coral_log_path, 'r') as f:
            for line in f:
                if '10500073' in line:
                    return True
    except Exception:
        pass
    return False


def classify_result(comparison, spike_instr_count, coral_instr_count,
                    spike_csv, coral_csv, coral_log=None):
    """Classify whether a real bug was found."""
    if comparison is None:
        return False, None, "COMPARISON_FAILED"

    real_mismatches = comparison['mismatch']

    if real_mismatches > 0:
        pc, inst = extract_first_mismatch(spike_csv, coral_csv)
        if pc and inst:
            fingerprint = f"MISMATCH:{pc}:{inst}"
        else:
            fingerprint = "MISMATCH:unknown:unknown"
        return True, fingerprint, "TRACE_MISMATCH"

    # Enhanced hang detection: check both ratio AND WFI completion
    if spike_instr_count > 10 and coral_instr_count > 0:
        ratio = coral_instr_count / spike_instr_count

        # Check if Coral actually completed (reached WFI)
        coral_completed = False
        if coral_log:
            coral_completed = _coral_reached_wfi(coral_log)

        # Case 1: Coral completed far fewer instructions AND didn't reach WFI
        # This catches Coral stalls where it produces some output but hangs
        if ratio < 0.90 and not coral_completed:
            try:
                with open(coral_csv, 'r') as f:
                    rows = list(csv.DictReader(f))
                    if rows:
                        last = rows[-1]
                        fingerprint = f"HANG:{last['pc']}:{last['inst']}"
                        return True, fingerprint, "CORAL_HANG"
            except Exception:
                pass
            fingerprint = "HANG:unknown:unknown"
            return True, fingerprint, "CORAL_HANG"

        # Case 2: Original threshold - very low ratio (likely real hang)
        if ratio < HANG_THRESHOLD:
            try:
                with open(coral_csv, 'r') as f:
                    rows = list(csv.DictReader(f))
                    if rows:
                        last = rows[-1]
                        fingerprint = f"HANG:{last['pc']}:{last['inst']}"
                        return True, fingerprint, "CORAL_HANG"
            except Exception:
                pass
            fingerprint = "HANG:unknown:unknown"
            return True, fingerprint, "CORAL_HANG"

    if spike_instr_count > 5 and coral_instr_count == 0:
        fingerprint = "CRASH:no_output:no_output"
        return True, fingerprint, "CORAL_CRASH"

    return False, None, "CLEAN"


# ---------------------------------------------------------------------------
# Main evaluation pipeline
# ---------------------------------------------------------------------------

def evaluate(design_dir, resources_dir, output_dir):
    """Main evaluation function."""
    log("=" * 60)
    log("CoralNPU Bug Bounty - Challenge 0011 Evaluation")
    log("=" * 60)
    log(f"Design dir:    {design_dir}")
    log(f"Resources dir: {resources_dir}")
    log(f"Output dir:    {output_dir}")

    results = {
        'success': False,
        'functionality_score': 0.0,
        'bug_found': False,
        'bug_fingerprint': None,
        'error_message': None,
        'details': {
            'step': None,
            'spike_instructions': 0,
            'coral_instructions': 0,
            'real_mismatches': 0,
            'false_positives_filtered': 0,
            'first_diverging_pc': None,
            'first_diverging_inst': None,
            'classification': None,
            'compilation_success': False,
            'errors': [],
        }
    }

    def fail_at_step(step, error_message):
        """Record failure at a specific pipeline step."""
        results['details']['step'] = step
        results['error_message'] = error_message
        results['details']['errors'].append(error_message)
        log(f"ERROR at {step}: {error_message}")

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    resources_path = Path(resources_dir)
    scripts_dir = resources_path / "scripts"
    link_ld = resources_path / "common" / "link.ld"
    link_spike_ld = resources_path / "common" / "link_spike.ld"

    # Coral binary fallback: prefer env var, fall back to bundle's bin/Vtb_top
    coral_bin = CORAL_BIN
    resources_coral = resources_path / "bin" / "Vtb_top"
    if not Path(coral_bin).exists() and resources_coral.exists():
        coral_bin = str(resources_coral)
        os.chmod(coral_bin, 0o755)
        log(f"Using bundled Coral binary: {coral_bin}")

    # ---- STEP 1: Find and validate .S file ----
    log("\n[STEP 1] Validating submission...")
    results['details']['step'] = 'validation'

    s_file, find_err = find_s_file(design_dir)
    if s_file is None:
        fail_at_step('validation', find_err)
        return results

    log(f"Found .S file: {s_file}")

    valid, err_msg = validate_s_file(s_file)
    if not valid:
        fail_at_step('validation', err_msg)
        return results

    log("Validation passed")

    # ---- STEP 2: Compile ----
    log("\n[STEP 2] Compiling .S to ELF...")
    results['details']['step'] = 'compilation'

    if not link_ld.exists():
        fail_at_step('compilation', "Evaluator linker script not found (internal error — contact challenge maintainer).")
        return results

    # Compile for Coral (linked at 0x0)
    elf_path, compile_err = compile_s_to_elf(s_file, link_ld, output_dir)
    if elf_path is None:
        fail_at_step('compilation', compile_err)
        return results

    results['details']['compilation_success'] = True
    log(f"Compiled (Coral): {elf_path}")

    # Compile for Spike (linked at 0x80000000)
    spike_elf = None
    if link_spike_ld.exists():
        spike_elf, spike_compile_err = compile_s_to_elf(s_file, link_spike_ld, output_dir,
                                         stem_suffix="_spike")
        if spike_elf:
            log(f"Compiled (Spike): {spike_elf}")
        else:
            log(f"WARNING: Spike ELF compilation failed: {spike_compile_err}")

    # Convert Coral ELF to memory images
    log("Converting ELF to memory images...")
    imem_path, dmem_path = elf_to_mem(elf_path, output_dir)
    if imem_path is None:
        fail_at_step('compilation', "Failed to convert ELF to memory images (objcopy failed).")
        return results

    log(f"ITCM: {imem_path}")
    log(f"DTCM: {dmem_path}")

    # ---- STEP 3: Run Spike ----
    log("\n[STEP 3] Running Spike (reference simulator)...")
    results['details']['step'] = 'spike_simulation'

    if spike_elf is None:
        fail_at_step('spike_simulation', "No Spike ELF available (Spike compilation failed).")
        results['success'] = True
        return results

    spike_log = run_spike(spike_elf, output_dir)
    if spike_log is None:
        fail_at_step('spike_simulation',
                     "Spike produced no output. Your test may contain an infinite loop "
                     "or illegal instructions that crash Spike before producing a trace.")
        results['success'] = True
        return results

    spike_csv, spike_count = convert_spike_trace(spike_log, output_dir, scripts_dir)
    if spike_csv is None:
        fail_at_step('spike_simulation', "Spike trace conversion failed (internal error).")
        results['success'] = True
        return results

    results['details']['spike_instructions'] = spike_count
    log(f"Spike trace: {spike_count} instructions")

    if spike_count == 0:
        fail_at_step('spike_simulation',
                     "Spike executed 0 instructions after bootrom filter. "
                     "Your test may not reach any instructions at the expected entry point, "
                     "or it may crash immediately on startup.")
        results['success'] = True
        return results

    # ---- STEP 4: Run Coral ----
    log("\n[STEP 4] Running Coral (DUT simulation)...")
    results['details']['step'] = 'coral_simulation'

    # Check that Coral binary exists before trying to run it
    if not Path(coral_bin).exists():
        fail_at_step('coral_simulation',
                     f"Coral binary (Vtb_top) not found. "
                     f"Checked: {CORAL_BIN} and {resources_coral}. "
                     f"This is an evaluator configuration issue — contact challenge maintainer.")
        return results

    coral_log = run_coral(imem_path, dmem_path, output_dir, coral_bin=coral_bin)
    if coral_log is None:
        results['details']['coral_instructions'] = 0

        if spike_count > 5:
            results['success'] = True
            results['bug_found'] = True
            results['functionality_score'] = 1.0
            results['bug_fingerprint'] = "CRASH:no_output:no_output"
            results['details']['classification'] = "CORAL_CRASH"
            results['details']['step'] = 'classification'
            results['error_message'] = None  # Not an error — it's a valid bug find
            log("BUG FOUND: Coral crashed while Spike completed normally")
        else:
            fail_at_step('coral_simulation',
                         "Coral simulation produced no trace output. "
                         "The Coral binary may have crashed or timed out.")
        return results

    coral_csv, coral_count = convert_coral_trace(coral_log, output_dir, scripts_dir)
    if coral_csv is None:
        fail_at_step('coral_simulation', "Coral trace conversion failed (internal error).")
        results['success'] = True
        return results

    results['details']['coral_instructions'] = coral_count
    log(f"Coral trace: {coral_count} instructions")

    # Build fingerprint: MD5 hash of Coral binary for audit trail
    try:
        coral_bin_path = Path(coral_bin)
        if coral_bin_path.exists():
            md5 = hashlib.md5(coral_bin_path.read_bytes()).hexdigest()
            results['details']['coral_build_hash'] = md5
            log(f"Coral build hash: {md5}")
    except Exception:
        pass

    # ---- STEP 5: Compare traces ----
    log("\n[STEP 5] Comparing traces...")
    results['details']['step'] = 'trace_comparison'

    comparison = compare_traces(spike_csv, coral_csv, scripts_dir)

    if comparison:
        results['details']['real_mismatches'] = comparison['mismatch']
        false_positives = (comparison['waw_tracer_bugs'] +
                          comparison['mask_tail_diffs'] +
                          comparison['cascade_diffs'] +
                          comparison['spike_loop'] +
                          comparison['store_tracer_bugs'])
        results['details']['false_positives_filtered'] = false_positives

    # ---- STEP 6: Classify ----
    log("\n[STEP 6] Classifying result...")
    results['details']['step'] = 'classification'

    bug_found, fingerprint, classification = classify_result(
        comparison, spike_count, coral_count, spike_csv, coral_csv,
        coral_log=coral_log
    )

    results['success'] = True
    results['error_message'] = None  # Clear any earlier partial errors
    results['bug_found'] = bug_found
    results['functionality_score'] = 1.0 if bug_found else 0.0
    results['bug_fingerprint'] = fingerprint
    results['details']['classification'] = classification
    results['details']['step'] = 'complete'

    if bug_found:
        pc, inst = extract_first_mismatch(spike_csv, coral_csv)
        results['details']['first_diverging_pc'] = pc
        results['details']['first_diverging_inst'] = inst

    log("\n" + "=" * 60)
    if bug_found:
        log(f"BUG FOUND! Classification: {classification}")
        log(f"Fingerprint: {fingerprint}")
        log(f"Score: 1.0")
    else:
        log(f"No bug found. Classification: {classification}")
        log(f"Score: 0.0")
    log("=" * 60)

    return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="CoralNPU Bug Bounty Evaluator - Challenge 0011"
    )
    parser.add_argument(
        "--design", required=True,
        help="Path to miner's submission directory (contains .S file)"
    )
    parser.add_argument(
        "--resources", required=True,
        help="Path to evaluator resources directory"
    )
    parser.add_argument(
        "--out", default="/tmp/coralnpu_bugbounty",
        help="Output directory for results"
    )
    args = parser.parse_args()

    if not Path(args.design).exists():
        print(json.dumps({
            'success': False, 'functionality_score': 0.0, 'bug_found': False,
            'error_message': f"Design directory not found: {args.design}",
            'details': {'step': 'setup', 'errors': [f"Design directory not found: {args.design}"]}
        }))
        sys.exit(1)

    if not Path(args.resources).exists():
        print(json.dumps({
            'success': False, 'functionality_score': 0.0, 'bug_found': False,
            'error_message': f"Resources directory not found: {args.resources}",
            'details': {'step': 'setup', 'errors': [f"Resources directory not found: {args.resources}"]}
        }))
        sys.exit(1)

    output_dir = Path(args.out)
    output_dir.mkdir(parents=True, exist_ok=True)

    results = evaluate(args.design, args.resources, str(output_dir))

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
