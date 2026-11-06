"""Bounded evidence from immutable simulator traces.

Trace ordering comes from the simulator, not node-local log timestamps.  These
summaries are diagnostic: truncation never changes the simulator's verdict.
"""

from __future__ import annotations

from collections import Counter, deque
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Callable, Iterator


MAX_SCAN_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
MAX_ACTIONS = 2048
MAX_ENTITIES = 40
MAX_TRACE_CHARS = 24_000
MAX_CAUSAL_WINDOWS = 3
_ABNORMAL = re.compile(
    r"(?:lost|dead|timeout|unknown|abandon|exception|reject|fenc|revoke|fail|abort|capacity|overload)",
    re.IGNORECASE,
)
_ENTITY_FIELDS = frozenset(
    {"action_id", "job", "job_id", "jkey", "key", "task", "task_id", "op_id", "worker", "holder"}
)


def _bounded(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return value if len(value) <= 600 else value[:600] + "…"
    if isinstance(value, int) and not isinstance(value, bool) and value.bit_length() > 256:
        return f"<integer omitted: {value.bit_length()} bits>"
    if isinstance(value, float) and not math.isfinite(value):
        return "<non-finite number omitted>"
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
        "effect", "worker", "job", "key", "task", "attempt", "epoch", "reason",
        "code", "status", "error", "payload",
    )
    small = {key: _bounded(event[key]) for key in primary if key in event}
    small.update(trace_line=line, truncated=True, detail_preview=encoded[:1000])
    return small


def _ends(items: list, limit: int) -> list:
    if len(items) <= limit:
        return list(items)
    head = (limit + 1) // 2
    tail = limit - head
    return items[:head] + (items[-tail:] if tail else [])


def _safe_payload(value: Any, depth: int = 0, *, path: tuple[str, ...] = (),
                  omissions: dict | None = None) -> Any:
    """Keep tool envelopes serializable, including unusual numeric metadata."""
    def omitted(unit: str, count: int) -> None:
        if omissions is not None and count:
            key = ".".join(path)[:200] or "<root>"
            entry = omissions.setdefault(key, {})
            entry[unit] = entry.get(unit, 0) + count

    if isinstance(value, int) and not isinstance(value, bool) and value.bit_length() > 256:
        omitted("integer_bits", value.bit_length())
        return f"<integer omitted: {value.bit_length()} bits>"
    if isinstance(value, float) and not math.isfinite(value):
        omitted("nonfinite_values", 1)
        return "<non-finite number omitted>"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if depth >= 10:
        omitted("nested_values", 1)
        return "<nested value omitted>"
    if isinstance(value, dict):
        out = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 128:
                omitted("fields", len(value) - index)
                break
            key = str(key)
            bounded_key = key[:200]
            if bounded_key in out:
                omitted("fields", 1)
                continue
            if len(key) > len(bounded_key):
                omitted("key_characters", len(key) - len(bounded_key))
            out[bounded_key] = _safe_payload(item, depth + 1, path=path + (bounded_key,), omissions=omissions)
        return out
    if isinstance(value, (list, tuple)):
        kept = _ends(list(value), 512)
        omitted("items", len(value) - len(kept))
        return [_safe_payload(item, depth + 1, path=path + (str(index),), omissions=omissions)
                for index, item in enumerate(kept)]
    omitted("nonjson_values", 1)
    return str(value)[:600]


def _encoded_chars(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False))


