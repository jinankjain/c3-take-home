"""Bounded evidence from immutable simulator traces.

Trace ordering comes from the simulator, not node-local log timestamps.  These
summaries are diagnostic: truncation never changes the simulator's verdict.
"""

from __future__ import annotations

from collections import Counter, deque
import json
from pathlib import Path
import re
from typing import Any, Iterator


MAX_SCAN_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
MAX_ACTIONS = 2048
MAX_ENTITIES = 40
_ABNORMAL = re.compile(
    r"(?:lost|dead|timeout|unknown|abandon|exception|reject|fenc|revoke|fail|abort)",
    re.IGNORECASE,
)
_ENTITY_FIELDS = frozenset(
    {"action_id", "job", "job_id", "jkey", "key", "task", "task_id", "op_id", "worker", "holder"}
)


def _bounded(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return value if len(value) <= 600 else value[:600] + "…"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= 4:
        return "<nested value omitted>"
    if isinstance(value, dict):
        items = list(value.items())[:24]
        out = {str(k)[:100]: _bounded(v, depth + 1) for k, v in items}
        if len(value) > len(items):
            out["_omitted_fields"] = len(value) - len(items)
        return out
    if isinstance(value, (list, tuple)):
        out = [_bounded(v, depth + 1) for v in value[:8]]
        if len(value) > 8:
            out.append({"_omitted_items": len(value) - 8})
        return out
    return str(value)[:600]


def _compact_event(event: dict, line: int) -> dict:
    out = _bounded(event)
    out["trace_line"] = line
    encoded = json.dumps(out, ensure_ascii=False, sort_keys=True)
    if len(encoded) <= 2600:
        return out
    primary = (
        "seq", "t", "kind", "node", "event", "src", "dst", "method", "msg_id",
        "rpc_id", "type", "action", "action_id", "phase", "outcome", "op_id",
        "effect", "worker", "job", "task", "attempt", "epoch", "reason",
    )
    small = {key: _bounded(event[key]) for key in primary if key in event}
    small.update(trace_line=line, truncated=True, detail_preview=encoded[:1000])
    return small


def _scan(path: Path, stats: dict) -> Iterator[tuple[int, dict]]:
    """Read JSONL without accumulating the trace or an unbounded line."""
    scanned = 0
    line_no = 0
    oversized_tail = False
    try:
        with path.open("rb") as stream:
            while scanned < MAX_SCAN_BYTES:
                raw = stream.readline(min(MAX_LINE_BYTES + 1, MAX_SCAN_BYTES - scanned))
                if not raw:
                    break
                scanned += len(raw)
                if oversized_tail:
                    oversized_tail = not raw.endswith(b"\n")
                    continue
                line_no += 1
                if len(raw) > MAX_LINE_BYTES:
                    stats["oversized_lines"] += 1
                    oversized_tail = not raw.endswith(b"\n")
                    continue
                if not raw.strip():
                    continue
                try:
                    event = json.loads(raw)
                except (ValueError, UnicodeDecodeError, RecursionError) as error:
                    stats["malformed_lines"] += 1
                    if len(stats["errors"]) < 3:
                        stats["errors"].append(f"line {line_no}: {type(error).__name__}")
                    continue
                if not isinstance(event, dict):
                    stats["malformed_lines"] += 1
                    continue
                stats["events_scanned"] += 1
                yield line_no, event
            stats["scan_truncated"] = oversized_tail or bool(stream.read(1))
    except OSError as error:
        stats["errors"].append(f"{type(error).__name__}: {str(error)[:300]}")
        stats["scan_truncated"] = True
    finally:
        stats["bytes_scanned"] = scanned


def _stats() -> dict:
    return {
        "events_scanned": 0, "bytes_scanned": 0, "malformed_lines": 0,
        "oversized_lines": 0, "scan_truncated": False, "errors": [],
    }


class _Sample:
    """Keep both the beginning and the end of a potentially long history."""

    def __init__(self, limit: int):
        self.limit = max(0, limit)
        self.head_limit = (self.limit + 1) // 2
        self.head: list[dict] = []
        self.tail: deque[dict] = deque(maxlen=max(0, self.limit - self.head_limit))
        self.matched = 0

    def add(self, event: dict, line: int) -> None:
        self.matched += 1
        if self.limit == 0:
            return
        compact = _compact_event(event, line)
        if len(self.head) < self.head_limit:
            self.head.append(compact)
        else:
            self.tail.append(compact)

    def events(self) -> list[dict]:
        return self.head + list(self.tail)


def _mentions(event: dict, entities: list[str]) -> bool:
    # Exact identifiers remain useful across unfamiliar service schemas.  RPC
    # payloads are included rather than limiting matching to fixed top-level keys.
    long_entities = [entity for entity in entities if len(entity) >= 3]
    if long_entities:
        text = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        if any(entity in text for entity in long_entities):
            return True
    short_entities = {entity for entity in entities if len(entity) < 3}

    def matches(value: Any, depth: int = 0) -> bool:
        if depth > 8:
            return False
        if isinstance(value, dict):
            return any(
                (key in _ENTITY_FIELDS and isinstance(item, (str, int)) and str(item) in short_entities)
                or matches(item, depth + 1)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return any(matches(item, depth + 1) for item in value)
        return False

    return bool(short_entities) and matches(event)


def _event_type_matches(event: dict, kinds: set[str] | None) -> bool:
    return kinds is None or any(
        str(event.get(key, "")) in kinds for key in ("kind", "event", "method", "effect")
    )


def trace_slice(
    trace: Path,
    entities: list[str],
    event_types: list[str] | None = None,
    limit: int = 100,
) -> dict:
    """Return a bounded entity history, preserving its first and last events."""
    trace = Path(trace)
    entities = [str(e)[:512] for e in entities[:MAX_ENTITIES] if str(e)]
    kinds = None if event_types is None else {str(k)[:100] for k in event_types[:40]}
    sample = _Sample(min(500, max(0, int(limit))))
    stats = _stats()
    for line, event in _scan(trace, stats):
        if _event_type_matches(event, kinds) and (not entities or _mentions(event, entities)):
            sample.add(event, line)
    events = sample.events()
    return {
        "trace": str(trace), "entities": entities, "event_types": event_types,
        "matched_events": sample.matched, "returned_events": len(events),
        "selection_truncated": sample.matched > len(events), "events": events, **stats,
    }


def _entities(value: Any, out: list[str], depth: int = 0) -> None:
    if depth > 4 or len(out) >= MAX_ENTITIES:
        return
    if isinstance(value, dict):
        for key, item in list(value.items())[:32]:
            if key in _ENTITY_FIELDS and isinstance(item, (str, int)):
                entity = str(item)
                if entity and entity not in out:
                    out.append(entity[:512])
            if isinstance(item, (dict, list)):
                _entities(item, out, depth + 1)
    elif isinstance(value, list):
        for item in value[:20]:
            _entities(item, out, depth + 1)


def _failure_groups(violations: list) -> list[dict]:
    grouped: dict[tuple[str, str], dict] = {}
    for violation in violations:
        if not isinstance(violation, dict):
            continue
        invariant = str(violation.get("invariant", "unknown"))[:100]
        detail = str(violation.get("detail", ""))[:1000]
        # Numeric differences belong in samples and identities.  Grouping them
        # prevents a separate family for every timestamp/epoch in a large soak.
        normalized = re.sub(r"\b\d+(?:\.\d+)?\b", "#", detail)
        key = invariant, normalized
        if key not in grouped:
            if len(grouped) >= 40:
                continue
            grouped[key] = {
                "invariant": invariant, "family": normalized[:400], "count": 0,
                "sample": {"t": violation.get("t"), "detail": detail},
            }
        grouped[key]["count"] += 1
    return sorted(grouped.values(), key=lambda item: (-item["count"], item["invariant"]))


def summarize_trace(trace: Path, violations: list, summary: dict, max_events: int = 60) -> dict:
    """Summarize failures and causal histories without loading a complete trace."""
    trace = Path(trace)
    stats = _stats()
    kinds: Counter[str] = Counter()
    logs: Counter[str] = Counter()
    starts: dict[str, dict] = {}
    ended: set[str] = set()
    action_overflow = False
    header: dict = {}
    faults = _Sample(8)
    abnormal = _Sample(min(12, max(0, int(max_events))))

    for line, event in _scan(trace, stats):
        kind = str(event.get("kind", "unknown"))[:100]
        kinds[kind if kind in kinds or len(kinds) < 500 else "<other>"] += 1
        if kind == "log":
            name = str(event.get("event", "unknown"))[:100]
            logs[name if name in logs or len(logs) < 500 else "<other>"] += 1
            if name == "scenario" and not header:
                header = _bounded(event)
            if _ABNORMAL.search(name):
                abnormal.add(event, line)
        if kind == "fault":
            faults.add(event, line)
        if kind == "client":
            aid = str(event.get("action_id", ""))
            if event.get("phase") == "start":
                if aid in starts or len(starts) < MAX_ACTIONS:
                    starts.setdefault(aid, _compact_event(event, line))
                else:
                    action_overflow = True
            elif event.get("phase") == "end":
                if aid in starts or len(ended) < MAX_ACTIONS:
                    ended.add(aid)
                else:
                    action_overflow = True

    unresolved = [record for aid, record in starts.items() if aid not in ended][:12]
    failed_actions: list[dict] = []
    supplied_actions = summary.get("actions", [])
    if isinstance(supplied_actions, list):
        for action in supplied_actions:
            if isinstance(action, dict) and action.get("outcome") == "unresolved":
                if len(unresolved) < 12:
                    unresolved.append(_bounded(action))
            elif isinstance(action, dict) and action.get("outcome") in ("timeout", "failed", "error"):
                if len(failed_actions) < 12:
                    failed_actions.append(_bounded(action))

    entities: list[str] = []
    for item in unresolved + failed_actions:
        _entities(item, entities)
    for violation in violations[:40]:
        if not isinstance(violation, dict):
            continue
        _entities(violation, entities)
        detail = str(violation.get("detail", ""))[:1500]
        for match in re.finditer(
            r"\b(?:action|job|key|task|op_id|worker|holder)\s*[=:]?\s+([\w:./@-]+)", detail,
        ):
            entity = match.group(1)
            if entity not in entities and len(entities) < MAX_ENTITIES:
                entities.append(entity)
        for entity in re.findall(r"\b[\w-]+:[\w:./@-]+", detail):
            if entity not in entities and len(entities) < MAX_ENTITIES:
                entities.append(entity)

    timeline = trace_slice(trace, entities, limit=min(200, max(0, int(max_events))))
    # Without identifiable affected entities, expose abnormal transitions and
    # faults instead of returning an arbitrary large successful history.
    timeline_events = timeline["events"] if entities else abnormal.events()
    families = _failure_groups(violations)
    return {
        "identity": {
            "trace": str(trace), "source_hash": summary.get("source_hash"),
            "scenario_hash": summary.get("scenario_hash"),
            "faults_hash": summary.get("faults_hash"),
            "scenario": summary.get("scenario", header.get("name")),
            "seed": summary.get("seed", header.get("seed")),
            "trace_sha256": summary.get("trace_sha256"),
        },
        "failure_families": families,
        "failure_families_truncated": sum(group["count"] for group in families) < len(violations),
        "violation_count": len(violations), "unresolved_actions": unresolved,
        "failed_actions": failed_actions,
        "affected_entities": entities, "action_index_truncated": action_overflow,
        "outcomes": _bounded(summary.get("outcomes", {})),
        "messages_sent": summary.get("messages_sent", kinds.get("msg_send", 0)),
        "event_counts": dict(kinds.most_common(40)),
        "log_event_counts": dict(logs.most_common(40)),
        "fault_events": faults.events(), "abnormal_events": abnormal.events(),
        "timeline": timeline_events,
        "timeline_truncated": timeline["selection_truncated"] if entities else abnormal.matched > len(timeline_events),
        **stats,
    }
