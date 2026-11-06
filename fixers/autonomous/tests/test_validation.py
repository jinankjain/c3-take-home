from __future__ import annotations

import json
from pathlib import Path
import sys
import time

import pytest

from fixer.validation import _scenario_hashes

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fixer.runtime import Deadline, ProcessRunner, TimeLimitError
from fixer.validation import Simulator


FAKE_CLI = r'''
import argparse, hashlib, json, os, pathlib, sys, time, uuid
p = argparse.ArgumentParser()
sub = p.add_subparsers(dest="mode", required=True)
r = sub.add_parser("replay")
r.add_argument("--scenario"); r.add_argument("--seed", type=int)
r.add_argument("--out"); r.add_argument("--faults")
c = sub.add_parser("check"); c.add_argument("--trace")
a = p.parse_args()
root = pathlib.Path.cwd()
behavior = json.loads((root / "behavior.json").read_text())
assert os.environ["C3_WORKSPACE"] == str(root)
assert os.environ["C3SIM_ROOT"] == str(root)
assert all(pathlib.Path(part).is_relative_to(root) for part in os.environ["PYTHONPATH"].split(os.pathsep))
assert os.environ["PYTHONHASHSEED"] == "0"
assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1"
if a.mode == "replay":
    if behavior.get("sleep"):
        time.sleep(behavior["sleep"])
    if behavior.get("config_error"):
        print("error: bad configuration", file=sys.stderr)
        sys.exit(2)
    out = pathlib.Path(a.out); out.mkdir(exist_ok=True)
    event = {"kind": "log", "node": "sim", "event": "scenario", "name": "public", "seed": a.seed, "seq": 0, "t": 0}
    if behavior.get("nondeterministic"):
        event["nonce"] = uuid.uuid4().hex
    text = json.dumps(event, sort_keys=True) + "\n"
    (out / "trace.jsonl").write_text(text)
    summary = {"scenario": "public", "seed": a.seed, "all_resolved": not behavior.get("unresolved", False), "outcomes": {}, "actions": [], "messages_sent": 0, "trace_sha256": hashlib.sha256(text.encode()).hexdigest(), "error": "example runtime error" if behavior.get("runtime_error") else None}
    if behavior.get("wrong_hash"):
        summary["trace_sha256"] = "f" * 64
    (out / "summary.json").write_text(json.dumps(summary))
    if behavior.get("mutate_source"):
        (root / "src" / "service.py").write_text("VALUE = 2\n")
    if behavior.get("mutate_scenario"):
        (root / "scenarios" / "public.yaml").write_text("name: different\n")
    print(json.dumps(summary))
    sys.exit(3 if behavior.get("runtime_error") else 0)
else:
    if behavior.get("malformed_check"):
        print("{}")
        sys.exit(0)
    violations = [{"invariant": "I2", "t": None, "detail": "action c1 never resolved"}] if behavior.get("violations") else []
    if behavior.get("check_diagnostic"):
        print("diagnostic before verdict")
    print(json.dumps({"violations": len(violations), "invariants": violations}))
    sys.exit(behavior.get("check_exit", 1 if violations else 0))
'''


@pytest.fixture
def fake_case(tmp_path: Path):
    root = tmp_path / "staged case"
    (root / "src").mkdir(parents=True)
    (root / "sim").mkdir()
    (root / "scenarios").mkdir()
    (root / "src" / "service.py").write_text("VALUE = 1\n")
    (root / "sim" / "__init__.py").write_text("")
    (root / "sim" / "__main__.py").write_text(FAKE_CLI)
    (root / "scenarios" / "public.yaml").write_text("name: public\n")
    (root / "behavior.json").write_text("{}")
    return root


@pytest.fixture
def simulator(tmp_path: Path):
    runner = ProcessRunner(Deadline(time.time() + 20))
    simulator = Simulator(runner, tmp_path / "runs")
    yield simulator
    runner.close()


def behavior(root: Path, **settings):
    (root / "behavior.json").write_text(json.dumps(settings))


def test_replay_exit_zero_is_not_a_correctness_verdict(fake_case, simulator):
    behavior(fake_case, violations=True)
    result = simulator.replay(fake_case, seed=11)
    assert result.replay_command.returncode == 0
    assert result.check_command.returncode == 1
    assert result.status == "invariant_failure"
    assert not result.ok
    assert result.violations[0]["invariant"] == "I2"
    assert result.evidence["identity"]["source_hash"] == result.source_hash


