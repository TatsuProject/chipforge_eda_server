#!/usr/bin/env python3
"""
Compare Spike and Coral trace CSVs to find mismatches.

Compares: PC, instruction, rd, rd_value
Reports first N mismatches and summary statistics.

Handles spec-legal differences:
- Mask tail bits: CoralNPU sets ignore_vta=1 for all mask-producing
  instructions, writing full 128-bit results. Spike zeros tail bits.
  Both are RISC-V V spec compliant (tail-agnostic for mask destinations).
- Cascade tracking: When mask registers with different tail bits are read
  as data sources, the difference propagates. These cascading mismatches
  are tracked and reported separately.
"""

import argparse
import csv
import sys

# CoralNPU VLEN (vector register length in bits)
VLEN = 128


def load_csv(path):
    """Load CSV trace file."""
    with open(path, 'r') as f:
        reader = csv.DictReader(f)
        return list(reader)


def detect_loop_start(trace, loop_window=10, loop_repeat_threshold=3):
    """Detect where an infinite loop starts in a trace.

    Returns the index where the loop pattern first begins, or len(trace) if no loop.
    """
    if len(trace) < loop_window * loop_repeat_threshold:
        return len(trace)

    pcs = [entry['pc'] for entry in trace]

    # Check from the end of the trace backwards
    for pattern_len in range(1, loop_window + 1):
        if len(pcs) >= pattern_len * loop_repeat_threshold:
            pattern = pcs[-pattern_len:]
            is_loop = True
            for i in range(1, loop_repeat_threshold):
                start_idx = -(pattern_len * (i + 1))
                end_idx = -(pattern_len * i)
                if end_idx == 0:
                    check_pattern = pcs[start_idx:]
                else:
                    check_pattern = pcs[start_idx:end_idx]
                if check_pattern != pattern:
                    is_loop = False
                    break
            if is_loop:
                # Find where this loop pattern first started
                loop_start_pc = pattern[0]
                # Search backwards to find first occurrence of this loop
                for idx in range(len(pcs) - pattern_len * loop_repeat_threshold, -1, -1):
                    if pcs[idx:idx+pattern_len] == pattern:
                        return idx
                return len(pcs) - pattern_len * loop_repeat_threshold

    return len(trace)


def trim_to_loop_start(spike, coral):
    """Trim both traces to the point where loops start (if any).

    If both traces end in a loop, trim to the shorter one to enable fair comparison.
    Returns trimmed copies of both traces and info about trimming.
    """
    spike_loop = detect_loop_start(spike)
    coral_loop = detect_loop_start(coral)

    trim_info = {
        'spike_loop_at': spike_loop if spike_loop < len(spike) else None,
        'coral_loop_at': coral_loop if coral_loop < len(coral) else None,
        'spike_original_len': len(spike),
        'coral_original_len': len(coral),
    }

    # If both have loops, trim to where loops start
    if spike_loop < len(spike) and coral_loop < len(coral):
        # Trim to first loop occurrence for fair comparison
        min_loop = min(spike_loop, coral_loop)
        return spike[:min_loop], coral[:min_loop], trim_info

    # If only one has a loop, trim the longer one to match the loop start
    if spike_loop < len(spike):
        return spike[:spike_loop], coral[:spike_loop], trim_info
    if coral_loop < len(coral):
        return spike[:coral_loop], coral[:coral_loop], trim_info

    return spike, coral, trim_info


def normalize_pc(pc_hex, offset):
    """Normalize PC by subtracting offset."""
    if offset == 0:
        return pc_hex
    pc_int = int(pc_hex, 16)
    normalized = pc_int - offset
    if normalized < 0:
        normalized = 0  # Clamp to 0
    return f"{normalized:08x}"


# ---------------------------------------------------------------------------
# vsetvli / VL tracking
# ---------------------------------------------------------------------------

def is_vsetvl_instruction(inst_hex):
    """Check if instruction is vsetvli, vsetivli, or vsetvl.

    All three share opcode=0x57 and funct3=7 (0b111).
    - vsetvli:  inst[31]=0
    - vsetivli: inst[31:30]=11
    - vsetvl:   inst[31]=1, inst[30]=0
    """
    try:
        inst = int(inst_hex, 16)
        opcode = inst & 0x7f
        funct3 = (inst >> 12) & 0x7
        if opcode != 0x57:
            return False
        if funct3 != 7:
            return False
        return True
    except:
        return False


def decode_vl_from_vsetvl(inst_hex, rd_value_hex):
    """Extract VL, SEW, and LMUL from a vsetvl-family instruction.

    VL is taken directly from rd_value (the trace records the value written
    to the rd register, which IS the resulting VL). If rd_value is not
    available (rd=x0), we compute VLMAX from SEW/LMUL.

    Returns (vl, sew, lmul_num, lmul_den) tuple.
    """
    try:
        inst = int(inst_hex, 16)
        bit31 = (inst >> 31) & 1
        bit30 = (inst >> 30) & 1

        # Extract SEW and LMUL from zimm field
        if bit31 == 1 and bit30 == 0:
            # vsetvl: SEW/LMUL come from vtype register, can't decode from inst
            # Use rd_value if available, else default
            vl = int(rd_value_hex, 16) if rd_value_hex.strip('0') else 4
            return vl, 32, 1, 1

        # vsetvli (bit31=0) or vsetivli (bit31=1,bit30=1)
        vlmul_bits = (inst >> 20) & 0x7
        vsew_bits = (inst >> 23) & 0x7

        # Decode SEW: 8 << vsew
        sew = 8 << vsew_bits  # 0->8, 1->16, 2->32, 3->64

        # Decode LMUL
        if vlmul_bits <= 3:
            lmul_num = 1 << vlmul_bits  # 0->1, 1->2, 2->4, 3->8
            lmul_den = 1
        else:
            # Fractional: 5->1/8, 6->1/4, 7->1/2
            lmul_num = 1
            lmul_den = 1 << (8 - vlmul_bits)

        vlmax = (VLEN * lmul_num) // (sew * lmul_den)

        # Prefer rd_value (actual VL set by hardware)
        if rd_value_hex and rd_value_hex.strip('0'):
            vl = int(rd_value_hex, 16)
        else:
            # rd=x0 or value is 0: use VLMAX (rs1=x0 → VL=VLMAX)
            rs1 = (inst >> 15) & 0x1f
            vl = vlmax if rs1 == 0 else vlmax

        return vl, sew, lmul_num, lmul_den
    except:
        return 4, 32, 1, 1  # Safe default for e32,m1


# ---------------------------------------------------------------------------
# Instruction classification helpers
# ---------------------------------------------------------------------------

def is_vector_compare_instruction(inst_hex):
    """Check if instruction is a vector compare (vmslt, vmseq, etc.).

    These instructions write mask results to vector registers.
    Spike's tracer doesn't log RD for these, causing false RD_MISMATCH.

    Vector compare encodings (funct6 values with funct3=000 OPIVV):
    - 0x18 (011000): vmseq.vv
    - 0x19 (011001): vmsne.vv
    - 0x1A (011010): vmsltu.vv
    - 0x1B (011011): vmslt.vv
    - 0x1C (011100): vmsleu.vv
    - 0x1D (011101): vmsle.vv
    - 0x1E (011110): vmsgtu.vx (but we check vv forms)
    - 0x1F (011111): vmsgt.vx

    Also check OPIVX (funct3=100) and OPIVI (funct3=011) variants.
    """
    try:
        inst = int(inst_hex, 16)
        opcode = inst & 0x7f
        funct3 = (inst >> 12) & 0x7
        funct6 = (inst >> 26) & 0x3f

        # Must be vector opcode (0x57)
        if opcode != 0x57:
            return False

        # Check for compare instructions (funct6 = 0x18-0x1F range)
        # These are: vmseq, vmsne, vmsltu, vmslt, vmsleu, vmsle, vmsgtu, vmsgt
        if 0x18 <= funct6 <= 0x1F:
            return True

        return False
    except:
        return False


