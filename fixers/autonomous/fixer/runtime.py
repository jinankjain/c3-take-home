"""Resource limits shared by every local tool and model request."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Mapping, Sequence


class TimeLimitError(RuntimeError):
    """An operation cannot fit within the available time."""


class ConfigurationError(ValueError):
    """The supplied case environment is incomplete or invalid."""


class Deadline:
    def __init__(self, unix_seconds: float, stop_event: threading.Event | None = None):
        if not math.isfinite(unix_seconds):
            raise ConfigurationError("C3_DEADLINE must be a finite Unix timestamp")
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self._end = time.monotonic() + max(0.0, unix_seconds - time.time())

    def remaining(self, reserve: float = 0) -> float:
        if self.stop_event.is_set():
            return 0.0
        return max(0.0, self._end - time.monotonic() - max(0, reserve))

    def timeout(self, requested: float, reserve: float = 0) -> float:
        if not math.isfinite(requested) or requested <= 0:
            raise ValueError("timeout must be positive and finite")
        allowance = min(requested, self.remaining(reserve))
        if allowance <= 0:
            raise TimeLimitError("deadline or termination request prevents more work")
        return allowance


@dataclass(frozen=True)
class CaseConfig:
    workspace: Path
    corpus: Path
    deadline: Deadline
    budget_usd: float
    metadata: dict

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "CaseConfig":
        env = os.environ if env is None else env
        try:
            workspace = Path(env["C3_WORKSPACE"]).expanduser().resolve(strict=True)
            corpus = Path(env["C3_CORPUS"]).expanduser().resolve(strict=True)
            deadline = Deadline(float(env["C3_DEADLINE"]))
            budget = float(env["C3_BUDGET_USD"])
        except (KeyError, OSError, ValueError) as error:
            raise ConfigurationError(f"invalid C3 environment: {type(error).__name__}") from error
        if not workspace.is_dir() or not (workspace / "src").is_dir():
            raise ConfigurationError("C3_WORKSPACE must contain a src directory")
        if not corpus.is_dir() or not (corpus / "INDEX.md").is_file():
            raise ConfigurationError("C3_CORPUS must contain INDEX.md")
        if not math.isfinite(budget) or budget < 0:
            raise ConfigurationError("C3_BUDGET_USD must be finite and nonnegative")
        case_file = workspace / "case.json"
        try:
            metadata = json.loads(case_file.read_text()) if case_file.is_file() else {}
        except (OSError, ValueError) as error:
            raise ConfigurationError("case.json must contain valid JSON") from error
        if not isinstance(metadata, dict):
            raise ConfigurationError("case.json must contain an object")
        return cls(workspace, corpus, deadline, budget, metadata)

    @property
    def seeds(self) -> list[int]:
        values = self.metadata.get("public_seeds", [])
        if not isinstance(values, list):
            return [1]
        return list(dict.fromkeys(v for v in values if isinstance(v, int) and not isinstance(v, bool))) or [1]


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.cancelled

    def as_dict(self, max_chars: int = 6000) -> dict:
        return {
            "argv": list(self.argv), "returncode": self.returncode,
            "stdout": self.stdout[-max_chars:], "stderr": self.stderr[-max_chars:],
            "duration": round(self.duration, 3), "timed_out": self.timed_out,
            "cancelled": self.cancelled, "truncated": self.truncated,
        }


class ProcessRunner:
    """Run bounded jobs without shell interpretation or pipe-output deadlocks."""

    def __init__(self, deadline: Deadline, concurrency: int = 2, output_limit: int = 1_048_576):
        if concurrency not in (1, 2) or output_limit < 1024:
            raise ValueError("invalid process concurrency or output limit")
        self.deadline = deadline
        self.output_limit = output_limit
        self._slots = threading.BoundedSemaphore(concurrency)
        self._lock = threading.RLock()
        self._active: set[subprocess.Popen] = set()
        self._killed: set[subprocess.Popen] = set()

    def _kill_group(self, process: subprocess.Popen) -> None:
        # A leader may exit while a descendant still holds resources. Kill the
        # session group even when poll() says the leader has already exited.
        with self._lock:
            if process in self._killed:
                return
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self._killed.add(process)

    def close(self) -> None:
        self.deadline.stop_event.set()
        with self._lock:
            for process in self._active:
                self._kill_group(process)

    def _capture(self, stream) -> tuple[str, bool]:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(0)
        if size <= self.output_limit:
            return stream.read().decode("utf-8", errors="replace"), False
        half = self.output_limit // 2
        head = stream.read(half)
        stream.seek(-half, os.SEEK_END)
        tail = stream.read(half)
        return (head + b"\n[output truncated]\n" + tail).decode("utf-8", errors="replace"), True

    def run(self, command: Sequence[str], *, cwd: Path, timeout: float = 60,
            reserve: float = 0, env: dict | None = None, input_text: str | None = None) -> CommandResult:
        allowance = self.deadline.timeout(timeout, reserve)
        end = time.monotonic() + allowance
        started = time.monotonic()
        while not self._slots.acquire(timeout=min(0.1, max(0, end - time.monotonic()))):
            if self.deadline.stop_event.is_set() or time.monotonic() >= end:
                raise TimeLimitError("no process slot available before operation deadline")
        argv = tuple(str(arg) for arg in command)
        process = None
        timed_out = cancelled = False
        try:
            if not argv:
                raise ValueError("command must not be empty")
            if self.deadline.stop_event.is_set() or time.monotonic() >= end:
                raise TimeLimitError("operation deadline reached before process launch")
            job_env = dict(os.environ)
            if env is not None:
                job_env.update(env)
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr, tempfile.TemporaryFile() as stdin:
                if input_text is not None:
                    stdin.write(input_text.encode("utf-8"))
                stdin.seek(0)
                process = subprocess.Popen(argv, cwd=cwd, env=job_env, stdin=stdin,
                                           stdout=stdout, stderr=stderr, start_new_session=True)
                with self._lock:
                    self._active.add(process)
                while process.poll() is None:
                    cancelled = self.deadline.stop_event.is_set()
                    timed_out = time.monotonic() >= end
                    # Bound disk output as well as the data loaded into memory.
                    excessive_output = max(os.fstat(stdout.fileno()).st_size,
                                           os.fstat(stderr.fileno()).st_size) > self.output_limit * 8
                    if cancelled or timed_out or excessive_output:
                        self._kill_group(process)
                        break
                    try:
                        process.wait(timeout=min(0.1, max(0.001, end - time.monotonic())))
                    except subprocess.TimeoutExpired:
                        pass
                process.wait()
                self._kill_group(process)
                out, out_truncated = self._capture(stdout)
                err, err_truncated = self._capture(stderr)
                return CommandResult(argv, process.returncode, out, err, time.monotonic() - started,
                                     timed_out, cancelled, out_truncated or err_truncated)
        finally:
            if process is not None:
                self._kill_group(process)
                with self._lock:
                    self._active.discard(process)
                    self._killed.discard(process)
            self._slots.release()


def emit(event: str, **fields) -> None:
    """Structured, flush-on-write review logs; callers never supply credentials."""
    print(json.dumps({"event": event, **fields}, sort_keys=True, default=str), flush=True)
