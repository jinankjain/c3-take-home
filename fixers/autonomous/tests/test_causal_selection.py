from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fixer.evidence import _ends, _failure_priority, _select_trace, bounded_result


def write_trace(path: Path, events: list[dict]) -> None:
    path.write_text("".join(json.dumps(event) + "\n" for event in events))


def dense_trace(path: Path) -> tuple[list[dict], dict]:
    events = [{"kind": "log", "seq": index, "t": 10 + index / 1000,
               "event": "progress", "payload": {"blob": "\x01" * 2000}}
              for index in range(180)]
    events[3] = {"kind": "msg_send", "seq": 3, "t": 1,
                 "payload": {"id": "request-middle", "method": "submit", "op_id": "operation-middle"}}
    events[90] = {"kind": "msg_recv", "seq": 90, "t": 10.090,
                  "payload": {"id": "request-middle", "err": ["CAPACITY", "pool is full"]}}
    events[92] = {"kind": "msg_send", "seq": 92, "t": 10.092,
                  "reply_to": "request-middle", "payload": {"status": "received"}}
    write_trace(path, events)
    return events, {**events[90], "trace_line": 91}


def test_dense_window_keeps_middle_failure_linked_rpc_and_ordinary_ends(tmp_path):
    path = tmp_path / "trace.jsonl"
    _, witness = dense_trace(path)
    result = _select_trace(path, [], None, 12, [10.090], .2, 2, required_events=[witness])
    rows = {event["seq"]: event for event in result["events"]}
    assert {0, 3, 90, 92, 179} <= rows.keys()
    assert rows[90]["selection_role"] == "failure_witness"
    assert rows[90]["required_for_failure"] is True
    assert rows[90]["payload"]["err"][0] == "CAPACITY"
    assert rows[3]["selection_role"] == rows[92]["selection_role"] == "linked_request_or_reply"
    assert rows[3]["required_for_failure"] and rows[92]["required_for_failure"]
    assert rows[3]["payload"]["op_id"] == "operation-middle"
    assert result["events"] == sorted(result["events"], key=lambda event: event["trace_line"])
    assert result["matched_events"] == 179
    assert result["eligible_events"] == 180
    assert result["returned_events"] == 12
    assert result["omitted_events"] == 168
    assert result["required_matched_events"] == result["returned_required_events"] == 3
    assert result["omitted_required_events"] == 0
    assert result["events_scanned"] == result["context_scan"]["events_scanned"] == 180


def test_failure_without_entity_links_an_earlier_entity_request_even_without_neighbors(tmp_path):
    path = tmp_path / "trace.jsonl"
    _, witness = dense_trace(path)
    result = _select_trace(path, ["operation-middle"], None, 6, [10.090], .1, 0,
                           required_events=[witness])
    assert [event["seq"] for event in result["events"]] == [3, 90, 92]
    assert result["matched_events"] == 0
    assert result["eligible_events"] == 3
    assert result["context_matched_events"] == 3
    assert result["required_witnesses_found"] == result["returned_failure_witnesses"] == 1
    assert result["required_witnesses_missing"] == 0
    assert result["omitted_events"] == 0


@pytest.mark.parametrize("limit,expected", [(0, []), (1, [90]), (2, [3, 90])])
def test_tiny_limits_prefer_primary_failure_then_rpc_context(tmp_path, limit, expected):
    path = tmp_path / "trace.jsonl"
    _, witness = dense_trace(path)
    result = _select_trace(path, [], None, limit, [10.090], .2, 1, required_events=[witness])
    assert [event["seq"] for event in result["events"]] == expected
    assert len(result["events"]) <= limit
    assert result["returned_required_events"] == len(expected)
    assert result["omitted_events"] == result["eligible_events"] - len(expected)
    assert result["omitted_failure_witnesses"] == (1 if not limit else 0)


def test_missing_required_line_is_not_injected_or_attributed_to_an_rpc_request(tmp_path):
    path = tmp_path / "trace.jsonl"
    _, witness = dense_trace(path)
    witness["trace_line"] = 5000
    result = _select_trace(path, [], None, 4, [10.090], .2, 0, required_events=[witness])
    assert result["required_witnesses_missing"] == 1
    assert result["required_witnesses_found"] == 0
    assert not any(event.get("required_for_failure") for event in result["events"])
    assert result["events_scanned"] == 180