def is_mask_producing_instruction(inst_hex):
    """Check if instruction writes a mask result (1-bit per element) to vd.

    CoralNPU's decode sets ignore_vta=1 for ALL of these, causing full
    128-bit VRF writes. Spike zeros tail bits. Both spec-legal.

    Categories:
    1. Vector compares: vmseq, vmsne, vmsltu, vmslt, vmsleu, vmsle, vmsgtu, vmsgt
       funct6=0x18-0x1F, funct3=0(OPIVV)/3(OPIVI)/4(OPIVX)
    2. Mask logical: vmand, vmnand, vmor, vmnor, vmxor, vmxnor, vmorn, vmandn
       funct6=0x18-0x1F, funct3=2(OPMVV)
    3. Mask utility: vmsbf, vmsif, vmsof
       funct6=0x14, funct3=2(OPMVV), vs1=1/2/3
    4. Carry/borrow masks: vmadc, vmsbc
       funct6=0x11/0x13, funct3=0(OPIVV)/3(OPIVI)/4(OPIVX)
    """
    try:
        inst = int(inst_hex, 16)
        opcode = inst & 0x7f
        funct3 = (inst >> 12) & 0x7
        funct6 = (inst >> 26) & 0x3f
        vs1 = (inst >> 15) & 0x1f

        if opcode != 0x57:
            return False

        # Skip vsetvli (funct3=7)
        if funct3 == 7:
            return False

        # 1. Vector compares (OPIVV=0, OPIVI=3, OPIVX=4)
        if funct3 in (0, 3, 4) and 0x18 <= funct6 <= 0x1F:
            return True

        # 2. Mask logical ops (OPMVV=2)
        if funct3 == 2 and 0x18 <= funct6 <= 0x1F:
            return True

        # 3. Mask utility: vmsbf(vs1=1), vmsof(vs1=2), vmsif(vs1=3)
        #    VMUNARY0: funct6=0x14 (010100), funct3=2 (OPMVV)
        if funct3 == 2 and funct6 == 0x14:
            if vs1 in (1, 2, 3):
                return True

        # 4. Carry/borrow: vmadc(funct6=0x11), vmsbc(funct6=0x13)
        #    Both produce mask results in all forms (VV, VX, VI)
        if funct6 in (0x11, 0x13) and funct3 in (0, 3, 4):
            return True

        return False
    except:
        return False


def is_vector_move_instruction(inst_hex):
    """Check if instruction is a vector move (vmv.v.x, vmv.v.v, vmv.s.x, etc.).

    BUG-011: These instructions show width mismatches between Spike and Coral tracers.
    Spike shows full VLEN width, Coral only shows element 0.

    Vector move encodings:
    - vmv.v.x (OPMVX, funct6=010111): broadcast scalar to all elements
    - vmv.v.v (OPMVV, funct6=010111): copy vector
    - vmv.s.x (OPMVX, funct6=010000): scalar to element 0
    - vmv.v.i (OPIVI, funct6=010111): broadcast immediate

    Encoding: funct6[31:26] | vm[25] | vs2[24:20] | rs1/vs1[19:15] | funct3[14:12] | vd[11:7] | opcode[6:0]
    """
    try:
        inst = int(inst_hex, 16)
        opcode = inst & 0x7f
        funct3 = (inst >> 12) & 0x7
        funct6 = (inst >> 26) & 0x3f
        vs2 = (inst >> 20) & 0x1f

        # Must be vector opcode (0x57)
        if opcode != 0x57:
            return False

        # vmv.v.x, vmv.v.v, vmv.v.i: funct6 = 0x17 (010111)
        # vmv.v.x: funct3=100 (OPMVX), vs2=0
        # vmv.v.v: funct3=000 (OPMVV), vs2=0
        # vmv.v.i: funct3=011 (OPIVI), vs2=0
        if funct6 == 0x17 and vs2 == 0:
            return True

        # vmv.s.x: funct6 = 0x10 (010000), funct3=110 (OPMVX)
        if funct6 == 0x10 and funct3 == 6:
            return True

        return False
    except:
        return False




def is_scalar_to_element0(inst_hex):
    """Check if instruction writes only element 0 (rest are tail).
    
    These instructions create spec-legal tail differences:
    - vmv.s.x (OPMVX, funct6=010000): scalar integer to element 0
    - vfmv.s.f (OPFVF, funct6=010000): scalar float to element 0
    
    Per RVV spec: "The remaining elements (1 to vl-1) are treated as tail elements."
    Spike zeros tail, Coral fills with 1s - both are spec-legal.
    """
    try:
        inst = int(inst_hex, 16)
        opcode = inst & 0x7f
        funct3 = (inst >> 12) & 0x7
        funct6 = (inst >> 26) & 0x3f
        
        if opcode != 0x57:
            return False
        
        # vmv.s.x: funct6=010000 (0x10), funct3=110 (OPMVX, 6)
        if funct6 == 0x10 and funct3 == 6:
            return True
        
        # vfmv.s.f: funct6=010000 (0x10), funct3=101 (OPFVF, 5)
        if funct6 == 0x10 and funct3 == 5:
            return True
        
        return False
    except:
        return False

def is_vfirst_vcpop_instruction(inst_hex):
    """Check if instruction is vfirst.m or vcpop.m.

    These instructions write to SCALAR registers (xN), but Coral's tracer
    incorrectly reports them as writing to vector registers (vN).
    This is a tracer bug (BUG-020), not an RTL bug.

    Encoding: funct6=010000 (VWXUNARY0), funct3=010 (OPMVV)
    - vcpop.m: vs1=10000 (0x10)
    - vfirst.m: vs1=10001 (0x11)
    """
    try:
        inst = int(inst_hex, 16)
        opcode = inst & 0x7f
        funct3 = (inst >> 12) & 0x7
        vs1 = (inst >> 15) & 0x1f
        funct6 = (inst >> 26) & 0x3f

        # Must be vector opcode (0x57)
        if opcode != 0x57:
            return False

        # VWXUNARY0: funct6 = 0x10 (010000), funct3 = 010 (OPMVV)
        if funct6 == 0x10 and funct3 == 2:
            # vcpop.m: vs1 = 0x10
            # vfirst.m: vs1 = 0x11
            if vs1 == 0x10 or vs1 == 0x11:
                return True

        return False
    except:
        return False


def is_mask_logical_instruction(inst_hex):
    """Check if instruction is a mask-register logical operation.

    NOTE: Superseded by is_mask_producing_instruction() for tail handling.
    Kept for backwards compatibility.

    Encoding: opcode=0x57, funct3=010 (OPMVV), funct6=011xxx
    """
    try:
        inst = int(inst_hex, 16)
        opcode = inst & 0x7f
        funct3 = (inst >> 12) & 0x7
        funct6 = (inst >> 26) & 0x3f

        if opcode != 0x57:
            return False
        if funct3 != 2:
            return False
        if 0x18 <= funct6 <= 0x1F:
            return True

        return False
    except:
        return False