def bounded_result(value: dict, max_chars: int = MAX_TRACE_CHARS) -> dict:
    """Reduce whole records before JSON encoding; never return a cut JSON prefix.

    First and last records survive reduction. Source/run identities are placed
    first and are never shortened. Omission metadata describes diagnostic loss,
    without changing a checker verdict or implying the entire trace was read.
    """
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 256:
        raise ValueError("response budget must be an integer of at least 256 characters")
    protected = {"identity", "run_id", "source_hash", "current_source_hash", "scenario", "scenario_hash",
                 "seed", "faults", "faults_hash", "trace_sha256", "probe_hash", "accepted_source_hash", "query_hash"}
    ordered = {key: value[key] for key in value if key in protected}
    ordered.update({key: item for key, item in value.items() if key not in protected})
    omissions: dict[str, dict[str, int]] = {}
    result = _safe_payload(ordered, omissions=omissions)
    initial_evidence = result.get("evidence", result)
    initial_windows_omitted = initial_evidence.get("causal_windows_omitted", 0) if isinstance(initial_evidence, dict) else 0

    def safe_count(value: Any, default: int = 0) -> int:
        return max(0, value) if isinstance(value, int) and not isinstance(value, bool) else default

    def add_omission(path: tuple[str, ...], unit: str, amount: int) -> None:
        entry = omissions.setdefault(".".join(path)[:200] or "<root>", {})
        entry[unit] = entry.get(unit, 0) + amount

    def reduce(item: Any, path: tuple[str, ...], count: int, chars: int) -> Any:
        if isinstance(item, str):
            if path and path[-1] in protected:
                return item
            if len(item) > chars:
                add_omission(path, "characters", len(item) - chars)
                return item[:chars] + "…"
            return item
        if isinstance(item, list):
            kept = _ends(item, count)
            if len(kept) < len(item):
                add_omission(path, "items", len(item) - len(kept))
            return [reduce(child, path + (str(index),), count, chars) for index, child in enumerate(kept)]
        if isinstance(item, dict):
            return {key: reduce(child, path + (key,), count, chars) for key, child in item.items()}
        return item

    def describe(output: dict) -> None:
        output["response_truncated"] = True
        output["response_omissions"] = dict(list(omissions.items())[:32])
        output["response_budget_chars"] = max_chars
        if isinstance(output.get("events"), list):
            output["returned_events"] = len(output["events"])
            total = output.get("eligible_events", output.get("matched_events", len(output["events"])))
            output["omitted_events"] = max(0, safe_count(total) - len(output["events"]))
            output["selection_truncated"] = output["omitted_events"] > 0
        evidence = output.get("evidence", output)
        if isinstance(evidence, dict) and isinstance(evidence.get("timeline"), list):
            total = evidence.get("timeline_matched_events", len(evidence["timeline"]))
            evidence["timeline_omitted_events"] = max(0, safe_count(total) - len(evidence["timeline"]))
            evidence["timeline_truncated"] = evidence["timeline_omitted_events"] > 0
        windows = evidence.get("causal_windows") if isinstance(evidence, dict) else None
        if isinstance(windows, list):
            window_key = "evidence.causal_windows" if "evidence" in output else "causal_windows"
            evidence["causal_windows_omitted"] = safe_count(initial_windows_omitted) + omissions.get(window_key, {}).get("items", 0)
            for window in windows:
                if isinstance(window, dict) and isinstance(window.get("events"), list):
                    window["returned_events"] = len(window["events"])
                    selected = safe_count(window.get("selected_events"), len(window["events"]))
                    window["omitted_selected_events"] = max(0, selected - len(window["events"]))
                    window["selection_truncated"] = bool(window.get("selection_truncated")) or window["omitted_selected_events"] > 0

    if omissions:
        describe(result)
    if _encoded_chars(result) <= max_chars:
        return result

    for keep_count, chars in ((64, 1024), (32, 768), (16, 512), (8, 384), (4, 256), (2, 128)):
        result.pop("response_omissions", None)
        result = reduce(result, (), keep_count, chars)
        describe(result)
        if _encoded_chars(result) <= max_chars:
            return result
    # Fixed metadata alone can exceed a tiny caller-supplied allowance. Keep a
    # complete verdict/provenance envelope and explicitly omit diagnostic bodies.
    minimal = {key: item for key, item in result.items()
               if key in protected and (not isinstance(item, (dict, list)) or key == "identity")}
    for key in ("ok", "status", "error", "violation_count", "matched_events", "eligible_events",
                "scan_truncated", "malformed_lines", "oversized_lines"):
        if key in result:
            minimal[key] = result[key]
    minimal.update(response_truncated=True, response_budget_chars=max_chars,
                   diagnostic_bodies_omitted=True)
    if "events" in result:
        minimal.update(events=[], returned_events=0,
                       omitted_events=result.get("eligible_events", result.get("matched_events", 0)),
                       selection_truncated=True)
    if _encoded_chars(minimal) > max_chars:
        raise ValueError("response budget cannot hold the exact source/run provenance")
    return minimal


