"""Stage source changes and promote only a verified, recoverable snapshot."""

from __future__ import annotations

from contextlib import contextmanager
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import tempfile
import threading
from typing import Callable


class PatchError(ValueError):
    pass


def _ignored(name: str) -> bool:
    return name in {"__pycache__", ".git", ".pytest_cache"} or name.startswith(".c3-fixer-") or name.endswith((".pyc", ".pyo"))


def _source_files(src: Path) -> dict[str, Path]:
    files = {}
    for directory, dirs, names in os.walk(src, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not _ignored(d))
        for name in dirs + sorted(names):
            path = Path(directory) / name
            if path.is_symlink():
                raise PatchError(f"source symlinks are unsupported: {path.relative_to(src)}")
        for name in sorted(names):
            if not _ignored(name):
                path = Path(directory) / name
                if path.is_file():
                    files[path.relative_to(src).as_posix()] = path
    return dict(sorted(files.items()))


def source_hash(src: Path) -> str:
    digest = hashlib.sha256()
    for name, path in _source_files(Path(src)).items():
        digest.update(name.encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update((path.stat().st_mode & 0o777).to_bytes(2, "big"))
    return digest.hexdigest()


def _copy_case(case: Path, destination: Path) -> None:
    def ignore(directory, names):
        excluded = [name for name in names if _ignored(name) or name == ".env"
                    or (Path(directory) == case and name in {"runs", "results"})]
        for name in names:
            path = Path(directory) / name
            if name not in excluded and path.is_symlink():
                if not path.resolve().is_relative_to(case) or path.is_dir():
                    raise PatchError(f"case symlink cannot be safely staged: {path.relative_to(case)}")
        return excluded
    shutil.copytree(case, destination, ignore=ignore, symlinks=False)


@contextmanager
def _defer_termination():
    blocked = None
    if threading.current_thread() is threading.main_thread() and hasattr(signal, "pthread_sigmask"):
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
    try:
        yield
    finally:
        if blocked is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, blocked)