def test_success_has_source_and_effective_scenario_identity(fake_case, simulator):
    behavior(fake_case, check_diagnostic=True)
    faults = fake_case / "scenarios" / "fault profile.yaml"
    faults.write_text("network: {drop: 0.01}\n")
    first = simulator.replay(fake_case, seed=3, faults="scenarios/fault profile.yaml")
    assert first.ok
    assert first.source_hash and first.scenario_hash and first.faults_hash
    assert first.check_command.argv[-1] == str(first.trace_path)
    faults.write_text("network: {drop: 0.02}\n")
    second = simulator.replay(fake_case, seed=3, faults="scenarios/fault profile.yaml")
    assert first.source_hash == second.source_hash
    assert first.scenario_hash != second.scenario_hash
    assert first.faults_hash != second.faults_hash
    assert first.run_id != second.run_id


@pytest.mark.parametrize("settings,status", [
    ({"config_error": True}, "config_error"),
    ({"runtime_error": True}, "runtime_error"),
    ({"malformed_check": True}, "check_error"),
    ({"unresolved": True}, "unresolved"),
    ({"wrong_hash": True}, "evidence_error"),
    ({"wrong_hash": True, "violations": True}, "evidence_error"),
    ({"violations": True, "check_exit": 3}, "check_error"),
    ({"violations": True, "check_exit": 0}, "check_error"),
])
def test_uncertified_execution_is_never_accepted(fake_case, simulator, settings, status):
    behavior(fake_case, **settings)
    result = simulator.replay(fake_case, seed=1)
    assert result.status == status
    assert not result.ok


@pytest.mark.parametrize("setting", ["mutate_source", "mutate_scenario"])
def test_results_from_changed_inputs_are_stale(fake_case, simulator, setting):
    behavior(fake_case, **{setting: True})
    result = simulator.replay(fake_case, seed=1)
    assert result.status == "stale_source"
    assert not result.ok


def test_timeout_kills_replay_and_skips_checker(fake_case, simulator):
    behavior(fake_case, sleep=5)
    result = simulator.replay(fake_case, seed=1, timeout=0.15)
    assert result.status == "timeout"
    assert result.replay_command.timed_out
    assert result.check_command is None
    assert result.replay_command.duration < 2


def test_no_launch_after_deadline(fake_case, tmp_path):
    sim = Simulator(ProcessRunner(Deadline(time.time() - 1)), tmp_path / "runs")
    with pytest.raises(TimeLimitError):
        sim.replay(fake_case, seed=1)


def test_determinism_allows_existing_reproducible_invariant_failures(fake_case, simulator):
    behavior(fake_case, violations=True)
    result = simulator.determinism(fake_case, seed=11)
    assert result.ok
    assert len(result.runs) == 2
    assert all(not replay.ok for replay in result.runs)
    assert len({replay.summary["trace_sha256"] for replay in result.runs}) == 1


def test_determinism_checks_fresh_process_outputs(fake_case, simulator):
    behavior(fake_case, nondeterministic=True)
    result = simulator.determinism(fake_case, seed=11)
    assert not result.ok
    assert result.status == "nondeterministic"


def test_tests_execute_against_stage_imports_and_environment(fake_case, simulator):
    (fake_case / "tests").mkdir()
    (fake_case / "tests" / "test_service.py").write_text(
        "import os\nfrom pathlib import Path\nimport service\n"
        "def test_stage():\n"
        "    assert service.VALUE == 1\n"
        "    assert Path(os.environ['C3_WORKSPACE']) == Path.cwd()\n"
        "    assert Path(service.__file__).is_relative_to(Path.cwd())\n"
    )
    result = simulator.tests(fake_case)
    assert result.ok, result.stderr + result.stdout
    assert "1 passed" in result.stdout


def test_determinism_rejects_unverified_hash_even_with_existing_failures(fake_case, simulator):
    behavior(fake_case, wrong_hash=True, violations=True)
    assert not simulator.determinism(fake_case, seed=11).ok


@pytest.mark.parametrize("with_override", [False, True])
def test_scenario_identity_binds_default_name_for_same_bytes_at_different_filenames(tmp_path, with_override):
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    first, second = scenarios / "first.yaml", scenarios / "second.yaml"
    first.write_text("end_at: 0.1\n")
    second.write_bytes(first.read_bytes())
    profile = scenarios / "faults.yaml"
    profile.write_text("network: {}\n")
    faults = str(profile) if with_override else None
    first_identity = _scenario_hashes(tmp_path, str(first), faults)
    second_identity = _scenario_hashes(tmp_path, str(second), faults)
    assert first_identity[0] is not None and second_identity[0] is not None
    assert first_identity[0] != second_identity[0]
    assert first_identity[1] == second_identity[1]
    assert _scenario_hashes(tmp_path, "first", faults) == first_identity


def test_explicit_named_same_byte_scenario_aliases_keep_the_same_identity(tmp_path):
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    first, second = scenarios / "first.yaml", scenarios / "second.yaml"
    first.write_text("name: stable-name\nend_at: 0.1\n")
    second.write_bytes(first.read_bytes())
    assert _scenario_hashes(tmp_path, str(first), None) == _scenario_hashes(tmp_path, str(second), None)
