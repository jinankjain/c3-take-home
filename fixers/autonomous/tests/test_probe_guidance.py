from __future__ import annotations

import json

import pytest

from controller_support import Reviewer, controller, plan, repair_ownership
from fixer.main import _probe_fingerprints, compact_messages, prompt_status, repair_loop
from fixer.model import ModelReply
from fixer.probe_guidance import MAX_FEEDBACK_CHARS, MAX_GUIDANCE_CHARS, bounded_feedback, construction_guidance, correction_record
from fixer.runtime import TimeLimitError
from fixer.tools import ToolDispatcher


def configuration_inputs(instance):
    for root in (instance.sources.original, instance.sources.best, instance.sources.candidate, instance.sources.live):
        scenarios = root / "scenarios"
        (scenarios / "public.yaml").write_text("name: public\nfaults: network.yaml\n")
        (scenarios / "network.yaml").write_text("faults:\n  network:\n    latency_s: 1\n")
        (scenarios / "slower.yaml").write_text("faults:\n  network:\n    latency_s: 2\n")


def test_guidance_reuses_protected_reads_and_resolves_inherited_fault_values(controller):
    configuration_inputs(controller)
    _, reader = _probe_fingerprints(ToolDispatcher(controller))
    reads = reader.reads[0]
    source, accepted = controller.sources.candidate_hash, controller.sources.best_hash
    text = construction_guidance(reader, [{"source_hash": source, "scenario": "public"}], None, source, accepted)
    context = json.loads(text)
    assert reader.reads[0] == reads
    assert context["bases"][0]["resolved_document"]["faults"]["network"]["latency_s"] == 1
    assert len(context["bases"][0]["file_sha256"]) == 64
    assert len(text) <= MAX_GUIDANCE_CHARS


def test_correction_keeps_exact_conflict_paths_within_the_escaped_json_bound():
    error = {"code": "composition_conflict", "message": "Explicit overrides are required.", "conflicts_total": 12,
             "conflicts": [{"path": ["faults", '"' * 700], "existing_value": "1", "incoming_value": "2"} for _ in range(12)]}
    record = bounded_feedback({"source_hash": "a" * 64, "accepted_source_hash": "b" * 64,
                               "request": {"base": "public"}, "error": error})
    assert len(json.dumps(record, ensure_ascii=False)) <= MAX_FEEDBACK_CHARS
    rows = record["error"]["conflicts"]
    assert rows and all(row["path"][1] == '"' * 700 for row in rows)
    assert record["error"]["conflicts_omitted"] == 12 - len(rows)


def test_feedback_excludes_old_source_errors_and_retains_current_input_corrections(controller):
    configuration_inputs(controller)
    _, reader = _probe_fingerprints(ToolDispatcher(controller))
    source = controller.sources.candidate_hash
    failed = correction_record(source, source, {"base": "public", "overrides": {"seeds": [1, 2]}},
                               {"ok": True}, {"complete": False, "reason": "configuration failure",
                                              "before": {"run_id": "original-run", "source_hash": source,
                                                         "status": "configuration_error", "error": "unsupported field seeds"}})
    controller._last_probe_construction = failed
    compacted = compact_messages(controller, [{"role": "user", "content": "Earlier reads."}], force=True)
    assert "unsupported field seeds" in json.dumps(compacted)
    assert json.loads(prompt_status(controller))["probe_construction"]["accepted_replay"]["run_id"] == "original-run"
    stale = {**failed, "source_hash": "0" * 64}
    guidance = json.loads(construction_guidance(reader, [], stale, source, source))
    assert guidance["previous_construction_failure"] is None


def test_guidance_propagates_cancellation_before_requesting_a_paid_correction(controller):
    _, reader = _probe_fingerprints(ToolDispatcher(controller))
    controller.deadline.stop_event.set()
    with pytest.raises(TimeLimitError):
        construction_guidance(reader, [], None, controller.sources.candidate_hash, controller.sources.best_hash)


def test_second_forced_attempt_resolves_the_reported_conflict_without_clearing_a_rejection(controller):
    configuration_inputs(controller)
    original = controller.sources.best_hash
    candidate = repair_ownership(controller)["source_hash"]
    controller.model = Reviewer(False)
    assert not controller.validate(plan())["accepted"]

    class ProbeModel(Reviewer):
        reads = 0
        attempts = 0

        def messages(self, system, messages, tools, **kwargs):
            if any(tool.get("name") == "submit_review" for tool in tools):
                raise AssertionError("Diagnostic construction must not retry a rejected review.")
            if kwargs.get("tool_choice", {}).get("name") == "compose_probe":
                self.attempts += 1
                context = json.dumps(messages)
                if self.attempts == 2:
                    assert "composition_conflict" in context and "latency_s" in context
                    assert "previous_construction_failure" in context
                arguments = {"name": "diagnostic-" + str(self.attempts), "base": "public", "fault_profiles": ["slower"]}
                if self.attempts == 2:
                    arguments["overrides"] = {"faults": {"network": {"latency_s": 2.5}}}
                return ModelReply([{"type": "tool_use", "id": "compose-" + str(self.attempts),
                                    "name": "compose_probe", "input": arguments}], "tool_use", {}, 0, 3)
            self.reads += 1
            if self.attempts >= 2:
                return ModelReply([{"type": "text", "text": "The diagnostic is measured; the prior repair is still rejected."}],
                                  "end_turn", {}, 0, 3)
            return ModelReply([{"type": "tool_use", "id": "read-" + str(self.reads), "name": "read_file",
                                "input": {"path": "src/service.py", "start": 1, "end": 4}}], "tool_use", {}, 0, 3)

    model = ProbeModel()
    repair_loop(controller, model)
    assert model.attempts == 2
    assert controller._last_probe_intervention["complete"]
    assert controller._last_probe_construction is None
    assert not controller.accepted and controller.sources.best_hash == original
    assert controller.sources.candidate_hash == candidate
    assert controller.validation_history[candidate]["accepted"] is False
