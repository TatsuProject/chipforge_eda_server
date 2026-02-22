#!/usr/bin/env python3
"""
Convert Spike commit log to CSV format for comparison with Coral trace.

Spike log format (with --log-commits -l):
  core   0: 0x80000000 (0xd82c00b7) lui     ra, 0xd82c0
  core   0: 3 0x80000000 (0xd82c00b7) x1  0xd82c0000

Spike log format with vector instructions:
  core   0: 3 0x80001000 (0x02056087) v1  0x00000004000000030000000200000001

The commit line (with mode '3') shows the register writeback.
Memory operations show: x5  0x80000000 mem 0x00001018
Vector registers show 128-bit values (32 hex digits for VLEN=128).
"""

import re
import sys
import argparse
import csv

def parse_spike_log(log_path, start_pc=None, end_pc=None, loop_threshold=5):
    """Parse Spike commit log and extract instruction trace.

    Args:
        log_path: Path to Spike log file
        start_pc: Optional PC to start tracing from (hex string without 0x)
        end_pc: Optional PC to stop tracing at (hex string without 0x)
        loop_threshold: Number of times to see same PC before detecting infinite loop

    Returns:
        List of dicts with keys: order, pc, inst, rd, rd_value
    """
    # Regex for commit line: core 0: 3 0x80000000 (0xd82c00b7) x1  0xd82c0000 [mem 0x...]
    # Also handles:
    #   - FP register writes: f10 0x40200000
    #   - Vector register writes: e32 m1 l4 v1  0x00000004000000030000000200000001 (128-bit)
    #   - CSR + reg: c1_fflags 0x00000001 x11 0x00000001
    #   - CSR + FP reg: c1_fflags 0x00000001 f13 0xc49a4000
    commit_re = re.compile(
        r'core\s+\d+:\s+(\d+)\s+0x([0-9a-fA-F]+)\s+\(0x([0-9a-fA-F]+)\)'
        r'(?:\s+e\d+\s+m\d+\s+l\d+)?'         # Optional vector info (eXX mX lX) for RVV
        r'(?:\s+c\d+_\w+\s+0x[0-9a-fA-F]+)*'  # Optional CSR updates (may be multiple)
        r'(?:\s+([xfv])(\d+)\s+0x([0-9a-fA-F]+))?'  # Optional register writeback (x, f, or v)
        r'(?:\s+c\d+_\w+\s+0x[0-9a-fA-F]+)*'  # Optional CSR updates after register
        r'(?:\s+mem\s+0x[0-9a-fA-F]+(?:\s+0x[0-9a-fA-F]+)?)*'  # Optional memory accesses
    )

    trace = []
    order = 0
    tracing = (start_pc is None)  # Start immediately if no start_pc

    # Loop detection - track recent PCs to detect multi-instruction loops
    recent_pcs = []
    loop_window = 10  # Check for loops up to 10 instructions
    loop_repeat_threshold = 100  # Detect after 100 repetitions (likely infinite)

    with open(log_path, 'r') as f:
        for line in f:
            line = line.strip()

            # Look for commit lines (have privilege mode number)
            m = commit_re.match(line)
            if not m:
                continue

            mode = m.group(1)
            pc = m.group(2).lower()
            inst = m.group(3).lower()
            reg_type = m.group(4)  # 'x', 'f', or 'v', may be None
            rd = m.group(5)  # May be None
            rd_value = m.group(6)  # May be None

            # Check for start/end conditions
            if start_pc and not tracing:
                if pc == start_pc.lower():
                    tracing = True
                else:
                    continue

            if end_pc and pc == end_pc.lower():
                break

            if not tracing:
                continue

            # Detect infinite loop (multi-instruction patterns)
            recent_pcs.append(pc)
            if len(recent_pcs) > loop_window * loop_repeat_threshold * 2:
                recent_pcs = recent_pcs[-loop_window * loop_repeat_threshold * 2:]

            # Check for repeating patterns of length 1 to loop_window
            loop_detected = False
            for pattern_len in range(1, min(loop_window + 1, len(recent_pcs) // loop_repeat_threshold + 1)):
                if len(recent_pcs) >= pattern_len * loop_repeat_threshold:
                    pattern = recent_pcs[-pattern_len:]
                    is_loop = True
                    for i in range(1, loop_repeat_threshold):
                        start_idx = -(pattern_len * (i + 1))
                        end_idx = -(pattern_len * i)
                        if end_idx == 0:
                            check_pattern = recent_pcs[start_idx:]
                        else:
                            check_pattern = recent_pcs[start_idx:end_idx]
                        if check_pattern != pattern:
                            is_loop = False
                            break
                    if is_loop:
                        print(f"  Detected {pattern_len}-instruction loop at PC=0x{pc} after {order} instructions")
                        loop_detected = True
                        break

            if loop_detected:
                break

            # Stop at pass/fail handler - PC jumps to 0x0001XXXX area (test infrastructure)
            # This is where riscv-tests pass/fail handling code lives
            pc_int = int(pc, 16)
            if pc_int >= 0x00010000 and pc_int < 0x00020000:
                # Reached test infrastructure pass/fail handler - stop tracing
                # Don't include handler instructions as they're not part of the test
                break

            # Stop at WFI instruction (0x10500073) - don't include it.
            # WFI is a termination marker, not part of the actual test.
            # This matches Coral which also excludes WFI from its trace.
            if inst == '10500073':
                break

            order += 1

            # Preserve register type prefix (x, f, v) for proper identification
            # Note: Coral traces FP reg writes as fN (not xN as previously thought)
            if reg_type and rd:
                rd_name = f'{reg_type}{rd}'
            else:
                rd_name = '-'

            entry = {
                'order': order,
                'pc': pc,
                'inst': inst,
                'rd': rd_name,
                'rd_value': rd_value.lower() if rd_value else '-',
                'rd_type': reg_type if reg_type else '-',  # Track register type
            }
            trace.append(entry)

    return trace


def write_csv(trace, output_path, include_type=False):
    """Write trace to CSV file.

    Args:
        trace: List of trace entries
        output_path: Output CSV file path
        include_type: If True, include rd_type column
    """
    if include_type:
        fieldnames = ['order', 'pc', 'inst', 'rd', 'rd_value', 'rd_type']
    else:
        # For backward compatibility, exclude rd_type by default
        fieldnames = ['order', 'pc', 'inst', 'rd', 'rd_value']
        # Remove rd_type from entries
        trace = [{k: v for k, v in e.items() if k != 'rd_type'} for e in trace]

    with open(output_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(trace)

    # Count instruction types
    x_count = sum(1 for e in trace if e.get('rd', '-').startswith('x'))
    v_count = sum(1 for e in trace if e.get('rd', '-').startswith('v'))
    f_count = sum(1 for e in trace if e.get('rd', '-').startswith('f'))

    print(f"Wrote {len(trace)} instructions to {output_path}")
    if v_count > 0 or f_count > 0:
        print(f"  Breakdown: {x_count} scalar (x), {f_count} float (f), {v_count} vector (v)")


def main():
    parser = argparse.ArgumentParser(description='Convert Spike commit log to CSV')
    parser.add_argument('log_file', help='Spike log file (with --log-commits -l)')
    parser.add_argument('-o', '--output', default='spike_trace.csv', help='Output CSV file')
    parser.add_argument('--start-pc', help='PC to start tracing (hex without 0x)')
    parser.add_argument('--end-pc', help='PC to stop tracing (hex without 0x)')
    args = parser.parse_args()

    trace = parse_spike_log(args.log_file, args.start_pc, args.end_pc)
    write_csv(trace, args.output)


if __name__ == '__main__':
    main()