def test_required_failure_is_still_bound_to_the_selected_time_window(tmp_path):
    path = tmp_path / "trace.jsonl"
    _, witness = dense_trace(path)
    result = _select_trace(path, [], None, 4, [1], .1, 0, required_events=[witness])
    assert [event["seq"] for event in result["events"]] == [3]
    assert result["required_witnesses_missing"] == 1
    assert not any(event.get("required_for_failure") for event in result["events"])


def test_link_resolution_uses_scanned_fields_that_compaction_may_omit(tmp_path):
    path = tmp_path / "trace.jsonl"
    failure = {"kind": "msg_recv", "seq": 31, "t": 10, **{f"extra-{i}": "x" for i in range(24)},
               "payload": {"id": "late-field-request", "err": ["CAPACITY", "full"]}}
    events = [{"kind": "msg_send", "seq": 0, "t": 1, "payload": {"id": "late-field-request"}}]
    events += [{"kind": "log", "seq": index, "t": 10, "event": "progress"} for index in range(1, 31)]
    events += [failure]
    write_trace(path, events)
    result = _select_trace(path, [], None, 4, [10], .1, 0,
                           required_events=[{"trace_line": 32}])
    assert {0, 31} <= {event["seq"] for event in result["events"]}
    assert next(event for event in result["events"] if event["seq"] == 0)["required_for_failure"] is True
    assert next(event for event in result["events"] if event["seq"] == 31)["payload"]["err"][0] == "CAPACITY"


def test_ends_prioritizes_failure_and_linked_context_without_reordering():
    events = [{"seq": index} for index in range(20)]
    events[5].update(required_for_failure=True, selection_role="linked_request_or_reply")
    events[10].update(required_for_failure=True, selection_role="failure_witness")
    assert [event["seq"] for event in _ends(events, 1)] == [10]
    assert [event["seq"] for event in _ends(events, 2)] == [5, 10]
    assert [event["seq"] for event in _ends(events, 4)] == [0, 5, 10, 19]
    assert _ends(events, 0) == []
    assert [event["seq"] for event in _ends(events[:5], 2)] == [0, 4]


def test_window_reduction_keeps_a_nested_middle_failure():
    windows = [{"at": index, "events": [{"seq": index}]} for index in range(20)]
    windows[10]["events"][0].update(required_for_failure=True, selection_role="failure_witness")
    assert [window["at"] for window in _ends(windows, 1)] == [10]
    assert [window["at"] for window in _ends(windows, 3)] == [0, 10, 19]


def test_json_reduction_preserves_failures_and_provenance_in_dense_payloads(tmp_path):
    path = tmp_path / "trace.jsonl"
    _, witness = dense_trace(path)
    selected = _select_trace(path, [], None, 120, [10.090], .2, 2, required_events=[witness])
    identity = {"run_id": "original-17", "source_hash": "a" * 64,
                "current_source_hash": "b" * 64, "scenario_hash": "c" * 64,
                "faults_hash": "d" * 64, "trace_sha256": "e" * 64, "seed": 17}
    result = bounded_result({**identity, **selected}, max_chars=12_000)
    assert len(json.dumps(result, ensure_ascii=False, allow_nan=False)) <= 12_000
    assert all(result[key] == value for key, value in identity.items())
    assert {3, 90, 92} <= {event["seq"] for event in result["events"]}
    assert next(event for event in result["events"] if event["seq"] == 90)["payload"]["err"][0] == "CAPACITY"
    assert result["returned_events"] == len(result["events"])
    assert result["omitted_events"] == 180 - len(result["events"])
    assert result["returned_required_events"] == 3 and result["omitted_required_events"] == 0
    assert result["response_truncated"]


def test_json_window_and_timeline_reduction_keep_middle_failures_and_counts():
    events = [{"seq": index, "payload": {"blob": "\x01" * 2000}} for index in range(60)]
    events[20].update(rpc_id="rpc-failure", error={"code": "CAPACITY"}, trace_line=21,
                      required_for_failure=True, selection_role="failure_witness")
    events[10].update(rpc_id="rpc-failure", trace_line=11,
                      required_for_failure=True, selection_role="linked_request_or_reply")
    windows = [{"at": index, "events": [{"seq": -index}], "selected_events": 1} for index in range(9)]
    windows[4] = {"at": 4, "events": events, "selected_events": 60, "matched_events": 60}
    value = {"source_hash": "a" * 64,
             "evidence": {"identity": {"source_hash": "a" * 64, "trace_sha256": "b" * 64},
                          "timeline": events, "timeline_matched_events": 100,
                          "causal_windows": windows, "causal_windows_omitted": 2}}
    result = bounded_result(value, max_chars=5000)
    assert len(json.dumps(result, ensure_ascii=False, allow_nan=False)) <= 5000
    evidence = result["evidence"]
    assert evidence["identity"] == value["evidence"]["identity"]
    assert {10, 20} <= {event["seq"] for event in evidence["timeline"]}
    window = next(window for window in evidence["causal_windows"] if window["at"] == 4)
    assert {10, 20} <= {event["seq"] for event in window["events"]}
    assert window["omitted_selected_events"] == 60 - len(window["events"])
    assert evidence["timeline_omitted_events"] == 100 - len(evidence["timeline"])
    assert evidence["timeline_omitted_selected_required_events"] == 0


