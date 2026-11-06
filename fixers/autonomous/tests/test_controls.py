from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
import yaml

from fixer import controls
from fixer.controller import Controller
from fixer.patches import source_hash
from fixer.runtime import CommandResult
from fixer.validation import ReplayResult, _scenario_hashes
from controller_support import controller, plan, repair_ownership


def fixture_inputs(root: Path, *, scenarios=3, profiles=7):
    (root / "src").mkdir(parents=True)
    (root / "src/service.py").write_text("VALUE = 1\n")
    (root / "scenarios/faults").mkdir(parents=True)
    for index in range(scenarios):
        name = "base" if index == 0 else f"regular-{index}"
        (root / f"scenarios/{name}.yaml").write_text(yaml.safe_dump({
            "name": name, "nodes": [{"name": "node", "class": "newservice:Node"}],
            "workload": [{"action": "submit", "at": 1}], "end_at": 20,
        }))
    for index in range(profiles):
        (root / f"scenarios/faults/profile-{index}.yaml").write_text(yaml.safe_dump({
            "gpu": {"latency": index / 100 + 0.1},
        }))


def test_default_cap_pairs_public_and_hash_seed_without_regular_crowding(tmp_path):
    fixture_inputs(tmp_path)
    result = controls.plan_controls(tmp_path, [12, 11])
    assert len(result.experiments) == 16 and result.metadata["planned_omitted"] == 4
    assert [r["seed_origin"] for r in result.experiments] == ["public", controls.SEED_POLICY] * 8
    # The third discovered profile gets its hash seed even with other scenarios.
    pair = [r for r in result.experiments if r["faults"] == "scenarios/faults/profile-2.yaml"]
    assert len(pair) == 2 and pair[0]["seed"] == 12
    raw = (tmp_path / "scenarios/base.yaml").read_bytes()
    profile = (tmp_path / "scenarios/faults/profile-2.yaml").read_bytes()
    material = raw + b"\0" + profile + b"\0healthy-control-v1:0"
    seed = int.from_bytes(hashlib.sha256(material).digest()[:4], "big") & 0x7fffffff
    assert pair[1]["seed"] == seed
    assert pair[1]["seed_input_sha256"] == hashlib.sha256(material).hexdigest()


def test_order_and_seed_are_stable_and_track_public_input_changes(tmp_path):
    fixture_inputs(tmp_path, scenarios=1, profiles=2)
    before = controls.plan_controls(tmp_path, [7])
    assert before == controls.plan_controls(tmp_path, [7])
    file = tmp_path / "scenarios/faults/profile-0.yaml"
    file.write_text(file.read_text() + "# changed public profile\n")
    after = controls.plan_controls(tmp_path, [7])
    first = next(r for r in before.experiments if r["faults"] == file.relative_to(tmp_path).as_posix() and r["seed_origin"] != "public")
    second = next(r for r in after.experiments if r["faults"] == file.relative_to(tmp_path).as_posix() and r["seed_origin"] != "public")
    assert first["seed"] != second["seed"]
    assert first["profile_input_sha256"] != second["profile_input_sha256"]


def test_unseen_layout_json_and_fault_directory_names_are_supported(tmp_path):
    (tmp_path / "scenarios/custom").mkdir(parents=True)
    (tmp_path / "scenarios/custom/healthy.json").write_text(json.dumps({"nodes": [], "workload": []}))
    (tmp_path / "scenarios/custom/probe.json").write_text(json.dumps({"nodes": {"crashes": []}}))
    result = controls.plan_controls(tmp_path, [17])
    assert len(result.experiments) == 4
    assert any(row["faults"] == "scenarios/custom/probe.json" for row in result.experiments)


@pytest.mark.parametrize("kind", ["file", "directory", "anchor", "reference"])
def test_symlinks_are_excluded_even_if_they_point_within_scenarios(tmp_path, kind):
    fixture_inputs(tmp_path, scenarios=1, profiles=0)
    anchor = tmp_path / "scenarios"
    if kind == "file":
        (anchor / "alias.yaml").symlink_to(anchor / "base.yaml")
    elif kind == "directory":
        (anchor / "alias").symlink_to(anchor / "faults", target_is_directory=True)
    elif kind == "anchor":
        anchor.rename(tmp_path / "actual")
        anchor.symlink_to(tmp_path / "actual", target_is_directory=True)
    else:
        (anchor / "alias.yaml").symlink_to(anchor / "base.yaml")
        base = anchor / "base.yaml"
        base.write_text(base.read_text() + "faults: alias.yaml\n")
    result = controls.plan_controls(tmp_path, [7])
    assert result.metadata["excluded_links"] >= 1
    assert all("alias" not in row["scenario"] for row in result.experiments)
    if kind in {"anchor", "reference"}:
        assert not result.experiments


