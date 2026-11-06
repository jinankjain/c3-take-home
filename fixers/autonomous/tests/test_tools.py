from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

import pytest
import yaml

from fixer.patches import SourceManager, source_hash
from fixer.retrieval import Corpus
from fixer.runtime import CommandResult, Deadline
from fixer.tools import TOOL_DEFINITIONS, ToolDispatcher


class FakeSimulator:
    def __init__(self):
        self.calls = []

    def tests(self, root, *, timeout=60, reserve=0):
        self.calls.append({"root": Path(root), "timeout": timeout, "reserve": reserve})
        return CommandResult(("python", "-m", "pytest"), 0, "1 passed", "", 0.01)


class FakeRun:
    def __init__(self, controller, source, scenario, seed, faults):
        self.run_id = f"run-{len(controller.runs) + 1}"
        source_root = {"candidate": controller.sources.candidate,
                       "original": controller.sources.original,
                       "accepted": controller.sources.best}[source]
        self.source_hash = source_hash(source_root / "src")
        self.scenario = scenario
        self.scenario_hash = hashlib.sha256(scenario.encode()).hexdigest()
        self.seed = seed
        self.faults = faults
        self.faults_hash = None
        self.out_dir = controller.sources.scratch / "runs" / self.run_id
        self.out_dir.mkdir(parents=True)
        self.trace_path = self.out_dir / "trace.jsonl"
        events = [
            {"kind": "msg_send", "seq": 0, "t": 0, "method": "submit",
             "payload": {"task": "task-fixture", "job": "job-fixture"}},
            {"kind": "log", "seq": 1, "t": 1, "event": "submit_unknown", "task": "task-fixture"},
        ]
        self.trace_path.write_text("".join(json.dumps(event) + "\n" for event in events))
        self.summary = {"scenario": scenario, "seed": seed,
                        "trace_sha256": hashlib.sha256(self.trace_path.read_bytes()).hexdigest()}

    def as_dict(self, compact=True):
        return {"run_id": self.run_id, "source_hash": self.source_hash,
                "scenario": self.scenario, "scenario_hash": self.scenario_hash,
                "seed": self.seed, "faults": self.faults,
                "trace": str(self.trace_path), "ok": False, "status": "invariant_failure"}


class FakeController:
    def __init__(self, live, corpus_root, scratch):
        self.sources = SourceManager(live, scratch)
        self.simulator = FakeSimulator()
        self.corpus = Corpus(corpus_root, codebase="Test")
        self.deadline = Deadline(time.time() + 900)
        self.final_reserve = 0
        self.probe_root = self.sources.scratch / "probes"
        self.runs = {}
        self.hypotheses = {}
        self.replay_calls = []
        self.validation_calls = []
        self.hypothesis_calls = []

    def status(self):
        return {"source_hash": self.sources.candidate_hash,
                "accepted_hash": self.sources.best_hash,
                "remaining_s": self.deadline.remaining()}

    def replay(self, source, scenario, seed, faults=None):
        self.replay_calls.append({"source": source, "scenario": scenario,
                                  "seed": seed, "faults": faults})
        run = FakeRun(self, source, scenario, seed, faults)
        self.runs[run.run_id] = run
        return run

    def validate(self, plan):
        self.validation_calls.append(plan)
        return {"accepted": False, "source_hash": self.sources.candidate_hash,
                "reason": "fake controller retains ownership of acceptance"}

    def record_hypothesis(self, args):
        self.hypothesis_calls.append(args)
        self.hypotheses[args["id"]] = dict(args)
        return {"recorded": True, **args}


