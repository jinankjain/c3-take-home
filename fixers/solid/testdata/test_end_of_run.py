"""End-of-run handling when the full acceptance check no longer fits in the time left.

Runs the real orchestrator on a copy of practice case A1 (about 30 s, no model):
    python -m pytest -q fixers/solid/testdata/test_end_of_run.py
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

FIX_OLD = '            if job is None or job["units"] is None:\n                continue'
FIX_NEW = '            if job is None:\n                continue'
BREAK_OLD = '    job["state"] = CANCELLED\n    job["precancelled"] = True'
BREAK_NEW = '    job["state"] = QUEUED\n    job["precancelled"] = True'


@pytest.fixture(scope="module")
def orch(tmp_path_factory):
    ws = tmp_path_factory.mktemp("case") / "workspace"
    shutil.copytree(PACK / "practice" / "A1", ws)
    os.environ.update(C3_WORKSPACE=str(ws), C3_CORPUS=str(PACK / "corpus/build/practice"),
                      C3_DEADLINE=str(int(time.time() + 3600)), C3_BUDGET_USD="3")
    from fixer import main
    main.log = lambda m: None
    o = main.Orchestrator(ws, PACK / "corpus/build/practice")
    o.bootstrap()
    return o


def edit(orch, rel, old, new):
    p = orch.src / rel
    text = p.read_text()
    assert old in text
    p.write_text(text.replace(old, new, 1))


def no_time_for_full_check(orch):
    orch.check_cost_s = 10_000.0          # time_left() is far below this: only the quick check fits


def test_quick_check_keeps_a_real_partial_fix(orch):
    edit(orch, "jobrouter/store.py", FIX_OLD, FIX_NEW)
    no_time_for_full_check(orch)
    orch.save_or_restore("test")
    assert FIX_NEW in (orch.src / "jobrouter/store.py").read_text()        # kept in src/
    assert not orch.src_dirty()                                            # and snapshotted


def test_quick_check_rolls_back_a_test_breaking_edit(orch):
    edit(orch, "jobrouter/models.py", BREAK_OLD, BREAK_NEW)
    no_time_for_full_check(orch)
    orch.save_or_restore("test")
    assert BREAK_OLD in (orch.src / "jobrouter/models.py").read_text()     # bad edit gone
    assert FIX_NEW in (orch.src / "jobrouter/store.py").read_text()        # earlier fix still kept


def test_quick_check_rolls_back_an_edit_without_progress(orch):
    # already fixed, so a harmless edit cannot reduce public failures any further
    edit(orch, "jobrouter/store.py", FIX_NEW, FIX_NEW.replace("continue", "continue  # no-op"))
    no_time_for_full_check(orch)
    orch.save_or_restore("test")
    assert "# no-op" not in (orch.src / "jobrouter/store.py").read_text()


def test_stop_point_depends_on_unchecked_edits(orch):
    from fixer import main
    saved = orch.deadline
    try:
        orch.deadline = time.time() + 60          # between STOP_CLEAN_S (45) and STOP_DIRTY_S (80)
        assert not orch.src_dirty()
        assert orch.should_stop() is None         # all accepted: keep working
        edit(orch, "jobrouter/store.py", FIX_NEW, FIX_NEW.replace("continue", "continue  # wip"))
        assert orch.src_dirty()
        assert orch.should_stop()                 # unchecked edits: stop, leave time for the quick check
        orch.restore_last_good()
    finally:
        orch.deadline = saved
    assert main.STOP_CLEAN_S < main.STOP_DIRTY_S


def test_sweep_sizing():
    from fixer.main import MAX_EXTRA_SEEDS, MIN_EXTRA_SEEDS, choose_extra_seeds
    assert choose_extra_seeds(0.14, 7, 5) == MAX_EXTRA_SEEDS        # fast machine: full sweeps
    assert choose_extra_seeds(0.3, 12, 5) < MAX_EXTRA_SEEDS         # slow machine, many fault files
    assert choose_extra_seeds(5.0, 12, 5) == MIN_EXTRA_SEEDS        # very slow: floor
    assert choose_extra_seeds(0.3, 12, 5, ok=False) == MAX_EXTRA_SEEDS
