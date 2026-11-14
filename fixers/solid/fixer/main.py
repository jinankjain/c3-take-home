"""Orchestrator: owns the clock, the budget, the baseline and the last accepted src/.

Phases: bootstrap (baseline sweep, no model) -> agent loop with checkpoints -> verification
rounds after done() -> finalize (src/ is always left at the last accepted state).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path

from . import context
from .agent import Agent
from .llm import make_llm
from .simgate import Gate, Summary, quick_check
from .tools import Tools

MAX_EXTRA_SEEDS = 15           # seeds beyond the public ones in baseline and checkpoints...
MIN_EXTRA_SEEDS = 5            # ...sized so that one full sweep takes about TARGET_SWEEP_S
TARGET_SWEEP_S = 40.0
VERIFY_SEED_BASE = 2000        # verification uses as many unseen seeds as a checkpoint sweep
STOP_CLEAN_S = 45              # stop new turns this close to the end when everything is accepted
STOP_DIRTY_S = 80              # ... or with unchecked edits (time for the quick check)
EARLY_SAVE_TURNS = 3           # unchecked edits this many turns old (or this old in seconds) get a
EARLY_SAVE_S = 150             # quick check, and a full checkpoint if it shows progress
MAX_VERIFY_ROUNDS = 2
REVIEW_MAX_TURNS = 6           # the coverage review is a sanity pass, not a second investigation
REVIEW_MAX_S = 150
BUDGET_STOP_FRACTION = 0.85
COVERAGE_REVIEW = (
    "Before finishing, check ticket coverage. Reply with one line per item or symptom in SYMPTOM.md: "
    "item -> kind -> evidence.\n"
    "- kind 'liveness/correctness' (stuck, never finishes, wrong or missing result, invariant "
    "violation): evidence is a run that failed on the original code and passes now; it is fine if "
    "other bugs also made that run fail.\n"
    "- kind 'unwanted event' (something happens that should not: re-queued, cancelled, retried, "
    "registered again, too many X): a run that failed for other reasons does NOT count. Evidence must "
    "be a scenario under the conditions the ticket describes whose expect block counts that event "
    "(rpc_requests_max for an RPC, log_events_max for a log event; usually max 0), that fails on the "
    "original code and passes now. If you have none, write_scenario now, fix until it passes, and "
    "checkpoint.\n"
    "When every item has evidence, call done() again. Do not spend turns isolating checks for items "
    "that already have evidence.")
FINAL_RESERVE_S = 20           # time kept back to restore src/ and exit
QUICK_CHECK_MIN_S = 45         # time needed for the short end-of-run check (tests + public seeds)
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
T0 = time.time()


def choose_extra_seeds(per_seed_s: float, runs: int, n_public: int, ok: bool = True) -> int:
    """How many seeds beyond the public ones fit a full sweep into TARGET_SWEEP_S."""
    if not ok or per_seed_s <= 0:
        return MAX_EXTRA_SEEDS
    total = int(TARGET_SWEEP_S / (per_seed_s * max(1, runs)))
    return max(MIN_EXTRA_SEEDS, min(MAX_EXTRA_SEEDS, total - n_public))


def log(msg: str):
    print(f"[{time.time() - T0:6.1f}s] {msg}", flush=True)


def tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}


class Orchestrator:
    def __init__(self, ws: Path, corpus: Path):
        self.ws, self.corpus = ws, corpus
        self.src = ws / "src"
        self.deadline = float(os.environ.get("C3_DEADLINE") or (time.time() + 900))
        self.budget = float(os.environ.get("C3_BUDGET_USD") or 3.0)
        self.gate = Gate(ws, procs=2, log=log)
        self.tmp = Path(tempfile.mkdtemp(prefix="c3fixer-"))
        self.last_good = self.tmp / "last_good"
        shutil.copytree(self.src, self.last_good, ignore=IGNORE)
        # A pristine copy of the case, to baseline any scenario the agent writes against the
        # original code.
        self.pristine = self.tmp / "pristine"
        shutil.copytree(ws, self.pristine, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "logs"))
        self.pristine_gate = Gate(self.pristine, procs=2, log=log)
        self.orig_scenarios = {p.name for p in (ws / "scenarios").glob("*.yaml")}
        self.scenario_hash: dict[str, bytes] = {}      # agent scenario -> content it was baselined with
        self.coverage_reviewed = False
        self._safety_state: str | None = None           # src/ state the safety checkpoint last saw
        self._dirty_since: float | None = None           # when src/ last got unchecked edits...
        self._dirty_turns = 0                            # ...and how many agent turns ago
        self.open_bug: str | None = None                 # a scenario that reproduces and still fails
        self.seeds = self.gate.seeds(MAX_EXTRA_SEEDS)     # resized in bootstrap() to the machine
        self.baseline: Summary | None = None
        self.accepted: Summary | None = None
        self.checkpoints = 0
        self.accepted_count = 0
        self.check_cost_s = 60.0
        self.llm = None

    # ------------------------------------------------------------------ clock and budget
    def time_left(self) -> float:
        return self.deadline - time.time()

    def spent(self) -> float:
        return self.llm.spent if self.llm else 0.0

    def should_stop(self) -> str | None:
        # With everything accepted only an exit is left to do; with unchecked edits, the end-of-run
        # quick check (the safety checkpoint has usually run a full check earlier).
        if self.time_left() < (STOP_DIRTY_S if self.src_dirty() else STOP_CLEAN_S):
            return f"{self.time_left():.0f}s left"
        if self.spent() > BUDGET_STOP_FRACTION * self.budget:
            return f"spent ${self.spent():.2f} of ${self.budget:.2f}"
        return None

    def status_line(self) -> str:
        return (f"[status: {self.time_left():.0f}s left, ${self.budget - self.spent():.2f} budget left, "
                f"checkpoint takes ~{self.check_cost_s:.0f}s, {self.accepted_count} accepted so far, "
                f"src/ {'differs from' if self.src_dirty() else 'matches'} the last accepted state]")

    # ------------------------------------------------------------------ src state
    def src_dirty(self) -> bool:
        return tree(self.src) != tree(self.last_good)

    def snapshot(self):
        new = self.tmp / "last_good.new"
        shutil.rmtree(new, ignore_errors=True)
        shutil.copytree(self.src, new, ignore=IGNORE)
        shutil.rmtree(self.last_good)
        new.rename(self.last_good)

    def restore_last_good(self):
        staged = self.ws / ".src.restore"
        shutil.rmtree(staged, ignore_errors=True)
        shutil.copytree(self.last_good, staged)
        old = self.ws / ".src.old"
        shutil.rmtree(old, ignore_errors=True)
        self.src.rename(old)
        staged.rename(self.src)
        shutil.rmtree(old, ignore_errors=True)

    def src_hash(self) -> str:
        h = hashlib.sha256()
        for k, v in sorted(tree(self.src).items()):
            h.update(k.encode() + b"\0" + v)
        return h.hexdigest()

    def maybe_safety_checkpoint(self) -> str | None:
        """When time is getting short and src/ holds unchecked edits, check them now so that a
        partial fix is kept even if the agent never gets back to checkpoint(). Returns a note for
        the agent, or None."""
        if not self.src_dirty():
            self._dirty_since, self._dirty_turns = None, 0
            return None
        if self._dirty_since is None:
            self._dirty_since = time.time()
        self._dirty_turns += 1
        left = self.time_left()
        state = self.src_hash()
        if state == self._safety_state or left < self.check_cost_s + FINAL_RESERVE_S:
            return None                                  # already tried this exact state / no time
        late = left <= 2 * self.check_cost_s + FINAL_RESERVE_S + 30
        stale = (self._dirty_turns > EARLY_SAVE_TURNS
                 or time.time() - self._dirty_since >= EARLY_SAVE_S)
        if not (late or stale):
            return None
        self._safety_state = state
        if not late:
            # Early: only spend a full check if a quick one shows the edits are progress.
            baselined = [n for n in self.scenario_hash if n in {r.label for r in self.baseline.runs}]
            q = quick_check(self.gate, self.baseline, self.accepted, baselined)
            log(f"early save: unchecked edits for {self._dirty_turns - 1} turns; quick check "
                f"{'shows progress' if q.ok else 'no progress'}: " + "; ".join(q.reasons)[:300])
            if not q.ok:
                return None
        log(f"safety checkpoint: {left:.0f}s left and src/ has unchecked edits")
        res = self.checkpoint()
        if not self.src_dirty():
            self._dirty_since, self._dirty_turns = None, 0
        why = "Time is getting short" if late else "Your edits had gone unchecked for a while"
        return (f"[orchestrator] {why}, so I ran checkpoint() on your current edits to keep any "
                f"progress:\n" + res)

    def save_or_restore(self, why: str):
        """End of a phase with unchecked edits: keep them if a check passes, else restore."""
        if not self.src_dirty():
            return
        if self.time_left() > self.check_cost_s + FINAL_RESERVE_S:
            log(f"{why}: src/ has unchecked edits; running the acceptance check")
            self.checkpoint()
        elif self.time_left() > QUICK_CHECK_MIN_S:
            log(f"{why}: not enough time for the full check ({self.time_left():.0f}s left); running the quick check")
            baselined = [n for n in self.scenario_hash if n in {r.label for r in self.baseline.runs}]
            v = quick_check(self.gate, self.baseline, self.accepted, baselined)
            log(f"{why}: quick check {'PASSED' if v.ok else 'FAILED'}: " + "; ".join(v.reasons)[:400])
            if v.ok:
                self.snapshot()
                self.accepted = v.summary
                self.accepted_count += 1
        if self.src_dirty():
            log(f"{why}: restoring the last accepted src/")
            self.restore_last_good()

    # ------------------------------------------------------------------ agent scenarios
    def agent_scenarios(self) -> list[str]:
        """Scenarios the agent wrote that also run on the original code (baselined lazily)."""
        names = []
        for p in sorted((self.ws / "scenarios").glob("*.yaml")):
            if p.name in self.orig_scenarios:
                continue
            name = p.stem
            content = p.read_bytes()
            if self.scenario_hash.get(name) == content and name not in {r.label for r in self.baseline.runs}:
                continue                                       # known broken, unchanged: still excluded
            if self.scenario_hash.get(name) != content:       # new, or rewritten by the agent
                for summ in (self.baseline, self.accepted):
                    if summ is not None:
                        summ.runs = [r for r in summ.runs if r.label != name]
                self.scenario_hash[name] = content
                shutil.copy(p, self.pristine / "scenarios" / p.name)
                r = self.pristine_gate.sweep(name, self.seeds)
                if r.broken:
                    log(f"agent scenario {name} cannot run (broken scenario); left out of checkpoints")
                    continue
                self.baseline.runs.append(r)
                if self.accepted is not None:       # the accepted code was never run on it: assume
                    self.accepted.runs.append(r)    # it fails like the original (conservative)
                log(f"baselined agent scenario: {r.line()}")
            names.append(name)
        return names

    def note_open_bug(self, name: str):
        """An agent scenario reproduces on the original code and still fails on the current code:
        there is real work left, so a phase cap (the coverage review) must not cut it off."""
        if self.open_bug is None:
            log(f"open bug found: scenario {name} fails on the original and the current code")
        self.open_bug = name

    def probe_scenario(self, name: str, seeds: list[int]):
        """Run a freshly written scenario on the original code and on the current code."""
        shutil.copy(self.ws / "scenarios" / f"{name}.yaml", self.pristine / "scenarios" / f"{name}.yaml")
        return self.pristine_gate.sweep(name, seeds), self.gate.sweep(name, seeds)

    # ------------------------------------------------------------------ the acceptance check
    def checkpoint(self) -> str:
        self.checkpoints += 1
        if self.time_left() < self.check_cost_s + FINAL_RESERVE_S:
            return "REJECTED: not enough time left to run the acceptance check"
        if not self.src_dirty():
            return "nothing to check: src/ matches the last accepted state"
        t0 = time.time()
        extra = self.agent_scenarios()
        v = self.gate.check(self.baseline, self.seeds, progress_ref=self.accepted, extra_scenarios=extra)
        if v.summary is not None:              # only full checks say how long a check takes
            self.check_cost_s = max(self.check_cost_s * 0.5, time.time() - t0)
        log(f"checkpoint #{self.checkpoints}: {'ACCEPTED' if v.ok else 'REJECTED'} in {time.time() - t0:.0f}s; "
            + "; ".join(v.reasons)[:600])
        if v.ok:
            self.snapshot()
            self.accepted = v.summary
            self.accepted_count += 1
            ob = v.summary.by_label().get(self.open_bug) if self.open_bug else None
            if ob is not None and not ob.failed:
                log(f"open bug {self.open_bug} now passes")
                self.open_bug = None
            left = [r for r in v.summary.runs if r.failed]
            tail = ("\nStill failing; keep fixing:\n" + "\n".join(r.line() for r in left)) if left else \
                "\nAll checked runs pass. Consider whether the ticket describes anything not yet covered."
            return v.text() + tail
        return v.text() + "\nsrc/ still contains the rejected edits; fix them or call revert()."

    # ------------------------------------------------------------------ phases
    def bootstrap(self) -> str:
        ok, tail = self.gate.run_tests()
        log(f"public tests on original code: {'pass' if ok else 'FAIL'}")
        if not ok:
            log(tail)
        self.seeds = self.gate.seeds(self.size_sweeps())
        t0 = time.time()
        self.baseline = self.gate.sweep_all(self.seeds)
        self.check_cost_s = (time.time() - t0) * 1.3 + 10
        log(f"baseline in {time.time() - t0:.0f}s ({len(self.seeds)} seeds per run):\n{self.baseline.text(8)}")
        return self.baseline.text(12)

    def size_sweeps(self) -> int:
        """Extra seeds per run so that one full sweep (public + every fault file) takes about
        TARGET_SWEEP_S on this machine: thorough on a fast one, more checkpoints on a slow one."""
        t0 = time.time()
        probe = self.gate.sweep("public", self.gate.public_seeds)
        per_seed = (time.time() - t0) / max(1, len(self.gate.public_seeds))
        runs = 1 + len(self.gate.fault_files())
        extra = choose_extra_seeds(per_seed, runs, len(self.gate.public_seeds), probe.error is None)
        log(f"sweep sizing: {per_seed:.2f}s per seed x {runs} runs -> {extra} extra seeds")
        return extra

    def verify(self) -> Summary:
        """A sweep on as many seeds as a checkpoint uses, none of which the agent has seen."""
        extra = self.agent_scenarios()
        n = len(self.seeds)
        return self.gate.sweep_all(list(range(VERIFY_SEED_BASE, VERIFY_SEED_BASE + n)), extra)

    def finalize(self, tools: Tools | None):
        self.save_or_restore("finalize")
        if tools:
            tools.cleanup()
        for p in (self.ws / "scenarios").glob("*.yaml"):     # leave the workspace as we found it
            if p.name not in self.orig_scenarios:
                p.unlink(missing_ok=True)
        log(f"finished: {self.accepted_count} accepted checkpoint(s), spent ${self.spent():.3f}, "
            f"{time.time() - T0:.0f}s")


def main() -> int:
    ws = Path(os.environ.get("C3_WORKSPACE") or os.getcwd()).resolve()
    corpus = Path(os.environ.get("C3_CORPUS") or "/corpus").resolve()
    orch = Orchestrator(ws, corpus)
    tools = None

    def on_term(signum, frame):
        log(f"signal {signum}: restoring the last accepted src/ and exiting")
        if orch.src_dirty():
            orch.restore_last_good()
        os._exit(0)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    try:
        log(f"case {ws.name}: {orch.time_left():.0f}s, ${orch.budget:.2f}")
        baseline_text = orch.bootstrap()
        ctx, notes = context.build(ws, corpus, baseline_text)
        log(f"context: {len(ctx)} chars (~{len(ctx) // 4} tokens); {len(notes)} item(s) not inlined")
        orch.llm = make_llm(log=log)
        tools = Tools(ws, corpus, orch.gate, orch, log=log)
        agent = Agent(orch.llm, tools, orch, context.INSTRUCTIONS, ctx, log=log)
        msg = ("Begin. FIRST, before editing any code: list each item/symptom in SYMPTOM.md and the "
               "baseline run that fails because of it. For any item with no failing run, write_scenario "
               "(base: public) with faults matching the ticket and an expect check that counts the "
               "symptom, until it fails on the original code. Then diagnose and fix everything. "
               + orch.status_line())
        verifies = 0
        limits: dict = {}
        for _ in range(MAX_VERIFY_ROUNDS + 4):        # bounded: also re-entered for checkpoint/coverage
            why = agent.run(msg, **limits)
            limits = {}
            if why == "limit":                         # coverage review ran long: keep what is accepted
                log("coverage review hit its cap; moving on to verification")
                orch.save_or_restore("coverage review cap")
                why = "done"
            if why != "done" or orch.should_stop():
                break
            if orch.src_dirty():
                msg = ("You called done() but src/ has edits that were never accepted by checkpoint(). "
                       "Call checkpoint() (or revert()), then done(). " + orch.status_line())
                tools.done_summary = None
                continue
            if not orch.coverage_reviewed:
                orch.coverage_reviewed = True
                tools.done_summary = None
                msg = (COVERAGE_REVIEW + "\n" + orch.status_line())
                limits = {"max_turns": REVIEW_MAX_TURNS, "max_seconds": REVIEW_MAX_S}
                continue
            if verifies == MAX_VERIFY_ROUNDS:
                break
            if orch.time_left() < orch.check_cost_s + FINAL_RESERVE_S:
                log("no time left for verification on unseen seeds; finishing")
                break
            verifies += 1
            v = orch.verify()
            log(f"verify round {verifies} on unseen seeds:\n{v.text(6)}")
            if v.failing_pairs == 0:
                break
            tools.done_summary = None
            msg = ("Verification on seeds you have not seen still finds failures. Some may be another "
                   "bug that was hidden behind the one you fixed. Investigate and fix:\n" + v.text(8)
                   + "\n" + orch.status_line())
    except Exception as e:  # noqa: BLE001
        import traceback
        log(f"fatal: {type(e).__name__}: {e}\n{traceback.format_exc()}")
    finally:
        orch.finalize(tools)
    return 0


if __name__ == "__main__":
    sys.exit(main())