class SourceManager:
    def __init__(self, live: Path, scratch: Path | None = None):
        self.live = Path(live).resolve(strict=True)
        if not (self.live / "src").is_dir():
            raise PatchError("case has no src directory")
        self.scratch = Path(scratch) if scratch is not None else Path(tempfile.mkdtemp(prefix=".c3-fixer-", dir=self.live))
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.original = self.scratch / "original"
        self.best = self.scratch / "best"
        self.candidate = self.scratch / "candidate"
        self._temp = self.scratch / "replacements"
        self._temp.mkdir()
        _source_files(self.live / "src")
        _copy_case(self.live, self.original)
        shutil.copytree(self.original, self.best)
        shutil.copytree(self.best, self.candidate)
        self._generation = 0

    @property
    def candidate_hash(self) -> str:
        return source_hash(self.candidate / "src")

    @property
    def best_hash(self) -> str:
        return source_hash(self.best / "src")

    def reset_candidate(self) -> str:
        shutil.rmtree(self.candidate)
        shutil.copytree(self.best, self.candidate)
        self._generation += 1
        return self.candidate_hash

    def _target(self, name: str) -> Path:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or len(path.parts) < 2 or path.parts[0] != "src":
            raise PatchError("edits must use a relative path under src/")
        if any(part.startswith(".") or _ignored(part) for part in path.parts[1:]) or "\\" in name:
            raise PatchError("hidden/generated source paths cannot be edited")
        target = self.candidate.joinpath(*path.parts)
        if not target.resolve().is_relative_to((self.candidate / "src").resolve()):
            raise PatchError("edit path escapes staged src")
        current = target
        while current != self.candidate:
            if current.is_symlink():
                raise PatchError("symlink paths cannot be edited")
            current = current.parent
        return target

    def _atomic_write(self, target: Path, content: bytes, mode: int = 0o644) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(dir=self._temp)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(mode)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def apply_edits(self, base_hash: str, edits: list[dict]) -> dict:
        if base_hash != self.candidate_hash:
            raise PatchError("stale source hash; read the current candidate before editing")
        if not isinstance(edits, list) or not 1 <= len(edits) <= 30:
            raise PatchError("provide between 1 and 30 edits")
        changes: dict[Path, bytes | None] = {}
        before: dict[Path, tuple[bytes | None, int]] = {}
        for edit in edits:
            if not isinstance(edit, dict) or not isinstance(edit.get("path"), str):
                raise PatchError("each edit needs a path")
            target = self._target(edit["path"])
            if target.is_dir():
                raise PatchError("edit target is a directory")
            if target not in before:
                before[target] = (target.read_bytes() if target.exists() else None,
                                  target.stat().st_mode & 0o777 if target.exists() else 0o644)
            data = changes.get(target, before[target][0])
            forms = int(edit.get("delete") is True) + int("content" in edit) + int("old" in edit or "new" in edit)
            if forms != 1:
                raise PatchError("use exactly one of content, old/new, or delete")
            if edit.get("delete") is True:
                if data is None:
                    raise PatchError("cannot delete a missing file")
                replacement = None
            elif "content" in edit:
                if not isinstance(edit["content"], str):
                    raise PatchError("content must be text")
                replacement = edit["content"].encode()
            else:
                old, new = edit.get("old"), edit.get("new")
                if data is None or not isinstance(old, str) or not old or not isinstance(new, str):
                    raise PatchError("old/new replacement requires an existing text file and nonempty old")
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise PatchError("old/new replacement requires UTF-8 source") from error
                if text.count(old) != 1:
                    raise PatchError("old text must match exactly once; include more context")
                replacement = text.replace(old, new, 1).encode()
            changes[target] = replacement
        for target, replacement in changes.items():
            if replacement is not None and target.suffix == ".py":
                try:
                    compile(replacement, str(target.relative_to(self.candidate)), "exec", dont_inherit=True)
                except (SyntaxError, ValueError) as error:
                    raise PatchError(f"invalid Python in {target.relative_to(self.candidate)}: {error}") from error
        try:
            for target, data in changes.items():
                if data is None:
                    target.unlink()
                else:
                    self._atomic_write(target, data, before[target][1])
        except BaseException:
            # Remove additions before restoring files that may now be directories.
            for target, (data, _) in before.items():
                if data is None and target.is_file():
                    target.unlink()
            for target, (data, mode) in before.items():
                if data is not None:
                    if target.is_dir() and not _source_files(target):
                        shutil.rmtree(target)
                    self._atomic_write(target, data, mode)
            raise
        self._generation += 1
        return {"source_hash": self.candidate_hash,
                "changed_files": [str(p.relative_to(self.candidate)) for p in changes]}

    def diff(self, max_chars: int = 30000) -> str:
        old = _source_files(self.best / "src")
        new = _source_files(self.candidate / "src")
        parts = []
        for name in sorted(old.keys() | new.keys()):
            before = old[name].read_text(errors="replace").splitlines(keepends=True) if name in old else []
            after = new[name].read_text(errors="replace").splitlines(keepends=True) if name in new else []
            parts.extend(difflib.unified_diff(before, after, f"a/src/{name}", f"b/src/{name}"))
        text = "".join(parts)
        return text if len(text) <= max_chars else text[:max_chars] + "\n[diff truncated]\n"

    def _install(self, src: Path, destination: Path) -> None:
        old, new = _source_files(destination), _source_files(src)
        # Delete obsolete descendants first so directory/file replacements and
        # their rollback work in either direction.
        for name in sorted(old.keys() - new.keys(), key=lambda value: (-value.count("/"), value)):
            old[name].unlink()
        for name in sorted(new):
            target = destination / name
            if target.is_dir():
                if _source_files(target):
                    raise PatchError(f"directory still contains source files: {name}")
                shutil.rmtree(target)
            if name not in old or old[name].read_bytes() != new[name].read_bytes() or old[name].stat().st_mode != new[name].stat().st_mode:
                self._atomic_write(target, new[name].read_bytes(), new[name].stat().st_mode & 0o777)

    def _prepare_install(self, src: Path, destination: Path, directory: Path) -> tuple[list[str], dict[str, Path]]:
        old, new = _source_files(destination), _source_files(src)
        directory.mkdir()
        prepared = {}
        for name, path in new.items():
            if name in old and old[name].read_bytes() == path.read_bytes() and old[name].stat().st_mode == path.stat().st_mode:
                continue
            temporary = directory / name
            self._atomic_write(temporary, path.read_bytes(), path.stat().st_mode & 0o777)
            prepared[name] = temporary
        return sorted(old.keys() - new.keys(), key=lambda name: (-name.count("/"), name)), prepared

    def _move_prepared(self, temporary: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_dir():
            if _source_files(target):
                raise PatchError("replacement directory still contains source files")
            shutil.rmtree(target)
        os.replace(temporary, target)

    def _install_prepared(self, plan: tuple[list[str], dict[str, Path]], destination: Path) -> None:
        deletes, prepared = plan
        for name in deletes:
            (destination / name).unlink()
        for name, temporary in prepared.items():
            self._move_prepared(temporary, destination / name)

    def promote(self, verified_hash: str, verify: Callable[[Path], bool]) -> dict:
        if self.candidate_hash != verified_hash:
            raise PatchError("candidate changed after validation")
        if source_hash(self.live / "src") != self.best_hash:
            raise PatchError("live source changed outside the fixer; refusing promotion")
        transaction = Path(tempfile.mkdtemp(prefix=f"promotion-{self._generation}-", dir=self.scratch))
        backup = transaction / "before"
        shutil.copytree(self.live / "src", backup)
        ready = transaction / "after"
        shutil.copytree(self.candidate / "src", ready)
        journal = transaction / "journal.json"
        def record(phase):
            self._atomic_write(journal, (json.dumps({"phase": phase, "source_hash": verified_hash,
                                                  "backup": str(backup)}, sort_keys=True) + "\n").encode())
        record("prepared")
        plan = self._prepare_install(ready, self.live / "src", transaction / "install")
        try:
            record("installing")
            with _defer_termination():
                self._install_prepared(plan, self.live / "src")
            if source_hash(self.live / "src") != verified_hash:
                raise PatchError("installed source differs from validated candidate")
            if not verify(self.live) or source_hash(self.live / "src") != verified_hash:
                raise PatchError("installed source did not pass verification")
            # Only the source changes; the staged case's test/simulator copies stay immutable.
            self._install(ready, self.best / "src")
            record("accepted")
        except BaseException:
            rollback = self._prepare_install(backup, self.live / "src", transaction / "rollback")
            with _defer_termination():
                self._install_prepared(rollback, self.live / "src")
            self._install(backup, self.best / "src")
            record("rolled_back")
            raise
        return {"source_hash": verified_hash, "journal": str(journal), "accepted": True}