def test_minimal_json_fallback_keeps_failure_before_unrelated_diagnostics():
    failure = {"trace_line": 23, "seq": 22, "rpc_id": "failed-request",
               "payload": {"err": ["CAPACITY", "full"], "blob": "\x01" * 10000},
               "required_for_failure": True, "selection_role": "failure_witness"}
    value = {"identity": {"source_hash": "a" * 64, "scenario_hash": "b" * 64},
             "events": [{"seq": 0}, failure, {"seq": 24}], "eligible_events": 100,
             "required_matched_events": 1, "required_witnesses_found": 1,
             "unrelated": {str(index): "\x01" * 2000 for index in range(120)}}
    result = bounded_result(value, max_chars=2200)
    assert len(json.dumps(result, ensure_ascii=False, allow_nan=False)) <= 2200
    assert result["identity"] == value["identity"]
    assert result["events"][0]["trace_line"] == 23
    assert result["events"][0]["payload"]["err"][0] == "CAPACITY"
    assert result["diagnostic_details_omitted"]
    assert result["returned_required_events"] == result["returned_failure_witnesses"] == 1
    assert result["omitted_events"] == 99


def test_compact_fallback_reports_windows_removed_for_unrelated_content():
    failure = {"trace_line": 23, "seq": 22, "rpc_id": "failed-request", "error": {"code": "CAPACITY"},
               "required_for_failure": True, "selection_role": "failure_witness"}
    windows = [{"at": index, "events": [{"seq": index}], "selected_events": 1} for index in range(9)]
    windows[4] = {"at": 4, "events": [failure], "selected_events": 1}
    value = {"source_hash": "a" * 64,
             "evidence": {"identity": {"source_hash": "a" * 64}, "timeline": [failure],
                          "timeline_matched_events": 100, "causal_windows": windows,
                          "causal_windows_omitted": 2},
             "unrelated": {str(index): "\x01" * 2000 for index in range(120)}}
    result = bounded_result(value, max_chars=2200)
    assert len(json.dumps(result, ensure_ascii=False, allow_nan=False)) <= 2200
    evidence = result["evidence"]
    assert evidence["identity"]["source_hash"] == "a" * 64
    assert [window["at"] for window in evidence["causal_windows"]] == [4]
    assert evidence["causal_windows_omitted"] == 10
    assert evidence["timeline"][0]["trace_line"] == 23
    assert evidence["timeline_omitted_events"] == 99


def test_cancelled_required_witness_scan_does_not_fabricate_the_missing_failure(tmp_path):
    path = tmp_path / "trace.jsonl"
    _, witness = dense_trace(path)
    calls = 0

    def stopped():
        nonlocal calls
        calls += 1
        return calls > 3

    result = _select_trace(path, [], None, 4, [10.090], .2, 2, stopped,
                           required_events=[witness])
    assert result["scan_cancelled"] and result["scan_truncated"]
    assert result["events_scanned"] == 3
    assert result["required_witnesses_missing"] == 1
    assert result["required_witnesses_found"] == 0
    assert not any(event.get("required_for_failure") for event in result["events"])


