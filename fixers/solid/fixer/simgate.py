"""Simulator, test and compile wrappers, and the acceptance check.

Everything here is deterministic code: the model never decides whether a patch is good.
"""
from __future__ import annotations

import collections
import json
import py_compile
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

PY = sys.executable


@dataclass
class RunResult:
    """One sweep: a scenario (optionally with a fault file) over a set of seeds."""
    label: str
    seeds: list[int]
    failed: dict[int, list[str]] = field(default_factory=dict)   # seed -> invariant ids
    examples: dict[str, str] = field(default_factory=dict)       # invariant -> first detail
    messages: dict[int, int] = field(default_factory=dict)       # seed -> msg_send count
    wall_s: float = 0.0
    error: str | None = None                                     # the sweep itself broke

    @property
    def kinds(self) -> collections.Counter:
        c = collections.Counter()
        for invs in self.failed.values():
            c.update(invs)
        return c

    @property
    def broken(self) -> bool:
        """The scenario itself could not run (e.g. a bad fault path): every seed failed with
        nothing but a simulator ERROR. Service bugs show up as invariant/expect failures instead."""
        return bool(self.error) or (bool(self.seeds) and len(self.failed) == len(self.seeds)
                                    and all(set(v) == {"ERROR"} for v in self.failed.values()))

    def line(self) -> str:
        if self.error:
            return f"{self.label:<34} SWEEP ERROR: {self.error[:200]}"
        k = dict(self.kinds)
        return f"{self.label:<34} fail {len(self.failed):>3}/{len(self.seeds):<3} {k if k else ''}"


@dataclass
class Summary:
    runs: list[RunResult]

    @property
    def failing_pairs(self) -> int:
        return sum(len(r.failed) for r in self.runs) + sum(len(r.seeds) for r in self.runs if r.error)

    @property
    def violations(self) -> int:
        return sum(sum(r.kinds.values()) for r in self.runs)

    def by_label(self) -> dict[str, RunResult]:
        return {r.label: r for r in self.runs}

    def text(self, examples: int = 3) -> str:
        out = [r.line() for r in self.runs]
        shown = 0
        for r in self.runs:
            for inv, ex in r.examples.items():
                if shown >= examples:
                    break
                out.append(f"  e.g. [{r.label}] {inv}: {ex}")
                shown += 1
        return "\n".join(out)


@dataclass
class Verdict:
    ok: bool
    reasons: list[str]
    summary: Summary | None = None
    tests_tail: str = ""

    def text(self) -> str:
        head = "ACCEPTED" if self.ok else "REJECTED"
        body = "\n".join(f"- {r}" for r in self.reasons)
        s = f"\n{self.summary.text(6)}" if self.summary else ""
        t = f"\nTest output:\n{self.tests_tail}" if self.tests_tail else ""
        return f"{head}\n{body}{s}{t}"


