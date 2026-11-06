"""Bounded file tools and corpus retrieval, without an embedding service.

Corpus metadata is discovered from INDEX.md and document headers. Service and
platform documents are small enough to inspect locally; background proceedings
are read only when explicitly requested and are never indexed in full.
"""

from __future__ import annotations

import fnmatch
import math
import os
from pathlib import Path
import re
import stat
from dataclasses import dataclass
from typing import Iterator


MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_SCAN_BYTES = 8 * 1024 * 1024
MAX_FILES = 512
MAX_ENTRIES = 8192
MAX_OUTPUT_CHARS = 48_000
MAX_QUERY_CHARS = 512
MAX_METADATA_BYTES = 16_384
MAX_CACHE_CHARS = 8 * 1024 * 1024
SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "scratch", "_scratch", ".fixer", ".fixer-work", "secrets",
}
SECRET_NAMES = {".env", "credentials", "credentials.json", "secrets.json", "secrets.yaml"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".pem", ".key", ".p12", ".pfx"}
DOC_SUFFIXES = {".md", ".txt", ".rst"}
STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "by", "for", "from",
    "has", "have", "in", "is", "it", "its", "of", "on", "or", "that", "the",
    "their", "this", "to", "was", "were", "with", "after", "before", "during",
    "incident", "problem", "reported", "symptom", "src", "logs", "py", "md",
}


