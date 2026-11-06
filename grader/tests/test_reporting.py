from __future__ import annotations

from types import SimpleNamespace
import subprocess

import pytest

import grader.evaluate as evaluation


@pytest.mark.parametrize("suite,expected,actual", [
    ("tests", "test_module::test_value", "tests.test_module::test_value"),
    ("tests", "tests.test_module::test_value", "test_module::test_value"),
    ("regression", "test_module.Class::test_value[p.q::r]", "regression.test_module.Class::test_value[p.q::r]"),
    ("regression", "regression.test_module.Class::test_value[p.q::r]", "test_module.Class::test_value[p.q::r]"),
    ("tests", "pkg.tests.module.Class::test_value", "tests.pkg.tests.module.Class::test_value"),
    ("tests", "tests.test_module::test_value", "tests.test_module::test_value"),
])
def test_known_suite_namespace_qualification_is_compatible(suite, expected, actual):
    assert evaluation.baseline_regressions([expected], {actual: "passed"}, suite) == []


@pytest.mark.parametrize("status", ["failed", "error", "skipped"])
@pytest.mark.parametrize("expected,alias", [
    ("test_module.Class::test_value", "tests.test_module.Class::test_value"),
    ("tests.test_module.Class::test_value", "test_module.Class::test_value"),
])
def test_exact_failure_or_skip_cannot_be_overridden_by_a_passing_alias(expected, alias, status):
    outcomes = {expected: status, alias: "passed"}
    assert evaluation.baseline_regressions([expected], outcomes, "tests") == [expected]
    assert evaluation.baseline_regressions([expected], {alias: status}, "tests") == [expected]


@pytest.mark.parametrize("suite,expected,actual", [
    ("tests", "pkg_a.test_module.Class::test_value", "tests.pkg_b.test_module.Class::test_value"),
    ("tests", "test_module.ClassA::test_value", "tests.test_module.ClassB::test_value"),
    ("tests", "test_module.Class::test_value[one]", "tests.test_module.Class::test_value[two]"),
    ("tests", "test_module.Class::test_value", "project.tests.test_module.Class::test_value"),
    ("tests", "test_module.Class::test_value", "regression.test_module.Class::test_value"),
    ("custom", "test_module.Class::test_value", "custom.test_module.Class::test_value"),
])
def test_other_module_class_parameter_or_namespace_differences_still_count_as_missing(suite, expected, actual):
    assert evaluation.baseline_regressions([expected], {actual: "passed"}, suite) == [expected]


@pytest.mark.parametrize("actual,missing", [
    ("tests.test_module::test_value", "test_module::test_value"),
    ("test_module::test_value", "tests.test_module::test_value"),
])
def test_one_result_cannot_satisfy_two_distinct_baseline_module_ids(actual, missing):
    expected = ["test_module::test_value", "tests.test_module::test_value"]
    assert evaluation.baseline_regressions(expected, {actual: "passed"}, "tests") == [missing]


@pytest.mark.parametrize("outcomes", [None, {}, {"other_module::test_value": "passed"}])
def test_missing_or_unreported_baseline_tests_are_rejected(outcomes):
    expected = "test_module::test_value"
    assert evaluation.baseline_regressions([expected], outcomes, "tests") == [expected]


@pytest.mark.parametrize("mode", ["timeout", "no_report", "failure", "error", "skipped"])
def test_pytest_report_failures_and_interrupted_exits_remain_failed_baseline_gates(tmp_path, monkeypatch, mode):
    tree = tmp_path / "tree"
    (tree / "tests").mkdir(parents=True)
    (tree / "tests/test_module.py").write_text("def test_value(): pass\n")
    log = tmp_path / "tests.log"
    def fake_run(command, **_kwargs):
        if mode == "timeout":
            raise subprocess.TimeoutExpired(command, 1, output="partial output")
        if mode != "no_report":
            xml = next(value.split("=", 1)[1] for value in command if value.startswith("--junitxml="))
            from pathlib import Path
            child = "failure" if mode == "failure" else mode
            Path(xml).write_text(
                '<testsuites><testsuite><testcase classname="tests.test_module" name="test_value">'
                f'<{child}/></testcase></testsuite></testsuites>')
        return SimpleNamespace(stdout="report", stderr="", returncode=1 if mode in {"failure", "error"} else 0)
    monkeypatch.setattr(evaluation.subprocess, "run", fake_run)
    monkeypatch.setattr(evaluation.simcli, "clean_env", lambda *_args, **_kwargs: {})
    outcomes, message = evaluation.pytest_results(tree, "tests", log)
    assert message
    assert evaluation.baseline_regressions(["test_module::test_value"], outcomes, "tests") == ["test_module::test_value"]


@pytest.fixture
def evaluator(tmp_path, monkeypatch):
    case_dir = tmp_path / "case"
    (case_dir / "src").mkdir(parents=True)
    (case_dir / "src/service.py").write_text("value = 1\n")
    (case_dir / "tests").mkdir()
    private = tmp_path / "private"
    (private / "scenarios").mkdir(parents=True)
    (private / "scenarios/sample.yaml").write_text("name: sample\n")
    (private / "regression").mkdir()
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "service.py").write_text("value = 2\n")
    work = tmp_path / "work"
    work.mkdir()
    case = SimpleNamespace(dir=case_dir, private=private, regression=private / "regression",
                           bugs=[{"id": "sample"}], bug_seeds=lambda _bid: [11],
                           scenario_name=lambda _bug: "sample")
    baseline = {"tests": ["test_module.Class::test_value[p]"], "regression": ["test_protocol::test_value"]}
    outcomes = {"tests": {"tests.test_module.Class::test_value[p]": "passed"},
                "regression": {"regression.test_protocol::test_value": "passed"}}
    monkeypatch.setattr(evaluation, "load_baseline", lambda *_args: (baseline, "synthetic"))
    monkeypatch.setattr(evaluation, "pytest_results", lambda _tree, which, _log: (outcomes[which], "synthetic suite"))
    monkeypatch.setattr(evaluation, "reference_counts", lambda *_args: ({11: 1}, "synthetic reference"))
    monkeypatch.setattr(evaluation, "run_seed", lambda _tree, _scenario, seed, *_args: {"seed": seed, "passed": True})
    return outcomes, lambda: evaluation.evaluate(case, candidate, work, workers=1)


def test_evaluator_scores_passing_bug_with_compatible_public_and_private_test_ids(evaluator):
    _, run = evaluator
    result = run()
    assert result["tests_ok"] and result["regression_ok"]
    assert not result["baseline_regressions"]
    assert result["bugs_fixed"] == 1 and result["bugs"][0]["seeds_passed"] == 1


@pytest.mark.parametrize("suite,outcome", [("tests", "failed"), ("regression", "skipped"), ("regression", None)])
def test_evaluator_still_zeros_a_passing_bug_for_real_failure_skip_or_missing_report(evaluator, suite, outcome):
    outcomes, run = evaluator
    if outcome is None:
        outcomes[suite] = None
    else:
        outcomes[suite][next(iter(outcomes[suite]))] = outcome
    result = run()
    assert result["baseline_regressions"] and result["bugs_fixed"] == 0
    assert not result["tests_ok"] or not result["regression_ok"]


def test_existing_non_gating_private_failure_remains_reported_without_zeroing_the_case(evaluator):
    outcomes, run = evaluator
    outcomes["regression"]["regression.test_existing_bug::test_broken"] = "failed"
    result = run()
    assert result["regression_ok"] and result["bugs_fixed"] == 1
    assert result["regression_failed"] == ["regression.test_existing_bug::test_broken"]