@pytest.mark.parametrize("reference", ["../outside.yaml", "/etc/passwd", "https://example.invalid/faults"])
def test_fault_references_cannot_leave_bounded_local_inventory(tmp_path, reference):
    fixture_inputs(tmp_path, scenarios=1, profiles=0)
    base = tmp_path / "scenarios/base.yaml"
    base.write_text(base.read_text() + "faults: " + reference + "\n")
    result = controls.plan_controls(tmp_path, [7])
    assert not result.experiments and result.metadata["invalid_inputs"]


@pytest.mark.parametrize("cycle", ["file", "alias"])
def test_reference_and_yaml_alias_cycles_are_rejected(tmp_path, cycle):
    fixture_inputs(tmp_path, scenarios=1, profiles=0)
    base = tmp_path / "scenarios/base.yaml"
    if cycle == "file":
        base.write_text(base.read_text() + "faults: cycle.yaml\n")
        (tmp_path / "scenarios/cycle.yaml").write_text("faults: base.yaml\n")
    else:
        base.write_text(base.read_text() + "faults: &cycle {self: *cycle}\n")
    result = controls.plan_controls(tmp_path, [7])
    assert not result.experiments and result.metadata["invalid_inputs"]


def test_file_byte_entry_and_cancellation_bounds_are_disclosed(tmp_path, monkeypatch):
    fixture_inputs(tmp_path, scenarios=1, profiles=2)
    (tmp_path / "scenarios/faults/profile-0.yaml").write_text("#" * (controls.MAX_FILE_BYTES + 1))
    bounded = controls.plan_controls(tmp_path, [7])
    assert bounded.metadata["invalid_inputs"] and bounded.metadata["bytes_read"] <= controls.MAX_TOTAL_BYTES
    stopped = controls.plan_controls(tmp_path, [7], should_stop=lambda: True)
    assert not stopped.experiments and stopped.metadata["discovery_cancelled"]
    assert stopped.metadata["bytes_read"] == 0
    monkeypatch.setattr(controls, "MAX_DIRECTORY_ENTRIES", 1)
    overflow = controls.plan_controls(tmp_path, [7])
    assert not overflow.experiments and overflow.metadata["discovery_truncated"]


def test_total_bytes_and_unvisited_directories_are_bounded(tmp_path, monkeypatch):
    fixture_inputs(tmp_path, scenarios=1, profiles=2)
    monkeypatch.setattr(controls, "MAX_TOTAL_BYTES", 100)
    result = controls.plan_controls(tmp_path, [7])
    assert result.metadata["bytes_read"] <= 100 and result.metadata["discovery_truncated"]
    monkeypatch.setattr(controls, "MAX_ENTRIES", 2)
    result = controls.plan_controls(tmp_path, [7])
    assert result.metadata["discovery_truncated"] and result.metadata["discovery_omissions"]


def test_nonregular_reference_is_rejected_without_opening_a_fifo(tmp_path):
    fixture_inputs(tmp_path, scenarios=1, profiles=0)
    fifo = tmp_path / "scenarios/faults/pipe.yaml"
    os.mkfifo(fifo)
    base = tmp_path / "scenarios/base.yaml"
    base.write_text(base.read_text() + "faults: faults/pipe.yaml\n")
    result = controls.plan_controls(tmp_path, [7])
    assert not result.experiments and result.metadata["invalid_inputs"]


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