def _bounded_int(value: int, maximum: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return min(value, maximum)


def _query(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("query must be nonempty")
    if len(value) > MAX_QUERY_CHARS:
        raise ValueError(f"query must be at most {MAX_QUERY_CHARS} characters")
    return value.strip()


def _excluded(parts: tuple[str, ...]) -> bool:
    return any(
        part in SKIP_DIRS or part in SECRET_NAMES or part.startswith(".env.")
        or part.startswith(".c3-fixer-")
        or Path(part).suffix.lower() in SKIP_SUFFIXES
        for part in parts
    )


def _safe_regex(query: str) -> re.Pattern:
    """Only fixed-width alternatives: no repeated or nested branching groups.

    This deliberately small grammar has bounded matching work. Python's regex
    engine has no deadline parameter, so arbitrary model-written expressions
    must never run in the controller process.
    """
    body = query[1:] if query.startswith("^") else query
    if body.endswith("$") and not body.endswith("\\$"):
        body = body[:-1]
    if body.startswith("(") and body.endswith(")"):
        body = body[1:-1]
    if not body or body.count("|") > 24:
        raise ValueError("regex needs 1-25 nonempty fixed-width alternatives")
    offset = 0
    while offset < len(body):
        char = body[offset]
        if char == "\\":
            offset += 1
            if offset >= len(body) or body[offset] not in r"\.^$|()[]{}*+?":
                raise ValueError("regex escapes must identify literal punctuation")
        elif char in "*+?{}[]()^$":
            raise ValueError("regex supports fixed-width alternatives and outer anchors; use literal search for other patterns")
        offset += 1
    try:
        return re.compile(query)
    except re.error as error:
        raise ValueError(f"invalid regular expression: {error}") from error


class FileTools:
    """Relative, read-only access to a root; symlinks are not followed."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("file root must be a directory")

    def _resolve(self, path: str | Path) -> Path:
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("path must stay inside the tool root")
        if _excluded(relative.parts):
            raise ValueError("path is excluded from file tools")
        candidate = self.root / relative
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("symlink paths are not allowed")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(self.root):
            raise ValueError("path must stay inside the tool root")
        return resolved

    def _files(self, path: str = ".", maximum: int = MAX_FILES) -> Iterator[Path]:
        target = self._resolve(path)
        if target.is_file():
            yield target
            return
        if not target.is_dir():
            return
        pending = [target]
        seen = yielded = 0
        while pending and yielded < maximum and seen < MAX_ENTRIES:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                batch = []
                for entry in entries:
                    seen += 1
                    if seen > MAX_ENTRIES:
                        break
                    if entry.is_symlink():
                        continue
                    relative = Path(entry.path).relative_to(self.root)
                    if _excluded(relative.parts):
                        continue
                    batch.append(entry)
            directories = []
            for entry in sorted(batch, key=lambda item: item.name):
                if entry.is_dir(follow_symlinks=False):
                    directories.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    yield Path(entry.path)
                    yielded += 1
                    if yielded >= maximum:
                        return
            pending.extend(reversed(directories))

    def _text(self, path: Path, max_bytes: int = MAX_FILE_BYTES) -> tuple[str, bool]:
        # Check the resolved path again, and reject nonregular files (e.g. FIFOs).
        checked = self._resolve(path.relative_to(self.root))
        if not stat.S_ISREG(checked.stat().st_mode):
            raise ValueError("only regular files can be read")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(checked, flags)
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("only regular files can be read")
            data = source.read(max_bytes)
        if b"\x00" in data[:4096]:
            raise ValueError("binary files cannot be read as text")
        return data.decode("utf-8", errors="replace"), metadata.st_size > len(data)

    def list_files(self, pattern: str = "*", limit: int = 200) -> list[str]:
        limit = _bounded_int(limit, MAX_FILES, "limit")
        if not isinstance(pattern, str) or len(pattern) > MAX_QUERY_CHARS:
            raise ValueError("invalid file pattern")
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise ValueError("pattern must stay inside the tool root")
        matches = [
            path.relative_to(self.root).as_posix() for path in self._files()
            if fnmatch.fnmatchcase(path.relative_to(self.root).as_posix(), pattern)
        ]
        return sorted(matches)[:limit]

    def read(
        self, path: str, start: int = 1, end: int | None = None,
        max_chars: int = 16_000,
    ) -> dict:
        start = _bounded_int(start, 1_000_000, "start")
        max_chars = _bounded_int(max_chars, MAX_OUTPUT_CHARS, "max_chars")
        if end is not None and (isinstance(end, bool) or not isinstance(end, int) or end < start):
            raise ValueError("end must be at least start")
        target = self._resolve(path)
        # An explicit line request may lie beyond the normal document prefix.
        # Keep the scan bounded while allowing navigation through larger files.
        text, byte_truncated = self._text(target, MAX_SCAN_BYTES)
        lines = text.splitlines(keepends=True)
        requested_end = len(lines) if end is None else end
        stop = min(requested_end, len(lines), start + 4095)
        selected = "".join(lines[start - 1:stop])
        output = selected[:max_chars]
        returned_lines = len(output.splitlines())
        result = {
            "path": target.relative_to(self.root).as_posix(), "start": start,
            "end": start + returned_lines - 1, "text": output,
            "truncated": byte_truncated or len(selected) > max_chars or stop < requested_end,
        }
        if byte_truncated and start > len(lines):
            result["error"] = "requested line lies beyond the bounded file scan"
        return result

    def search(
        self, query: str, path: str = "src", mode: str = "literal", limit: int = 30,
    ) -> list[dict]:
        query = _query(query)
        limit = _bounded_int(limit, 200, "limit")
        if mode == "literal":
            matches = lambda line: query in line
        elif mode == "regex":
            expression = _safe_regex(query)
            matches = lambda line: expression.search(line[:4096]) is not None
        else:
            raise ValueError("mode must be literal or regex")
        results = []
        remaining_bytes = MAX_SCAN_BYTES
        remaining_chars = MAX_OUTPUT_CHARS
        for target in sorted(self._files(path)):
            if remaining_bytes <= 0 or remaining_chars <= 0:
                break
            read_bytes = min(MAX_FILE_BYTES, remaining_bytes)
            try:
                text, truncated = self._text(target, read_bytes)
            except (OSError, ValueError):
                continue
            remaining_bytes -= min(target.stat().st_size, read_bytes)
            for number, line in enumerate(text.splitlines(), 1):
                if not matches(line):
                    continue
                cap = min(1500, remaining_chars)
                if mode == "literal":
                    position = line.find(query)
                else:
                    found = expression.search(line[:4096])
                    position = found.start() if found is not None else 0
                begin = max(0, position - cap // 3)
                excerpt = line[begin:begin + cap]
                results.append({
                    "path": target.relative_to(self.root).as_posix(), "start": number,
                    "end": number, "text": excerpt,
                    "truncated": truncated or len(line) > len(excerpt),
                })
                remaining_chars -= len(excerpt)
                if len(results) >= limit or remaining_chars <= 0:
                    return results
        return results


def _terms(text: str) -> set[str]:
    tokens = re.findall(r"[a-z][a-z0-9_-]{1,79}", text.lower())
    words = set(tokens)
    for token in tokens:
        words.update(re.split(r"[_-]", token))
    normalized = set()
    for word in words:
        if word in STOP_WORDS or len(word) < 2:
            continue
        if word.startswith("cancell"):
            word = "cancel"
        elif word.startswith("releas"):
            word = "release"
        elif word in {"retries", "retrying", "retried"}:
            word = "retry"
        elif word.endswith("s") and len(word) > 4 and not word.endswith("ss"):
            word = word[:-1]
        normalized.add(word)
    return normalized


def _status_priority(status: str) -> int:
    return {"CURRENT": 0, "UNKNOWN": 1, "SUPERSEDED": 2}[status]


@dataclass
class _Document:
    path: str
    text: str
    status: str
    title: str
    truncated: bool = False
    byte_limit: int = 0


class Corpus:
    def __init__(self, corpus_root: Path, codebase: str | None = None):
        self.files = FileTools(corpus_root)
        self.root = self.files.root
        self.codebase = str(codebase) if codebase is not None else None
        self._cache: dict[str, _Document] = {}
        self._cache_chars = 0
        self._indexed_status: dict[str, str] = {}
        try:
            self.index = self.files.read("INDEX.md", max_chars=16_000)["text"]
        except (OSError, ValueError):
            self.index = ""
        for line in self.index.splitlines():
            status = self._header_status(line)
            if status == "UNKNOWN":
                continue
            for reference in re.findall(r"(?:platform|service|background)/[\w./-]+", line):
                self._indexed_status[reference.rstrip("/.")] = status

    @staticmethod
    def _header_status(text: str) -> str:
        upper = text.upper()
        if re.search(r"\bSUPERSEDED\b", upper):
            return "SUPERSEDED"
        if re.search(r"\bCURRENT\b", upper):
            return "CURRENT"
        return "UNKNOWN"

    def _make_document(self, path: Path, text: str, truncated: bool, max_bytes: int) -> _Document:
        relative = path.relative_to(self.root).as_posix()
        header_lines = text.splitlines()[:12]
        cleaned_headers = [line.strip().lstrip("> ").replace("**", "").replace("__", "") for line in header_lines]
        explicit = next((line for line in cleaned_headers if re.match(r"Status\s*:", line, re.I)), "")
        markers = "\n".join(
            line for line in cleaned_headers
            if re.match(r"(?:CURRENT|SUPERSEDED)\b", line, re.I)
            or (line.startswith("#") and re.search(r"\((?:CURRENT|SUPERSEDED)\)", line, re.I))
        )
        status = self._header_status(explicit or markers)
        index_status = self._indexed_status.get(relative, "UNKNOWN")
        if "SUPERSEDED" in {status, index_status}:
            status = "SUPERSEDED"
        elif index_status == "CURRENT":
            status = "CURRENT"
        title = next((line.lstrip("# ") for line in text.splitlines() if line.startswith("#")), path.stem)
        return _Document(relative, text, status, title, truncated, max_bytes)

    def _document(self, path: Path, max_bytes: int = MAX_FILE_BYTES) -> _Document:
        relative = path.relative_to(self.root).as_posix()
        cached = self._cache.get(relative)
        if cached is not None and (not cached.truncated or cached.byte_limit >= max_bytes):
            return cached
        text, truncated = self.files._text(path, max_bytes)
        document = self._make_document(path, text, truncated, max_bytes)
        if not relative.startswith("background/") and len(text) <= MAX_CACHE_CHARS:
            previous = self._cache.pop(relative, None)
            if previous is not None:
                self._cache_chars -= len(previous.text)
            while self._cache and self._cache_chars + len(text) > MAX_CACHE_CHARS:
                oldest = next(iter(self._cache))
                self._cache_chars -= len(self._cache.pop(oldest).text)
            self._cache[relative] = document
            self._cache_chars += len(text)
        return document

    def _service_roots(self, query: str = "", byte_budget: int = MAX_SCAN_BYTES) -> tuple[list[str], int]:
        try:
            service = self.files._resolve("service")
        except (OSError, ValueError):
            return [], 0
        roots = sorted(
            path.relative_to(self.root).as_posix() for path in service.iterdir()
            if path.is_dir() and not path.is_symlink() and not _excluded(path.relative_to(self.root).parts)
        )[:64]
        if self.codebase:
            direct = [path for path in roots if Path(path).name.casefold() == self.codebase.casefold()]
            if direct:
                return direct, 0
        if not query or len(roots) <= 1:
            return roots, 0
        terms = _terms(query)
        ranked = []
        remaining = byte_budget
        for root in roots:
            score = len(_terms(root) & terms) * 10
            for path in self.files._files(root, maximum=8):
                if path.suffix.lower() not in DOC_SUFFIXES:
                    continue
                if remaining <= 0:
                    break
                try:
                    allowance = min(MAX_METADATA_BYTES, remaining)
                    text, truncated = self.files._text(path, allowance)
                    remaining -= min(path.stat().st_size, allowance)
                    # Discovery reads small headers/descriptions and never puts
                    # their incomplete prefixes into the full-document cache.
                    doc = self._make_document(path, text, truncated, allowance)
                except (OSError, ValueError):
                    continue
                score += len(_terms(doc.title) & terms) * 5
                score += min(10, len(_terms(doc.text[:4000]) & terms))
            ranked.append((score, root))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        if ranked and ranked[0][0]:
            best = ranked[0][0]
            return [root for score, root in ranked if score >= best * 0.65][:2], byte_budget - remaining
        return roots, byte_budget - remaining

    def _documents(self, root: str, query: str = "") -> list[_Document]:
        if not isinstance(root, str) or root.split("/", 1)[0] not in {"service", "platform", "background"}:
            raise ValueError("corpus search root must be service, platform, or a focused background path")
        if Path(root).is_absolute() or ".." in Path(root).parts or _excluded(Path(root).parts):
            raise ValueError("corpus search root must stay inside the corpus")
        if root.rstrip("/") == "service":
            roots, discovery_bytes = self._service_roots(query, MAX_SCAN_BYTES)
        else:
            roots, discovery_bytes = [root], 0
        documents = []
        remaining = MAX_SCAN_BYTES - discovery_bytes
        maximum = 12 if root.startswith("background") else 192
        for selected in roots:
            try:
                candidates = sorted(self.files._files(selected, maximum=maximum))
            except OSError:
                continue
            if selected.startswith("background"):
                terms = _terms(query)
                candidates.sort(key=lambda path: (-len(_terms(path.name) & terms), path.as_posix()))
            for path in candidates:
                if path.suffix.lower() not in DOC_SUFFIXES or remaining <= 0 or len(documents) >= maximum:
                    continue
                try:
                    allowance = min(MAX_FILE_BYTES, remaining)
                    document = self._document(path, allowance)
                    documents.append(document)
                    remaining -= min(path.stat().st_size, allowance)
                except (OSError, ValueError):
                    continue
        return documents

    @staticmethod
    def _sections(document: _Document) -> Iterator[tuple[int, int, str]]:
        lines = document.text.splitlines(keepends=True)
        start = 0
        for offset, line in enumerate(lines):
            if offset > start and (line.startswith("#") or offset - start >= 90):
                yield start + 1, offset, "".join(lines[start:offset])
                start = offset
        if start < len(lines):
            yield start + 1, len(lines), "".join(lines[start:])

    @staticmethod
    def _score(document: _Document, text: str, terms: set[str]) -> float:
        body_terms = _terms(text)
        matched = terms & (body_terms | _terms(document.path + " " + document.title))
        if not matched:
            return 0.0
        score = len(terms & _terms(document.path + " " + document.title)) * 7.0
        score += len(terms & body_terms) * 3.0
        heading = "\n".join(line for line in text.splitlines() if line.startswith("#"))
        score += len(terms & _terms(heading)) * 5.0
        score /= 1.0 + math.log1p(max(0, len(text) - 1500)) / 20
        return round(score, 3)

    @staticmethod
    def _excerpt(text: str, terms: set[str], cap: int) -> tuple[int, str]:
        """Return a contiguous window that contains the strongest body match."""
        if len(text) <= cap:
            return 0, text
        lines = text.splitlines(keepends=True)
        scores = [len(_terms(line) & terms) for line in lines]
        best = max(range(len(lines)), key=lambda offset: (scores[offset], -offset))
        if scores[best] == 0:
            return 0, text[:cap]
        match_line = lines[best].lower()
        positions = [match_line.find(term) for term in terms if term in match_line]
        pivot = sum(len(line) for line in lines[:best]) + min(positions, default=0)
        begin = max(0, pivot - cap // 3)
        line_begin = text.rfind("\n", 0, begin) + 1
        if pivot - line_begin < cap:
            begin = line_begin
        return text.count("\n", 0, begin), text[begin:begin + cap]

    def search(self, query: str, root: str = "service", limit: int = 12) -> list[dict]:
        query = _query(query)
        limit = _bounded_int(limit, 40, "limit")
        terms = _terms(query)
        if not terms:
            return []
        ranked = []
        for document in self._documents(root, query):
            for start, end, text in self._sections(document):
                score = self._score(document, text, terms)
                if score:
                    ranked.append({
                        "path": document.path, "start": start, "end": end,
                        "status": document.status, "score": score, "text": text,
                        "truncated": document.truncated,
                    })
        ranked.sort(key=lambda item: (_status_priority(item["status"]), -item["score"], item["path"], item["start"]))
        results = []
        per_document: dict[str, int] = {}
        remaining = MAX_OUTPUT_CHARS
        for item in ranked:
            if per_document.get(item["path"], 0) >= 2:
                continue
            offset, excerpt = self._excerpt(item["text"], terms, min(3500, remaining))
            item = {**item, "start": item["start"] + offset, "text": excerpt,
                    "end": item["start"] + offset + len(excerpt.splitlines()) - 1,
                    "truncated": item["truncated"] or len(excerpt) < len(item["text"])}
            results.append(item)
            per_document[item["path"]] = per_document.get(item["path"], 0) + 1
            remaining -= len(excerpt)
            if len(results) >= limit or remaining <= 0:
                break
        return results

    def read(self, path: str, start: int = 1, end: int | None = None, max_chars: int = 16_000) -> dict:
        result = self.files.read(path, start, end, max_chars)
        target = self.files._resolve(path)
        document = self._document(target, max_bytes=MAX_METADATA_BYTES)
        return {**result, "status": document.status}

    def bootstrap(self, query: str, max_chars: int = 40_000) -> str:
        if not isinstance(query, str):
            raise ValueError("bootstrap query must be text")
        # Bootstrap accepts a whole ticket/inventory; search queries remain short.
        terms = _terms(query[:16_000])
        compact_query = " ".join(sorted(terms))[:MAX_QUERY_CHARS] or "service contract"
        max_chars = _bounded_int(max_chars, MAX_OUTPUT_CHARS, "max_chars")
        blocks = []
        remaining = max_chars

        def append(path: str, status: str, text: str, start: int = 1, cap: int = 6000) -> None:
            nonlocal remaining
            heading = f"\n[{path}:{start} | {status}]\n"
            available = min(cap, remaining - len(heading))
            if available <= 0:
                return
            block = heading + text[:available]
            blocks.append(block)
            remaining -= len(block)

        if self.index:
            append("INDEX.md", "INDEX", self.index, cap=min(5000, max_chars // 5))
        service_docs = self._documents("service", compact_query)
        service_docs.sort(key=lambda doc: (_status_priority(doc.status), -self._score(doc, doc.text, terms), doc.path))
        core = []
        for keywords in ({"overview", "design"}, {"operational", "deployment", "constants", "configuration"}):
            candidates = [doc for doc in service_docs if doc.status != "SUPERSEDED"
                          and _terms(doc.path + " " + doc.title) & keywords]
            for doc in candidates[:2]:
                if doc.path not in {existing.path for existing in core}:
                    core.append(doc)
        if not core:
            core = [doc for doc in service_docs if doc.status != "SUPERSEDED"][:2]

        platform_docs = self._documents("platform", compact_query)
        platform_docs = [doc for doc in platform_docs if doc.status != "SUPERSEDED" and "changelog" not in doc.path.lower()]
        platform_docs.sort(key=lambda doc: (_status_priority(doc.status), -self._score(doc, doc.text, terms), doc.path))
        selected = core + platform_docs[:6]
        retrieval_reserve = min(10_000, max_chars // 4)
        for position, document in enumerate(selected):
            available = max(0, remaining - retrieval_reserve)
            cap = min(5500, available // max(1, len(selected) - position))
            if cap < 300:
                continue
            sections = list(self._sections(document))
            if len(document.text) <= cap:
                append(document.path, document.status, document.text, cap=cap)
                continue
            prefix_cap = min(1800, cap // 3)
            append(document.path, document.status, document.text, cap=prefix_cap)
            sections.sort(key=lambda section: (-self._score(document, section[2], terms), section[0]))
            for start, _, text in sections[:2]:
                if start > 1:
                    append(document.path, document.status, text, start=start, cap=max(1, (cap - prefix_cap) // 2))

        core_paths = {document.path for document in selected}
        for result in self.search(compact_query, limit=12):
            if result["path"] in core_paths or result["status"] == "SUPERSEDED":
                continue
            append(result["path"], result["status"], result["text"], result["start"], cap=2500)
            if remaining < 500:
                break
        return "".join(blocks)
