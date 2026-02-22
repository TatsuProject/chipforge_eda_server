#!/usr/bin/env python3
"""
Convert Coral retirement trace log to CSV format for comparison with Spike.

Coral trace format (scalar):
       1      502  00000000  00000093   x1=0x00000000   x0:0x00000000

Coral trace format (vector):
       1      502  00000000  010072d7   x5=0x00000004   x0:0x00000000
       2      503  00000004  02056087  v1=0x00000004000000030000000200000001

Fields: order cycle pc inst [rd=value] [rs1:value] [rs2:value] [TRAP]
        rd can be x/f/v register with appropriate bit-width value

Smart Duplicate Detection:
    The Coral retirement buffer has a bug where it may report the same instructions
    as valid across multiple cycles (duplicate retirement). This causes the trace
    to show instructions "rewinding" to earlier PCs without a branch/jump.

    We detect this by checking: when PC goes backward, was the previous instruction
    a branch/jump? If not, it's a duplicate retirement bug and we filter it out.
    Legitimate loops always have a branch instruction before the PC rewinds.
"""

import re
import sys
import argparse
import csv


def is_branch_or_jump(inst_hex):
    """Check if instruction is a branch or jump (causes PC to go backward legitimately).

    Args:
        inst_hex: Instruction encoding as hex string (e.g., '00000063')

    Returns:
        True if instruction is a branch (BEQ, BNE, BLT, BGE, BLTU, BGEU),
        JAL, or JALR.
    """
    try:
        inst = int(inst_hex, 16)
    except ValueError:
        return False

    # Extract opcode (bits 6:0)
    opcode = inst & 0x7F

    # Branch instructions: opcode = 1100011 (0x63)
    # JAL: opcode = 1101111 (0x6F)
    # JALR: opcode = 1100111 (0x67)
    # SYSTEM (mret/sret/ecall/ebreak): opcode = 1110011 (0x73), funct3=0
    if opcode in (0x63, 0x6F, 0x67):
        return True
    # mret (0x30200073), sret (0x10200073) — SYSTEM instructions that change PC
    if opcode == 0x73 and (inst & 0x7000) == 0:
        return True
    return False