@pytest.fixture
def controller(tmp_path):
    live = tmp_path / "live case"
    for directory in ("src", "tests", "sim", "scenarios/faults"):
        (live / directory).mkdir(parents=True)
    (live / "src" / "main.py").write_text("VALUE = 1\n")
    (live / "tests" / "test_value.py").write_text("from main import VALUE\ndef test_value(): assert VALUE == 1\n")
    (live / "sim" / "__init__.py").write_text("")
    (live / "scenarios" / "public.yaml").write_text("name: public\nfaults: faults/network.yaml\n")
    (live / "scenarios" / "faults" / "network.yaml").write_text("network: {drop: 0.01}\n")
    (live / "README.md").write_text("# Fixture service\n")
    corpus_root = tmp_path / "corpus"
    (corpus_root / "service" / "Test").mkdir(parents=True)
    (corpus_root / "platform").mkdir()
    (corpus_root / "INDEX.md").write_text("service/Test/design.md CURRENT\n")
    (corpus_root / "service" / "Test" / "design.md").write_text(
        "# Operation identity\nStatus: CURRENT\nAn operation identity survives retries.\n"
    )
    (corpus_root / "platform" / "runtime.md").write_text("# Runtime\nStatus: CURRENT\n")
    return FakeController(live, corpus_root, tmp_path / "scratch")


@pytest.fixture
def dispatcher(controller):
    return ToolDispatcher(controller)


def success(dispatcher, name, args):
    answer = dispatcher.execute(name, args)
    assert answer["ok"] is True, answer
    assert isinstance(answer["result"], dict)
    return answer["result"]


def failure(dispatcher, name, args):
    answer = dispatcher.execute(name, args)
    assert answer["ok"] is False, answer
    assert isinstance(answer["error"], dict)
    assert answer["error"]["code"]
    assert answer["error"]["message"]
    return answer["error"]


def test_schemas_advertise_only_the_safe_controller_tools():
    expected = {"status", "list_files", "read_file", "search", "trace_slice", "replay",
                "run_tests", "write_probe", "apply_edits", "reset_candidate",
                "record_hypothesis", "validate"}
    assert {definition["name"] for definition in TOOL_DEFINITIONS} == expected
    assert len(TOOL_DEFINITIONS) == len(expected)
    for definition in TOOL_DEFINITIONS:
        assert isinstance(definition["description"], str)
        assert definition["input_schema"]["type"] == "object"
        assert definition["input_schema"]["additionalProperties"] is False


def test_read_file_distinguishes_candidate_original_and_accepted(controller, dispatcher):
    old_hash = controller.sources.candidate_hash
    success(dispatcher, "apply_edits", {"base_hash": old_hash,
            "edits": [{"path": "src/main.py", "old": "VALUE = 1", "new": "VALUE = 2"}]})
    for root, expected in (("case", 2), ("original", 1), ("accepted", 1)):
        result = success(dispatcher, "read_file", {"root": root, "path": "src/main.py", "start": 1, "end": 1})
        assert result["root"] == root
        assert f"VALUE = {expected}" in result["text"]
        selected = {"case": controller.sources.candidate, "original": controller.sources.original,
                    "accepted": controller.sources.best}[root]
        assert result["source_hash"] == source_hash(selected / "src")
    assert (controller.sources.live / "src" / "main.py").read_text() == "VALUE = 1\n"


@pytest.mark.parametrize("root", ["case", "original", "accepted", "corpus"])
def test_read_file_rejects_relative_path_escape(dispatcher, root):
    failure(dispatcher, "read_file", {"root": root, "path": "../outside.txt"})


