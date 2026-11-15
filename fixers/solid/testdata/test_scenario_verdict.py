"""write_scenario must not call a scenario a reproduction of an unwanted event when only other checks
fail. B2 run (2/3): the agent counted `missing_from_heartbeat` (a field value, not an event, so the
check could never fail), moved to a fault file that fails for other bugs, got "REPRODUCES", and
stopped with bug 2 half-fixed.

Runs the real orchestrator and tool on a copy of practice case B2 (about 60 s, no model):
    python -m pytest -q fixers/solid/testdata/test_scenario_verdict.py
"""
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PACK = HERE.parent.parent.parent
sys.path.insert(0, str(HERE.parent))


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    ws = tmp_path_factory.mktemp("b2") / "workspace"
    shutil.copytree(PACK / "practice" / "B2", ws)
    os.environ.update(C3_WORKSPACE=str(ws), C3_CORPUS=str(PACK / "corpus/build/practice"),
                      C3_DEADLINE=str(int(time.time() + 3600)), C3_BUDGET_USD="9")
    from fixer import main
    main.log = lambda m: None
    o = main.Orchestrator(ws, PACK / "corpus/build/practice")
    o.bootstrap()
    from fixer.tools import Tools
    return Tools(ws, PACK / "corpus/build/practice", o.gate, o, log=lambda m: None)


def test_failing_for_other_reasons_is_not_a_reproduction(tools):
    out = tools.t_write_scenario("sym2_net", "faults: faults/network.yaml\nexpect:\n  log_events_max:\n"
                                 "    missing_from_heartbeat: 0\n", base="public")
    assert "REPRODUCES" not in out
    assert "NOT on your counting check" in out
    assert "no log record in logs/ has event=missing_from_heartbeat" in out
    assert "event=task_finished" in out                       # points at where the value does occur
    assert tools.orch.coverage_gap == "sym2_net"               # unproven: the review must not cap it


def test_a_counting_check_that_fails_is_a_reproduction(tools):
    out = tools.t_write_scenario("sym2_reorder", "faults: faults/reorder.yaml\nexpect:\n  log_events_max:\n"
                                 "    shard_requeued: 0\n", base="public")
    assert "REPRODUCES on the original code (your counting check fails there)" in out
    assert "log event shard_requeued:" in out                 # the counts themselves are shown
    assert "note: no log record" not in out
    assert tools.orch.coverage_gap is None                     # proven by its own count
    line = next(ln for ln in out.splitlines() if "log event shard_requeued: original" in ln)
    orig = [int(x) for x in line.split("original ")[1].split(" |")[0].split(",")]
    assert sum(orig) > 0                                      # the real counts, not just pass/fail
    assert not list((tools.ws / "scenarios").glob("*__counts.yaml"))   # the probe copy is cleaned up


def test_unfamiliar_checker_wording_falls_back_to_the_old_verdict(tools):
    """A hidden case's simulator might word failures differently: then never claim 'NOT on your
    counting check', just report any failure on the original code as before."""
    saved = tools.judge_counts
    tools.judge_counts = False
    try:
        out = tools.t_write_scenario("sym2_net2", "faults: faults/network.yaml\nexpect:\n  log_events_max:\n"
                                     "    missing_from_heartbeat: 0\n", base="public")
    finally:
        tools.judge_counts = saved
    assert "REPRODUCES on the original code" in out and "NOT on your counting check" not in out


def test_practice_checker_wording_is_recognised(tools):
    assert tools.judge_counts
