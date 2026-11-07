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
                "run_tests", "write_probe", "compose_probe", "apply_edits", "reset_candidate",
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
    assert result["current_source_hash"] == controller.sources.candidate_hash


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


def write_fault(controller, name, document):
    path = controller.sources.candidate / "scenarios" / "faults" / (name + ".yaml")
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    return path


def test_composed_restart_loss_probe_is_immutable_and_binds_all_inputs(controller, dispatcher):
    from fixer.validation import _scenario_hashes

    source_before = controller.sources.candidate_hash
    shipped = controller.sources.candidate / "scenarios" / "public.yaml"
    shipped_before = shipped.read_bytes()
    restart = success(dispatcher, "write_probe", {"name": "restart", "scenario": {
        "faults": {"nodes": {"crashes": [{"node": "worker-1", "at": 8, "restart_after": 3}]}},
        "workload": [{"at": 8.5, "action": "submit", "args": {"task": "interaction-task"}}],
    }})
    loss = write_fault(controller, "loss", {"network": {"duplicate": .02}, "gpu": {"lost_reply": .3}})
    args = {"name": "restart-loss", "base": "public", "probes": [restart["probe"]],
            "fault_profiles": ["scenarios/faults/loss.yaml"]}
    result = success(dispatcher, "compose_probe", args)
    path = Path(result["path"])
    document = yaml.safe_load(path.read_text())
    assert document["faults"] == {
        "network": {"drop": .01, "duplicate": .02}, "gpu": {"lost_reply": .3},
        "nodes": {"crashes": [{"node": "worker-1", "at": 8, "restart_after": 3}]},
    }
    assert document["workload"][0]["at"] == 8.5
    assert result["source_hash"] == source_before
    assert result["scenario_hash"] == _scenario_hashes(controller.sources.candidate, str(path), None)[0]
    assert result["probe_hash"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result["composition"]["base"]["reference"] == "public"
    assert result["composition"]["probes"][0]["reference"] == "probe:restart"
    assert result["composition"]["fault_profiles"][0]["sha256"] == hashlib.sha256(loss.read_bytes()).hexdigest()
    assert result["composition"]["inherited_faults"][0]["reference"] == "scenarios/faults/network.yaml"
    assert result["composition"]["read_files"] == 4
    before = path.read_bytes()
    failure(dispatcher, "compose_probe", args)
    assert path.read_bytes() == before and shipped.read_bytes() == shipped_before
    # The effective scenario no longer depends on a subsequently changed profile.
    loss.write_text("gpu: {lost_reply: 1}\n")
    assert _scenario_hashes(controller.sources.candidate, str(path), None)[0] == result["scenario_hash"]
    assert not (controller.sources.live / "scenarios" / "restart-loss.yaml").exists()
    assert controller.sources.candidate_hash == source_before


def test_composition_scalar_conflicts_require_the_matching_nested_override(controller, dispatcher):
    write_fault(controller, "different-drop", {"network": {"drop": .02}})
    args = {"name": "conflict", "base": "public", "fault_profiles": ["faults/different-drop.yaml"]}
    error = failure(dispatcher, "compose_probe", args)
    assert "faults.network.drop" in error["message"]
    failure(dispatcher, "compose_probe", {**args, "overrides": {"faults": {"gpu": {"lost_reply": .2}}}})
    assert not (controller.sources.scratch / "probes" / "conflict.yaml").exists()
    result = success(dispatcher, "compose_probe", {**args, "overrides": {"faults": {"network": {"drop": .05}}}})
    assert yaml.safe_load(Path(result["path"]).read_text())["faults"]["network"]["drop"] == .05


def test_composition_preserves_ranges_and_rejects_conflicting_clock_records(controller, dispatcher):
    clock = {"node": "worker-1", "offset": [-.1, .1], "drift_ppm": [-10, 10]}
    replacement = {**clock, "offset": [-.2, .2]}
    base = success(dispatcher, "write_probe", {"name": "clock", "scenario": {
        "faults": {"nodes": {"clocks": [clock]}}
    }})
    write_fault(controller, "clock-change", {"nodes": {"clocks": [replacement]}})
    args = {"name": "clock-range", "base": base["probe"], "fault_profiles": ["faults/clock-change.yaml"]}
    error = failure(dispatcher, "compose_probe", args)
    assert "faults.nodes.clocks" in error["message"]
    result = success(dispatcher, "compose_probe", {**args, "overrides": {
        "faults": {"nodes": {"clocks": [replacement]}}
    }})
    assert yaml.safe_load(Path(result["path"]).read_text())["faults"]["nodes"]["clocks"] == [replacement]


def test_composition_deduplicates_identical_records_and_combines_distinct_restart_times(controller, dispatcher):
    first = {"node": "worker-1", "at": 10, "restart_after": 1}
    second = {"node": "worker-1", "at": 20, "restart_after": 2}
    base = success(dispatcher, "write_probe", {"name": "crash-base", "scenario": {
        "faults": {"nodes": {"crashes": [first]}}
    }})
    write_fault(controller, "crash-more", {"nodes": {"crashes": [first, second]}})
    result = success(dispatcher, "compose_probe", {"name": "crashes", "base": base["probe"],
            "fault_profiles": ["faults/crash-more.yaml"]})
    assert yaml.safe_load(Path(result["path"]).read_text())["faults"]["nodes"]["crashes"] == [first, second]
    write_fault(controller, "crash-conflict", {"nodes": {"crashes": [{**first, "restart_after": 99}]}})
    failure(dispatcher, "compose_probe", {"name": "bad-crash", "base": base["probe"],
            "fault_profiles": ["faults/crash-conflict.yaml"]})


@pytest.mark.parametrize("reference", ["../outside.yaml", "/outside.yaml", "scenarios/../outside.yaml"])
def test_composition_rejects_path_escapes_before_publishing(controller, dispatcher, reference):
    failure(dispatcher, "compose_probe", {"name": "escape", "base": reference})
    failure(dispatcher, "compose_probe", {"name": "escape", "base": "public", "fault_profiles": [reference]})
    failure(dispatcher, "compose_probe", {"name": "escape", "base": "public", "overrides": {"faults": reference}})
    assert not (controller.sources.scratch / "probes" / "escape.yaml").exists()


def test_composition_rejects_symlinks_cycles_invalid_yaml_and_oversized_inputs(controller, dispatcher, tmp_path):
    outside = tmp_path / "outside.yaml"
    outside.write_text("secret: sentinel\n")
    (controller.sources.candidate / "scenarios" / "faults" / "linked.yaml").symlink_to(outside)
    error = failure(dispatcher, "compose_probe", {"name": "link", "base": "public",
                    "fault_profiles": ["faults/linked.yaml"]})
    assert "sentinel" not in json.dumps(error)
    write_fault(controller, "a", {"faults": "b.yaml"})
    write_fault(controller, "b", {"faults": "a.yaml"})
    error = failure(dispatcher, "compose_probe", {"name": "cycle", "base": "public", "fault_profiles": ["faults/a.yaml"]})
    assert "cyclic" in error["message"]
    invalid = controller.sources.candidate / "scenarios" / "faults" / "invalid.yaml"
    invalid.write_text("sentinel: [unfinished\n")
    error = failure(dispatcher, "compose_probe", {"name": "invalid", "base": "public", "fault_profiles": ["faults/invalid.yaml"]})
    assert "sentinel" not in error["message"]
    invalid.write_text("payload: " + "x" * 70_000)
    failure(dispatcher, "compose_probe", {"name": "large", "base": "public", "fault_profiles": ["faults/invalid.yaml"]})
    assert not list(controller.sources.scratch.glob("probes/*.yaml"))


def test_composition_inlines_explicit_fault_reference_override(controller, dispatcher):
    write_fault(controller, "override", {"gpu": {"lost_reply": .4}})
    result = success(dispatcher, "compose_probe", {"name": "fault-override", "base": "public",
            "overrides": {"faults": "faults/override.yaml"}})
    assert yaml.safe_load(Path(result["path"]).read_text())["faults"] == {"gpu": {"lost_reply": .4}}
    assert result["composition"]["inherited_faults"][-1]["reference"] == "scenarios/faults/override.yaml"


def test_composition_bounds_section_preview_without_losing_reference_hashes(controller, dispatcher):
    write_fault(controller, "many-sections", {str(i) + "\x01" * 110: {} for i in range(40)})
    result = success(dispatcher, "compose_probe", {"name": "many", "base": "public", "fault_profiles": ["faults/many-sections.yaml"]})
    assert len(json.dumps(result, ensure_ascii=False)) <= 24_000
    assert result["fault_sections_omitted"] == 25
    assert result["fault_section_names_truncated"] > 0
    assert len(result["scenario_hash"]) == 64
    assert result["composition"]["fault_profiles"][0]["reference"] == "faults/many-sections.yaml"


def test_composition_reports_all_unresolved_paths_and_publishes_only_after_explicit_resolution(controller, dispatcher):
    existing = {"network": {"drop.rate": .01}, "gpu": {"latency": [1, 2]},
                "nodes": {"clocks": [{"node": "worker-1", "offset": .1}]}}
    incoming = {"network": {"drop.rate": .2}, "gpu": {"latency": [2, 3]},
                "nodes": {"clocks": [{"node": "worker-1", "offset": .3}]}}
    shipped = controller.sources.candidate / "scenarios" / "public.yaml"
    shipped.write_text(yaml.safe_dump({"name": "public", "faults": existing}, sort_keys=False))
    shipped_bytes = shipped.read_bytes()
    write_fault(controller, "different-settings", incoming)
    args = {"name": "resolved-settings", "base": "public", "fault_profiles": ["faults/different-settings.yaml"]}
    error = failure(dispatcher, "compose_probe", args)

    assert error["code"] == "composition_conflict"
    assert error["source_hash"] == controller.sources.candidate_hash
    assert error["conflicts_total"] == 3 and error["conflicts_omitted"] == 0
    rows = {tuple(row["path"]): row for row in error["conflicts"]}
    assert set(rows) == {("faults", "network", "drop.rate"), ("faults", "gpu", "latency"),
                         ("faults", "nodes", "clocks")}
    assert json.loads(rows[("faults", "network", "drop.rate")]["existing_value"]) == .01
    assert json.loads(rows[("faults", "network", "drop.rate")]["incoming_value"]) == .2
    assert rows[("faults", "nodes", "clocks")]["required_override"] == "whole_list"
    assert not rows[("faults", "gpu", "latency")]["incoming_truncated"]
    assert not list(controller.probe_root.glob("*.yaml"))

    partial = {**args, "overrides": {"faults": {"network": incoming["network"]}}}
    remaining = failure(dispatcher, "compose_probe", partial)
    assert remaining["conflicts_total"] == 2
    assert not list(controller.probe_root.glob("*.yaml"))
    result = success(dispatcher, "compose_probe", {**args, "overrides": {"faults": incoming}})
    assert yaml.safe_load(Path(result["path"]).read_text())["faults"] == incoming
    assert shipped.read_bytes() == shipped_bytes
    assert result["probe_hash"] == hashlib.sha256(Path(result["path"]).read_bytes()).hexdigest()
    failure(dispatcher, "compose_probe", {**args, "overrides": {"faults": incoming}})


def test_composition_conflict_previews_and_count_are_bounded(controller, dispatcher):
    from fixer.evidence import MAX_TRACE_CHARS
    from fixer.tools import MAX_CONFLICT_VALUE_CHARS

    original = {"setting-" + str(number): '"before\\' * 180 for number in range(16)}
    changed = {key: '"after\\' * 180 for key in original}
    base = success(dispatcher, "write_probe", {"name": "many-settings", "scenario": {"faults": original}})
    write_fault(controller, "many-changes", changed)
    answer = dispatcher.execute("compose_probe", {"name": "not-published", "base": base["probe"],
                                                  "fault_profiles": ["faults/many-changes.yaml"]})
    assert not answer["ok"]
    error = answer["error"]
    assert error["conflicts_total"] == 16
    assert 0 < len(error["conflicts"]) <= 12
    assert error["conflicts_omitted"] == 16 - len(error["conflicts"])
    assert len(json.dumps(answer, ensure_ascii=False)) <= MAX_TRACE_CHARS
    for row in error["conflicts"]:
        assert row["existing_truncated"] and row["incoming_truncated"]
        assert len(row["existing_value"]) <= MAX_CONFLICT_VALUE_CHARS
        assert len(row["incoming_value"]) <= MAX_CONFLICT_VALUE_CHARS
    assert not (controller.probe_root / "not-published.yaml").exists()


def test_composition_preserves_exact_escaped_paths_within_the_response_budget(controller, dispatcher):
    from fixer.evidence import MAX_TRACE_CHARS

    keys = ['prefix-"\\\n' * 11 + str(number) for number in range(8)]

    def nested(value):
        document = {"setting-" + str(number): value for number in range(16)}
        for key in reversed(keys):
            document = {key: document}
        return {"faults": document}

    base = success(dispatcher, "write_probe", {"name": "escaped-settings", "scenario": nested('"old\\' * 120)})
    write_fault(controller, "escaped-changes", nested('"new\\' * 120))
    answer = dispatcher.execute("compose_probe", {"name": "escaped-result", "base": base["probe"],
                                                  "fault_profiles": ["faults/escaped-changes.yaml"]})
    assert not answer["ok"]
    error = answer["error"]
    assert error["conflicts_total"] == 16
    assert error["conflicts_omitted"] == 16 - len(error["conflicts"])
    assert len(json.dumps(answer, ensure_ascii=False)) <= MAX_TRACE_CHARS
    assert error["conflicts"]
    for row in error["conflicts"]:
        assert row["path"][:-1] == ["faults", *keys]
        assert row["path"][-1].startswith("setting-")
    assert not (controller.probe_root / "escaped-result.yaml").exists()


def test_composition_cancellation_during_conflict_collection_publishes_nothing(controller, dispatcher, monkeypatch):
    from fixer.tools import _CompositionConflicts

    write_fault(controller, "cancel-changes", {"network": {"drop": .4, "new_setting": 1}, "other": 2})
    original_add = _CompositionConflicts.add

    def cancel_after_conflict(self, *args, **kwargs):
        original_add(self, *args, **kwargs)
        controller.deadline.stop_event.set()

    monkeypatch.setattr(_CompositionConflicts, "add", cancel_after_conflict)
    error = failure(dispatcher, "compose_probe", {"name": "cancelled", "base": "public",
                                                 "fault_profiles": ["faults/cancel-changes.yaml"]})
    assert error["code"] == "deadline"
    assert not list(controller.probe_root.glob("*.yaml"))


@pytest.mark.parametrize("arguments", [
    {"name": "bad-keys", "base": "public", "seeds": [1, 2]},
    {"name": "missing-base"},
])
def test_composition_argument_errors_advertise_allowed_and_required_keys(controller, dispatcher, arguments):
    error = failure(dispatcher, "compose_probe", arguments)
    assert error["code"] == "invalid_arguments"
    assert error["allowed_keys"] == ["base", "fault_profiles", "name", "overrides", "probes"]
    assert error["required_keys"] == ["base", "name"]
    assert not (controller.sources.scratch / "probes").exists()


@pytest.mark.parametrize("plan", [
    None, {}, {"reason": "valid", "unknown": True}, {"reason": "x" * 6001},
    {"reason": "valid", "target_invariants": ["INV"] * 13},
    {"reason": "valid", "reproductions": [{"scenario": "public", "seed": 1}] * 13},
    {"reason": "valid", "reproductions": [{"scenario": "public", "seed": True}]},
    {"reason": "valid", "reproductions": [{"scenario": "../outside.yaml", "seed": 1}]},
    {"reason": "valid", "reproductions": [{"scenario": "public", "seed": 1, "faults": "../outside.yaml"}]},
])
def test_invalid_repair_metadata_cannot_change_source_or_capture_intent(controller, dispatcher, plan):
    calls = []
    controller.remember_repair_plan = lambda *args: calls.append(args)
    before = controller.sources.candidate_hash
    answer = dispatcher.execute("apply_edits", {"base_hash": before, "repair_plan": plan,
            "edits": [{"path": "src/main.py", "content": "VALUE = 2\n"}]})
    assert not answer["ok"] and "result" not in answer
    assert controller.sources.candidate_hash == before
    assert (controller.sources.candidate / "src/main.py").read_text() == "VALUE = 1\n"
    assert not calls


def test_edit_plan_is_captured_after_success_with_exact_source_identity(controller, dispatcher):
    calls = []

    def remember(base_hash, new_hash, plan):
        assert new_hash == controller.sources.candidate_hash
        calls.append((base_hash, new_hash, plan))
        return {**plan, "source_hash": new_hash, "base_source_hash": base_hash}

    controller.remember_repair_plan = remember
    base = controller.sources.candidate_hash
    plan = {"reason": "Preserve the operation across recovery.", "target_invariants": ["RECOVERY"],
            "reproductions": [{"scenario": "public", "seed": 1, "faults": "scenarios/faults/network.yaml"}]}
    result = success(dispatcher, "apply_edits", {"base_hash": base, "repair_plan": plan,
            "edits": [{"path": "src/main.py", "content": "VALUE = 2\n"}]})
    assert calls == [(base, result["source_hash"], plan)]
    assert result["repair_plan"] == {**plan, "source_hash": result["source_hash"], "base_source_hash": base}
    assert not controller.validation_calls
    assert (controller.sources.live / "src/main.py").read_text() == "VALUE = 1\n"


def test_metadata_free_edit_notifies_capture_with_none_and_does_not_reuse_the_previous_plan(controller, dispatcher):
    calls = []
    controller.remember_repair_plan = lambda *args: calls.append(args)
    first_base = controller.sources.candidate_hash
    first = success(dispatcher, "apply_edits", {"base_hash": first_base, "repair_plan": {"reason": "First repair."},
            "edits": [{"path": "src/main.py", "content": "VALUE = 2\n"}]})
    second = success(dispatcher, "apply_edits", {"base_hash": first["source_hash"],
            "edits": [{"path": "src/main.py", "content": "VALUE = 3\n"}]})
    assert calls[0] == (first_base, first["source_hash"],
                        {"reason": "First repair.", "target_invariants": [], "reproductions": []})
    assert calls[1] == (first["source_hash"], second["source_hash"], None)
    assert "repair_plan" not in second


@pytest.mark.parametrize("bad_edit", ["syntax", "stale"])
def test_failed_edit_never_captures_a_repair_plan(controller, dispatcher, bad_edit):
    calls = []
    controller.remember_repair_plan = lambda *args: calls.append(args)
    before = controller.sources.candidate_hash
    answer = dispatcher.execute("apply_edits", {"base_hash": "0" * 64 if bad_edit == "stale" else before,
            "repair_plan": {"reason": "Intent must bind only successful edits."},
            "edits": [{"path": "src/main.py", "content": "this is invalid python (\n" if bad_edit == "syntax" else "VALUE = 2\n"}]})
    assert not answer["ok"] and "result" not in answer
    assert controller.sources.candidate_hash == before and not calls


def test_legacy_controller_rejects_metadata_before_editing_and_still_allows_metadata_free_edits(controller, dispatcher):
    assert not hasattr(controller, "remember_repair_plan")
    before = controller.sources.candidate_hash
    failure(dispatcher, "apply_edits", {"base_hash": before, "repair_plan": {"reason": "Needs capture support."},
            "edits": [{"path": "src/main.py", "content": "VALUE = 2\n"}]})
    assert controller.sources.candidate_hash == before
    success(dispatcher, "apply_edits", {"base_hash": before,
            "edits": [{"path": "src/main.py", "content": "VALUE = 2\n"}]})


def test_large_trace_envelope_remains_valid_json_through_main_rendering(controller, dispatcher):
    from fixer.main import tool_text

    run = success(dispatcher, "replay", {"source": "accepted", "scenario": "public", "seed": 1})
    trace = controller.runs[run["run_id"]].trace_path
    trace.write_text("".join(json.dumps({"kind": "log", "event": "progress", "seq": i, "t": i,
                    "task": "long-task", "payload": {"blob": "\x01" * 2000}}) + "\n" for i in range(500)))
    success(dispatcher, "apply_edits", {"base_hash": controller.sources.candidate_hash,
            "edits": [{"path": "src/main.py", "content": "VALUE = 2\n"}]})
    answer = dispatcher.execute("trace_slice", {"run_id": run["run_id"], "entities": ["long-task"], "limit": 200})
    rendered = tool_text(answer)
    parsed = json.loads(rendered)["result"]
    assert len(rendered) <= 32_000
    assert parsed["source_hash"] == run["source_hash"]
    assert parsed["current_source_hash"] == controller.sources.candidate_hash != parsed["source_hash"]
    assert parsed["events"][0]["seq"] == 0 and parsed["events"][-1]["seq"] == 499
    assert parsed["omitted_events"] == 500 - len(parsed["events"])


def test_large_replay_envelope_discloses_omissions_and_preserves_checker_verdict(controller, dispatcher, monkeypatch):
    from fixer.main import tool_text

    original = FakeRun.as_dict

    def large_result(run, compact=True):
        result = original(run, compact)
        result.update(violations=[{"invariant": "EVENTUAL", "t": i, "detail": "d" * 1000} for i in range(1000)],
                      violation_count=1000)
        return result

    monkeypatch.setattr(FakeRun, "as_dict", large_result)
    answer = dispatcher.execute("replay", {"scenario": "public", "seed": 1})
    parsed = json.loads(tool_text(answer))["result"]
    assert len(json.dumps(parsed, ensure_ascii=False)) <= 24_000
    assert parsed["ok"] is False and parsed["status"] == "invariant_failure"
    assert parsed["violation_count"] == 1000 and parsed["source_hash"] == controller.sources.candidate_hash
    assert parsed["violations"][0]["t"] == 0 and parsed["violations"][-1]["t"] == 999
    assert parsed["response_omissions"]["violations"]["items"] == 1000 - len(parsed["violations"])


@pytest.mark.parametrize("field,value", [("at", -1), ("at", True), ("at", 2 ** 1024),
                                        ("window_s", float("inf")), ("window_s", 3601),
                                        ("context_lines", -1), ("context_lines", True)])
def test_trace_focus_rejects_invalid_or_unbounded_arguments(controller, dispatcher, field, value):
    run = success(dispatcher, "replay", {"scenario": "public", "seed": 1})
    failure(dispatcher, "trace_slice", {"run_id": run["run_id"], "entities": [], field: value})