def _scan(path: Path, stats: dict, should_stop: Callable[[], bool] | None = None) -> Iterator[tuple[int, dict]]:
    """Read JSONL without accumulating the trace or an unbounded line."""
    scanned = 0
    line_no = 0
    oversized_tail = False
    try:
        with path.open("rb") as stream:
            while scanned < MAX_SCAN_BYTES:
                if should_stop is not None and should_stop():
                    stats.update(scan_cancelled=True, scan_truncated=True)
                    return
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


def _global_time(event: dict) -> float | None:
    value = event.get("t")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            value = float(value)
        except (ValueError, OverflowError):
            return None
        if math.isfinite(value):
            return value
    return None


def _links(event: dict) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 6 or len(found) >= 32:
            return
        if isinstance(value, dict):
            for key, item in list(value.items())[:32]:
                if key in {"rpc_id", "msg_id", "request_id", "reply_to", "id"} and isinstance(item, (str, int)):
                    # Request/reply payload ids and top-level rpc ids share a
                    # namespace; message ids identify individual envelopes.
                    namespace = "msg" if key == "msg_id" else "rpc"
                    found.add((namespace, str(_bounded(item))[:128]))
                if isinstance(item, (dict, list)):
                    visit(item, depth + 1)
        elif isinstance(value, list):
            for item in value[:32]:
                visit(item, depth + 1)

    visit(event)
    return found


