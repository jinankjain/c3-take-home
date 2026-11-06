"""Run simulator checks and tests against one explicit source snapshot."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import uuid

from .evidence import summarize_trace
from .runtime import CommandResult, ProcessRunner, TimeLimitError


def _source_hash(src: Path) -> str:
    # Imported lazily so evidence utilities do not depend on staging machinery.
    from .patches import source_hash
    return source_hash(src)


def _case_env(root: Path) -> dict[str, str]:
    return {
        "C3_WORKSPACE": str(root), "C3SIM_ROOT": str(root),
        "PYTHONPATH": os.pathsep.join(str(root / part) for part in ("src", "sim", "")),
        "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1",
    }


def _scenario_path(root: Path, scenario: str) -> Path | None:
    supplied = Path(scenario)
    direct = supplied if supplied.is_absolute() else root / supplied
    if direct.is_file():
        return direct.resolve()
    for suffix in (".yaml", ".yml", ".json", ""):
        candidate = root / "scenarios" / (scenario + suffix)
        if candidate.is_file():
            return candidate.resolve()
    return None


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _scenario_hashes(root: Path, scenario: str, faults: str | None) -> tuple[str | None, str | None]:
    """Include an externally referenced effective fault file in scenario identity."""
    path = _scenario_path(root, scenario)
    if path is None:
        return None, None
    try:
        scenario_digest = _file_hash(path)
        document = None
        if path.stat().st_size <= 2 * 1024 * 1024:
            import yaml
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        # c3sim's loader supplies the filename stem when name is absent. Its
        # value is effective input, so unnamed copies must not share a cache
        # identity merely because their bytes match. For oversized inputs,
        # conservatively bind the stem without parsing an unbounded document.
        if document is None or isinstance(document, dict) and "name" not in document:
            scenario_digest = hashlib.sha256(
                ("scenario-default-name-v1\0" + scenario_digest + "\0" + path.stem).encode()
            ).hexdigest()
        fault_path: Path | None = None
        if faults is not None:
            supplied = Path(faults)
            fault_path = supplied if supplied.is_absolute() else root / supplied
        else:
            if isinstance(document, dict) and isinstance(document.get("faults"), str):
                fault_path = path.parent / document["faults"]
        fault_digest = _file_hash(fault_path) if fault_path is not None and fault_path.is_file() else None
        if fault_path is not None and fault_digest is None:
            # Distinguish a missing override from the unmodified embedded faults.
            effective_faults = "missing:" + str(fault_path)
        else:
            effective_faults = fault_digest or "embedded"
        combined = hashlib.sha256(
            ("scenario-v1\0" + scenario_digest + "\0" + effective_faults).encode()
        ).hexdigest()
        return combined, fault_digest
    except (OSError, ValueError, ImportError, RecursionError):
        # The replay remains the authoritative configuration check.
        return None, None
    except Exception as error:
        # PyYAML's parser errors are not ValueErrors. Do not turn a malformed
        # scenario into a controller exception before the CLI can report it.
        if error.__class__.__module__.startswith("yaml"):
            return None, None
        raise


def _read_summary(path: Path) -> tuple[dict, str | None]:
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            return {}, "summary exceeds 8 MiB"
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return {}, "summary must be an object"
        return value, None
    except (OSError, ValueError, RecursionError) as error:
        return {}, f"{type(error).__name__}: {str(error)[:300]}"


def _check_payload(stdout: str) -> dict | None:
    try:
        value = json.loads(stdout)
    except (ValueError, RecursionError):
        # Harmless diagnostics may precede the CLI JSON.  Accept only a complete
        # trailing object with the checker fields, never an embedded log payload.
        value = None
        decoder = json.JSONDecoder()
        attempts = 0
        for offset, char in enumerate(stdout):
            if char != "{" or attempts >= 64:
                continue
            attempts += 1
            try:
                candidate, end = decoder.raw_decode(stdout, offset)
            except (ValueError, RecursionError):
                continue
            if stdout[end:].strip() == "" and isinstance(candidate, dict):
                value = candidate
                break
    if not isinstance(value, dict):
        return None
    violations = value.get("invariants")
    count = value.get("violations")
    if not isinstance(violations, list) or not isinstance(count, int) or isinstance(count, bool):
        return None
    if count != len(violations) or any(
        not isinstance(v, dict) or not isinstance(v.get("invariant"), str)
        or not isinstance(v.get("detail"), str)
        or (v.get("t") is not None and (not isinstance(v["t"], (int, float)) or isinstance(v["t"], bool) or not math.isfinite(v["t"])))
        for v in violations
    ):
        return None
    return value


@dataclass(frozen=True)
class ReplayResult:
    source_hash: str
    scenario: str
    seed: int
    faults: str | None
    run_id: str
    out_dir: Path
    replay_command: CommandResult
    check_command: CommandResult | None
    violations: list[dict]
    summary: dict
    status: str
    scenario_hash: str | None = None
    faults_hash: str | None = None
    evidence: dict | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "pass"

    @property
    def trace_path(self) -> Path:
        return self.out_dir / "trace.jsonl"

    def as_dict(self, compact: bool = True) -> dict:
        identity = {
            "run_id": self.run_id, "source_hash": self.source_hash,
            "scenario": self.scenario, "scenario_hash": self.scenario_hash,
            "seed": self.seed, "faults": self.faults, "faults_hash": self.faults_hash,
            "out_dir": str(self.out_dir), "trace": str(self.trace_path),
        }
        summary = self.summary
        if compact:
            summary = {
                key: self.summary[key] for key in (
                    "scenario", "seed", "outcomes", "all_resolved", "messages_sent",
                    "event_counts", "trace_sha256", "error", "final_t",
                ) if key in self.summary
            }
        return {
            **identity, "ok": self.ok, "status": self.status, "error": self.error,
            "replay": self.replay_command.as_dict(max_chars=2000 if compact else 12000),
            "check": None if self.check_command is None else self.check_command.as_dict(max_chars=2000 if compact else 12000),
            "violations": self.violations[:20] if compact else self.violations,
            "violation_count": len(self.violations), "summary": summary,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class DeterminismResult:
    source_hash: str
    scenario: str
    seed: int
    faults: str | None
    runs: tuple[ReplayResult, ...]
    status: str

    @property
    def ok(self) -> bool:
        return self.status == "pass"

    def as_dict(self) -> dict:
        return {
            "source_hash": self.source_hash, "scenario": self.scenario, "seed": self.seed,
            "faults": self.faults, "status": self.status, "ok": self.ok,
            "hashes": [result.summary.get("trace_sha256") for result in self.runs],
            "runs": [{"run_id": r.run_id, "status": r.status, "source_hash": r.source_hash}
                     for r in self.runs],
        }


class Simulator:
    def __init__(self, runner: ProcessRunner, scratch: Path):
        self.runner = runner
        self.scratch = Path(scratch).resolve()
        self.scratch.mkdir(parents=True, exist_ok=True)

    def replay(
        self, case_root: Path, scenario: str = "public", *, seed: int,
        faults: str | None = None, timeout: float = 60, reserve: float = 0,
    ) -> ReplayResult:
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise ValueError("seed must be an integer")
        if not isinstance(scenario, str) or not scenario or "\x00" in scenario:
            raise ValueError("scenario must be a nonempty path or name")
        if faults is not None and (not isinstance(faults, str) or not faults or "\x00" in faults):
            raise ValueError("faults must be a nonempty path")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        root = Path(case_root).resolve(strict=True)
        before = _source_hash(root / "src")
        scenario_hash, faults_hash = _scenario_hashes(root, scenario, faults)
        run_id = uuid.uuid4().hex[:16]
        out_dir = self.scratch / run_id
        out_dir.mkdir()
        end = time.monotonic() + timeout
        command = [sys.executable, "-m", "sim", "replay", "--scenario", scenario,
                   "--seed", str(seed), "--out", str(out_dir)]
        if faults is not None:
            command.extend(["--faults", faults])
        replay = self.runner.run(command, cwd=root, timeout=timeout, reserve=reserve, env=_case_env(root))
        trace = out_dir / "trace.jsonl"
        check: CommandResult | None = None
        check_error = None
        if trace.is_file() and not replay.cancelled and not replay.timed_out:
            remaining = end - time.monotonic()
            if remaining <= 0:
                check_error = "replay exhausted the validation timeout"
            else:
                try:
                    check = self.runner.run(
                        [sys.executable, "-m", "sim", "check", "--trace", str(trace)],
                        cwd=root, timeout=remaining, reserve=reserve, env=_case_env(root),
                    )
                except TimeLimitError as error:
                    check_error = str(error)
        summary, summary_error = _read_summary(out_dir / "summary.json")
        payload = None if check is None or check.truncated else _check_payload(check.stdout)
        violations = [] if payload is None else payload["invariants"]
        after = _source_hash(root / "src")
        after_scenario, after_faults = _scenario_hashes(root, scenario, faults)
        trace_digest = _file_hash(trace) if trace.is_file() else None
        error: str | None = None

        if before != after or (scenario_hash, faults_hash) != (after_scenario, after_faults):
            status, error = "stale_source", "source or scenario changed during validation"
        elif replay.cancelled or (check is not None and check.cancelled):
            status = "cancelled"
        elif replay.timed_out or (check is not None and check.timed_out) or check_error is not None:
            status, error = "timeout", check_error
        elif replay.returncode == 2:
            status = "config_error"
        elif replay.returncode == 3 or summary.get("error") is not None:
            status, error = "runtime_error", summary.get("error")
        elif not replay.ok:
            status = "replay_error"
        elif not trace.is_file() or summary_error is not None:
            status, error = "evidence_error", summary_error or "replay produced no trace"
        elif check is None or payload is None:
            status, error = "check_error", "checker produced no complete valid JSON verdict"
        elif check.returncode not in (0, 1) or (check.returncode == 0) != (not violations):
            status, error = "check_error", "checker exit code disagrees with its verdict"
        elif scenario_hash is None:
            status, error = "evidence_error", "scenario identity could not be established"
        elif summary.get("trace_sha256") != trace_digest:
            status, error = "evidence_error", "summary trace hash does not match replay output"
        elif violations:
            status = "invariant_failure"
        elif not check.ok:
            status, error = "check_error", "checker failed without reporting violations"
        elif summary.get("all_resolved") is not True:
            status, error = "unresolved", "summary does not certify all client actions resolved"
        else:
            status = "pass"

        summary = dict(summary, source_hash=before, scenario_hash=scenario_hash, faults_hash=faults_hash)
        evidence = summarize_trace(
            trace, violations, summary,
            should_stop=lambda: self.runner.deadline.stop_event.is_set()
            or self.runner.deadline.remaining() <= reserve,
        ) if trace.is_file() else None
        return ReplayResult(
            source_hash=before, scenario=scenario, seed=seed, faults=faults,
            run_id=run_id, out_dir=out_dir, replay_command=replay, check_command=check,
            violations=violations, summary=summary, status=status,
            scenario_hash=scenario_hash, faults_hash=faults_hash, evidence=evidence, error=error,
        )

    def tests(self, case_root: Path, *, timeout: float = 60, reserve: float = 0) -> CommandResult:
        root = Path(case_root).resolve(strict=True)
        return self.runner.run(
            [sys.executable, "-m", "pytest", "-q", "tests"], cwd=root,
            timeout=timeout, reserve=reserve, env=_case_env(root),
        )

    def determinism(
        self, case_root: Path, scenario: str = "public", *, seed: int,
        faults: str | None = None, runs: int = 2, timeout: float = 60, reserve: float = 0,
    ) -> DeterminismResult:
        if not isinstance(runs, int) or isinstance(runs, bool) or not 2 <= runs <= 4:
            raise ValueError("determinism runs must be between two and four")
        root = Path(case_root).resolve(strict=True)
        before = _source_hash(root / "src")
        # Threads only orchestrate subprocesses.  Each actual replay and checker
        # still acquires a slot from the one shared ProcessRunner; no nested pools.
        end = time.monotonic() + timeout

        def fresh_replay() -> ReplayResult:
            allowance = end - time.monotonic()
            if allowance <= 0:
                raise TimeLimitError("determinism timeout exhausted")
            return self.replay(root, scenario, seed=seed, faults=faults,
                               timeout=allowance, reserve=reserve)

        with ThreadPoolExecutor(max_workers=min(2, runs)) as pool:
            futures = [pool.submit(fresh_replay) for _ in range(runs)]
            results = tuple(future.result() for future in futures)
        hashes = [result.summary.get("trace_sha256") for result in results]
        valid_hashes = all(isinstance(value, str) and len(value) == 64
                           and all(char in "0123456789abcdef" for char in value) for value in hashes)
        if any(result.source_hash != before or result.status == "stale_source" for result in results):
            status = "stale_source"
        elif not all(result.status in ("pass", "invariant_failure", "unresolved") for result in results):
            status = "validation_failure"
        elif not valid_hashes:
            status = "evidence_error"
        elif len(set(hashes)) != 1:
            status = "nondeterministic"
        else:
            status = "pass"
        return DeterminismResult(before, scenario, seed, faults, results, status)