def test_absolute_and_symlink_reads_cannot_reveal_outside_files(controller, dispatcher, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret sentinel that must not leave the fixture")
    failure(dispatcher, "read_file", {"root": "case", "path": str(outside)})
    (controller.sources.candidate / "notes").mkdir()
    (controller.sources.candidate / "notes" / "escape.txt").symlink_to(outside)
    answer = failure(dispatcher, "read_file", {"root": "case", "path": "notes/escape.txt"})
    assert "secret sentinel" not in json.dumps(answer)


def test_secret_names_remain_excluded_even_if_present_in_stage(controller, dispatcher):
    (controller.sources.candidate / ".env").write_text("API_KEY=secret sentinel")
    answer = failure(dispatcher, "read_file", {"root": "case", "path": ".env"})
    assert "secret sentinel" not in json.dumps(answer)


@pytest.mark.parametrize("name,args", [
    ("promote", {}), ("__getattribute__", {"name": "sources"}),
    ("status", {"command": "arbitrary execution"}),
    ("read_file", {"root": "live", "path": "src/main.py"}),
    ("replay", {"source": "candidate", "scenario": "public", "seed": True}),
    ("status", []),
])
def test_unknown_tools_and_arguments_are_not_executed(dispatcher, controller, name, args):
    failure(dispatcher, name, args)
    assert not controller.replay_calls


def test_stale_edit_fails_without_changing_current_source(controller, dispatcher):
    original = controller.sources.candidate_hash
    success(dispatcher, "apply_edits", {"base_hash": original,
            "edits": [{"path": "src/main.py", "content": "VALUE = 2\n"}]})
    current = controller.sources.candidate_hash
    error = failure(dispatcher, "apply_edits", {"base_hash": original,
            "edits": [{"path": "src/main.py", "content": "VALUE = 3\n"}]})
    assert "stale" in error["message"].lower()
    assert controller.sources.candidate_hash == current
    assert (controller.sources.candidate / "src" / "main.py").read_text() == "VALUE = 2\n"


@pytest.mark.parametrize("path", ["tests/test_value.py", "sim/__init__.py", "scenarios/public.yaml", "../outside.py"])
def test_edits_cannot_change_test_simulator_or_scenario_inputs(controller, dispatcher, path):
    before = controller.sources.candidate_hash
    tests_before = (controller.sources.candidate / "tests" / "test_value.py").read_bytes()
    failure(dispatcher, "apply_edits", {"base_hash": before, "edits": [{"path": path, "content": "changed"}]})
    assert controller.sources.candidate_hash == before
    assert (controller.sources.candidate / "tests" / "test_value.py").read_bytes() == tests_before


def test_reset_restores_accepted_source_without_touching_live(controller, dispatcher):
    success(dispatcher, "apply_edits", {"base_hash": controller.sources.candidate_hash,
            "edits": [{"path": "src/main.py", "content": "VALUE = 2\n"}]})
    success(dispatcher, "reset_candidate", {})
    assert controller.sources.candidate_hash == controller.sources.best_hash
    assert (controller.sources.live / "src" / "main.py").read_text() == "VALUE = 1\n"


@pytest.mark.parametrize("reference", ["faults/network.yaml", "scenarios/faults/network.yaml"])
def test_probe_fault_references_bind_to_unchanged_staged_scenarios(controller, dispatcher, reference):
    original_faults = (controller.sources.original / "scenarios" / "faults" / "network.yaml").read_bytes()
    result = success(dispatcher, "write_probe", {"name": "network-cut",
            "scenario": {"name": "small", "workload": [], "faults": reference}})
    assert result["probe"] == "probe:network-cut"
    path = Path(result["path"])
    assert path == controller.sources.scratch / "probes" / "network-cut.yaml"
    document = yaml.safe_load(path.read_text())
    assert Path(document["faults"]) == controller.sources.candidate / "scenarios" / "faults" / "network.yaml"
    assert (controller.sources.original / "scenarios" / "faults" / "network.yaml").read_bytes() == original_faults
    assert not (controller.sources.live / "scenarios" / "network-cut.yaml").exists()


def test_probe_names_and_external_fault_references_are_rejected(controller, dispatcher, tmp_path):
    outside = tmp_path / "outside.yaml"
    outside.write_text("network: {drop: 1.0}\n")
    for name in ("../escape", "/absolute", "with space", "x" * 65):
        failure(dispatcher, "write_probe", {"name": name, "scenario": {}})
    for reference in ("../../outside.yaml", "../outside.yaml", str(outside)):
        failure(dispatcher, "write_probe", {"name": "outside", "scenario": {"faults": reference}})
    assert not (controller.sources.scratch / "probes" / "outside.yaml").exists()


def test_symlink_fault_reference_is_rejected(controller, dispatcher, tmp_path):
    outside = tmp_path / "outside.yaml"
    outside.write_text("network: {drop: 1.0}\n")
    (controller.sources.candidate / "scenarios" / "faults" / "escape.yaml").symlink_to(outside)
    failure(dispatcher, "write_probe", {"name": "bad-link", "scenario": {"faults": "faults/escape.yaml"}})


def test_probe_creation_is_immutable_and_bounded(controller, dispatcher):
    first = success(dispatcher, "write_probe", {"name": "first", "scenario": {"name": "first"}})
    before = Path(first["path"]).read_bytes()
    failure(dispatcher, "write_probe", {"name": "first", "scenario": {"name": "rewritten"}})
    assert Path(first["path"]).read_bytes() == before
    failure(dispatcher, "write_probe", {"name": "oversized", "scenario": {"payload": "x" * 70000}})
    assert not (controller.sources.scratch / "probes" / "oversized.yaml").exists()
    for number in range(23):
        success(dispatcher, "write_probe", {"name": f"probe-{number}", "scenario": {"name": str(number)}})
    failure(dispatcher, "write_probe", {"name": "twenty-fifth", "scenario": {}})


def test_replay_execution_preserves_source_choice_and_failure_evidence(controller, dispatcher):
    result = success(dispatcher, "replay", {"source": "accepted", "scenario": "public", "seed": 7,
            "faults": "scenarios/faults/network.yaml"})
    assert controller.replay_calls[-1]["source"] == "accepted"
    assert controller.replay_calls[-1]["seed"] == 7
    assert result["ok"] is False
    assert result["status"] == "invariant_failure"
    assert result["run_id"] in controller.runs
    assert result["source_hash"] == controller.sources.best_hash


def test_trace_slice_reads_only_registered_run_and_retains_provenance(controller, dispatcher):
    run = success(dispatcher, "replay", {"source": "candidate", "scenario": "public", "seed": 1})
    result = success(dispatcher, "trace_slice", {"run_id": run["run_id"], "entities": ["task-fixture"],
            "event_types": ["submit"], "limit": 1})
    assert len(result["events"]) == 1
    assert result["events"][0]["method"] == "submit"
    assert result["source_hash"] == run["source_hash"]
    failure(dispatcher, "trace_slice", {"run_id": "../../outside", "entities": []})


def test_run_tests_binds_accepted_stage_and_rejects_unbounded_time(controller, dispatcher):
    result = success(dispatcher, "run_tests", {"source": "accepted", "timeout": 30})
    assert controller.simulator.calls[-1]["root"] == controller.sources.best
    assert controller.simulator.calls[-1]["timeout"] == 30
    assert result["returncode"] == 0
    assert result["source_hash"] == controller.sources.best_hash
    before = len(controller.simulator.calls)
    for timeout in (0, 61, True, float("inf")):
        failure(dispatcher, "run_tests", {"source": "candidate", "timeout": timeout})
    assert len(controller.simulator.calls) == before


def test_search_and_listing_use_stage_and_ranked_corpus(controller, dispatcher):
    result = success(dispatcher, "list_files", {"root": "case", "pattern": "src/*", "limit": 2})
    assert "src/main.py" in json.dumps(result)
    case_matches = success(dispatcher, "search", {"root": "case", "query": "VALUE", "path": "src"})
    assert "src/main.py" in json.dumps(case_matches)
    assert case_matches["source_hash"] == controller.sources.candidate_hash
    corpus_matches = success(dispatcher, "search", {"root": "corpus", "query": "operation identity", "path": "service"})
    assert "service/Test/design.md" in json.dumps(corpus_matches)
    assert "CURRENT" in json.dumps(corpus_matches)


def test_hypotheses_cannot_self_mark_verified(controller, dispatcher):
    arguments = {"id": "h1", "obligation": "stable identity", "mechanism": "retry changes identity",
                 "evidence": ["src/main.py:1", "trace witness"], "status": "proposed"}
    success(dispatcher, "record_hypothesis", arguments)
    assert controller.hypotheses["h1"]["status"] == "proposed"
    for status in ("verified", "fixed", "accepted"):
        failure(dispatcher, "record_hypothesis", {**arguments, "status": status})
    assert len(controller.hypothesis_calls) == 1


def test_validate_forwards_plan_without_owning_acceptance(controller, dispatcher):
    plan = {"target_invariants": ["I2"], "reproductions": [{"scenario": "public", "seed": 11}],
            "reason": "test the repaired obligation"}
    result = success(dispatcher, "validate", plan)
    assert controller.validation_calls[-1]["target_invariants"] == ["I2"]
    assert controller.validation_calls[-1]["reproductions"][0]["scenario"] == "public"
    assert controller.validation_calls[-1]["reproductions"][0]["seed"] == 11
    assert result["accepted"] is False
