import json
from pathlib import Path

import pytest

from fixer.patches import PatchError, SourceManager, source_hash


@pytest.fixture
def manager(tmp_path):
    case = tmp_path / "case"
    (case / "src" / "service").mkdir(parents=True)
    (case / "src" / "service" / "worker.py").write_text("VALUE = 1\n")
    (case / "tests").mkdir()
    (case / "tests" / "test_service.py").write_text("# unchanged regression test\n")
    return SourceManager(case)


def edit(manager, value=2):
    return manager.apply_edits(manager.candidate_hash, [{"path": "src/service/worker.py",
                                                        "old": "VALUE = 1", "new": f"VALUE = {value}"}])


def test_staging_uses_real_copies_and_preserves_live_source(manager):
    original = source_hash(manager.live / "src")
    edit(manager)
    assert source_hash(manager.live / "src") == original
    assert manager.candidate_hash != original
    assert (manager.candidate / "tests" / "test_service.py").read_text().startswith("# unchanged")
    assert not (manager.candidate / "tests").is_symlink()
    assert "-VALUE = 1" in manager.diff()
    manager.reset_candidate()
    assert manager.candidate_hash == original


@pytest.mark.parametrize("path", ["tests/test_service.py", "../src/service/worker.py", "/src/worker.py", "src/../../worker.py", "src/.hidden.py"])
def test_edit_paths_cannot_escape_source(manager, path):
    with pytest.raises(PatchError):
        manager.apply_edits(manager.candidate_hash, [{"path": path, "content": "VALUE = 2\n"}])


def test_edits_reject_stale_hash_and_validate_all_before_writing(manager):
    original = manager.candidate_hash
    with pytest.raises(PatchError):
        manager.apply_edits("stale", [{"path": "src/service/worker.py", "content": "VALUE = 2\n"}])
    with pytest.raises(PatchError):
        manager.apply_edits(original, [{"path": "src/service/worker.py", "old": "VALUE = 1", "new": "VALUE = 2"},
                                       {"path": "src/helper.py", "content": "def invalid(\n"}])
    assert manager.candidate_hash == original


def test_source_symlink_is_rejected(manager, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET = 1\n")
    (manager.candidate / "src" / "escape.py").symlink_to(outside)
    with pytest.raises(PatchError):
        source_hash(manager.candidate / "src")


def test_exact_match_and_multiple_edits_to_one_file(manager):
    with pytest.raises(PatchError):
        manager.apply_edits(manager.candidate_hash, [{"path": "src/service/worker.py", "old": "missing", "new": ""}])
    manager.apply_edits(manager.candidate_hash, [{"path": "src/service/worker.py", "old": "VALUE = 1", "new": "VALUE = 2"},
                                                {"path": "src/service/worker.py", "old": "VALUE = 2", "new": "VALUE = 3"}])
    assert (manager.candidate / "src/service/worker.py").read_text() == "VALUE = 3\n"


def test_successful_promotion_verifies_actual_workspace_and_updates_best(manager):
    candidate = edit(manager)["source_hash"]
    checked = []
    def verify(case):
        checked.append(case)
        return (case / "src/service/worker.py").read_text() == "VALUE = 2\n"
    result = manager.promote(candidate, verify)
    assert checked == [manager.live]
    assert manager.best_hash == candidate == source_hash(manager.live / "src")
    assert json.loads(Path(result["journal"]).read_text())["phase"] == "accepted"


def test_failed_actual_workspace_verification_restores_additions_and_deletions(manager):
    original = manager.best_hash
    manager.apply_edits(manager.candidate_hash, [{"path": "src/service/worker.py", "delete": True},
                                                {"path": "src/helper.py", "content": "VALUE = 2\n"}])
    with pytest.raises(PatchError, match="verification"):
        manager.promote(manager.candidate_hash, lambda case: False)
    assert source_hash(manager.live / "src") == original == manager.best_hash
    assert not (manager.live / "src/helper.py").exists()


def test_promotion_rolls_back_mid_install_failure(manager, monkeypatch):
    original = manager.best_hash
    edit(manager)
    manager.apply_edits(manager.candidate_hash, [{"path": "src/new.py", "content": "VALUE = 3\n"}])
    real = manager._move_prepared
    fired = False
    def fail_once(temporary, target):
        nonlocal fired
        if target == manager.live / "src/service/worker.py" and not fired:
            fired = True
            raise OSError("injected disk failure")
        return real(temporary, target)
    monkeypatch.setattr(manager, "_move_prepared", fail_once)
    with pytest.raises(OSError, match="injected"):
        manager.promote(manager.candidate_hash, lambda case: True)
    assert source_hash(manager.live / "src") == original == manager.best_hash


def test_promotion_rejects_stale_validation_or_external_source_edit(manager):
    candidate = edit(manager)["source_hash"]
    with pytest.raises(PatchError, match="changed after"):
        manager.promote("stale", lambda case: True)
    (manager.live / "src/service/worker.py").write_text("VALUE = 99\n")
    with pytest.raises(PatchError, match="outside"):
        manager.promote(candidate, lambda case: True)


def test_rollback_restores_file_replaced_by_directory(manager):
    node = manager.candidate / "src/node"
    node.write_text("original data\n")
    (manager.best / "src/node").write_text("original data\n")
    (manager.live / "src/node").write_text("original data\n")
    before = manager.best_hash
    manager.apply_edits(manager.candidate_hash, [{"path": "src/node", "delete": True},
                                                {"path": "src/node/helper.py", "content": "VALUE = 1\n"}])
    with pytest.raises(PatchError):
        manager.promote(manager.candidate_hash, lambda case: False)
    assert source_hash(manager.live / "src") == before == manager.best_hash
    assert (manager.live / "src/node").read_text() == "original data\n"


def test_rejected_promotion_can_be_retried(manager):
    candidate = edit(manager)["source_hash"]
    with pytest.raises(PatchError):
        manager.promote(candidate, lambda case: False)
    assert manager.promote(candidate, lambda case: True)["accepted"]


@pytest.mark.parametrize("source", ["return 1\n", "break\n", "nonlocal x\n", "await work()\n"])
def test_compile_rejects_semantically_invalid_python(manager, source):
    before = manager.candidate_hash
    with pytest.raises(PatchError, match="invalid Python"):
        manager.apply_edits(before, [{"path": "src/helper.py", "content": source}])
    assert manager.candidate_hash == before


def test_source_package_named_results_is_staged_and_editable(tmp_path):
    case = tmp_path / "case"
    (case / "src/results").mkdir(parents=True)
    (case / "src/results/__init__.py").write_text("VALUE = 1\n")
    manager = SourceManager(case)
    assert (manager.candidate / "src/results/__init__.py").is_file()
    manager.apply_edits(manager.candidate_hash, [{"path": "src/results/__init__.py", "old": "1", "new": "2"}])


def test_final_rollback_retains_the_previous_accepted_repair(manager):
    first = edit(manager)["source_hash"]
    manager.promote(first, lambda case: True)
    manager.apply_edits(manager.candidate_hash, [{"path": "src/service/worker.py", "old": "VALUE = 2", "new": "VALUE = 3"},
                                                {"path": "src/helper.py", "content": "VALUE = 4\n"}])
    manager.promote(manager.candidate_hash, lambda case: True)
    assert manager.rollback_last() == first
    assert source_hash(manager.live / "src") == first == manager.best_hash
    assert not (manager.live / "src/helper.py").exists()