class Gate:
    def __init__(self, ws: Path, procs: int = 2, log=print):
        self.ws = Path(ws)
        self.procs = procs
        self.log = log
        meta = json.loads((self.ws / "case.json").read_text())
        self.public_seeds = [int(s) for s in meta.get("public_seeds", [])] or [1, 2, 3]

    # ---------------------------------------------------------------- building blocks
    def fault_files(self) -> list[Path]:
        d = self.ws / "scenarios" / "faults"
        return sorted(d.glob("*.yaml")) if d.is_dir() else []

    def seeds(self, extra: int) -> list[int]:
        return self.public_seeds + [s for s in range(1000, 1000 + extra) if s not in self.public_seeds]

    def sweep(self, scenario: str, seeds: list[int], faults: Path | str | None = None,
              timeout: float = 300) -> RunResult:
        label = scenario if not faults else f"{scenario}+{Path(faults).name}"
        res = RunResult(label=label, seeds=list(seeds))
        fd, outp = tempfile.mkstemp(suffix=".json")
        cmd = [PY, "-m", "sim", "sweep", "--scenario", scenario, "--seeds", ",".join(map(str, seeds)),
               "--procs", str(self.procs), "--out", outp]
        if faults:
            cmd += ["--faults", str(faults)]
        t0 = time.time()
        try:
            p = subprocess.run(cmd, cwd=self.ws, capture_output=True, text=True, timeout=timeout)
            rows = json.loads(Path(outp).read_text() or "[]")
            if not rows:
                res.error = (p.stderr or p.stdout or "no output").strip()[-400:]
        except subprocess.TimeoutExpired:
            res.error, rows = f"timed out after {timeout:.0f}s", []
        except (json.JSONDecodeError, OSError) as e:
            res.error, rows = f"{type(e).__name__}: {e}", []
        finally:
            Path(outp).unlink(missing_ok=True)
        res.wall_s = time.time() - t0
        for r in rows:
            seed = int(r["seed"])
            res.messages[seed] = int(r.get("messages") or 0)
            if r.get("ok"):
                continue
            invs = []
            for v in r.get("violations") or []:
                invs.append(v["invariant"])
                res.examples.setdefault(v["invariant"], str(v.get("detail", ""))[:200])
            if r.get("error"):
                invs.append("ERROR")
                res.examples.setdefault("ERROR", str(r["error"])[:300])
            res.failed[seed] = invs or ["unknown"]
        return res

    def sweep_all(self, seeds: list[int], extra_scenarios: list[str] = ()) -> Summary:
        runs = [self.sweep("public", seeds)]
        runs += [self.sweep("public", seeds, f) for f in self.fault_files()]
        runs += [self.sweep(name, seeds) for name in extra_scenarios]
        return Summary(runs)

    def run_tests(self, timeout: float = 300) -> tuple[bool, str]:
        try:
            p = subprocess.run([PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "tests"],
                               cwd=self.ws, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "pytest timed out"
        tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-25:])
        return p.returncode == 0, tail

    def determinism(self, seeds: list[int]) -> tuple[bool, str]:
        p = subprocess.run([PY, "-m", "sim", "determinism", "--scenario", "public",
                            "--seeds", ",".join(map(str, seeds)), "--runs", "2"],
                           cwd=self.ws, capture_output=True, text=True, timeout=300)
        bad = [ln for ln in p.stdout.splitlines() if '"ok": false' in ln]
        return (p.returncode == 0 and not bad), "\n".join(bad)[:500]

    def compile_errors(self) -> list[str]:
        errs = []
        for f in sorted((self.ws / "src").rglob("*.py")):
            try:
                py_compile.compile(str(f), doraise=True, cfile=tempfile.gettempdir() + "/_c3_compile.pyc")
            except py_compile.PyCompileError as e:
                errs.append(str(e.msg)[:300])
        return errs

    # ---------------------------------------------------------------- acceptance check
    def check(self, baseline: Summary, seeds: list[int], progress_ref: Summary | None = None,
              extra_scenarios: list[str] = (), require_progress: bool = True) -> Verdict:
        """Is the current src/ safe to keep, and better than the baseline?

        Rejects: compile errors, failing tests, nondeterminism, a run that was clean at
        baseline now failing, simulator crashes, or message counts over 2x baseline.
        Accepts if nothing got worse and (when require_progress) fewer (run, seed) pairs fail.
        New failure kinds inside runs that were already failing are allowed but reported:
        fixing one bug can expose another (chained cases).
        """
        reasons: list[str] = []
        errs = self.compile_errors()
        if errs:
            return Verdict(False, [f"does not compile: {e}" for e in errs])
        ok, tail = self.run_tests()
        if not ok:
            return Verdict(False, ["public tests fail (a patch that breaks tests scores 0)"], tests_tail=tail)
        cand = self.sweep_all(seeds, extra_scenarios)
        base = baseline.by_label()
        bad = False
        for r in cand.runs:
            b = base.get(r.label)
            if r.error:
                reasons.append(f"{r.label}: the simulator could not run: {r.error[:200]}")
                bad = True
                continue
            if "ERROR" in r.kinds:
                reasons.append(f"{r.label}: unhandled exception: {r.examples.get('ERROR', '')}")
                bad = True
            if b is None:
                continue
            common = [s for s in r.seeds if s in b.seeds]
            newly = [s for s in common if s in r.failed and s not in b.failed]
            if newly and not b.failed:
                reasons.append(f"{r.label}: was clean at baseline, now fails on seeds {newly[:5]}: "
                               f"{dict(r.kinds)}")
                bad = True
            elif newly:
                reasons.append(f"note: {r.label} now fails on seeds {newly[:5]} that passed before "
                               f"(possibly a bug the old one was hiding): {dict(r.kinds)}")
            new_kinds = set(r.kinds) - set(b.kinds)
            if new_kinds and b.failed:
                reasons.append(f"note: {r.label} shows new failure kinds {sorted(new_kinds)}")
            for s in common:
                if b.messages.get(s) and r.messages.get(s, 0) > 2 * b.messages[s]:
                    reasons.append(f"{r.label} seed {s}: {r.messages[s]} messages vs {b.messages[s]} "
                                   f"at baseline (graders fail runs over 3x)")
                    bad = True
                    break
        det_ok, det = self.determinism(seeds[:2])
        if not det_ok:
            reasons.append(f"replays are not deterministic: {det}")
            bad = True
        ref = progress_ref or baseline
        ref_pairs, cand_pairs = _pairs(ref, cand), _pairs(cand, ref)
        progress = cand_pairs < ref_pairs or (
            cand_pairs == ref_pairs and _viol(cand, ref) < _viol(ref, cand))
        if require_progress and not progress and not bad:
            reasons.append(f"no progress: {cand_pairs} failing (run, seed) pairs vs {ref_pairs} "
                           f"at the last accepted state")
            bad = True
        reasons.insert(0, f"failing (run, seed) pairs: {baseline.failing_pairs} at baseline -> "
                          f"{cand.failing_pairs} now")
        return Verdict(not bad, reasons, cand)