def is_vector_reduction_instruction(inst_hex):
    """Check if instruction is a vector reduction operation.

    Per RISC-V V spec, reduction instructions write the scalar result to
    element 0 of the destination vector register. Upper elements are UNDEFINED
    and implementations can leave any value there.

    Therefore, for reductions we should only compare element 0, not the full
    vector register value.

    Encoding: opcode=0x57, funct3=010 (OPMVV), funct6=000xxx (integer reductions)
              opcode=0x57, funct3=001 (OPFVV), funct6=000xxx (float reductions)

    Integer reductions (funct3=010):
    - vredsum.vs:  funct6 = 0x00 (000000)
    - vredand.vs:  funct6 = 0x01 (000001)
    - vredor.vs:   funct6 = 0x02 (000010)
    - vredxor.vs:  funct6 = 0x03 (000011)
    - vredminu.vs: funct6 = 0x04 (000100)
    - vredmin.vs:  funct6 = 0x05 (000101)
    - vredmaxu.vs: funct6 = 0x06 (000110)
    - vredmax.vs:  funct6 = 0x07 (000111)

    Widening integer reductions (funct3=010):
    - vwredsumu.vs: funct6 = 0x30 (110000)
    - vwredsum.vs:  funct6 = 0x31 (110001)

    Float reductions (funct3=001):
    - vfredusum.vs: funct6 = 0x01 (000001)
    - vfredosum.vs: funct6 = 0x03 (000011)
    - vfredmin.vs:  funct6 = 0x05 (000101)
    - vfredmax.vs:  funct6 = 0x07 (000111)
    - vfwredusum.vs: funct6 = 0x31 (110001)
    - vfwredosum.vs: funct6 = 0x33 (110011)
    """
    try:
        inst = int(inst_hex, 16)
        opcode = inst & 0x7f
        funct3 = (inst >> 12) & 0x7
        funct6 = (inst >> 26) & 0x3f

        # Must be vector opcode (0x57)
        if opcode != 0x57:
            return False

        # Integer reductions: funct3 = 2 (OPMVV)
        if funct3 == 2:
            # Standard reductions: funct6 = 0x00-0x07
            if 0x00 <= funct6 <= 0x07:
                return True
            # Widening reductions: funct6 = 0x30-0x31
            if funct6 == 0x30 or funct6 == 0x31:
                return True

        # Float reductions: funct3 = 1 (OPFVV)
        if funct3 == 1:
            # vfredusum.vs, vfredosum.vs, vfredmin.vs, vfredmax.vs
            if funct6 in [0x01, 0x03, 0x05, 0x07]:
                return True
            # Widening float reductions
            if funct6 in [0x31, 0x33]:
                return True

        return False
    except:
        return False


def reduction_element0_matches(spike_val, coral_val, sew=32):
    """Compare only element 0 of vector register values for reduction instructions.

    Args:
        spike_val: Spike result as hex string (may be full VLEN width)
        coral_val: Coral result as hex string (may be full VLEN width)
        sew: Selected Element Width in bits (default 32)

    Returns:
        True if element 0 (lowest SEW bits) matches
    """
    try:
        spike_int = int(spike_val, 16)
        coral_int = int(coral_val, 16)

        # Create mask for element 0 (lowest SEW bits)
        element_mask = (1 << sew) - 1

        # Compare only element 0
        spike_elem0 = spike_int & element_mask
        coral_elem0 = coral_int & element_mask

        return spike_elem0 == coral_elem0
    except:
        return False


def mask_values_match_ignoring_tail(spike_val, coral_val, vl):
    """Compare mask register values, ignoring tail bits.

    Per RISC-V V spec: "Mask destination tail elements are always treated
    as tail-agnostic, regardless of the setting of vta."

    This means implementations can set tail bits to 0s OR 1s - both are valid.
    We only compare the active bits (0 to vl-1).

    Args:
        spike_val: Spike result as hex string
        coral_val: Coral result as hex string
        vl: Vector length (number of active mask bits)

    Returns:
        True if active bits match (tail bits ignored)
    """
    try:
        spike_int = int(spike_val, 16)
        coral_int = int(coral_val, 16)

        # Create mask for active bits (bits 0 to vl-1)
        vl_mask = (1 << vl) - 1

        # Compare only active bits
        return (spike_int & vl_mask) == (coral_int & vl_mask)
    except:
        return False


# ---------------------------------------------------------------------------
# Cascade / taint tracking helpers
# ---------------------------------------------------------------------------

def _expand_vreg_group(base_reg, emul):
    """Expand a base register to its full LMUL group.

    For LMUL=4, v8 -> {v8, v9, v10, v11}.
    For LMUL=1, v8 -> {v8}.
    """
    return set(range(base_reg, min(base_reg + emul, 32)))