def _select_trace(trace: Path, entities: list[str], kinds: set[str] | None, limit: int,
                  times: list[float], window_s: float, context_lines: int,
                  should_stop: Callable[[], bool] | None = None) -> dict:
    def matching(event: dict) -> bool:
        if not _event_type_matches(event, kinds) or (entities and not _mentions(event, entities)):
            return False
        t = _global_time(event)
        return not times or (t is not None and any(abs(t - at) <= window_s for at in times))

    anchor_limit = limit if not context_lines else max(1, limit // (context_lines * 2 + 1))
    sample = _Sample(anchor_limit if limit else 0)
    stats = _stats()
    window_counts = [0 for _ in times]
    for line, event in _scan(trace, stats, should_stop):
        if matching(event):
            sample.add(event, line)
            t = _global_time(event)
            for index, at in enumerate(times):
                if t is not None and abs(t - at) <= window_s:
                    window_counts[index] += 1
    anchors = sample.events()
    for event in anchors:
        event["selection_role"] = "anchor"
    events = anchors
    eligible = sample.matched
    context_count = 0
    context_stats = None
    if context_lines and anchors and not stats.get("scan_cancelled"):
        anchor_lines = {event["trace_line"] for event in anchors}
        neighbors = {line for anchor in anchor_lines
                     for line in range(max(1, anchor - context_lines), anchor + context_lines + 1)}
        link_ids = set().union(*(_links(event) for event in anchors))
        context = _Sample(max(0, limit - len(anchors)))
        context_stats = _stats()
        eligible = 0
        for line, event in _scan(trace, context_stats, should_stop):
            is_match = matching(event)
            is_context = line in neighbors or bool(link_ids and link_ids & _links(event))
            if is_match or is_context:
                eligible += 1
            if is_context and not is_match:
                context_count += 1
            if is_context and line not in anchor_lines:
                context.add(event, line)
        eligible = max(sample.matched, eligible)
        context_events = context.events()
        for event in context_events:
            event["selection_role"] = "neighbor" if event["trace_line"] in neighbors else "linked_request_or_reply"
        events = sorted(anchors + context_events, key=lambda event: event["trace_line"])
    output = {
        "trace": str(trace), "entities": entities,
        "event_types": sorted(kinds) if kinds is not None else None,
        "at": times[0] if len(times) == 1 else None, "focus_times": times,
        "window_s": window_s if times else None, "context_lines": context_lines,
        "matched_events": sample.matched, "context_matched_events": context_count,
        "eligible_events": eligible, "returned_events": len(events),
        "omitted_events": max(0, eligible - len(events)),
        "selection_truncated": eligible > len(events), "events": events,
        "window_match_counts": window_counts, **stats,
    }
    if context_stats is not None:
        output["context_scan"] = context_stats
        output["scan_truncated"] |= context_stats["scan_truncated"]
        output["scan_cancelled"] = stats.get("scan_cancelled", False) or context_stats.get("scan_cancelled", False)
    return output


def trace_slice(
    trace: Path,
    entities: list[str],
    event_types: list[str] | None = None,
    limit: int = 100,
    *, at: float | None = None, window_s: float = 2.0, context_lines: int = 0,
    max_chars: int = MAX_TRACE_CHARS, provenance: dict | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    """Select entity/time anchors and optional neighbor/RPC context in trace order."""
    trace = Path(trace)
    entities = [str(e)[:512] for e in entities[:MAX_ENTITIES] if str(e)]
    kinds = None if event_types is None else {str(k)[:100] for k in event_types[:40]}
    normalized_window = _global_time({"t": window_s})
    normalized_at = None if at is None else _global_time({"t": at})
    if normalized_window is None or not 0 <= normalized_window <= 3600:
        raise ValueError("window_s must be finite and between 0 and 3600")
    if at is not None and (normalized_at is None or normalized_at < 0):
        raise ValueError("at must be a nonnegative global trace time")
    if not isinstance(context_lines, int) or isinstance(context_lines, bool) or not 0 <= context_lines <= 20:
        raise ValueError("context_lines must be between 0 and 20")
    limit = min(500, max(0, int(limit)))
    result = _select_trace(trace, entities, kinds, limit, [] if normalized_at is None else [normalized_at],
                           normalized_window, context_lines, should_stop)
    query = json.dumps({"entities": entities, "event_types": sorted(kinds) if kinds is not None else None,
                        "at": normalized_at, "window_s": normalized_window,
                        "context_lines": context_lines, "limit": limit}, ensure_ascii=False)
    result["query_hash"] = hashlib.sha256(query.encode()).hexdigest()
    return bounded_result({**(provenance or {}), **result}, max_chars)


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


def _is_abnormal(event: dict) -> bool:
    if _ABNORMAL.search(str(event.get("event", ""))) or _ABNORMAL.search(str(event.get("effect", ""))):
        return True
    if re.search(r"error|exception|fail", str(event.get("kind", "")), re.IGNORECASE):
        return True

    def error(value: Any, depth: int = 0) -> bool:
        if depth > 5:
            return False
        if isinstance(value, dict):
            return any((key in {"error", "exception"} and bool(item))
                       or (key in {"ok", "success"} and item is False)
                       or error(item, depth + 1) for key, item in list(value.items())[:32])
        if isinstance(value, list):
            return any(error(item, depth + 1) for item in value[:16])
        return False

    return error(event)


def _error_codes(violations: list) -> list[str]:
    codes: list[str] = []
    for violation in violations[:40]:
        if not isinstance(violation, dict):
            continue
        for code in re.findall(r"[\"']?code[\"']?\s*[:=]\s*[\"']?([\w.-]+)",
                               str(violation.get("detail", ""))[:1500], re.IGNORECASE):
            if code.casefold() not in codes and len(codes) < 6:
                codes.append(code.casefold()[:100])
    return codes


def _representative_times(values: list[float], radius: float = 2.0) -> list[float]:
    times: list[float] = []
    for value in sorted(set(values)):
        if value >= 0 and (not times or value - times[-1] > radius):
            times.append(value)
    return times


def summarize_trace(trace: Path, violations: list, summary: dict, max_events: int = 60, *,
                    should_stop: Callable[[], bool] | None = None) -> dict:
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
    error_witnesses = _Sample(8)
    codes = _error_codes(violations)

    for line, event in _scan(trace, stats, should_stop):
        kind = str(event.get("kind", "unknown"))[:100]
        kinds[kind if kind in kinds or len(kinds) < 500 else "<other>"] += 1
        if kind == "log":
            name = str(event.get("event", "unknown"))[:100]
            logs[name if name in logs or len(logs) < 500 else "<other>"] += 1
            if name == "scenario" and not header:
                header = _bounded(event)
        if _is_abnormal(event):
            abnormal.add(event, line)
            if codes:
                text = json.dumps(event, ensure_ascii=False).casefold()
                if any(code in text for code in codes):
                    error_witnesses.add(event, line)
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

    focus_reason = "invariant_entities_and_time"
    witnesses = error_witnesses.events() or abnormal.events()
    times = [_global_time(violation) for violation in violations[:40] if isinstance(violation, dict)]
    times = [t for t in times if t is not None]
    if not entities:
        # Aggregate expectations often report the scenario's end time. Locate
        # the actual failed response/effect before choosing a causal window.
        for event in witnesses:
            _entities(event, entities)
        witness_times = [_global_time(event) for event in witnesses]
        times = [t for t in witness_times if t is not None] or times
        focus_reason = "checker_error_witnesses" if error_witnesses.matched else "abnormal_events"
    available_times = _representative_times(times)
    times = _ends(available_times, MAX_CAUSAL_WINDOWS)
    if entities or times:
        timeline = _select_trace(trace, entities, None, min(200, max(0, int(max_events))), times, 2.0, 2, should_stop)
    else:
        # Without a causal focus, the already collected error witnesses are
        # more useful than another scan of unrelated progress records.
        witness_count = error_witnesses.matched or abnormal.matched
        timeline = {"events": witnesses, "eligible_events": witness_count,
                    "selection_truncated": witness_count > len(witnesses),
                    "window_match_counts": [], **_stats()}
    if not timeline["events"] and entities:
        # Some schemas omit global timestamps; still retain their entity
        # sequence rather than interpreting an empty time query as no evidence.
        timeline = _select_trace(trace, entities, None, min(200, max(0, int(max_events))), [], 2.0, 0, should_stop)
        focus_reason = "entity_history_without_time_matches"
        times = []
    timeline_events = timeline["events"] if entities or times else witnesses
    for event in timeline_events:
        _entities(event, entities)
    stats["scan_truncated"] |= timeline["scan_truncated"]
    if stats.get("scan_cancelled") or timeline.get("scan_cancelled"):
        stats["scan_cancelled"] = True
    windows = []
    for index, at in enumerate(times):
        nearby = [event for event in timeline_events
                  if _global_time(event) is None or min(times, key=lambda t: abs(_global_time(event) - t)) == at]
        kept = _ends(nearby, 12)
        windows.append({"at": at, "window_s": 2.0, "entities": list(entities),
                        "events": kept, "returned_events": len(kept), "selected_events": len(nearby),
                        "omitted_selected_events": len(nearby) - len(kept),
                        "matched_events": timeline["window_match_counts"][index],
                        "context_lines": 2,
                        "selection_truncated": timeline["selection_truncated"] or len(kept) < len(nearby)})
    families = _failure_groups(violations)
    return bounded_result({
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
        "failure_witnesses": _ends(witnesses, 8),
        "causal_windows": windows, "causal_windows_omitted": len(available_times) - len(windows),
        "causal_focus_reason": focus_reason,
        "timeline": timeline_events,
        "timeline_matched_events": timeline["eligible_events"],
        "timeline_omitted_events": max(0, timeline["eligible_events"] - len(timeline_events)),
        "timeline_truncated": timeline["selection_truncated"],
        "causal_scan": {key: timeline[key] for key in ("events_scanned", "bytes_scanned", "scan_truncated",
                                                       "scan_cancelled", "context_scan") if key in timeline},
        **stats,
    }, max_chars=20_000)