def fake_controller(root, clock, monkeypatch, *, duration=0.25, statuses=()):
    from fixer import controller as module
    monkeypatch.setattr(module.time, "monotonic", clock.monotonic)
    instance = Controller.__new__(Controller)
    stop = threading.Event()
    instance.deadline = SimpleNamespace(stop_event=stop, remaining=lambda reserve=0: 0 if stop.is_set() else max(0, 100 - clock.now - reserve))
    instance.config = SimpleNamespace(seeds=[7])
    instance.final_reserve = 10
    instance.replay_seconds = 0.1
    instance.sources = SimpleNamespace(original=root, best_hash=source_hash(root / "src"))
    instance._cache = {}
    instance._lock = threading.RLock()
    instance.runs = {}
    instance.retained_experiments = {}
    instance._experiment_timings = {}
    calls = []

    def replay(case, scenario, *, seed, faults, timeout, reserve):
        assert case == root and reserve >= instance.final_reserve
        assert timeout <= instance.deadline.remaining(reserve) + 0.000001
        calls.append({"seed": seed, "timeout": timeout, "reserve": reserve})
        clock.now += duration
        index = len(calls) - 1
        status = statuses[index] if index < len(statuses) else "pass"
        digest, fault_digest = _scenario_hashes(root, scenario, faults)
        command = CommandResult(("sim",), 0, "", "", duration / 2)
        return ReplayResult(instance.sources.best_hash, scenario, seed, faults, f"run-{index}", root / "runs",
                            command, command, [] if status == "pass" else [{"invariant": "EXISTING"}],
                            {"all_resolved": True}, status, digest, fault_digest)

    instance.simulator = SimpleNamespace(replay=replay)
    return instance, calls


def test_bootstrap_budget_is_measured_and_failures_stay_diagnostic(tmp_path, monkeypatch):
    fixture_inputs(tmp_path, scenarios=2, profiles=7)
    clock = Clock()
    instance, calls = fake_controller(tmp_path, clock, monkeypatch, duration=0.35,
                                      statuses=("pass", "invariant_failure", "runtime_error"))
    original = source_hash(tmp_path / "src")
    instance._bootstrap_controls()
    coverage = instance.control_coverage
    assert coverage["budget_s"] == 3 and coverage["elapsed_s"] <= 3
    assert coverage["budget_exhausted"] and coverage["executed_omitted"] > 0
    assert len(calls) == coverage["executed"] == len(instance.runs)
    assert coverage["failed"] == 2 and coverage["runtime_errors"] == 1
    assert all(item["source_hash"] == original for item in instance.retained_experiments.values())
    assert len(instance.retained_experiments) == coverage["retained"]
    assert source_hash(tmp_path / "src") == original


def test_cached_controls_are_not_recounted_as_measured_compute(tmp_path, monkeypatch):
    fixture_inputs(tmp_path, scenarios=1, profiles=1)
    clock = Clock()
    instance, calls = fake_controller(tmp_path, clock, monkeypatch)
    instance._bootstrap_controls()
    count = len(calls)
    instance._bootstrap_controls()
    assert len(calls) == count == instance.control_coverage["cached"]
    assert sum(row["count"] for row in instance._experiment_timings.values()) == count


def test_cancellation_prevents_control_processes(tmp_path, monkeypatch):
    fixture_inputs(tmp_path, scenarios=1, profiles=1)
    instance, calls = fake_controller(tmp_path, Clock(), monkeypatch)
    instance.deadline.stop_event.set()
    instance._bootstrap_controls()
    assert not calls and instance.control_coverage["cancelled"]


def test_original_passing_hash_control_is_required_for_partial_promotion(controller):
    # The existing first-seed base check passes; the new hash-seed control fails.
    for case in (controller.sources.original, controller.sources.best, controller.sources.candidate):
        path = case / "scenarios/base.yaml"
        path.write_text(path.read_text() + "nodes: []\nworkload: []\n")
    controller._bootstrap_controls()
    extra = next(item["seed"] for item in controls.plan_controls(controller.sources.original, controller.config.seeds).experiments
                 if item["seed_origin"] == controls.SEED_POLICY)
    original = controller.sources.best_hash
    repair_ownership(controller)
    condition = ('("--seed" in __import__("sys").argv and '
                 'int(__import__("sys").argv[__import__("sys").argv.index("--seed") + 1]) == ' + str(extra) + ')')
    controller.sources.apply_edits(controller.sources.candidate_hash, [{
        "path": "src/service.py", "old": "NORMAL_BROKEN = False", "new": "NORMAL_BROKEN = " + condition,
    }])
    result = controller.validate(plan())
    assert not result["accepted"] and "previously passing scenario regressed" in result["reason"]
    assert controller.sources.best_hash == source_hash(controller.sources.live / "src") == original
    assert any(item["seed"] == extra for item in controller.retained_experiments.values())
    assert controller.status()["control_coverage"]["retained"] == 2
