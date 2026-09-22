"""How many cores this box really has, and how to split them between simulation and synthesis.

Stdlib only, Python 3.9 syntax (the openlane image's system python). Vendored byte-identical into
verilator-api/ and openlane-api/ because each service's docker build context is its own directory;
run/check_capacity_sync.sh asserts the copies match.

WHY THIS EXISTS. Both services sized themselves from os.cpu_count(): 12 on this laptop, which is
two P-cores with hyperthreading plus eight E-cores -- TEN physical cores. verilator-api took
min(4, 12//6) = 2 evaluation lanes x (12-1)//2 = 5 jobs = 10 simulations, and openlane-api added
2-3 syntheses on top, from a MemAvailable figure that flips between runs. Thirteen single-threaded
processes on ten cores. The divisor 6 had nothing recorded behind it.

The cost was measured, not modelled: the accelerated design's synthesis is one single-threaded ABC
run that takes ~19 minutes on an idle core and 38-52 minutes beside ten simulations. For any group
of real accelerators that ABC run IS the critical path, and the simulations were starving it.

THE RULE. Count PHYSICAL cores over the affinity set (sysfs core_cpus_list), honour a cgroup cpu
quota, size synthesis lanes L from MemTotal (never MemAvailable) with a reservation per lane, and
give simulation the rest: S = P - L. Synthesis lanes are pinned to the fastest cores; simulations are
not pinned, because with runnable processes <= physical cores the kernel keeps them off the pinned
cores' siblings by itself, and when synthesis is idle the fast cores are free for sims.

ONE CODEBASE. Nothing here is per machine. On a 32-vCPU c6i.8xlarge (16 physical, 64 GB) the same
rule gives L = 5, S = 11; on this laptop L = 2, S = 8. Env overrides exist for an operator who has
measured better: OPENLANE_LANES, VERILATOR_SIM_SLOTS, EDA_SYNTH_TREE_MB.
"""
import os


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _parse_cpulist(s):
    """'0-1,4,6-7' -> {0, 1, 4, 6, 7}"""
    out = set()
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def affinity():
    """The CPUs this process may run on. A docker cpuset or taskset narrows it; a cpu quota does not."""
    try:
        return set(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return set(range(os.cpu_count() or 2))


def quota_cores():
    """Whole cores allowed by a cgroup cpu quota (v2 then v1), or None when unlimited.

    A quota is invisible to nproc and to sched_getaffinity: a container given `cpus: 2.5` still sees
    every CPU on the host. Sizing from what it sees, rather than what it may use, is how a quota'd
    box admits four evaluations and then time-slices them all to death."""
    cm = _read("/sys/fs/cgroup/cpu.max")
    if cm and not cm.startswith("max"):
        parts = cm.split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit() and int(parts[1]) > 0:
            return max(1, int(parts[0]) // int(parts[1]))
    q = _read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    p = _read("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if q and p and q.lstrip("-").isdigit() and p.isdigit() and int(q) > 0 and int(p) > 0:
        return max(1, int(q) // int(p))
    return None


def physical_cores(cpus):
    """[frozenset(sibling cpus)] per physical core, FASTEST FIRST; None if the topology is not exposed.

    Fastest first so that synthesis -- the single-threaded critical path -- lands on a P-core here and
    on any core on a uniform EC2 box, where the order is just by id."""
    groups, seen = [], set()
    for c in sorted(cpus):
        if c in seen:
            continue
        sib = None
        for name in ("core_cpus_list", "thread_siblings_list"):
            s = _read("/sys/devices/system/cpu/cpu%d/topology/%s" % (c, name))
            if s:
                sib = _parse_cpulist(s) & set(cpus)
                break
        if not sib:
            return None
        khz = 0
        for x in sib:
            v = _read("/sys/devices/system/cpu/cpu%d/cpufreq/cpuinfo_max_freq" % x)
            if v and v.isdigit():
                khz = max(khz, int(v))
        groups.append((-khz, min(sib), frozenset(sib)))
        seen |= sib
    groups.sort()
    return [g[2] for g in groups]


def mem_total_mb():
    for line in (_read("/proc/meminfo") or "").splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) // 1024
    return 0


def _envint(env, name, default):
    raw = (env.get(name) or "").strip()
    return int(raw) if raw.isdigit() else default


def plan(role, env=None, g_max=8, m_slot_mb=256):
    """The split. role is 'sim' or 'synth' -- both compute the same L and S so their banners agree;
    the role only decides which env override wins.

    Returns dict(P, S, L, synth_cores, logical, quota, topology, mem_total_mb, warn)."""
    env = os.environ if env is None else env
    A = affinity()
    Q = quota_cores()
    cores = physical_cores(A)
    topology = cores is not None
    if cores is None:
        # No topology files: assume SMT pairs so an oversubscription is impossible, never the
        # optimistic count. Ordered by id; there is nothing to prefer.
        half = max(1, len(A) // 2)
        cores = [frozenset({c}) for c in sorted(A)][:half]
    P = min(len(cores), Q) if Q else len(cores)
    P = max(1, P)

    # Synthesis lanes from MEMORY, reserved first. yosys alone peaked at 5,676 MB in the recorded log
    # and yosys-abc is a separate process under the same per-process RLIMIT_AS, so a lane's tree is
    # provably under 2 x 6,144 and was measured at about 8 GB in practice; 8,192 MB per lane until
    # someone measures it with EDA_SYNTH_TREE_MB, in which case 1.25x that.
    tree = _envint(env, "EDA_SYNTH_TREE_MB", 0)
    m_tree = 1.25 * tree if tree else 8192.0
    m_host = 0.75 * mem_total_mb()
    l_mem = int((m_host - P * m_slot_mb) // m_tree)
    L = max(1, min(g_max, P - 2, l_mem))
    S = max(1, P - L)
    warn = []
    if l_mem < 1:
        warn.append("memory reserves fewer than one synthesis lane; running one anyway")
    if L * 12288 > m_host:
        warn.append("synthesis tree unmeasured; worst case %d x 12288 MB exceeds 0.75 x MemTotal" % L)
    if not topology:
        warn.append("no cpu topology exposed; assumed SMT pairs and used half the logical cpus")

    ol = _envint(env, "OPENLANE_LANES", 0)
    vs = _envint(env, "VERILATOR_SIM_SLOTS", 0)
    if ol:
        L = max(1, ol)
    if vs:
        S = max(1, vs)
    elif ol:
        S = max(1, P - L)
    if role == "sim" and Q and not vs:
        S = max(1, min(S, Q))

    return {
        "P": P, "S": S, "L": L,
        "synth_cores": [sorted(c) for c in cores[:L]] if topology else [],
        "logical": len(A), "quota": Q, "topology": topology,
        "mem_total_mb": int(m_host / 0.75), "warn": warn,
    }


if __name__ == "__main__":
    import json
    for role in ("sim", "synth"):
        print(role, json.dumps(plan(role), indent=1))