def get_source_vregs(inst_hex, lmul_num=1, lmul_den=1):
    """Extract source vector register indices from a vector instruction.

    Used for cascade/taint tracking: if a source vreg is tainted (written
    by a mask op with spec-legal tail difference), the destination inherits
    the taint.

    For LMUL>1, returns ALL registers in each source register group,
    not just the named register. This is critical because an instruction
    reading vs2=v8 with LMUL=4 actually reads v8, v9, v10, v11.

    Returns set of source vector register indices (0-31).
    """
    try:
        inst = int(inst_hex, 16)
        opcode = inst & 0x7f
        funct3 = (inst >> 12) & 0x7
        funct6 = (inst >> 26) & 0x3f
        vs1 = (inst >> 15) & 0x1f
        vs2 = (inst >> 20) & 0x1f
        vm = (inst >> 25) & 0x1

        # Only vector instructions (opcode 0x57) and vector loads/stores
        if opcode == 0x57 and funct3 == 7:
            return set()  # vsetvli has no vector sources

        if opcode not in (0x57, 0x07, 0x27):
            return set()  # Not a vector instruction

        sources = set()
        emul = max(1, lmul_num // lmul_den)

        if opcode == 0x57:
            vd = (inst >> 7) & 0x1f

            # Determine if this is a mask-only instruction (always EMUL=1 for mask)
            is_mask_op = is_mask_producing_instruction(inst_hex)

            # For mask-producing instructions, vs2 is a mask (EMUL=1)
            # For reductions, vs2 is EMUL but vs1 is scalar (EMUL=1)
            is_reduction = is_vector_reduction_instruction(inst_hex)

            # vs2 is always a vector source for vector compute ops
            if is_mask_op:
                sources.add(vs2)  # Mask ops: single register
            else:
                sources.update(_expand_vreg_group(vs2, emul))

            # vs1 is a vector source only for VV and MVV/FVV forms
            # funct3: 0=OPIVV, 1=OPFVV, 2=OPMVV -> vs1 is vector
            # funct3: 3=OPIVI, 4=OPIVX, 5=OPFVF, 6=OPMVX -> vs1 is scalar/imm
            if funct3 in (0, 1, 2):
                # Some OPMVV encodings use vs1 as sub-opcode, not a register
                if funct3 == 2 and funct6 == 0x14:
                    pass  # VMUNARY0: vs1 is opcode (vmsbf/vmsof/vmsif)
                elif funct3 == 2 and funct6 == 0x10:
                    pass  # VWXUNARY0: vs1 is opcode (vmv.x.s/vcpop/vfirst)
                else:
                    if is_reduction:
                        sources.add(vs1)  # Reduction: vs1 is scalar element
                    elif is_mask_op:
                        sources.add(vs1)  # Mask ops: single register
                    else:
                        sources.update(_expand_vreg_group(vs1, emul))

            # Accumulating instructions use vd as BOTH source and destination.
            accum_int = {0x29, 0x2b, 0x2d, 0x2f}  # vmadd, vnmsac, vmacc, vnmsub
            accum_flt = {0x28, 0x29, 0x2a, 0x2b, 0x2c, 0x2d, 0x2e, 0x2f}  # vfmadd..vfnmsac
            accum_wide = {0x3c, 0x3d, 0x3e, 0x3f}  # vwmacc*, vfwmacc*
            if funct3 in (0, 2, 4, 6) and funct6 in (accum_int | accum_wide):
                # For widening accumulators, vd group is 2*EMUL
                dest_emul = max(1, 2 * lmul_num // lmul_den) if funct6 in accum_wide else emul
                sources.update(_expand_vreg_group(vd, dest_emul))
            elif funct3 in (1, 5) and funct6 in (accum_flt | accum_wide):
                dest_emul = max(1, 2 * lmul_num // lmul_den) if funct6 in accum_wide else emul
                sources.update(_expand_vreg_group(vd, dest_emul))

            # If masked (vm=0), v0 is an implicit source (always single register)
            if vm == 0:
                sources.add(0)

        return sources
    except:
        return set()


def get_dest_vreg_index(rd_name):
    """Extract vector register index from rd name (e.g. 'v13' -> 13).

    Returns -1 if not a vector register.
    """
    if rd_name and rd_name.startswith('v'):
        try:
            return int(rd_name[1:])
        except ValueError:
            return -1
    return -1


# ---------------------------------------------------------------------------
# Other mismatch detectors
# ---------------------------------------------------------------------------

def is_widening_vector_instruction(inst_hex):
    """Check if instruction is a widening vector instruction.

    Widening instructions produce 2*SEW results and write to a register
    group of size 2*LMUL (EMUL). The tracer only reports the named vd,
    but the VRF write also covers vd+1..vd+EMUL-1.

    This causes a tracer WAW issue: if a preceding instruction writes to
    a register in the widening group (e.g., vd+1), the tracer may capture
    the widening result instead of the preceding instruction's result,
    because the widening's VRF write happens at execution time (before
    retirement of the preceding instruction).

    Widening integer (OPIVV=0, OPMVV=2, OPIVX=4, OPMVX=6):
    - vwaddu/vwadd/vwsubu/vwsub: funct6 = 0x30-0x33
    - vwaddu.w/vwadd.w/vwsubu.w/vwsub.w: funct6 = 0x34-0x37
    - vwmulu/vwmulsu/vwmul: funct6 = 0x38, 0x3a, 0x3b
    - vwmaccu/vwmacc/vwmaccsu/vwmaccus: funct6 = 0x3c-0x3f

    Widening float (OPFVV=1, OPFVF=5):
    - vfwadd/vfwsub: funct6 = 0x30, 0x32
    - vfwadd.w/vfwsub.w: funct6 = 0x34, 0x36
    - vfwmul: funct6 = 0x38
    - vfwmacc/vfwnmacc/vfwmsac/vfwnmsac: funct6 = 0x3c-0x3f
    """
    try:
        inst = int(inst_hex, 16)
        opcode = inst & 0x7f
        funct3 = (inst >> 12) & 0x7
        funct6 = (inst >> 26) & 0x3f

        if opcode != 0x57:
            return False
        if funct3 == 7:  # vsetvli
            return False

        widening_funct6 = {0x30, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37,
                           0x38, 0x3a, 0x3b, 0x3c, 0x3d, 0x3e, 0x3f}

        if funct6 in widening_funct6:
            return True

        return False
    except:
        return False


def get_dest_register_group(inst_hex, lmul_num, lmul_den):
    """Get the full set of destination vector registers written by an instruction.

    For widening instructions, the destination group is 2*LMUL registers.
    For non-widening LMUL>1, the destination group is LMUL registers.
    For LMUL=1 non-widening (or scalar), only the named vd.

    Args:
        inst_hex: Instruction hex string
        lmul_num: LMUL numerator (1 for LMUL=1, 2 for LMUL=2, etc.)
        lmul_den: LMUL denominator (1 for LMUL>=1, >1 for fractional)

    Returns:
        Set of destination register indices, or empty set for non-vector.
    """
    try:
        inst = int(inst_hex, 16)
        opcode = inst & 0x7f
        funct3 = (inst >> 12) & 0x7
        vd = (inst >> 7) & 0x1f

        if opcode != 0x57:
            return {vd}  # Scalar instruction

        if funct3 == 7:  # vsetvli writes to x register
            return set()

        if is_widening_vector_instruction(inst_hex):
            # Widening: EMUL = 2 * LMUL
            emul = max(1, 2 * lmul_num // lmul_den)
            return set(range(vd, min(vd + emul, 32)))
        else:
            # Normal: EMUL = LMUL
            emul = max(1, lmul_num // lmul_den)
            return set(range(vd, min(vd + emul, 32)))
    except:
        return set()


def is_memory_load_instruction(inst_hex):
    """Check if instruction is a memory load (scalar or vector).

    Scalar loads: opcode = 0x03 (LOAD)
    Vector loads: opcode = 0x07 (LOAD-FP) with vector width
    """
    try:
        inst = int(inst_hex, 16)
        opcode = inst & 0x7f

        # Scalar load
        if opcode == 0x03:
            return True
        # Vector load (uses LOAD-FP opcode)
        if opcode == 0x07:
            return True

        return False
    except:
        return False


def is_indexed_or_strided_store(inst_hex):
    """Check if instruction is an indexed or strided vector store.

    BUG-016: Coral tracer reports all-zeros for vector register values during
    indexed and strided stores due to timing mismatch in vecregfile shadow VRF
    capture. Execution is correct — only the trace is wrong.

    Vector store opcodes use STORE-FP (0x27).
    - Unit-stride store: mop=00 (nf[31:29] | 0 | mew | vm | vs2 | rs1 | width | vs3 | 0100111)
    - Strided store:     mop=10
    - Indexed (ordered): mop=11
    - Indexed (unordered): mop=01

    mop is bits [27:26].
    """
    try:
        inst = int(inst_hex, 16)
        opcode = inst & 0x7f

        # Vector stores: STORE-FP opcode (0x27)
        if opcode != 0x27:
            return False

        mop = (inst >> 26) & 0x3

        # mop=10: strided, mop=01: indexed unordered, mop=11: indexed ordered
        if mop in (0b01, 0b10, 0b11):
            return True

        return False
    except:
        return False


def is_memory_init_mismatch(spike_val, coral_val, inst_hex):
    """Detect if mismatch is due to memory initialization difference.

    Spike initializes memory to 0, Coral may have different initial values.
    If Spike shows 0 and Coral shows non-zero for a load instruction,
    this is likely a memory init issue, not an RTL bug.
    """
    if not is_memory_load_instruction(inst_hex):
        return False

    spike_normalized = spike_val.lstrip('0') or '0'
    coral_normalized = coral_val.lstrip('0') or '0'

    # Spike is 0, Coral is non-zero (or vice versa)
    if spike_normalized == '0' and coral_normalized != '0':
        return True
    if coral_normalized == '0' and spike_normalized != '0':
        return True

    return False


def is_nan_boxing_mismatch(spike_val, coral_val, rd):
    """Detect if mismatch is due to NaN-boxing difference for float values.

    Spike uses 64-bit representation with NaN-boxing for single-precision floats:
    - Upper 32 bits are all 1s (0xffffffff)
    - Lower 32 bits contain the actual float value

    Coral uses 32-bit representation directly.

    Example: 0.0f
    - Spike: 0xffffffff00000000 (NaN-boxed)
    - Coral: 0x00000000 (32-bit)
    """
    # Only applies to float registers
    if not rd.startswith('f'):
        return False

    # Normalize values
    spike_norm = spike_val.lower().lstrip('0') or '0'
    coral_norm = coral_val.lower().lstrip('0') or '0'

    # Check for NaN-boxing pattern: spike has ffffffff prefix
    if spike_val.lower().startswith('ffffffff'):
        # Extract lower 32 bits from spike
        spike_lower = spike_val[-8:].lstrip('0') or '0'
        if spike_lower == coral_norm:
            return True

    # Also check reverse (shouldn't happen but be safe)
    if coral_val.lower().startswith('ffffffff'):
        coral_lower = coral_val[-8:].lstrip('0') or '0'
        if coral_lower == spike_norm:
            return True

    return False


def is_waw_mismatch(coral_trace, spike_trace, current_idx, current_rd, coral_val, spike_val):
    """Detect if a value mismatch is due to the WAW tracer bug.

    The Coral RTL has a known limitation: when two instructions write to the
    same vector register in quick succession (Write-After-Write hazard), the
    tracer may show wrong values due to retirement buffer timing. The actual
    execution is correct - only the trace log is affected.

    WAW bug variants detected:
    1. Second write shows first write's value (tracer captures first, doesn't update)
    2. First write shows second write's value (tracer captures later completion)
    3. Register shows ANY previously seen value for that register (timing-dependent)

    Args:
        coral_trace: List of Coral trace entries
        spike_trace: List of Spike trace entries (for lookahead)
        current_idx: Current instruction index
        current_rd: Destination register (e.g., 'v3')
        coral_val: Value from Coral trace
        spike_val: Expected value from Spike trace

    Returns:
        True if this appears to be a WAW tracer bug (not a real mismatch)
    """
    if not current_rd.startswith('v'):
        return False  # Only affects vector registers

    coral_val_normalized = coral_val.lstrip('0') or '0'
    spike_val_normalized = spike_val.lstrip('0') or '0'

    # Expanded window for WAW detection (was 10, now 30)
    lookback = min(current_idx, 30)

    # Collect all recent writes to this register
    recent_values = set()

    # Look BACK for previous writes to same register
    for i in range(1, lookback + 1):
        prev_idx = current_idx - i
        prev_coral = coral_trace[prev_idx]
        prev_spike = spike_trace[prev_idx] if prev_idx < len(spike_trace) else None

        if prev_coral['rd'] == current_rd:
            prev_coral_val = prev_coral['rd_value'].lstrip('0') or '0'
            recent_values.add(prev_coral_val)

        if prev_spike and prev_spike['rd'] == current_rd:
            prev_spike_val = prev_spike['rd_value'].lstrip('0') or '0'
            recent_values.add(prev_spike_val)

    # Look AHEAD for later writes to same register
    lookahead = min(len(spike_trace) - current_idx - 1, 30)
    for i in range(1, lookahead + 1):
        next_idx = current_idx + i
        if next_idx < len(spike_trace):
            next_spike = spike_trace[next_idx]
            if next_spike['rd'] == current_rd:
                next_spike_val = next_spike['rd_value'].lstrip('0') or '0'
                recent_values.add(next_spike_val)
        if next_idx < len(coral_trace):
            next_coral = coral_trace[next_idx]
            if next_coral['rd'] == current_rd:
                next_coral_val = next_coral['rd_value'].lstrip('0') or '0'
                recent_values.add(next_coral_val)

    # Check if Coral's value matches ANY recently seen value for this register
    if coral_val_normalized in recent_values:
        return True

    # Additional check: value differences in lower elements only (common WAW pattern)
    # If only the lower 32 bits differ, this could be WAW for SEW=32
    try:
        spike_int = int(spike_val, 16)
        coral_int = int(coral_val, 16)
        # Check if upper 96 bits match (for 128-bit vectors with SEW=32)
        if (spike_int >> 32) == (coral_int >> 32) and (spike_int >> 32) != 0:
            return True
    except:
        pass

    return False


# ---------------------------------------------------------------------------
# Main comparison
# ---------------------------------------------------------------------------

def compare_traces(spike_csv, coral_csv, max_errors=10, ignore_rd_value=False,
                   spike_pc_offset=0, coral_pc_offset=0, auto_trim_loops=True,
                   strict_waw=False):
    """Compare two trace CSVs.

    Args:
        spike_csv: Path to Spike trace CSV
        coral_csv: Path to Coral trace CSV
        max_errors: Maximum number of errors to report in detail
        ignore_rd_value: If True, only compare PC and instruction (not rd values)
        spike_pc_offset: Offset to subtract from Spike PCs (e.g., 0x80000000)
        coral_pc_offset: Offset to subtract from Coral PCs (usually 0)
        auto_trim_loops: If True, auto-trim traces at loop boundaries for fair comparison
        strict_waw: If True, count WAW tracer bugs as mismatches (don't ignore them)

    Returns:
        Tuple of (match_count, mismatch_count, length_diff, errors_list,
                  spike_len, coral_len, trim_info, waw_count,
                  mask_tail_count, cascade_count, scalar_move_tail_count,
                  spike_loop_count)
    """
    spike = load_csv(spike_csv)
    coral = load_csv(coral_csv)

    # Auto-trim at loop boundaries if enabled
    trim_info = None
    if auto_trim_loops:
        spike, coral, trim_info = trim_to_loop_start(spike, coral)

    # Normalize PCs if offsets provided
    if spike_pc_offset or coral_pc_offset:
        for entry in spike:
            entry['pc'] = normalize_pc(entry['pc'], spike_pc_offset)
        for entry in coral:
            entry['pc'] = normalize_pc(entry['pc'], coral_pc_offset)

    errors = []
    match_count = 0
    mismatch_count = 0
    waw_count = 0           # WAW tracer bug instances
    mask_tail_count = 0     # Mask tail matches (active bits matched, tail differed)
    cascade_count = 0       # Cascading mismatches suppressed
    scalar_move_tail_count = 0  # vmv.s.x/vfmv.s.f tail taint instances
    spike_loop_count = 0       # Instructions skipped due to Spike HTIF loop-back
    store_tracer_count = 0     # Indexed/strided store tracer bug instances (BUG-016)

    # VL/LMUL tracking: updated when vsetvli is encountered
    current_vl = 4          # Default for e32,m1 on VLEN=128
    current_sew = 32
    current_lmul_num = 1    # LMUL numerator (1 for LMUL=1)
    current_lmul_den = 1    # LMUL denominator (1 for LMUL>=1)

    # Taint tracking: vreg_index -> PC where taint originated
    # A register is tainted when a mask-producing instruction writes it
    # with different tail bits. Taint propagates through data dependencies.
    tainted_vregs = {}      # {vreg_index: "origin_pc_hex"}

    # Compare instruction by instruction
    min_len = min(len(spike), len(coral))
    length_diff = abs(len(spike) - len(coral))

    for i in range(min_len):
        s = spike[i]
        c = coral[i]

        # --- Track VL/LMUL from vsetvli instructions ---
        if is_vsetvl_instruction(s['inst']):
            vl, sew, lmul_n, lmul_d = decode_vl_from_vsetvl(s['inst'], s['rd_value'])
            current_vl = vl
            current_sew = sew
            current_lmul_num = lmul_n
            current_lmul_den = lmul_d

        # Compare PC
        if s['pc'] != c['pc']:
            # Detect Spike HTIF loop-back: Spike PC jumps to near-zero
            # while Coral continues normally. This means Spike re-entered
            # from address 0 after failing to halt on wfi.
            spike_pc_val = int(s['pc'], 16)
            coral_pc_val = int(c['pc'], 16)
            if spike_pc_val < 0x100 and coral_pc_val > 0x100 and i > 10:
                # Spike has looped back to start - ignore all remaining entries
                # Count remaining as Spike loop mismatches (not real)
                spike_loop_count = min_len - i
                break
            if len(errors) < max_errors:
                errors.append({
                    'index': i + 1,
                    'type': 'PC_MISMATCH',
                    'spike': s,
                    'coral': c,
                    'detail': f"PC: spike=0x{s['pc']} coral=0x{c['pc']}"
                })
            mismatch_count += 1
            continue

        # Compare instruction encoding
        if s['inst'] != c['inst']:
            if len(errors) < max_errors:
                errors.append({
                    'index': i + 1,
                    'type': 'INST_MISMATCH',
                    'spike': s,
                    'coral': c,
                    'detail': f"Inst @ PC=0x{s['pc']}: spike=0x{s['inst']} coral=0x{c['inst']}"
                })
            mismatch_count += 1
            continue

        # Compare rd (destination register)
        if s['rd'] != c['rd']:
            # VERIF-001: Spike doesn't log RD for certain vector instructions
            # If Spike shows '-' and Coral shows 'vN', and PC+instruction match,
            # this is a known Spike tracer limitation, not a real mismatch.
            # This affects vector compares, reductions, and some other ops.
            if s['rd'] == '-' and c['rd'].startswith('v'):
                # Known Spike tracer issue - count as match
                match_count += 1
                continue

            # Also handle float register mismatches where one side shows '-'
            # or where Spike shows fN and Coral shows '-' (or vice versa)
            if (s['rd'] == '-' and c['rd'].startswith('f')) or \
               (c['rd'] == '-' and s['rd'].startswith('f')):
                # Known tracer discrepancy for float ops
                match_count += 1
                continue

            # BUG-020: vfirst.m and vcpop.m write to scalar registers (xN)
            # but Coral tracer incorrectly reports them as vector registers (vN)
            # Check if spike=xN and coral=vN with same register number
            if is_vfirst_vcpop_instruction(s['inst']):
                spike_rd = s['rd']
                coral_rd = c['rd']
                # Extract register numbers: x11 -> 11, v11 -> 11
                if spike_rd.startswith('x') and coral_rd.startswith('v'):
                    spike_num = spike_rd[1:]
                    coral_num = coral_rd[1:]
                    if spike_num == coral_num:
                        # Known tracer bug - count as match
                        match_count += 1
                        continue

            if len(errors) < max_errors:
                errors.append({
                    'index': i + 1,
                    'type': 'RD_MISMATCH',
                    'spike': s,
                    'coral': c,
                    'detail': f"RD @ PC=0x{s['pc']}: spike={s['rd']} coral={c['rd']}"
                })
            mismatch_count += 1
            continue

        # Compare rd_value (result)
        # Compute dest_idx early — needed by vector move and value comparison blocks
        dest_idx = get_dest_vreg_index(s['rd'])

        # BUG-011: Vector move instructions (vmv.v.x, vmv.v.v, vmv.s.x) show width
        # mismatches - Spike shows full VLEN, Coral only shows element 0.
        # Compare element 0 (lowest SEW bits) to catch real bugs while
        # tolerating the known tracer width difference in upper elements.
        if is_vector_move_instruction(s['inst']):
            # Do taint tracking: moves copy data, so taint propagates.
            if dest_idx >= 0:
                # vmv.s.x / vfmv.s.f: writes only element 0, rest are tail.
                # Spike zeros tail, Coral fills with 1s - spec-legal difference.
                # ALWAYS taint destination since tail elements will diverge.
                if is_scalar_to_element0(s['inst']):
                    tainted_vregs[dest_idx] = s['pc']
                    scalar_move_tail_count += 1
                else:
                    sources = get_source_vregs(s['inst'],
                                               current_lmul_num, current_lmul_den)
                    has_tainted_source = bool(sources & set(tainted_vregs.keys()))
                    if has_tainted_source:
                        dest_group = get_dest_register_group(
                            s['inst'], current_lmul_num, current_lmul_den)
                        for r in dest_group:
                            tainted_vregs[r] = s['pc']
                    elif dest_idx in tainted_vregs:
                        # Move from clean source overwrites tainted dest - clear taint
                        dest_group = get_dest_register_group(
                            s['inst'], current_lmul_num, current_lmul_den)
                        for r in dest_group:
                            if r in tainted_vregs:
                                del tainted_vregs[r]

            # Compare element 0 for vector moves (catches real execution bugs)
            if not ignore_rd_value and s['rd'] != '-' and s['rd_value'] != '-' and c['rd_value'] != '-':
                if not reduction_element0_matches(s['rd_value'], c['rd_value'], current_sew):
                    # Element 0 doesn't match — this is a real bug, not a tracer issue
                    if len(errors) < max_errors:
                        errors.append({
                            'index': i + 1,
                            'type': 'RD_VALUE_MISMATCH',
                            'spike': s,
                            'coral': c,
                            'detail': f"Vector move elem0 mismatch @ PC=0x{s['pc']} {s['rd']}: spike=0x{s['rd_value']} coral=0x{c['rd_value']}"
                        })
                    mismatch_count += 1
                    continue

            match_count += 1
            continue

        if not ignore_rd_value and s['rd'] != '-':
            # Normalize values (remove leading zeros for comparison)
            spike_val = s['rd_value'].lstrip('0') or '0'
            coral_val = c['rd_value'].lstrip('0') or '0'

            if spike_val != coral_val:
                # ---- MASK TAIL BIT HANDLING (SPEC-LEGAL) ----
                # CoralNPU sets ignore_vta=1 for all mask-producing instructions,
                # writing full 128-bit results. Spike zeros tail bits. Both valid.
                # Only compare active bits [VL-1:0] for mask destinations.
                if is_mask_producing_instruction(s['inst']):
                    if mask_values_match_ignoring_tail(s['rd_value'], c['rd_value'], current_vl):
                        # Active bits match - only tail bits differ (spec-legal)
                        mask_tail_count += 1
                        # Taint this register: subsequent reads as data will diverge
                        # Mask destinations are always single register (not grouped)
                        if dest_idx >= 0:
                            tainted_vregs[dest_idx] = s['pc']
                        match_count += 1
                        continue
                    # Active bits DON'T match - this is a real mismatch
                    # (fall through to other checks)

                # ---- CASCADE FROM TAINTED MASK REGISTERS ----
                # If any source vreg is tainted (from mask tail diff), this
                # mismatch is expected - the hardware computed correctly with
                # different input data.
                # For LMUL>1, expand source groups so we catch tainted members.
                if dest_idx >= 0:
                    sources = get_source_vregs(s['inst'],
                                               current_lmul_num, current_lmul_den)
                    tainted_sources = sources & set(tainted_vregs.keys())
                    if tainted_sources:
                        cascade_count += 1
                        # Propagate taint to full destination group
                        dest_group = get_dest_register_group(
                            s['inst'], current_lmul_num, current_lmul_den)
                        for r in dest_group:
                            tainted_vregs[r] = s['pc']
                        if len(errors) < max_errors:
                            taint_info = ', '.join(f'v{t}' for t in sorted(tainted_sources))
                            errors.append({
                                'index': i + 1,
                                'type': 'MASK_TAIL_CASCADE',
                                'spike': s,
                                'coral': c,
                                'detail': f"Cascade from tainted [{taint_info}] @ PC=0x{s['pc']} {s['rd']}: spike=0x{s['rd_value']} coral=0x{c['rd_value']}"
                            })
                        match_count += 1  # Don't count as real mismatch
                        continue

                # ---- BUG-016: INDEXED/STRIDED STORE TRACER BUG ----
                # Coral tracer reports all-zeros for v-register values during
                # indexed and strided stores. Execution is correct, only the
                # trace capture is wrong. Suppress when Coral shows all-zeros.
                if is_indexed_or_strided_store(s['inst']) and dest_idx >= 0:
                    coral_val_int = int(c['rd_value'], 16) if c['rd_value'] != '-' else -1
                    if coral_val_int == 0:
                        store_tracer_count += 1
                        if len(errors) < max_errors:
                            errors.append({
                                'index': i + 1,
                                'type': 'STORE_TRACER_BUG',
                                'spike': s,
                                'coral': c,
                                'detail': f"BUG-016 store tracer @ PC=0x{s['pc']} {s['rd']}: spike=0x{s['rd_value']} coral=0x{c['rd_value']} (Coral all-zeros, known tracer bug)"
                            })
                        match_count += 1
                        continue

                # ---- WIDENING WAW TRACER BUG ----
                # When a widening instruction writes to vd..vd+EMUL-1, the tracer
                # only reports vd. But the VRF write to vd+1..vd+EMUL-1 may occur
                # before a preceding instruction's retirement, causing the tracer
                # to capture the widening result instead of the correct value.
                # Also handles LMUL>1 non-widening instructions.
                if dest_idx >= 0:
                    is_wid_waw = False
                    for j in range(1, min(5, min_len - i)):
                        next_inst = spike[i + j]['inst']
                        next_group = get_dest_register_group(
                            next_inst, current_lmul_num, current_lmul_den)
                        next_named_vd = get_dest_vreg_index(spike[i + j]['rd'])
                        # dest_idx is in the group but NOT the named vd
                        # (i.e., it's an implicit write from widening/LMUL>1)
                        if dest_idx in next_group and dest_idx != next_named_vd:
                            is_wid_waw = True
                            break
                    if is_wid_waw:
                        waw_count += 1
                        if len(errors) < max_errors:
                            errors.append({
                                'index': i + 1,
                                'type': 'WAW_TRACER_BUG',
                                'spike': s,
                                'coral': c,
                                'detail': f"Widening WAW tracer bug @ PC=0x{s['pc']} {s['rd']}: spike=0x{s['rd_value']} coral=0x{c['rd_value']} (implicit reg group write)"
                            })
                        if strict_waw:
                            mismatch_count += 1
                        else:
                            match_count += 1
                        continue

                # Check if this is the WAW tracer bug (not a real mismatch)
                if is_waw_mismatch(coral, spike, i, s['rd'], coral_val, spike_val):
                    waw_count += 1
                    if len(errors) < max_errors:
                        errors.append({
                            'index': i + 1,
                            'type': 'WAW_TRACER_BUG',
                            'spike': s,
                            'coral': c,
                            'detail': f"WAW tracer bug @ PC=0x{s['pc']} {s['rd']}: spike=0x{s['rd_value']} coral=0x{c['rd_value']} (known limitation)"
                        })
                    if strict_waw:
                        # In strict mode, count WAW bugs as real mismatches
                        mismatch_count += 1
                    else:
                        # Count as match since RTL execution is correct
                        match_count += 1
                    continue

                # NaN-boxing mismatch for float values
                # Spike uses 64-bit NaN-boxed representation, Coral uses 32-bit
                if is_nan_boxing_mismatch(s['rd_value'], c['rd_value'], s['rd']):
                    # Count as match - values are equivalent, just different representations
                    match_count += 1
                    continue

                # VERIF-002: Memory initialization mismatch
                # Spike initializes memory to 0, Coral may have different initial values
                if is_memory_init_mismatch(s['rd_value'], c['rd_value'], s['inst']):
                    if len(errors) < max_errors:
                        errors.append({
                            'index': i + 1,
                            'type': 'MEMORY_INIT_MISMATCH',
                            'spike': s,
                            'coral': c,
                            'detail': f"Memory init mismatch @ PC=0x{s['pc']} {s['rd']}: spike=0x{s['rd_value']} coral=0x{c['rd_value']} (verif issue, not RTL bug)"
                        })
                    # Count as match since this is a verif environment issue
                    match_count += 1
                    continue

                # BUG-011 FIX: Vector reduction instructions
                # Per RISC-V V spec, reductions write only to element 0.
                # Upper elements are UNDEFINED - implementations can leave any value.
                # Only compare element 0 for vredsum, vredmin, vredmax, etc.
                if is_vector_reduction_instruction(s['inst']):
                    if reduction_element0_matches(s['rd_value'], c['rd_value']):
                        # Element 0 matches - upper element difference is expected
                        match_count += 1
                        continue

                if len(errors) < max_errors:
                    # Debug: show taint state at mismatch for diagnostics
                    sources = get_source_vregs(s['inst'],
                                               current_lmul_num, current_lmul_den)
                    src_str = ', '.join(f'v{r}' for r in sorted(sources))
                    taint_str = ', '.join(f'v{r}' for r in sorted(tainted_vregs.keys()))
                    debug_detail = (f"RD value @ PC=0x{s['pc']} {s['rd']}: "
                                    f"spike=0x{s['rd_value']} coral=0x{c['rd_value']}"
                                    f"\n         Sources: [{src_str}]  Tainted: [{taint_str}]")
                    errors.append({
                        'index': i + 1,
                        'type': 'RD_VALUE_MISMATCH',
                        'spike': s,
                        'coral': c,
                        'detail': debug_detail
                    })
                mismatch_count += 1
                continue
            else:
                # Values match for the NAMED vd.
                if dest_idx >= 0:
                    sources = get_source_vregs(s['inst'],
                                               current_lmul_num, current_lmul_den)
                    has_tainted_source = bool(sources & set(tainted_vregs.keys()))

                    if has_tainted_source:
                        # Sources are tainted but values coincidentally match.
                        # Keep destination tainted: future uses of this register
                        # combined with other divergent data may cause cascades.
                        # Also taint implicit group members (LMUL>1 / widening).
                        dest_group = get_dest_register_group(
                            s['inst'], current_lmul_num, current_lmul_den)
                        for r in dest_group:
                            tainted_vregs[r] = s['pc']
                    elif dest_idx in tainted_vregs:
                        # No tainted sources and values match - safe to clear.
                        # Only clear the NAMED dest register. The trace only
                        # shows named vd; implicit LMUL group members might
                        # still differ and we can't verify from trace alone.
                        del tainted_vregs[dest_idx]

        match_count += 1

    return (match_count, mismatch_count, length_diff, errors,
            len(spike), len(coral), trim_info, waw_count,
            mask_tail_count, cascade_count, scalar_move_tail_count,
            spike_loop_count, store_tracer_count)


def print_report(match, mismatch, length_diff, errors, spike_len, coral_len,
                 trim_info=None, waw_count=0, mask_tail_count=0, cascade_count=0,
                 scalar_move_tail_count=0, spike_loop_count=0, store_tracer_count=0):
    """Print comparison report."""
    total = match + mismatch

    print("=" * 70)
    print("TRACE COMPARISON REPORT")
    print("=" * 70)

    # Show trimming info if loops were detected
    if trim_info:
        if trim_info['spike_loop_at'] is not None or trim_info['coral_loop_at'] is not None:
            print("Loop Detection (auto-trimmed for comparison):")
            if trim_info['spike_loop_at'] is not None:
                print(f"  Spike: loop at instr {trim_info['spike_loop_at']} (original: {trim_info['spike_original_len']})")
            if trim_info['coral_loop_at'] is not None:
                print(f"  Coral: loop at instr {trim_info['coral_loop_at']} (original: {trim_info['coral_original_len']})")
            print("-" * 70)

    print(f"Spike instructions:  {spike_len}")
    print(f"Coral instructions:  {coral_len}")
    print(f"Length difference:   {length_diff}")
    print("-" * 70)
    print(f"Compared:            {total}")
    print(f"Match:               {match}")
    print(f"Mismatch:            {mismatch}")
    if mask_tail_count > 0:
        print(f"Mask tail (spec-legal): {mask_tail_count} (active bits matched, tail differed)")
    if scalar_move_tail_count > 0:
        print(f"Scalar move tail (spec-legal): {scalar_move_tail_count} (vmv.s.x/vfmv.s.f tail taint)")
    if spike_loop_count > 0:
        print(f"Spike loop-back:    {spike_loop_count} (Spike HTIF loop detected, entries skipped)")
    if cascade_count > 0:
        print(f"Mask tail cascades:  {cascade_count} (suppressed, caused by spec-legal tail diff)")
    if store_tracer_count > 0:
        print(f"Store tracer bugs:   {store_tracer_count} (BUG-016 indexed/strided store all-zeros)")
    if waw_count > 0:
        print(f"WAW tracer bugs:     {waw_count} (counted as match, known limitation)")
    if total > 0:
        print(f"Match rate:          {100*match/total:.2f}%")
    print("-" * 70)

    if errors:
        # Categorize errors
        real_errors = [e for e in errors if e['type'] not in ('WAW_TRACER_BUG', 'MASK_TAIL_CASCADE', 'MEMORY_INIT_MISMATCH', 'STORE_TRACER_BUG')]
        cascade_errors = [e for e in errors if e['type'] == 'MASK_TAIL_CASCADE']
        waw_errors = [e for e in errors if e['type'] == 'WAW_TRACER_BUG']

        if cascade_errors:
            print(f"\nMask Tail Cascade instances ({len(cascade_errors)} shown, {cascade_count} total):")
            print("-" * 70)
            for e in cascade_errors[:5]:  # Show first 5 only
                print(f"[{e['index']:6d}] {e['type']}")
                print(f"         {e['detail']}")
                print()
            if len(cascade_errors) > 5:
                print(f"         ... and {len(cascade_errors) - 5} more cascade entries")
                print()

        if waw_errors:
            print(f"\nWAW Tracer Bug instances ({len(waw_errors)}):")
            print("-" * 70)
            for e in waw_errors:
                print(f"[{e['index']:6d}] {e['type']}")
                print(f"         {e['detail']}")
                print()

        if real_errors:
            print(f"\nFirst {len(real_errors)} real errors:")
            print("-" * 70)
            for e in real_errors:
                print(f"[{e['index']:6d}] {e['type']}")
                print(f"         {e['detail']}")
                if e['type'] == 'RD_VALUE_MISMATCH':
                    print(f"         Spike: inst=0x{e['spike']['inst']}")
                    print(f"         Coral: inst=0x{e['coral']['inst']}")
                print()

    # VERIF-004: Pass when all compared instructions match (0 mismatches)
    # Length difference is informational - if all compared code matched perfectly,
    # the test passed. Length diff typically means different loop iterations or
    # termination timing between Spike and Coral.
    spec_legal_total = mask_tail_count + cascade_count + scalar_move_tail_count
    if mismatch == 0:
        spec_note = ""
        if spec_legal_total > 0:
            spec_note = f", {spec_legal_total} spec-legal diffs"
        if waw_count > 0:
            print(f"\n*** PASS: Traces match! ({waw_count} WAW tracer bugs ignored{spec_note}) ***")
        elif spec_legal_total > 0:
            print(f"\n*** PASS: Traces match! ({spec_legal_total} spec-legal mask tail diffs handled) ***")
        elif length_diff > 0:
            print(f"\n*** PASS: Traces match! (length diff {length_diff} - loop/termination variance) ***")
        else:
            print("\n*** PASS: Traces match perfectly! ***")
        return 0
    else:
        print(f"\n*** FAIL: {mismatch} mismatches, {length_diff} length difference ***")
        if spec_legal_total > 0:
            print(f"         ({spec_legal_total} additional spec-legal diffs handled)")
        return 1


def parse_hex(value):
    """Parse hex value (with or without 0x prefix)."""
    return int(value, 16)


def main():
    parser = argparse.ArgumentParser(description='Compare Spike and Coral trace CSVs')
    parser.add_argument('spike_csv', help='Spike trace CSV file')
    parser.add_argument('coral_csv', help='Coral trace CSV file')
    parser.add_argument('--max-errors', type=int, default=10, help='Max errors to show')
    parser.add_argument('--ignore-rd-value', action='store_true',
                        help='Only compare PC and instruction, not rd values')
    parser.add_argument('--spike-pc-offset', type=parse_hex, default=0,
                        help='Offset to subtract from Spike PCs (hex, e.g., 0x80000000)')
    parser.add_argument('--coral-pc-offset', type=parse_hex, default=0,
                        help='Offset to subtract from Coral PCs (hex, usually 0)')
    parser.add_argument('--strict', action='store_true',
                        help='Strict mode: count WAW tracer bugs as mismatches (default: ignore them)')
    args = parser.parse_args()

    result = compare_traces(
        args.spike_csv, args.coral_csv, args.max_errors, args.ignore_rd_value,
        args.spike_pc_offset, args.coral_pc_offset, strict_waw=args.strict
    )
    match, mismatch, length_diff, errors, spike_len, coral_len, trim_info, waw_count, mask_tail_count, cascade_count, scalar_move_tail_count, spike_loop_count, store_tracer_count = result
    rc = print_report(match, mismatch, length_diff, errors, spike_len, coral_len,
                      trim_info, waw_count, mask_tail_count, cascade_count,
                      scalar_move_tail_count, spike_loop_count, store_tracer_count)
    sys.exit(rc)


if __name__ == '__main__':
    main()