def parse_coral_log(log_path, start_pc=None, end_pc=None, loop_threshold=5):
    """Parse Coral retirement trace log.

    Args:
        log_path: Path to Coral trace log file
        start_pc: Optional PC to start tracing from (hex string without 0x)
        end_pc: Optional PC to stop tracing at (hex string without 0x)
        loop_threshold: Number of times to see same PC before detecting infinite loop

    Returns:
        List of dicts with keys: order, pc, inst, rd, rd_value, rd_type
    """
    # Regex for coral trace line
    # Format: order cycle pc inst [xN/fN/vN=0xVALUE] [xN:0xVALUE] [xN:0xVALUE] [TRAP]
    # rd value can be 8 hex digits (32-bit) or 32 hex digits (128-bit for vectors)
    line_re = re.compile(
        r'^\s*(\d+)\s+'           # order
        r'(\d+)\s+'               # cycle
        r'([0-9a-fA-F]+)\s+'      # pc
        r'([0-9a-fA-F]+)'         # inst
        r'(?:\s+([xfv])(\d+)=0x([0-9a-fA-F]+))?'  # rd=value (x/f/v register, optional)
        r'(?:\s+x(\d+):0x([0-9a-fA-F]+))?'  # rs1:value (optional)
        r'(?:\s+x(\d+):0x([0-9a-fA-F]+))?'  # rs2:value (optional)
        r'(?:\s+(TRAP))?'         # trap flag (optional)
    )

    trace = []
    tracing = (start_pc is None)  # Start immediately if no start_pc
    new_order = 0

    # Loop detection - track recent PCs to detect multi-instruction loops
    # Note: Legitimate bounded loops will repeat many times, so we use a high
    # threshold to avoid false positives. Only truly infinite loops (no exit)
    # will repeat hundreds of times.
    recent_pcs = []
    loop_window = 10  # Check for loops up to 10 instructions
    loop_repeat_threshold = 100  # Detect after 100 repetitions (likely infinite)

    # Smart duplicate detection
    # Track previous instruction and high-water PC mark
    prev_inst = None
    prev_pc = 0
    high_water_pc = 0
    in_duplicate_sequence = False
    duplicate_count = 0
    trap_count = 0

    with open(log_path, 'r') as f:
        for line in f:
            line = line.strip()

            # Skip comments and headers
            if line.startswith('#') or not line:
                continue

            m = line_re.match(line)
            if not m:
                continue

            order = m.group(1)
            cycle = m.group(2)
            pc = m.group(3).lower()
            inst = m.group(4).lower()
            rd_type = m.group(5)   # 'x', 'f', or 'v'
            rd_num = m.group(6)
            rd_value = m.group(7)
            rs1_num = m.group(8)
            rs1_value = m.group(9)
            rs2_num = m.group(10)
            rs2_value = m.group(11)
            trap = m.group(12)

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

            # Smart duplicate detection
            # The retirement buffer bug causes instructions to be reported multiple times.
            # We detect this by checking: if PC goes backward and previous instruction
            # was NOT a branch/jump, it's a duplicate retirement (not a real loop).
            pc_int = int(pc, 16)

            # Skip trapped instructions: Coral records them but Spike doesn't.
            # Including them creates a one-instruction offset that cascades as
            # false PC_MISMATCHes for every subsequent instruction.
            # Still update prev tracking so duplicate detection works correctly.
            if trap:
                trap_count += 1
                prev_inst = inst
                prev_pc = pc_int
                continue

            if in_duplicate_sequence:
                # We're in a duplicate sequence - skip until PC advances past high_water_pc
                if pc_int <= high_water_pc:
                    duplicate_count += 1
                    continue
                else:
                    # PC advanced past high water mark - exit duplicate sequence
                    in_duplicate_sequence = False

            # Detect immediate duplicate: same PC and same instruction as previous
            # This is BUG-008 - retirement buffer reports same instruction multiple times
            if pc_int == prev_pc and inst == prev_inst:
                duplicate_count += 1
                continue

            if pc_int < prev_pc and prev_inst is not None:
                # PC went backward - check if previous instruction was a branch/jump
                if is_branch_or_jump(prev_inst):
                    # Legitimate loop - previous instruction was a branch/jump
                    # Update high_water_pc since this is a new loop iteration
                    pass
                else:
                    # Duplicate retirement bug - PC rewound without a branch
                    # Enter duplicate sequence mode and skip this instruction
                    in_duplicate_sequence = True
                    duplicate_count += 1
                    continue

            # Update high water mark
            if pc_int > high_water_pc:
                high_water_pc = pc_int

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
                        print(f"  Detected {pattern_len}-instruction loop at PC=0x{pc} after {new_order} instructions")
                        loop_detected = True
                        break

            if loop_detected:
                break

            # Stop at WFI instruction (0x10500073) - don't include it
            # WFI is a termination marker, not part of the actual test
            # This matches Spike which stops when jumping to pass/fail handler
            if inst == '10500073':
                break

            new_order += 1

            # Format rd based on register type (x/f/v)
            if rd_type and rd_num:
                rd_name = f'{rd_type}{rd_num}'
            else:
                rd_name = '-'

            entry = {
                'order': new_order,
                'pc': pc,
                'inst': inst,
                'rd': rd_name,
                'rd_value': rd_value.lower() if rd_value else '-',
                'rd_type': rd_type if rd_type else '-',  # Track register type for filtering
            }
            trace.append(entry)

            # Track for next iteration's duplicate detection
            prev_inst = inst
            prev_pc = pc_int

    if trap_count > 0:
        print(f"  Filtered {trap_count} trapped instructions (Coral traps not in Spike trace)")
    if duplicate_count > 0:
        print(f"  Filtered {duplicate_count} duplicate retirements (retirement buffer bug)")

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
    parser = argparse.ArgumentParser(description='Convert Coral retirement trace to CSV')
    parser.add_argument('log_file', help='Coral trace log file')
    parser.add_argument('-o', '--output', default='coral_trace.csv', help='Output CSV file')
    parser.add_argument('--start-pc', help='PC to start tracing (hex without 0x)')
    parser.add_argument('--end-pc', help='PC to stop tracing (hex without 0x)')
    args = parser.parse_args()

    trace = parse_coral_log(args.log_file, args.start_pc, args.end_pc)
    write_csv(trace, args.output)


if __name__ == '__main__':
    main()
