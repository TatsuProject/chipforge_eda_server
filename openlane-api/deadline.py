"""How long an evaluation may run, and what to say when it runs out.

Stdlib only, Python 3.9 syntax (the openlane image's system python). Vendored byte-identical into
verilator-api/ and openlane-api/ like capacity.py; check_capacity_sync.sh asserts the copies match.

ONE CLOCK, SET BY THE CHALLENGE SERVER. Every batch has an absolute evaluation deadline
(evaluation_ends_at). The validator turns it into a per-request budget when it sends a submission
(time_limit_s = deadline - now - its own margin for answering) and the gateway passes what is left of
it to each service. A service measures time from the moment the request ARRIVED, so a job that waits
in this service's queue spends its own budget waiting; when it leaves the queue it may run for
whatever is left. EVAL_TIMEOUT_S, the operator's constant, only applies to a request that carries no
budget (a validator older than this change).

WHOSE FAULT IS A TIMEOUT. The validator also sends fair_limit_s: the run time every submission of the
batch is entitled to (the evaluation window minus the margins). A run that was given at least that
much and still did not finish is the submission's (fault miner, score 0). A run cut shorter than that
-- it waited in a queue, or the validator picked it up late -- proves nothing about the submission:
fault system, retryable, and the challenge server puts it in a later batch (MAX_REBATCH_ATTEMPTS).

Every timeout names where it happened (which service, which stage), the limit it ran under, what
decided that limit, how long it ran and how long it was queued.
"""
import os
import re
import time


def _env_seconds(name, default):
    try:
        return max(0, int(str(os.environ.get(name, "")).strip() or default))
    except ValueError:
        return default


# A run that would get less than this after the queue is not started: it cannot finish, and starting
# it would only hold a lane that the next request could use. (EDA_MIN_RUN_S: tests use short budgets.)
MIN_RUN_S = _env_seconds("EDA_MIN_RUN_S", 120)

# fair_limit_s missing (an older validator that sends only time_limit_s): a run that lost less than
# this to the queue is still treated as having had its full budget.
QUEUE_TOLERANCE_S = 60

STAGE_TEXT = {
    "intake": "the submission intake checks",
    "compile": "compiling the kernels and the secret inputs",
    "build": "building the simulator (Verilator elaboration and C++ compile)",
    "simulation": "the model simulations",
    "robustness": "the robustness simulations",
    "synthesis": "synthesis and timing analysis",
}


def _num(raw):
    """A positive number of seconds from a form field, or None (absent, empty, junk, <= 0)."""
    if raw is None:
        return None
    try:
        v = float(str(raw).strip())
    except ValueError:
        return None
    return v if v > 0 else None


def fmt(seconds):
    s = int(round(max(0.0, float(seconds))))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"