def _pairs(a: Summary, other: Summary) -> int:
    """Failing (run, seed) pairs in `a`, counting only runs that `other` also has."""
    labels = {r.label for r in other.runs}
    return sum(len(r.failed) + (len(r.seeds) if r.error else 0) for r in a.runs if r.label in labels)


def _viol(a: Summary, other: Summary) -> int:
    labels = {r.label for r in other.runs}
    return sum(sum(r.kinds.values()) for r in a.runs if r.label in labels)


def quick_check(gate: "Gate", baseline: Summary, ref: Summary | None = None,
                extra_scenarios: list[str] = ()) -> Verdict:
    """A short check for when the full one no longer fits in the time left: compiles, public tests
    pass (a broken test zeroes the case), replays are deterministic, and on the public scenario plus
    every baselined agent scenario (public seeds only) no seed newly fails, nothing crashes, messages
    stay bounded, and fewer (run, seed) pairs fail than in `ref` (the last accepted state, or the
    original baseline). Agent scenarios matter: once public passes, a later bug's progress may only
    show there."""
    errs = gate.compile_errors()
    if errs:
        return Verdict(False, [f"does not compile: {e}" for e in errs])
    ok, tail = gate.run_tests()
    if not ok:
        return Verdict(False, ["public tests fail"], tests_tail=tail)
    det_ok, det = gate.determinism(gate.public_seeds[:1])
    if not det_ok:
        return Verdict(False, [f"replays are not deterministic: {det}"])
    base_runs = baseline.by_label()
    ref_runs = (ref or baseline).by_label()
    if "public" not in base_runs:
        return Verdict(False, ["no public baseline to compare against"])
    labels = ["public"] + [n for n in extra_scenarios if n in base_runs and n != "public"]
    before = after = 0
    cands = []
    for label in labels:
        base = base_runs[label]
        seeds = [s for s in gate.public_seeds if s in base.seeds]
        cand = gate.sweep(label, seeds)
        if cand.broken or "ERROR" in cand.kinds:
            return Verdict(False, [f"{label}: errors: {cand.error or cand.examples.get('ERROR', '')}"])
        newly = [s for s in seeds if s in cand.failed and s not in base.failed]
        if newly:
            return Verdict(False, [f"{label} now fails on seeds {newly} that passed before"])
        for s in seeds:
            if base.messages.get(s) and cand.messages.get(s, 0) > 2 * base.messages[s]:
                return Verdict(False, [f"{label} seed {s}: {cand.messages[s]} messages vs "
                                       f"{base.messages[s]} at baseline"])
        r = ref_runs.get(label)
        if r is None or not set(seeds) <= set(r.seeds):
            r = base
        before += sum(1 for s in seeds if s in r.failed)
        after += len(cand.failed)
        cands.append(cand)
    if after >= before:
        return Verdict(False, [f"no progress: {after} failing (run, seed) pairs vs {before} "
                               f"on {', '.join(labels)}"])
    return Verdict(True, [f"quick check: failing (run, seed) pairs {before} -> {after} on "
                          f"{', '.join(labels)}"], Summary(cands))
