"""Native model tools with explicit roots, bounded arguments, and staged writes."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any

import yaml

from .evidence import MAX_TRACE_CHARS, bounded_result, trace_slice as slice_trace
from .patches import PatchError, source_hash
from .retrieval import FileTools
from .runtime import TimeLimitError


MAX_ARGUMENT_BYTES = 256 * 1024
MAX_PROBE_BYTES = 64 * 1024
MAX_PROBES = 24
MAX_COMPOSITION_PARTS = 8
MAX_COMPOSITION_FILES = 32
_PROBE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_SOURCES = ("candidate", "original", "accepted")


def _string(maximum: int = 1024, **extra) -> dict:
    return {"type": "string", "minLength": 1, "maxLength": maximum, **extra}


def _enum(values: tuple[str, ...], default: str | None = None) -> dict:
    return {"type": "string", "enum": list(values), **({"default": default} if default else {})}


def _integer(maximum: int, minimum: int = 1, default: int | None = None) -> dict:
    return {"type": "integer", "minimum": minimum, "maximum": maximum,
            **({"default": default} if default is not None else {})}


def _array(item: dict, maximum: int, minimum: int = 0) -> dict:
    return {"type": "array", "items": item, "minItems": minimum, "maxItems": maximum}


def _object(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


def _tool(name: str, description: str, properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"name": name, "description": description, "input_schema": _object(properties, required)}


_reproduction_schema = _object({
    "scenario": _string(256, default="public"),
    "seed": _integer(2**63 - 1, minimum=0),
    "faults": {"type": ["string", "null"], "minLength": 1, "maxLength": 1024},
}, ("scenario", "seed"))


TOOL_DEFINITIONS = [
    _tool("status", "Read current source hashes, validation progress, time, and budget.", {}),
    _tool("list_files", "List bounded relative file paths in the staged case or corpus.", {
        "root": _enum(("case", "corpus"), "case"), "pattern": _string(512, default="*"),
        "limit": _integer(200, default=200),
    }),
    _tool("read_file", "Read a numbered range from the staged case, original, accepted source tree, or corpus. Case means candidate; source hashes identify excerpts.", {
        "root": _enum(("case", "corpus", "original", "accepted"), "case"),
        "path": _string(), "start": _integer(1_000_000, default=1),
        "end": {"type": ["integer", "null"], "minimum": 1, "maximum": 1_000_000},
    }, ("path",)),
    _tool("search", "Search case source literally or using bounded regex alternatives; search corpus sections by relevant terms. Corpus uses ranked lexical retrieval and supports literal mode. Default path is src for case, service for corpus.", {
        "root": _enum(("case", "corpus"), "case"), "query": _string(512),
        "path": _string(), "mode": _enum(("literal", "regex"), "literal"),
        "limit": _integer(40, default=20),
    }, ("query",)),
    _tool("trace_slice", "Read a retained run's entity history in global trace order. at/window_s focus on global trace t; context_lines includes neighboring and linked RPC records. Filters select anchors by kind, log event, method, or effect. Complete bounded JSON preserves source provenance and first/last records, with omission counts.", {
        "run_id": _string(64), "entities": _array(_string(512), 40),
        "event_types": {"type": ["array", "null"], "items": _string(100), "maxItems": 40},
        "limit": _integer(200, default=100),
        "at": {"type": ["number", "null"], "minimum": 0},
        "window_s": {"type": "number", "minimum": 0, "maximum": 3600, "default": 2.0},
        "context_lines": _integer(20, minimum=0, default=0),
    }, ("run_id", "entities")),
    _tool("replay", "Replay and check one source snapshot. Use a shipped scenario name/path or probe:name. Invariant failures are evidence; replay exit zero alone does not certify correctness.", {
        "source": _enum(_SOURCES, "candidate"), "scenario": _string(256, default="public"),
        "seed": _integer(2**63 - 1, minimum=0),
        "faults": {"type": ["string", "null"], "minLength": 1, "maxLength": 1024},
    }, ("seed",)),
    _tool("run_tests", "Run the unchanged public tests against the chosen source snapshot. Passing tests protect regression behavior; they do not establish a repair.", {
        "source": _enum(_SOURCES, "candidate"),
        "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 60, "default": 60},
    }),
    _tool("write_probe", "Create an immutable scratch scenario using the documented schema. Relative faults references resolve within the case's unchanged scenarios. Demonstrate a probe's intended failure on original/accepted source before using candidate success as repair evidence.", {
        "name": _string(64, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$"),
        "scenario": {"type": "object"},
    }, ("name", "scenario")),
    _tool("compose_probe", "Compose an immutable scratch scenario from a shipped/scratch base, additional scenario probes, and fault profiles. References use shipped names/paths or probe:name. Inherited fault files are inlined. Mappings merge; record lists append with exact dedup. Conflicting scalar/range settings require explicit nested overrides. Replay identical inputs on accepted and candidate source before using them as repair evidence.", {
        "name": _string(64, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$"),
        "base": _string(256),
        "fault_profiles": _array(_string(256), MAX_COMPOSITION_PARTS),
        "probes": _array(_string(256), MAX_COMPOSITION_PARTS),
        "overrides": {"type": "object"},
    }, ("name", "base")),
    _tool("apply_edits", "Apply coherent edits only to staged src using the current candidate hash. Each edit is path/content, path/old/new (one exact match), or path/delete:true. Python must remain syntactically valid. Validation owns source promotion.", {
        "base_hash": _string(64, pattern=r"^[0-9a-f]{64}$"),
        "edits": _array({
            **_object({"path": _string(), "content": {"type": "string", "maxLength": 131072},
                       "old": _string(131072), "new": {"type": "string", "maxLength": 131072},
                       "delete": {"const": True}}, ("path",)),
            "oneOf": [{"required": ["content"]}, {"required": ["old", "new"]},
                      {"required": ["delete"]}],
        }, 30, 1),
    }, ("base_hash", "edits")),
    _tool("reset_candidate", "Replace staged source with the last accepted source snapshot.", {}),
    _tool("record_hypothesis", "Record a causal protocol obligation and supporting evidence. Only controller validation marks verified repairs; agent statuses are open or proposed.", {
        "id": _string(64, pattern=r"^[A-Za-z0-9_-]{1,64}$"),
        "obligation": _string(2000), "mechanism": _string(4000),
        "evidence": _array(_string(3000), 20),
        "status": _enum(("open", "proposed"), "open"),
    }, ("id", "obligation", "mechanism", "evidence")),
    _tool("validate", "Ask the controller to validate the candidate's stated obligation using targeted reproductions, original comparisons, public tests, controls, and determinism. Provide causal reason and the invariant families repaired.", {
        "target_invariants": _array(_string(100), 12, 1),
        "reproductions": _array(_reproduction_schema, 12, 1), "reason": _string(6000),
    }, ("target_invariants", "reproductions", "reason")),
]

_KEYS = {definition["name"]: set(definition["input_schema"]["properties"]) for definition in TOOL_DEFINITIONS}
_REQUIRED = {definition["name"]: set(definition["input_schema"]["required"]) for definition in TOOL_DEFINITIONS}


def _text(value: Any, name: str, maximum: int = 1024, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{name} must be {'a' if empty else 'nonempty'} bounded string")
    return value


def _choice(value: Any, values: tuple[str, ...], name: str) -> str:
    if not isinstance(value, str) or value not in values:
        raise ValueError(f"{name} must be one of {', '.join(values)}")
    return value


def _int(value: Any, name: str, maximum: int, minimum: int = 1) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _texts(value: Any, name: str, maximum: int, item_maximum: int, minimum: int = 0) -> list[str]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{name} must be a list with {minimum} to {maximum} items")
    return [_text(item, name, item_maximum) for item in value]


def _relative(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or any(part.startswith(".") for part in path.parts):
        raise ValueError("path must remain relative within the chosen tool root")
    return path


class ToolDispatcher:
    def __init__(self, controller: Any):
        self.controller = controller

    def _root(self, source: str) -> Path:
        return Path(getattr(self.controller.sources, {"candidate": "candidate", "original": "original", "accepted": "best"}[source])).resolve(strict=True)

    def _context(self, root: str) -> dict:
        if root == "corpus":
            return {"root": root, "current_source_hash": self.controller.sources.candidate_hash}
        source = "candidate" if root == "case" else root
        return {"root": root, "source": source, "source_hash": source_hash(self._root(source) / "src")}

    def _files(self, root: str) -> FileTools:
        return self.controller.corpus.files if root == "corpus" else FileTools(self._root("candidate" if root == "case" else root))

    def _probe_root(self) -> Path:
        scratch = Path(self.controller.sources.scratch).resolve(strict=True)
        expected = scratch / "probes"
        supplied = Path(getattr(self.controller, "probe_root", expected))
        if supplied.is_symlink() or supplied.resolve() != expected.resolve():
            raise ValueError("probe root must stay in the controller scratch directory")
        expected.mkdir(exist_ok=True)
        if expected.is_symlink():
            raise ValueError("probe directory cannot be a symlink")
        return expected

    def _scenario(self, value: Any) -> str:
        value = _text(value, "scenario", 256)
        if value.startswith("probe:"):
            name = value[6:]
            if not _PROBE_NAME.fullmatch(name):
                raise ValueError("invalid probe name")
            path = FileTools(self._probe_root())._resolve(name + ".yaml")
            if not path.is_file():
                raise ValueError("probe must be a regular file")
        else:
            path = _relative(value)
            if len(path.parts) > 1 and path.parts[0] != "scenarios":
                raise ValueError("scenario must be a shipped name/path or probe:name")
        return value

    def _faults(self, value: Any) -> str | None:
        if value is None:
            return None
        value = _text(value, "faults")
        path = _relative(value)
        if not path.parts or path.parts[0] != "scenarios":
            raise ValueError("faults override must be under scenarios/")
        return value

    def execute(self, name: str, args: dict) -> dict:
        """Return tool errors as data, without exposing credentials or raw requests."""
        if not isinstance(name, str) or name not in _KEYS:
            return {"ok": False, "error": {"code": "unknown_tool", "message": "unknown tool name"}}
        try:
            if not isinstance(args, dict) or any(not isinstance(key, str) for key in args):
                raise ValueError("tool arguments must be an object with string keys")
            if set(args) - _KEYS[name]:
                raise ValueError("unrecognized tool argument keys")
            if _REQUIRED[name] - set(args):
                raise ValueError("required tool arguments are missing")
            if len(json.dumps(args, ensure_ascii=False).encode()) > MAX_ARGUMENT_BYTES:
                raise ValueError("tool arguments exceed 256 KiB")
            if name != "status" and self.controller.deadline.remaining() <= 0:
                raise TimeLimitError("deadline prevents additional tool work")
            result = getattr(self, "_" + name)(args)
            return {"ok": True, "result": result}
        except (TimeLimitError, PatchError, ValueError, TypeError, RecursionError,
                KeyError, OSError, RuntimeError) as error:
            if isinstance(error, TimeLimitError):
                code = "deadline"
            elif isinstance(error, PatchError):
                code = "patch_error"
            elif isinstance(error, (ValueError, TypeError, RecursionError)):
                code = "invalid_arguments"
            elif isinstance(error, KeyError):
                code = "unknown_run"
            elif isinstance(error, OSError):
                code = "filesystem_error"
            else:
                code = "tool_error"
            return {"ok": False, "error": {"code": code, "message": str(error)[:1200]}}

    def _status(self, args: dict) -> dict:
        return self.controller.status()

    def _list_files(self, args: dict) -> dict:
        root = _choice(args.get("root", "case"), ("case", "corpus"), "root")
        pattern = _text(args.get("pattern", "*"), "pattern", 512)
        limit = _int(args.get("limit", 200), "limit", 200)
        files = self._files(root).list_files(pattern=pattern, limit=limit)
        return {**self._context(root), "files": files, "limit": limit, "pattern": pattern}

    def _read_file(self, args: dict) -> dict:
        root = _choice(args.get("root", "case"), ("case", "corpus", "original", "accepted"), "root")
        path = _text(args["path"], "path")
        start = _int(args.get("start", 1), "start", 1_000_000)
        end = args.get("end")
        if end is not None:
            end = _int(end, "end", 1_000_000, start)
        result = self.controller.corpus.read(path, start=start, end=end) if root == "corpus" else self._files(root).read(path, start=start, end=end)
        return {**self._context(root), **result}

    def _search(self, args: dict) -> dict:
        root = _choice(args.get("root", "case"), ("case", "corpus"), "root")
        query = _text(args["query"], "query", 512)
        path = _text(args.get("path", "src" if root == "case" else "service"), "path")
        mode = _choice(args.get("mode", "literal"), ("literal", "regex"), "mode")
        limit = _int(args.get("limit", 20), "limit", 40)
        if root == "corpus":
            if mode != "literal":
                raise ValueError("corpus uses lexical queries; regex is available for case search")
            matches = self.controller.corpus.search(query, root=path, limit=limit)
        else:
            matches = self._files(root).search(query, path=path, mode=mode, limit=limit)
        return {**self._context(root), "matches": matches, "query": query, "mode": mode}

    def _trace_slice(self, args: dict) -> dict:
        run_id = _text(args["run_id"], "run_id", 64)
        entities = _texts(args["entities"], "entities", 40, 512)
        kinds = args.get("event_types")
        if kinds is not None:
            kinds = _texts(kinds, "event_types", 40, 100)
        limit = _int(args.get("limit", 100), "limit", 200)
        at = args.get("at")
        if at is not None:
            at = self._finite(args["at"], "at", 0, 1e15)
        window = self._finite(args.get("window_s", 2.0), "window_s", 0, 3600)
        context = _int(args.get("context_lines", 0), "context_lines", 20, 0)
        run = self.controller.runs[run_id]
        trace = FileTools(run.out_dir)._resolve("trace.jsonl")
        if not stat.S_ISREG(trace.stat().st_mode):
            raise ValueError("trace must be a regular file")
        provenance = {"run_id": run_id, "source_hash": run.source_hash,
                      "current_source_hash": self.controller.sources.candidate_hash,
                      "scenario": run.scenario, "scenario_hash": run.scenario_hash, "seed": run.seed,
                      "faults": getattr(run, "faults", None), "faults_hash": getattr(run, "faults_hash", None)}
        return slice_trace(trace, entities, event_types=kinds, limit=limit, at=at,
                           window_s=window, context_lines=context, provenance=provenance,
                           max_chars=MAX_TRACE_CHARS,
                           should_stop=lambda: self.controller.deadline.remaining() <= 0)

    @staticmethod
    def _finite(value: Any, name: str, minimum: float, maximum: float) -> float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"{name} must be a finite number")
        try:
            number = float(value)
        except (ValueError, OverflowError):
            raise ValueError(f"{name} must be a finite number") from None
        if not math.isfinite(number) or not minimum <= number <= maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}")
        return number

    def _replay(self, args: dict) -> dict:
        source = _choice(args.get("source", "candidate"), _SOURCES, "source")
        scenario = self._scenario(args.get("scenario", "public"))
        seed = _int(args["seed"], "seed", 2**63 - 1, 0)
        faults = self._faults(args.get("faults"))
        result = self.controller.replay(source=source, scenario=scenario, seed=seed, faults=faults)
        return bounded_result({**result.as_dict(), "current_source_hash": self.controller.sources.candidate_hash},
                              MAX_TRACE_CHARS)

    def _run_tests(self, args: dict) -> dict:
        source = _choice(args.get("source", "candidate"), _SOURCES, "source")
        timeout = args.get("timeout", 60)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("timeout must be a finite positive number at most 60")
        root = self._root(source)
        before = source_hash(root / "src")
        result = self.controller.simulator.tests(root, timeout=timeout,
                                                 reserve=self.controller.final_reserve)
        if source_hash(root / "src") != before:
            raise ValueError("source changed while its tests were running; result is stale")
        return {**result.as_dict(max_chars=6000), "ok": result.ok, "source": source,
                "source_hash": before}

    def _probe_fault_path(self, value: str) -> str:
        candidate = self._root("candidate")
        scenario_root = candidate / "scenarios"
        path = Path(_text(value, "probe faults"))
        if path.is_absolute():
            if not path.resolve().is_relative_to(scenario_root.resolve()):
                raise ValueError("probe faults path escapes case scenarios")
            relative = path.relative_to(candidate)
        else:
            relative = _relative(str(path))
            if not relative.parts or relative.parts[0] != "scenarios":
                relative = Path("scenarios") / relative
        resolved = FileTools(candidate)._resolve(relative)
        if not resolved.is_relative_to(scenario_root.resolve()) or not resolved.is_file():
            raise ValueError("probe faults must name a regular case scenario file")
        if resolved.suffix.lower() not in {".yaml", ".yml", ".json"}:
            raise ValueError("probe faults must use a YAML or JSON file")
        return str(resolved)

    @staticmethod
    def _probe_structure(value: Any, depth: int = 0, count: list[int] | None = None) -> None:
        count = [0] if count is None else count
        count[0] += 1
        if depth > 12 or count[0] > 4000:
            raise ValueError("probe structure exceeds depth or node limits")
        if isinstance(value, dict):
            for key, item in value.items():
                _text(key, "probe object key", 128)
                ToolDispatcher._probe_structure(item, depth + 1, count)
        elif isinstance(value, list):
            if len(value) > 256:
                raise ValueError("probe list exceeds 256 items")
            for item in value:
                ToolDispatcher._probe_structure(item, depth + 1, count)
        elif isinstance(value, str):
            _text(value, "probe string", 16_384, empty=True)
        elif value is None or isinstance(value, (bool, int)):
            return
        elif isinstance(value, float) and math.isfinite(value):
            return
        else:
            raise ValueError("probe values must be finite JSON data")

    def _composition_path(self, reference: str) -> Path:
        reference = _text(reference, "composition reference", 256)
        if any(ord(char) < 32 for char in reference):
            raise ValueError("composition references cannot contain control characters")
        if reference.startswith("probe:"):
            self._scenario(reference)
            return FileTools(self._probe_root())._resolve(reference[6:] + ".yaml")
        supplied = _relative(reference)
        relative = supplied if supplied.parts and supplied.parts[0] == "scenarios" else Path("scenarios") / supplied
        options = [relative] if relative.suffix else [relative.with_suffix(ext) for ext in (".yaml", ".yml", ".json")]
        for option in options:
            try:
                path = FileTools(self._root("candidate"))._resolve(option)
            except FileNotFoundError:
                continue
            if option.suffix.lower() not in {".yaml", ".yml", ".json"} or not path.is_file():
                raise ValueError("composition inputs must be regular scenario/fault files")
            return path
        raise ValueError("composition input is not a shipped scenario/fault file")

    def _composition_document(self, path: Path, reference: str, reads: list[int]) -> tuple[dict, dict]:
        if self.controller.deadline.remaining() <= 0:
            raise TimeLimitError("deadline prevents additional composition reads")
        reads[0] += 1
        if reads[0] > MAX_COMPOSITION_FILES:
            raise ValueError("composition exceeds its input file limit")
        if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > MAX_PROBE_BYTES:
            raise ValueError("composition input must be a regular file of at most 64 KiB")
        with path.open("rb") as stream:
            raw = stream.read(MAX_PROBE_BYTES + 1)
        if len(raw) > MAX_PROBE_BYTES:
            raise ValueError("composition input exceeds 64 KiB")
        try:
            document = yaml.safe_load(raw)
        except (yaml.YAMLError, ValueError, UnicodeError):
            raise ValueError("composition input is not valid YAML/JSON") from None
        if not isinstance(document, dict):
            raise ValueError("composition inputs must contain mappings")
        self._probe_structure(document)
        document = json.loads(json.dumps(document, ensure_ascii=False, allow_nan=False))
        candidate = self._root("candidate")
        resolved = str(path.relative_to(candidate)) if path.is_relative_to(candidate) else "probe:" + path.stem
        metadata = {"reference": reference, "sha256": hashlib.sha256(raw).hexdigest()}
        if resolved != reference:
            metadata["resolved"] = resolved
        return document, metadata

    def _inline_composition_faults(self, document: dict, origin: Path, reads: list[int],
                                   inherited: list[dict], stack: tuple[Path, ...] = ()) -> dict:
        document = deepcopy(document)
        faults = document.get("faults")
        if not isinstance(faults, str):
            return document
        if len(stack) >= 8:
            raise ValueError("fault reference chain exceeds eight files")
        reference = _text(faults, "probe faults", 1024)
        if any(ord(char) < 32 for char in reference):
            raise ValueError("fault references cannot contain control characters")
        raw = Path(reference)
        candidate = self._root("candidate")
        if raw.is_absolute():
            target = raw
        else:
            relative = _relative(reference)
            if relative.parts and relative.parts[0] == "scenarios":
                target = candidate / relative
            elif origin.is_relative_to(candidate / "scenarios"):
                target = origin.parent / relative
            else:
                target = candidate / "scenarios" / relative
        path = Path(self._probe_fault_path(str(target)))
        if path in stack:
            raise ValueError("cyclic fault references cannot be composed")
        profile, metadata = self._composition_document(path, str(path.relative_to(candidate)), reads)
        inherited.append(metadata)
        content = profile.get("faults", profile)
        if isinstance(content, str):
            content = self._inline_composition_faults({"faults": content}, path, reads,
                                                      inherited, stack + (path,))["faults"]
        if not isinstance(content, dict):
            raise ValueError("fault profiles must contain mappings")
        document["faults"] = content
        return document

    @staticmethod
    def _composition_override(overrides: dict, path: tuple[str, ...]) -> bool:
        value: Any = overrides
        for part in path:
            if not isinstance(value, dict):
                return True  # An explicit replacement of an ancestor covers it.
            if part not in value:
                return False
            value = value[part]
        return True

    @staticmethod
    def _record_identity(record: dict) -> tuple | None:
        for key in ("id", "name"):
            if isinstance(record.get(key), (str, int)):
                return key, json.dumps(record[key], sort_keys=True)
        if isinstance(record.get("node"), str):
            schedule = tuple((key, json.dumps(record[key], sort_keys=True))
                             for key in ("at", "start", "end", "from", "to") if key in record)
            return ("node", record["node"], *schedule)
        return None

    @classmethod
    def _merge_composition(cls, before: Any, after: Any, overrides: dict,
                           path: tuple[str, ...] = ()) -> Any:
        if isinstance(before, dict) and isinstance(after, dict):
            merged = deepcopy(before)
            for key in sorted(after):
                merged[key] = cls._merge_composition(merged[key], after[key], overrides, path + (key,)) \
                    if key in merged else deepcopy(after[key])
            return merged
        same_scalar = type(before) is type(after) and before == after
        same_number = (isinstance(before, (int, float)) and not isinstance(before, bool)
                       and isinstance(after, (int, float)) and not isinstance(after, bool) and before == after)
        if not isinstance(before, (dict, list)) and (same_scalar or same_number):
            return deepcopy(before)
        if isinstance(before, list) and isinstance(after, list):
            if not before:
                return deepcopy(after)
            if not after:
                return deepcopy(before)
            if all(isinstance(item, dict) for item in before + after):
                result = deepcopy(before)
                encoded = {json.dumps(item, sort_keys=True, allow_nan=False) for item in result}
                identities = {cls._record_identity(item): item for item in result if cls._record_identity(item) is not None}
                for item in after:
                    text = json.dumps(item, sort_keys=True, allow_nan=False)
                    if text in encoded:
                        continue
                    identity = cls._record_identity(item)
                    if identity is not None and identity in identities:
                        if cls._composition_override(overrides, path):
                            return deepcopy(before)
                        raise ValueError(f"conflicting records at {'.'.join(path)}; provide an explicit list override")
                    result.append(deepcopy(item))
                    encoded.add(text)
                    if identity is not None:
                        identities[identity] = item
                return result
            if json.dumps(before, sort_keys=True, allow_nan=False) == json.dumps(after, sort_keys=True, allow_nan=False):
                return deepcopy(before)
        if cls._composition_override(overrides, path):
            return deepcopy(before)  # Apply the declared replacement after merging.
        raise ValueError(f"conflicting probe setting at {'.'.join(path) or '<root>'}; provide a nested override")

    @classmethod
    def _apply_composition_overrides(cls, before: Any, overrides: Any, path: tuple[str, ...] = ()) -> Any:
        if isinstance(before, dict) and isinstance(overrides, dict):
            if not overrides and path:
                return {}
            result = deepcopy(before)
            for key in sorted(overrides):
                result[key] = cls._apply_composition_overrides(result.get(key), overrides[key], path + (key,))
            return result
        return deepcopy(overrides)

    def _compose_probe(self, args: dict) -> dict:
        name = _text(args["name"], "name", 64)
        if not _PROBE_NAME.fullmatch(name):
            raise ValueError("invalid probe name")
        base = _text(args["base"], "base", 256)
        profiles = _texts(args.get("fault_profiles", []), "fault_profiles", MAX_COMPOSITION_PARTS, 256)
        probes = _texts(args.get("probes", []), "probes", MAX_COMPOSITION_PARTS, 256)
        overrides = args.get("overrides", {})
        if not isinstance(overrides, dict):
            raise ValueError("overrides must be a nested mapping")
        self._probe_structure(overrides)
        if "name" in overrides and overrides["name"] != name:
            raise ValueError("the composed scenario name is set by the name argument")
        reads = [0]
        inherited: list[dict] = []
        path = self._composition_path(base)
        document, base_metadata = self._composition_document(path, base, reads)
        document = self._inline_composition_faults(document, path, reads, inherited)
        probe_metadata = []
        profile_metadata = []
        for reference in probes:
            path = self._composition_path(reference)
            addition, metadata = self._composition_document(path, reference, reads)
            addition = self._inline_composition_faults(addition, path, reads, inherited)
            addition.pop("name", None)
            document = self._merge_composition(document, addition, overrides)
            probe_metadata.append(metadata)
        for reference in profiles:
            path = self._composition_path(reference)
            addition, metadata = self._composition_document(path, reference, reads)
            content = addition.get("faults", addition)
            normalized = self._inline_composition_faults({"faults": content}, path, reads, inherited)
            if not isinstance(normalized["faults"], dict):
                raise ValueError("fault profiles must contain mappings")
            document = self._merge_composition(document, normalized, overrides)
            profile_metadata.append(metadata)
        document = self._apply_composition_overrides(document, overrides)
        document["name"] = name
        # Override fault references use write_probe's case-relative semantics.
        # Inline them too so later changes to an external file cannot change
        # the composed scenario's effective inputs.
        if isinstance(document.get("faults"), str):
            document["faults"] = self._probe_fault_path(document["faults"])
            document = self._inline_composition_faults(document, path, reads, inherited)
        self._probe_structure(document)
        composition = {"base": base_metadata, "probes": probe_metadata, "fault_profiles": profile_metadata,
                       "inherited_faults": deepcopy(inherited), "inherited_faults_omitted": 0,
                       "fault_manifest_hash": hashlib.sha256(json.dumps(inherited, sort_keys=True).encode()).hexdigest(),
                       "overrides_hash": hashlib.sha256(json.dumps(overrides, sort_keys=True).encode()).hexdigest(),
                       "read_files": reads[0], "resolved_hints_omitted": 0}
        sections = sorted(document.get("faults", {})) if isinstance(document.get("faults"), dict) else []
        kept_sections = sections[:8] + (sections[-8:] if len(sections) > 16 else sections[8:])
        # Check response size before publishing. The placeholder scalar fields
        # are at least as large as write_probe's actual metadata values.
        preview = {"probe": "probe:" + name, "path": str(self._probe_root() / (name + ".yaml")),
                   "bytes": MAX_PROBE_BYTES, "probe_hash": "0" * 64,
                   "source_hash": self.controller.sources.candidate_hash, "probe_count": MAX_PROBES,
                   "scenario_hash": "0" * 64, "composition": composition,
                   "fault_sections": [section if len(section) <= 64 else section[:64] + "…" for section in kept_sections],
                   "fault_sections_omitted": len(sections) - len(kept_sections),
                   "fault_section_names_truncated": sum(len(section) > 64 for section in kept_sections)}
        # Explicit component references/hashes remain complete. Only redundant
        # resolution hints and inherited-file previews may be reduced.
        if len(json.dumps(preview, ensure_ascii=False)) > MAX_TRACE_CHARS:
            for metadata in [base_metadata, *probe_metadata, *profile_metadata, *composition["inherited_faults"]]:
                if metadata.pop("resolved", None) is not None:
                    composition["resolved_hints_omitted"] += 1
        while len(json.dumps(preview, ensure_ascii=False)) > MAX_TRACE_CHARS and composition["inherited_faults"]:
            items = composition["inherited_faults"]
            head = (len(items) // 2 + 1) // 2
            tail = len(items) // 2 - head
            composition["inherited_faults"] = items[:head] + (items[-tail:] if tail else [])
            composition["inherited_faults_omitted"] = len(inherited) - len(composition["inherited_faults"])
        if len(json.dumps(preview, ensure_ascii=False)) > MAX_TRACE_CHARS:
            raise ValueError("composition references exceed the response budget")
        if self.controller.deadline.remaining() <= 0:
            raise TimeLimitError("deadline prevents publishing a composed probe")
        result = self._write_probe({"name": name, "scenario": document})
        from .validation import _scenario_hashes
        identity, _ = _scenario_hashes(self._root("candidate"), result["path"], None)
        result.pop("faults", None)
        result.update({key: value for key, value in preview.items()
                       if key in {"composition", "fault_sections", "fault_sections_omitted", "fault_section_names_truncated"}})
        result["scenario_hash"] = identity
        return result

    def _write_probe(self, args: dict) -> dict:
        name = _text(args["name"], "name", 64)
        if not _PROBE_NAME.fullmatch(name):
            raise ValueError("invalid probe name")
        scenario = args["scenario"]
        if not isinstance(scenario, dict):
            raise ValueError("scenario must be an object")
        self._probe_structure(scenario)
        if len(json.dumps(scenario, ensure_ascii=False).encode()) > MAX_PROBE_BYTES:
            raise ValueError("probe exceeds 64 KiB")
        # JSON round-tripping copies nested structures and rejects shared Python
        # object tricks; native model arguments are ordinary JSON already.
        scenario = json.loads(json.dumps(scenario, ensure_ascii=False))
        if any(key in scenario for key in ("include", "extends", "$include")):
            raise ValueError("probe file includes are unsupported; use the documented schema")
        if isinstance(scenario.get("faults"), str):
            scenario["faults"] = self._probe_fault_path(scenario["faults"])
        elif scenario.get("faults") is not None and not isinstance(scenario.get("faults"), dict):
            raise ValueError("probe faults must be a mapping or a case scenario file")
        payload = yaml.safe_dump(scenario, sort_keys=False, allow_unicode=True).encode()
        if len(payload) > MAX_PROBE_BYTES:
            raise ValueError("serialized probe exceeds 64 KiB")
        directory = self._probe_root()
        target = directory / (name + ".yaml")
        if target.exists() or target.is_symlink():
            raise ValueError("probe already exists; use a new name to preserve validation evidence")
        count = 0
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.endswith((".yaml", ".yml", ".json")):
                    count += 1
                    if count >= MAX_PROBES:
                        raise ValueError("probe count limit reached")
        descriptor, filename = tempfile.mkstemp(prefix=".probe-", dir=directory)
        temporary = Path(filename)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            # Exclusive atomic publication also protects against an accidental
            # concurrent writer reusing a validated probe's name.
            os.link(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return {"probe": "probe:" + name, "path": str(target), "bytes": len(payload),
                "probe_hash": hashlib.sha256(payload).hexdigest(),
                "source_hash": self.controller.sources.candidate_hash,
                "faults": scenario.get("faults"), "probe_count": count + 1}

    def _apply_edits(self, args: dict) -> dict:
        base = _text(args["base_hash"], "base_hash", 64)
        if not _HASH.fullmatch(base):
            raise ValueError("base_hash must be a SHA-256 source hash")
        edits = args["edits"]
        if not isinstance(edits, list) or not 1 <= len(edits) <= 30:
            raise ValueError("edits must contain one to thirty edits")
        for edit in edits:
            if not isinstance(edit, dict) or set(edit) - {"path", "content", "old", "new", "delete"}:
                raise ValueError("invalid edit fields")
            _text(edit.get("path"), "edit path")
            fields = set(edit) - {"path"}
            if fields == {"content"}:
                _text(edit["content"], "edit content", 131072, empty=True)
            elif fields == {"old", "new"}:
                if _text(edit["old"], "old", 131072, empty=True) == "":
                    raise ValueError("old replacement text must be nonempty")
                _text(edit["new"], "new", 131072, empty=True)
            elif fields != {"delete"} or edit["delete"] is not True:
                raise ValueError("edit must use exactly content, old/new, or delete:true")
        return self.controller.sources.apply_edits(base, edits)

    def _reset_candidate(self, args: dict) -> dict:
        return {"source_hash": self.controller.sources.reset_candidate(), "reset": True}

    def _record_hypothesis(self, args: dict) -> dict:
        identifier = _text(args["id"], "hypothesis id", 64)
        if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier) is None:
            raise ValueError("invalid hypothesis id")
        clean = {
            "id": identifier, "obligation": _text(args["obligation"], "obligation", 2000),
            "mechanism": _text(args["mechanism"], "mechanism", 4000),
            "evidence": _texts(args["evidence"], "evidence", 20, 3000),
            "status": _choice(args.get("status", "open"), ("open", "proposed"), "status"),
        }
        return self.controller.record_hypothesis(clean)

    def _validate(self, args: dict) -> dict:
        invariants = _texts(args["target_invariants"], "target_invariants", 12, 100, 1)
        reason = _text(args["reason"], "reason", 6000)
        reproductions = args["reproductions"]
        if not isinstance(reproductions, list) or not 1 <= len(reproductions) <= 12:
            raise ValueError("provide one to twelve reproductions")
        clean = []
        for reproduction in reproductions:
            if not isinstance(reproduction, dict) or set(reproduction) - {"scenario", "seed", "faults"} or not {"scenario", "seed"} <= set(reproduction):
                raise ValueError("each reproduction needs scenario/seed and optional faults")
            clean.append({
                "scenario": self._scenario(reproduction["scenario"]),
                "seed": _int(reproduction["seed"], "seed", 2**63 - 1, 0),
                "faults": self._faults(reproduction.get("faults")),
            })
        return self.controller.validate({"target_invariants": invariants,
                                         "reproductions": clean, "reason": reason})
