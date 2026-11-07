"""Small, public-input healthy-control plans for an unseen simulator case."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Callable

import yaml


MAX_CONTROLS = 16
MAX_FILES = 48
MAX_ENTRIES = 256
MAX_DIRECTORY_ENTRIES = 128
MAX_DEPTH = 3
MAX_FILE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 512 * 1024
MAX_REFERENCES = 8
MAX_SCENARIOS = 12
MAX_PROFILES = 12
SEED_POLICY = "healthy-control-v1"


@dataclass(frozen=True)
class ControlPlan:
    experiments: tuple[dict, ...]
    metadata: dict


class _Inventory:
    def __init__(self, root: Path, should_stop: Callable[[], bool] | None):
        self.root = root.resolve(strict=True)
        self.anchor = self.root / "scenarios"
        self.should_stop = should_stop or (lambda: False)
        self.counts = {
            "discovery_entries": 0, "files_read": 0, "bytes_read": 0,
            "invalid_inputs": 0, "excluded_links": 0, "discovery_omissions": 0,
            "discovery_truncated": False, "discovery_cancelled": False,
        }
        self.cache: dict[Path, tuple[bytes, dict]] = {}

    def stopped(self) -> bool:
        if self.should_stop():
            self.counts["discovery_cancelled"] = True
            return True
        return False

    def safe_path(self, path: Path) -> Path:
        relative = path.relative_to(self.anchor)
        if not relative.parts or ".." in relative.parts or len(str(path.relative_to(self.root))) > 240:
            raise ValueError("invalid control input path")
        current = self.anchor
        for part in (None, *relative.parts):
            current = current if part is None else current / part
            if current.is_symlink():
                raise ValueError("control input contains a symbolic link")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self.anchor.resolve(strict=True)):
            raise ValueError("control input leaves scenarios")
        return resolved

    def files(self) -> list[Path]:
        if not self.anchor.is_dir() or self.anchor.is_symlink():
            self.counts["excluded_links"] += int(self.anchor.is_symlink())
            return []
        pending = [(self.anchor, 0)]
        output = []
        while pending and not self.stopped():
            folder, depth = pending.pop()
            entries = []
            try:
                with os.scandir(folder) as scan:
                    for entry in scan:
                        if self.stopped():
                            return sorted(output)
                        self.counts["discovery_entries"] += 1
                        if (self.counts["discovery_entries"] > MAX_ENTRIES
                                or len(entries) >= MAX_DIRECTORY_ENTRIES):
                            self.counts["discovery_truncated"] = True
                            self.counts["discovery_omissions"] += 1
                            # A partial directory prefix depends on filesystem order.
                            entries = []
                            break
                        entries.append(entry)
            except OSError:
                self.counts["invalid_inputs"] += 1
                continue
            children = []
            for entry in sorted(entries, key=lambda item: item.name):
                if entry.is_symlink():
                    self.counts["excluded_links"] += 1
                    continue
                if entry.name.startswith(".") or entry.name in {"__pycache__", "runs", "logs"}:
                    continue
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    if depth < MAX_DEPTH:
                        children.append((path, depth + 1))
                    else:
                        self.counts["discovery_omissions"] += 1
                        self.counts["discovery_truncated"] = True
                elif entry.is_file(follow_symlinks=False) and path.suffix.lower() in {".yaml", ".yml", ".json"}:
                    output.append(path)
            pending.extend(reversed(children))
            if self.counts["discovery_entries"] >= MAX_ENTRIES:
                if pending:
                    self.counts["discovery_omissions"] += len(pending)
                    self.counts["discovery_truncated"] = True
                break
        output.sort()
        if len(output) > MAX_FILES:
            self.counts["discovery_omissions"] += len(output) - MAX_FILES
            self.counts["discovery_truncated"] = True
        return output[:MAX_FILES]

    def load(self, path: Path, chain: tuple[Path, ...] = ()) -> tuple[bytes, dict]:
        if self.stopped():
            raise ValueError("control discovery stopped")
        path = self.safe_path(path)
        if path in chain or len(chain) >= MAX_REFERENCES:
            raise ValueError("cyclic or excessive fault references")
        if path in self.cache:
            return self.cache[path]
        if self.counts["files_read"] >= MAX_FILES:
            self.counts["discovery_truncated"] = True
            raise ValueError("control file limit")
        if not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
            raise ValueError("control input is not a regular file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                raise ValueError("control input is not a bounded regular file")
            allowance = min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - self.counts["bytes_read"])
            if info.st_size > allowance:
                self.counts["discovery_truncated"] = True
                raise ValueError("control byte limit")
            raw = stream.read(allowance)
            if os.fstat(stream.fileno()).st_size != len(raw):
                raise ValueError("control input changed during discovery")
        self.counts["files_read"] += 1
        self.counts["bytes_read"] += len(raw)
        text = raw.decode("utf-8")
        document = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
        if not isinstance(document, dict):
            raise ValueError("control document must be a mapping")
        _bounded_structure(document)
        reference = document.get("faults")
        if isinstance(reference, str):
            supplied = Path(reference)
            if supplied.is_absolute() or ".." in supplied.parts or not supplied.parts:
                raise ValueError("fault reference leaves scenarios")
            self.load(path.parent / supplied, (*chain, path))
        elif reference is not None and not isinstance(reference, dict):
            raise ValueError("faults must be a mapping or local document")
        self.cache[path] = raw, document
        return raw, document


def _bounded_structure(document: dict) -> None:
    stack: set[int] = set()
    completed: set[int] = set()
    count = 0

    def visit(value, depth: int = 0) -> None:
        nonlocal count
        count += 1
        if count > 4096 or depth > 12:
            raise ValueError("control structure exceeds its bounds")
        if isinstance(value, (dict, list)):
            identity = id(value)
            if identity in stack:
                raise ValueError("cyclic YAML aliases")
            if identity in completed:
                return
            stack.add(identity)
            if isinstance(value, dict):
                if any(not isinstance(key, str) for key in value):
                    raise ValueError("control mapping keys must be strings")
                children = value.values()
            else:
                children = value
            for child in children:
                visit(child, depth + 1)
            stack.remove(identity)
            completed.add(identity)

    visit(document)


def _seed(raw: bytes, profile: bytes, public_seeds: set[int]) -> tuple[int, str]:
    material = raw + b"\0" + profile + b"\0" + SEED_POLICY.encode() + b":0"
    digest = hashlib.sha256(material).digest()
    seed = int.from_bytes(digest[:4], "big") & 0x7fffffff
    while seed in public_seeds:
        seed = (seed + 1) & 0x7fffffff
    return seed, digest.hex()


def plan_controls(root: Path, public_seeds: list[int], limit: int = MAX_CONTROLS, *,
                  should_stop: Callable[[], bool] | None = None) -> ControlPlan:
    """Alternate scenario/profile inputs and cover distinct holdouts first.

    Each input gets its stable hash seed before public-seed repeats consume
    the bounded phase. No service or fault family names determine priority.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= 64:
        raise ValueError("control limit must be between zero and 64")
    inventory = _Inventory(Path(root), should_stop)
    available_seeds = {seed for seed in public_seeds[:64]
                       if isinstance(seed, int) and not isinstance(seed, bool) and seed.bit_length() <= 256}
    first_seed = next((seed for seed in public_seeds[:64]
                       if isinstance(seed, int) and not isinstance(seed, bool) and seed in available_seeds), 1)
    scenarios = []
    profiles = []
    for path in inventory.files():
        if inventory.stopped():
            break
        try:
            raw, document = inventory.load(path)
        except (OSError, ValueError, RecursionError, yaml.YAMLError):
            inventory.counts["invalid_inputs"] += 1
            continue
        if any(word in path.stem.casefold() for word in ("template", "example")):
            continue
        if isinstance(document.get("workload"), list) and isinstance(document.get("nodes"), (list, dict)):
            scenarios.append((path, raw, document))
        elif "workload" not in document and any(key in document for key in ("gpu", "network", "nodes", "faults")):
            profiles.append((path, raw, document))
    eligible_regular = [item for item in scenarios if item[0].stem.casefold() != "public"] or scenarios
    scenario_omitted = max(0, len(eligible_regular) - MAX_SCENARIOS)
    profile_omitted = max(0, len(profiles) - MAX_PROFILES)
    regular = eligible_regular[:MAX_SCENARIOS]
    profiles = profiles[:MAX_PROFILES]
    regular_inputs = deque((item, None) for item in regular)
    profile_inputs = deque((regular[0], item) for item in profiles) if regular else deque()
    inputs = []
    while regular_inputs or profile_inputs:
        for queue in (regular_inputs, profile_inputs):
            if queue:
                inputs.append(queue.popleft())
    holdouts = []
    repeats = []
    for scenario, profile in inputs:
        path, raw, _ = scenario
        profile_raw = profile[1] if profile else b""
        stable_seed, seed_hash = _seed(raw, profile_raw, available_seeds)
        common = {
            "scenario": path.relative_to(inventory.root).as_posix(),
            "faults": profile[0].relative_to(inventory.root).as_posix() if profile else None,
            "control": True, "scenario_input_sha256": hashlib.sha256(raw).hexdigest(),
            "profile_input_sha256": hashlib.sha256(profile_raw).hexdigest() if profile else None,
        }
        holdouts.append({**common, "seed": stable_seed, "seed_origin": SEED_POLICY,
                         "seed_input_sha256": seed_hash})
        repeats.append({**common, "seed": first_seed, "seed_origin": "public"})
    experiments = holdouts + repeats
    metadata = {
        **inventory.counts, "scenario_inputs": len(regular), "profile_inputs": len(profiles),
        "scenario_inputs_omitted": scenario_omitted, "profile_inputs_omitted": profile_omitted,
        "control_inputs": len(inputs), "planned": min(limit, len(experiments)),
        "planned_omitted": max(0, len(experiments) - limit), "limit": limit,
        "public_seeds_omitted": max(0, len(public_seeds) - 64),
        "seed_policy": SEED_POLICY,
        "coverage_order": "distinct_holdouts_then_public",
    }
    return ControlPlan(tuple(experiments[:limit]), metadata)