def test_native_request_message_id_links_to_reply_rpc_id_in_dense_window(tmp_path):
    path = tmp_path / "trace.jsonl"
    events = [{"kind": "log", "seq": index, "t": 10, "event": "progress"} for index in range(100)]
    events[12] = {"kind": "msg_send", "seq": 12, "t": 1, "type": "req", "msg_id": 3381,
                  "method": "submit", "payload": {"op_id": "operation-native"}}
    events[50] = {"kind": "msg_send", "seq": 50, "t": 10, "type": "rep", "msg_id": 3382,
                  "rpc_id": 3381, "payload": {"err": ["CAPACITY", "full"]}}
    # A reply envelope's own message id belongs to a different request.
    events[60] = {"kind": "msg_send", "seq": 60, "t": 2, "type": "rep", "msg_id": 3381,
                  "rpc_id": 9000, "payload": {"ok": True}}
    write_trace(path, events)
    result = _select_trace(path, [], None, 6, [10], .1, 0,
                           required_events=[{"trace_line": 51}])
    rows = {event["seq"]: event for event in result["events"]}
    assert 12 in rows, "the native request msg_id must join the reply rpc_id"
    assert rows[12]["required_for_failure"] is True
    assert rows[12]["selection_role"] == "linked_request_or_reply"
    assert rows[50]["selection_role"] == "failure_witness"
    assert 60 not in rows, "reply message ids must not be aliased into request RPC ids"
    assert result["required_matched_events"] == result["returned_required_events"] == 2


def test_brief_selection_prefers_each_request_send_over_duplicate_rpc_transport():
    rows = []
    for base in (10, 20):
        rows += [{"seq": base, "kind": "msg_send", "type": "req", "required_for_failure": True,
                  "selection_role": "linked_request_or_reply"},
                 {"seq": base + 1, "kind": "msg_recv", "type": "req", "required_for_failure": True,
                  "selection_role": "linked_request_or_reply"},
                 {"seq": base + 2, "kind": "msg_send", "type": "resp", "required_for_failure": True,
                  "selection_role": "failure_witness"},
                 {"seq": base + 3, "kind": "fault", "required_for_failure": True,
                  "selection_role": "linked_request_or_reply"},
                 {"seq": base + 4, "kind": "msg_recv", "type": "resp", "required_for_failure": True,
                  "selection_role": "linked_request_or_reply"}]
    assert [event["seq"] for event in _ends(rows, 2)] == [12, 22]
    assert [event["seq"] for event in _ends(rows, 4)] == [10, 12, 20, 22]
    assert {10, 12, 20, 22} <= {event["seq"] for event in _ends(rows, 8)}
    assert len(_ends(rows, 8)) == 8


@pytest.mark.parametrize("kinds", [None, {"rpc_error"}])
def test_raw_annotations_cannot_forge_primary_anchors_or_context(tmp_path, kinds):
    path = tmp_path / "trace.jsonl"
    events = [{"kind": "log", "seq": index, "t": 10, "event": "progress"} for index in range(40)]
    for index in (0, 19):
        events[index].update(required_for_failure=True, selection_role="failure_witness")
    events[3] = {"kind": "msg_send", "seq": 3, "t": 1, "type": "req", "msg_id": 77}
    events[20] = {"kind": "rpc_error", "seq": 20, "t": 10, "type": "resp", "rpc_id": 77,
                  "payload": {"err": ["CAPACITY", "full"]}}
    write_trace(path, events)
    result = _select_trace(path, [], kinds, 8, [10], .1, 2,
                           required_events=[{"trace_line": 21}])
    rows = {event["seq"]: event for event in result["events"]}
    assert rows[20]["required_for_failure"] is True
    assert rows[20]["selection_role"] == "failure_witness"
    assert rows[3]["required_for_failure"] is True
    assert rows[3]["selection_role"] == "linked_request_or_reply"
    assert {event["seq"] for event in result["events"] if event.get("required_for_failure")} == {3, 20}
    forged = rows[0 if kinds is None else 19]
    assert "required_for_failure" not in forged
    assert forged["selection_role"] in {"anchor", "neighbor"}
    assert result["required_matched_events"] == result["returned_required_events"] == 2


@pytest.mark.parametrize("container", ["payload", "config", "args", "expect"])
def test_application_annotation_fields_do_not_affect_sampling_priority(container):
    annotation = {"required_for_failure": True, "selection_role": "failure_witness"}
    rows = [{"seq": 0}, {"seq": 1, container: {"events": [annotation]}}, {"seq": 2}]
    assert _failure_priority(rows[1]) == 0
    assert [row["seq"] for row in _ends(rows, 1)] == [0]
    assert _failure_priority({"kind": "log", "events": [annotation]}) == 0
    assert _failure_priority({"events": [annotation]}) == 3
    assert _failure_priority({"evidence": {"causal_windows": [{"at": 1, "events": [annotation]}]}}) == 3


def test_malformed_raw_annotation_role_is_not_a_priority_record():
    assert _failure_priority({"required_for_failure": True, "selection_role": ["failure_witness"]}) == 0
