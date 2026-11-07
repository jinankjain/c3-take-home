"""Bounded source evidence for an independent review; never import case code."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

MAX_FILES = 64
MAX_FILE_BYTES = 128 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 512
MAX_DEPTH = 8
MAX_SYMBOLS = 1024
MAX_BLOCKS = 40
MAX_OMISSIONS = 12
MAX_DIFF_CHARS = 64_000
MAX_HYPOTHESIS_CHARS = 64_000
MAX_LINKS = 32
IGNORED_CALLS = frozenset({
    "str", "int", "float", "bool", "len", "range", "list", "dict", "set", "tuple",
    "sorted", "min", "max", "isinstance", "get", "items", "keys", "values", "pop",
    "append", "add", "discard", "update", "log", "sleep", "rpc", "spawn", "print",
})
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PATH = re.compile(r"(?<![\w./-])((?:src/)?[A-Za-z_][\w./-]*\.py)(?::([1-9][0-9]{0,6}))?")
_CALL = re.compile(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\(")
_QUOTED = re.compile(r"['\"]([A-Za-z_]\w{0,79})['\"]")


@dataclass
class _Symbol:
    path: str
    name: str
    start: int
    end: int
    calls: set[str] = field(default_factory=set)
    fields: set[str] = field(default_factory=set)
    messages: set[str] = field(default_factory=set)

    @property
    def key(self) -> tuple[str, int]:
        return self.path, self.start

    @property
    def short_name(self) -> str:
        return self.name.rsplit(".", 1)[-1]


@dataclass
class _Document:
    path: str
    text: str
    sha256: str
    lines: list[str]
    symbols: list[_Symbol]
    imports: set[str]
    parsed: bool


def _safe_path(value: str) -> str | None:
    if value.startswith(("a/", "b/")):
        value = value[2:]
    path = PurePosixPath(value)
    if (not value.startswith("src/") or path.is_absolute() or ".." in path.parts
            or "\\" in value or "\x00" in value or len(value) > 512 or path.suffix != ".py"):
        return None
    return str(path)


def _diff_changes(text: str) -> tuple[dict[str, set[int]], set[str], bool]:
    changes: dict[str, set[int]] = {}
    fields: set[str] = set()
    current = None
    at = None
    old_left = new_left = 0
    clipped = len(text) > MAX_DIFF_CHARS
    for line in text[:MAX_DIFF_CHARS].splitlines():
        if at is not None and line.startswith(("+", "-", " ")):
            if current is not None and line.startswith(("+", "-")):
                changes.setdefault(current, set()).add(at)
                fields.update(_QUOTED.findall(line[1:]))
                fields.update(re.findall(r"\.([A-Za-z_]\w{0,79})", line[1:]))
                fields.update(re.findall(r"^\s*([A-Z_][A-Z0-9_]{1,79})\s*(?::[^=]+)?=", line[1:]))
            if line.startswith(("+", " ")):
                at += 1
                new_left -= 1
            if line.startswith(("-", " ")):
                old_left -= 1
            if old_left <= 0 and new_left <= 0:
                at = None
        elif line.startswith(("diff --git ", "--- ")):
            current = at = None
        elif line.startswith("+++ "):
            raw = line[4:].split("\t", 1)[0].strip()
            if raw.startswith('"'):
                try:
                    raw = json.loads(raw)
                except (ValueError, TypeError):
                    raw = ""
            current = _safe_path(raw) if isinstance(raw, str) else None
            at = None
        elif line.startswith("@@ "):
            match = re.match(r"@@ -([0-9]{1,9})(?:,([0-9]{1,9}))? \+([0-9]{1,9})(?:,([0-9]{1,9}))? @@", line)
            at = max(1, int(match.group(3))) if match else None
            if match:
                old_left = int(match.group(2)) if match.group(2) is not None else 1
                new_left = int(match.group(4)) if match.group(4) is not None else 1
            else:
                clipped = True
        if line.startswith(("[diff truncated", "... [truncated", "... diff truncated")):
            clipped = True
    return changes, fields, clipped


def _hypothesis_references(hypotheses: list[dict]) -> tuple[list[tuple[str, int | None]], set[str], set[str], bool]:
    fragments = []
    remaining = MAX_HYPOTHESIS_CHARS
    clipped = len(hypotheses) > 32
    for item in hypotheses[:32]:
        if not isinstance(item, dict):
            clipped = True
            continue
        for key in ("obligation", "mechanism", "evidence"):
            value = item.get(key)
            values = value[:8] if isinstance(value, list) else [value]
            if isinstance(value, list) and len(value) > 8:
                clipped = True
            for fragment in values:
                if not isinstance(fragment, str):
                    continue
                part = fragment[:min(4000, remaining)]
                clipped |= len(part) < len(fragment)
                fragments.append(part)
                remaining -= len(part)
                if remaining <= 0:
                    return _references("\n".join(fragments), True)
    return _references("\n".join(fragments), clipped)


def _references(text: str, clipped: bool):
    paths = list(dict.fromkeys((match.group(1), int(match.group(2)) if match.group(2) else None)
                               for match in _PATH.finditer(text)))[:64]
    symbols = {name for name in _CALL.findall(text) if name.rsplit(".", 1)[-1] not in IGNORED_CALLS}
    words = set(_IDENTIFIER.findall(text))
    return paths, symbols, words, clipped


def _module_name(path: str) -> str:
    parts = list(PurePosixPath(path).parts[1:])
    parts[-1] = parts[-1][:-3]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _analyse(path: str, text: str, stopped) -> tuple[list[_Symbol], set[str], bool]:
    try:
        tree = ast.parse(text, filename=path)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return [], set(), False
    imports: set[str] = set()
    package = list(PurePosixPath(path).parts[1:-1])
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = package[:max(0, len(package) - node.level + 1)] if node.level else []
            module = ".".join([*prefix, *(node.module or "").split(".")]).strip(".")
            if module:
                imports.add(module)
    symbols = []
    pending = [(node, "") for node in reversed(tree.body)]
    while pending and len(symbols) < MAX_SYMBOLS and not stopped():
        node, prefix = pending.pop()
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            name = prefix + node.name
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                symbol = _Symbol(path, name, start, node.end_lineno or node.lineno)
                todo = [node]
                visited = 0
                while todo and not stopped():
                    child = todo.pop()
                    if child is not node and isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    visited += 1
                    if visited > MAX_FILE_BYTES:
                        break
                    if isinstance(child, ast.Call):
                        if isinstance(child.func, ast.Name):
                            symbol.calls.add(child.func.id)
                        elif isinstance(child.func, ast.Attribute):
                            symbol.calls.add(child.func.attr)
                        symbol.messages.update(arg.value for arg in child.args
                                               if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                                               and _IDENTIFIER.fullmatch(arg.value) and len(arg.value) <= 80)
                    if isinstance(child, ast.Attribute):
                        symbol.fields.add(child.attr)
                    elif isinstance(child, ast.Constant) and isinstance(child.value, str):
                        if len(child.value) <= 80 and _IDENTIFIER.fullmatch(child.value):
                            symbol.fields.add(child.value)
                    elif isinstance(child, ast.Name):
                        symbol.fields.add(child.id)
                    todo.extend(ast.iter_child_nodes(child))
                symbol.calls.difference_update(IGNORED_CALLS)
                symbols.append(symbol)
            pending.extend((child, name + ".") for child in reversed(node.body))
        elif isinstance(node, (ast.If, ast.Try, ast.With)):
            pending.extend((child, prefix) for child in reversed(list(ast.iter_child_nodes(node))))
    return symbols, imports, not stopped() and not pending


class _Reader:
    def __init__(self, root: Path, stopped):
        self.root = root
        self.stopped = stopped
        self.documents: dict[str, _Document] = {}
        self.attempted: set[str] = set()
        self.omissions: list[dict] = []
        self.omitted_count = 0
        self.stats = {"files_read": 0, "bytes_read": 0, "entries_seen": 0, "parsed_files": 0,
                      "excluded_links": 0, "nonregular_files": 0}
        self.src_fd = None

    def omit(self, reason: str, path: str | None = None, symbol: str | None = None):
        self.omitted_count += 1
        if len(self.omissions) < MAX_OMISSIONS:
            item = {"reason": reason}
            if path is not None:
                item["path"] = path[:512]
            if symbol is not None:
                item["symbol"] = symbol[:256]
            self.omissions.append(item)

    def __enter__(self):
        if not self.stopped():
            try:
                self.src_fd = os.open(self.root / "src", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            except OSError:
                self.omit("source_root_unavailable", "src")
        return self

    def __exit__(self, *_):
        if self.src_fd is not None:
            os.close(self.src_fd)

    def read(self, path: str) -> _Document | None:
        if path in self.attempted:
            return self.documents.get(path)
        safe = _safe_path(path)
        if safe is None:
            self.omit("unsafe_source_path", path)
            return None
        if self.src_fd is None or self.stopped():
            return None
        if self.stats["files_read"] >= MAX_FILES:
            self.omit("file_limit", path)
            return None
        self.attempted.add(path)
        parts = PurePosixPath(safe).parts[1:]
        descriptors = []
        parent = self.src_fd
        fd = None
        try:
            if len(parts) > MAX_DEPTH + 1:
                self.omit("directory_depth_limit", path)
                return None
            for part in parts[:-1]:
                parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                descriptors.append(parent)
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                self.stats["nonregular_files"] += 1
                self.omit("nonregular_source", path)
                return None
            if info.st_size > MAX_FILE_BYTES or self.stats["bytes_read"] + info.st_size > MAX_TOTAL_BYTES:
                self.omit("source_byte_limit", path)
                return None
            self.stats["files_read"] += 1
            data = bytearray()
            while not self.stopped() and len(data) <= MAX_FILE_BYTES:
                available = MAX_TOTAL_BYTES - self.stats["bytes_read"]
                if available <= 0:
                    self.omit("source_byte_limit", path)
                    return None
                chunk = os.read(fd, min(8192, MAX_FILE_BYTES + 1 - len(data), available))
                if not chunk:
                    break
                data.extend(chunk)
                self.stats["bytes_read"] += len(chunk)
            if self.stopped():
                return None
            if len(data) > MAX_FILE_BYTES:
                self.omit("source_byte_limit", path)
                return None
            try:
                text = data.decode("utf-8")
            except UnicodeError:
                self.omit("source_encoding", path)
                return None
            symbols, imports, parsed = _analyse(path, text, self.stopped)
            self.stats["parsed_files"] += int(parsed)
            if not parsed and not self.stopped():
                self.omit("source_parse_incomplete", path)
            doc = _Document(path, text, hashlib.sha256(data).hexdigest(), text.splitlines(keepends=True), symbols, imports, parsed)
            self.documents[path] = doc
            return doc
        except OSError:
            self.omit("source_unreadable_or_link", path)
            return None
        finally:
            if fd is not None:
                os.close(fd)
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def inventory(self):
        def walk(fd: int, prefix: str, depth: int):
            if self.stopped():
                return
            available = MAX_ENTRIES - self.stats["entries_seen"]
            if available <= 0:
                self.omit("inventory_entry_limit", prefix)
                return
            entries = []
            with os.scandir(fd) as iterator:
                for entry in iterator:
                    if self.stopped():
                        return
                    self.stats["entries_seen"] += 1
                    if len(entries) >= available:
                        self.omit("inventory_entry_limit", prefix)
                        return  # Never select an order-dependent partial directory.
                    entries.append(entry)
            for entry in sorted(entries, key=lambda item: item.name):
                if self.stopped():
                    return
                path = prefix + "/" + entry.name
                try:
                    mode = entry.stat(follow_symlinks=False).st_mode
                    if stat.S_ISLNK(mode):
                        self.stats["excluded_links"] += 1
                        self.omit("source_symlink", path)
                    elif stat.S_ISDIR(mode):
                        if depth >= MAX_DEPTH:
                            self.omit("directory_depth_limit", path)
                            continue
                        child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                        try:
                            walk(child, path, depth + 1)
                        finally:
                            os.close(child)
                    elif path.endswith(".py"):
                        if stat.S_ISREG(mode):
                            self.read(path)
                        else:
                            self.stats["nonregular_files"] += 1
                            self.omit("nonregular_source", path)
                except OSError:
                    self.omit("inventory_unreadable", path)
        if self.src_fd is not None:
            walk(self.src_fd, "src", 0)


def _block(doc: _Document, symbol: _Symbol | None, role: str, reason: str, *, at: int | None = None) -> dict:
    start, end = (symbol.start, symbol.end) if symbol else (1, len(doc.lines))
    if symbol is None and len(doc.text) > 2000:
        center = at or 1
        start, end = max(1, center - 8), min(len(doc.lines), center + 22)
    return {"path": doc.path, "start": start, "end": end, "symbol": symbol.name if symbol else "<module>",
            "role": role, "reason": reason, "complete": symbol is not None or (start == 1 and end == len(doc.lines)),
            "text": "".join(doc.lines[start - 1:end]), "sha256": doc.sha256}


def _excerpt(block: dict, doc: _Document, changed: set[int], allowance: int) -> dict | None:
    if allowance < 500:
        return None
    low, high = block["start"], block["end"]
    focus = next((line for line in sorted(changed) if low <= line <= high), low)
    segments = [(low, min(high, low + 3)), (max(low, focus - 4), min(high, focus + 8))]
    if segments[1][0] <= segments[0][1] + 1:
        segments = [(segments[0][0], max(segments[0][1], segments[1][1]))]
    while segments:
        text = "\n".join(f"# Source lines {start}-{end} (partial)\n" + "".join(doc.lines[start - 1:end])
                         for start, end in segments)
        result = {**block, "complete": False, "text": text, "segments": [{"start": a, "end": b} for a, b in segments],
                  "omitted_lines": high - low + 1 - sum(b - a + 1 for a, b in segments)}
        if len(json.dumps(result, ensure_ascii=False)) <= allowance:
            return result
        if len(segments) > 1 and segments[-1][1] > segments[-1][0]:
            segments[-1] = (segments[-1][0], segments[-1][1] - 1)
        elif segments[0][1] > segments[0][0]:
            segments[0] = (segments[0][0], segments[0][1] - 1)
        else:
            return None
    return None


def review_source_context(candidate_root: Path, diff_text: str, hypotheses: list[dict],
                          max_chars: int = 24_000, stop_event=None) -> dict:
    """Return current source bodies and related consumers with explicit coverage.

    The limit applies to the complete serialized result, including provenance and
    omissions. Files are read through descriptors anchored to a nonsymlink src
    directory; Python is parsed with ast, never executed.
    """
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or not 256 <= max_chars <= 64_000:
        raise ValueError("max_chars must be an integer between 256 and 64000")
    if not isinstance(diff_text, str):
        raise ValueError("diff_text must be text")
    stopped = (lambda: stop_event.is_set()) if stop_event is not None else (lambda: False)
    hypotheses = hypotheses if isinstance(hypotheses, list) else []
    changes, changed_fields, diff_clipped = _diff_changes(diff_text)
    citations, named_symbols, words, hypotheses_clipped = _hypothesis_references(hypotheses)
    with _Reader(Path(candidate_root), stopped) as reader:
        if diff_clipped:
            reader.omit("diff_input_truncated")
        if hypotheses_clipped:
            reader.omit("hypotheses_truncated")
        for path in sorted(changes):
            reader.read(path)
        for path, _ in citations:
            if path.startswith("src/"):
                reader.read(path)
        reader.inventory()
        docs = reader.documents
        indexed_count = sum(len(doc.symbols) for doc in docs.values())
        symbols = [symbol for path in sorted(docs) for symbol in docs[path].symbols][:MAX_SYMBOLS]
        reader.stats.update(symbols_indexed=len(symbols), symbols_omitted=max(0, indexed_count - len(symbols)))
        if indexed_count > len(symbols):
            reader.omit("symbol_index_limit")
        candidates: dict[tuple[str, int], tuple[int, int, _Symbol | None, str, str, int | None]] = {}
        seeds = []

        def offer(symbol, priority: int, score: int, role: str, reason: str, path=None, at=None):
            path = symbol.path if symbol else path
            key = (path, symbol.start if symbol else -(at or 1))
            item = (priority, -score, symbol, role, reason, at)
            previous = candidates.get(key)
            if previous is None or item[:2] < previous[:2]:
                candidates[key] = item

        for path, lines in sorted(changes.items()):
            doc = docs.get(path)
            if doc is None:
                continue
            offer(None, 1, 0, "module_context", "imports and module definitions", path=path)
            for line in sorted(lines):
                enclosing = [s for s in doc.symbols if s.start <= line <= s.end]
                if enclosing:
                    symbol = min(enclosing, key=lambda s: (s.end - s.start, s.start))
                    offer(symbol, 0, 0, "changed", "complete function containing changed source")
                    if symbol not in seeds:
                        seeds.append(symbol)
                else:
                    offer(None, 0, 0, "changed_module", "module-level change or unparsed source", path=path, at=line)

        for reference, line in citations:
            matches = [path for path in docs if path == reference or path.endswith("/" + reference)]
            if len(matches) != 1:
                reader.omit("citation_unavailable_or_ambiguous", reference)
                continue
            path = matches[0]
            choices = docs[path].symbols
            if line is not None:
                choices = [s for s in choices if s.start <= line <= s.end]
            else:
                choices = [s for s in choices if s.name in named_symbols or s.short_name in named_symbols
                           or s.short_name in words or s.fields.intersection(words)]
                choices.sort(key=lambda s: (-len(s.fields.intersection(words)), s.start))
                choices = choices[:3]
            if choices:
                for symbol in choices:
                    offer(symbol, 2, 1, "cited", "source cited by the repair hypothesis")
            else:
                offer(None, 3, 0, "cited_module", "cited source has no resolved function", path=path, at=line)

        frontier = seeds[:MAX_LINKS]
        reader.stats.update(graph_seeds=len(frontier), graph_seeds_omitted=max(0, len(seeds) - len(frontier)))
        if len(seeds) > len(frontier):
            reader.omit("call_graph_seed_limit")
        visited = {s.key for s in seeds}
        modules = {path: _module_name(path) for path in docs}
        changed_modules = {modules[path] for path in docs if path in changes}
        for peer in symbols:
            if peer.path not in changes and peer.fields.intersection(changed_fields):
                imported = bool(changed_modules.intersection(docs[peer.path].imports))
                if imported:
                    offer(peer, 3, 6, "field_consumer", "imports and consumes a changed source field")
        for depth in range(2):
            related = []
            for target in frontier:
                for peer in symbols:
                    if stopped():
                        break
                    if peer.key in visited:
                        continue
                    same_file = peer.path == target.path
                    same_class = same_file and peer.name.rsplit(".", 1)[0] == target.name.rsplit(".", 1)[0]
                    target_module, peer_module = modules[target.path], modules[peer.path]
                    imported = target_module in docs[peer.path].imports or peer_module in docs[target.path].imports
                    shared = peer.fields.intersection(changed_fields)
                    messages = peer.messages.intersection(target.messages)
                    callee = peer.short_name in target.calls and (same_file or imported)
                    caller = target.short_name in peer.calls and (same_file or imported)
                    if callee or caller:
                        score = 8 + int(same_class) * 2 + int(imported)
                        role = "helper" if callee else "caller"
                        reason = "direct call to/from " + target.name
                    elif depth == 0 and messages and shared:
                        score, role, reason = 12 + min(3, len(shared)), "protocol_peer", "shared message and changed protocol field"
                    elif depth == 0 and shared and not same_file:
                        score, role, reason = 3 + min(3, len(shared)), "field_consumer", "unchanged consumer of changed protocol field"
                    else:
                        continue
                    priority = 2 + depth if score >= 7 else 4
                    offer(peer, priority, score, role, reason)
                    related.append((priority, -score, peer.path, peer.start, peer))
            related.sort(key=lambda item: item[:4])
            frontier = []
            for _, _, _, _, peer in related:
                if peer.key not in visited:
                    visited.add(peer.key)
                    frontier.append(peer)
                if len(frontier) >= MAX_LINKS:
                    break

        output = {"blocks": [], "stats": dict(reader.stats), "omissions": [], "omitted_count": 0,
                  "truncated": False, "cancelled": bool(stopped())}

        def metadata():
            output.update(omissions=list(reader.omissions), omitted_count=reader.omitted_count,
                          truncated=bool(reader.omitted_count or any(not b["complete"] for b in output["blocks"])),
                          cancelled=bool(stopped()))

        ordered = sorted(candidates.items(), key=lambda entry: (entry[1][0], entry[1][1], entry[0][0], entry[0][1]))
        for (path, _), (priority, _, symbol, role, reason, at) in ordered:
            if stopped():
                break
            block = _block(docs[path], symbol, role, reason, at=at)
            metadata()
            if len(output["blocks"]) < MAX_BLOCKS:
                trial = {**output, "blocks": [*output["blocks"], block]}
                if len(json.dumps(trial, ensure_ascii=False)) <= max_chars - 800:
                    output["blocks"].append(block)
                    continue
                if priority == 0:
                    allowance = max_chars - len(json.dumps(output, ensure_ascii=False)) - 1000
                    partial = _excerpt(block, docs[path], changes.get(path, set()), allowance)
                    if partial is not None:
                        output["blocks"].append(partial)
                        reader.omit("function_excerpt", path, symbol.name if symbol else None)
                        continue
            reader.omit("context_output_limit", path, symbol.name if symbol else None)
        metadata()
        while len(json.dumps(output, ensure_ascii=False)) > max_chars:
            if output["omissions"]:
                output["omissions"].pop()
            elif output["blocks"]:
                output["blocks"].pop()
                output["omitted_count"] += 1
            else:
                output = {"blocks": [], "omitted_count": reader.omitted_count, "truncated": True,
                          "cancelled": bool(stopped())}
                break
            output["truncated"] = True
        return output
