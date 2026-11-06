import sys
import threading
import time

import pytest

from fixer.runtime import CaseConfig, CommandResult, ConfigurationError, Deadline, ProcessRunner, TimeLimitError


def test_configuration_uses_supplied_paths_and_deduplicates_seeds(tmp_path):
    case = tmp_path / "not-workspace"
    corpus = tmp_path / "not-corpus"
    (case / "src").mkdir(parents=True)
    corpus.mkdir()
    (corpus / "INDEX.md").write_text("current docs")
    (case / "case.json").write_text('{"public_seeds": [12, 12, 22, true, "bad"]}')
    config = CaseConfig.from_env({"C3_WORKSPACE": str(case), "C3_CORPUS": str(corpus),
                                 "C3_DEADLINE": str(time.time() + 30), "C3_BUDGET_USD": "3"})
    assert config.workspace == case
    assert config.corpus == corpus
    assert config.seeds == [12, 22]


@pytest.mark.parametrize("value", ["nan", "inf", "-1"])
def test_configuration_rejects_invalid_budget(tmp_path, value):
    (tmp_path / "src").mkdir()
    (tmp_path / "INDEX.md").write_text("")
    with pytest.raises(ConfigurationError):
        CaseConfig.from_env({"C3_WORKSPACE": str(tmp_path), "C3_CORPUS": str(tmp_path),
                             "C3_DEADLINE": str(time.time() + 30), "C3_BUDGET_USD": value})


def test_deadline_reserve_and_stop_request():
    deadline = Deadline(time.time() + 10)
    assert 8 < deadline.remaining(1) <= 9
    assert deadline.timeout(2) == 2
    with pytest.raises(TimeLimitError):
        deadline.timeout(1, reserve=20)
    deadline.stop_event.set()
    with pytest.raises(TimeLimitError):
        deadline.timeout(1)


def test_runner_preserves_input_and_does_not_interpret_shell(tmp_path):
    runner = ProcessRunner(Deadline(time.time() + 10))
    literal = '$(touch should-not-exist); `echo bad`'
    result = runner.run([sys.executable, "-c", "import sys; print(sys.argv[1]); print(sys.stdin.read())", literal],
                        cwd=tmp_path, input_text="payload")
    assert result.ok
    assert result.stdout == literal + "\npayload\n"
    assert not (tmp_path / "should-not-exist").exists()


def test_runner_enforces_absolute_timeout_and_cancellation(tmp_path):
    deadline = Deadline(time.time() + 10)
    runner = ProcessRunner(deadline)
    timed = runner.run([sys.executable, "-c", "import time; time.sleep(10)"], cwd=tmp_path, timeout=0.2)
    assert timed.timed_out and not timed.ok and timed.duration < 2
    timer = threading.Timer(0.2, deadline.stop_event.set)
    timer.start()
    try:
        stopped = runner.run([sys.executable, "-c", "import time; time.sleep(10)"], cwd=tmp_path)
    finally:
        timer.cancel()
    assert stopped.cancelled and not stopped.ok and stopped.duration < 2


def test_runner_bounds_output_and_cleans_up_descendants(tmp_path):
    runner = ProcessRunner(Deadline(time.time() + 10), output_limit=1024)
    result = runner.run([sys.executable, "-c", "print('x' * 100000)"], cwd=tmp_path)
    assert result.truncated
    assert len(result.stdout) < 1100
    marker = tmp_path / "descendant-survived"
    child = "import time; from pathlib import Path; time.sleep(0.5); Path(%r).write_text('bad')" % str(marker)
    parent = "import subprocess,sys; subprocess.Popen([sys.executable,'-c',sys.argv[1]])"
    runner.run([sys.executable, "-c", parent, child], cwd=tmp_path)
    time.sleep(0.7)
    assert not marker.exists()


def test_result_marks_nonzero_exit_as_failure():
    assert not CommandResult(("python",), 1, "", "", 0).ok