class Budget:
    """The time one request may use, measured from its arrival at this service."""

    def __init__(self, time_limit_s=None, fair_limit_s=None, cap_s=2700, ceiling_s=None):
        self.arrived = time.monotonic()
        self.time_limit_s = _num(time_limit_s)
        # The gateway stops waiting at EDA_REQUEST_TIMEOUT_S whatever the budget says: a run must not
        # outlive the request that wants its answer.
        self.ceiling_s = _num(ceiling_s)
        self.over_ceiling = (self.time_limit_s is not None and self.ceiling_s is not None
                             and self.time_limit_s > self.ceiling_s)
        if self.over_ceiling:
            self.time_limit_s = self.ceiling_s
        fair = _num(fair_limit_s)
        if self.time_limit_s is not None and fair is None:
            fair = self.time_limit_s - QUEUE_TOLERANCE_S
        self.fair_limit_s = fair
        self.cap_s = float(cap_s)
        self.queued_s = 0.0
        self.run_s = None
        self.limited_by = None
        self.started = None

    def start(self):
        """Call when the job leaves the queue. Returns the seconds it may run, or None when what is
        left is too short to start (see not_started())."""
        now = time.monotonic()
        self.queued_s = now - self.arrived
        if self.time_limit_s is None:
            self.run_s, self.limited_by = self.cap_s, "eda_cap"
        else:
            self.run_s = self.time_limit_s - self.queued_s
            self.limited_by = "eda_request_ceiling" if self.over_ceiling else "batch_deadline"
        self.started = now
        return self.run_s if self.run_s >= MIN_RUN_S else None

    def used_s(self):
        return 0.0 if self.started is None else time.monotonic() - self.started

    def full_budget(self):
        """True when this run had the time every submission is entitled to."""
        if self.limited_by == "eda_cap":
            return True
        if self.limited_by == "eda_request_ceiling":      # this server's setting, shorter than the batch's
            return False
        return self.fair_limit_s is not None and self.run_s >= self.fair_limit_s - 1

    def facts(self):
        return {"limit_s": round(self.run_s or 0, 1), "used_s": round(self.used_s(), 1),
                "queued_s": round(self.queued_s, 1), "limited_by": self.limited_by,
                "time_limit_s": self.time_limit_s, "fair_limit_s": self.fair_limit_s}

    def timeout_error(self, service, stage):
        """The structured error for a run that hit its limit in `stage`."""
        miner = self.full_budget()
        what = STAGE_TEXT.get(stage, stage)
        if self.limited_by == "eda_cap":
            why = f"this EDA server's run-time limit (EVAL_TIMEOUT_S, {fmt(self.run_s)})"
        elif self.limited_by == "eda_request_ceiling":
            why = (f"this EDA server's request ceiling (EDA_REQUEST_TIMEOUT_S, {fmt(self.ceiling_s)}), which is "
                   "shorter than the batch's evaluation time: raise it on this EDA server")
        else:
            why = f"the batch's evaluation deadline ({fmt(self.run_s)} left when it started)"
        msg = (f"Timeout in the EDA server ({service}) during {what}: the evaluation ran for "
               f"{fmt(self.used_s())} and was stopped by {why}"
               + (f", after {fmt(self.queued_s)} queued" if self.queued_s >= 1 else "") + ". ")
        if miner:
            msg += "It had the full evaluation time of the batch, so the submission must finish within it."
        else:
            msg += (f"It had less than the full evaluation time ({fmt(self.fair_limit_s)}), so this is not "
                    "held against the submission: it will be evaluated again in a later batch.")
        return {"code": f"EDA_TIMEOUT_{stage.upper()}", "category": "timeout",
                "fault": "miner" if miner else "system", "retryable": not miner,
                "stage": stage, "where": f"eda_server/{service}", "message": msg, "timing": self.facts()}

    def not_started(self, service, stage):
        """The structured error for a job whose budget ran out in this service's queue."""
        return {"code": "EDA_QUEUE_EXHAUSTED", "category": "timeout", "fault": "system", "retryable": True,
                "stage": stage, "where": f"eda_server/{service}",
                "message": (f"Not started in the EDA server ({service}): it waited {fmt(self.queued_s)} in the "
                            f"queue and only {fmt(max(0.0, self.run_s or 0))} of the batch's evaluation time was "
                            "left. Not a property of the submission: it will be evaluated again in a later batch."),
                "timing": self.facts()}


# ------------------------------------------------------------------------- verilator run.py stages
# run.py reports its progress on stderr; the label of the line says which stage is running:
#   [compile] ...                  compile
#   [<label>] verilating the ...   build (any label: the scored build and the self-check builds)
#   [sim] / [sim-public] / [weights] simulation
#   [robust...]                    robustness
_LABEL = re.compile(r"^\[([A-Za-z0-9_-]+)\]\s*(.*)")


def stage_of(line, current):
    m = _LABEL.match(line.strip())
    if not m:
        return current
    label, rest = m.group(1).lower(), m.group(2).lower()
    if rest.startswith("verilating"):
        return "build"
    if label.startswith("compile"):
        return "compile"
    if label.startswith("robust"):
        return "robustness"
    if label.startswith("sim") or label == "weights":
        return "simulation"
    return current
