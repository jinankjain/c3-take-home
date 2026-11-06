from __future__ import annotations

import json
from pathlib import Path
import time

import pytest

from fixer.controller import Controller
from fixer.model import ModelReply
from fixer.patches import source_hash
from fixer.runtime import CaseConfig, Deadline


CLI = r'''
import argparse, hashlib, json, os, pathlib, runpy, sys
import yaml
p = argparse.ArgumentParser()
sub = p.add_subparsers(dest="mode", required=True)
r = sub.add_parser("replay")
r.add_argument("--scenario"); r.add_argument("--seed", type=int); r.add_argument("--out"); r.add_argument("--faults")
c = sub.add_parser("check"); c.add_argument("--trace")
a = p.parse_args()
root = pathlib.Path.cwd()
if a.mode == "replay":
    source = runpy.run_path(str(root / "src/service.py"))
    scenario = yaml.safe_load(pathlib.Path(a.scenario).read_text())
    checks = scenario.get("checks", ["OWNERSHIP", "RECOVERY"])
    event = {"seq": 0, "t": 0, "kind": "log", "event": "scenario", "node": "sim",
             "name": scenario["name"], "seed": a.seed,
             "violations": [name for name in checks if source.get(name + "_BROKEN", False)]}
    text = json.dumps(event, sort_keys=True) + "\n"
    out = pathlib.Path(a.out); out.mkdir(exist_ok=True)
    (out / "trace.jsonl").write_text(text)
    summary = {"scenario": scenario["name"], "seed": a.seed, "all_resolved": True,
               "outcomes": {"ok": 1}, "actions": [], "messages_sent": 2, "error": None,
               "trace_sha256": hashlib.sha256(text.encode()).hexdigest()}
    (out / "summary.json").write_text(json.dumps(summary))
    print(json.dumps(summary))
else:
    event = json.loads(pathlib.Path(a.trace).read_text())
    violations = [{"invariant": name, "t": 0, "detail": "contract violated"} for name in event["violations"]]
    print(json.dumps({"violations": len(violations), "invariants": violations}))
    sys.exit(1 if violations else 0)
'''


class Reviewer:
    remaining_budget = 3.0
    spent_usd = 0.0

    def __init__(self, approve=True):
        self.approve = approve
        self.reviews = []

    def messages(self, system, messages, tools, **kwargs):
        self.reviews.append(messages)
        verdict = {"approve": self.approve, "issues": [] if self.approve else ["ownership guard is incomplete"],
                   "missing_evidence": []}
        return ModelReply([{"type": "text", "text": json.dumps(verdict)}], "end_turn", {}, 0, 3)


@pytest.fixture
def controller(tmp_path):
    case = tmp_path / "case"
    for name in ("src", "tests", "sim", "scenarios"):
        (case / name).mkdir(parents=True, exist_ok=True)
    (case / "src/service.py").write_text(
        "OWNERSHIP_BROKEN = True\nRECOVERY_BROKEN = True\nNORMAL_BROKEN = False\n"
        "def calculate(value):\n    return value * 2\n")
    (case / "tests/test_behavior.py").write_text(
        "from service import calculate\n"
        "def test_public_api():\n    assert calculate(2) == 4\n")
    (case / "sim/__init__.py").write_text("")
    (case / "sim/__main__.py").write_text(CLI)
    (case / "scenarios/public.yaml").write_text("name: public\n")
    (case / "scenarios/base.yaml").write_text("name: base\nchecks: [NORMAL]\n")
    for name in ("SYMPTOM.md", "README.md", "INVARIANTS.md", "scenario_schema.md"):
        (case / name).write_text("Ownership and recovery must preserve accepted operations.")
    corpus = tmp_path / "corpus"
    (corpus / "service/Unseen").mkdir(parents=True)
    (corpus / "platform").mkdir()
    (corpus / "INDEX.md").write_text("# Contracts\n- service/Unseen/\n")
    (corpus / "service/Unseen/design.md").write_text("# Service design\nStatus: CURRENT\nOwnership must be current and recovery durable.\n")
    config = CaseConfig(case, corpus, Deadline(time.time() + 180), 3, {"public_seeds": [1, 2], "codebase": "Unseen"})
    instance = Controller(config, model=Reviewer())
    instance.bootstrap()
    yield instance
    instance.runner.close()


def repair_ownership(controller):
    return controller.sources.apply_edits(controller.sources.candidate_hash, [{
        "path": "src/service.py", "old": "OWNERSHIP_BROKEN = True", "new": "OWNERSHIP_BROKEN = False"}])


def plan():
    return {"target_invariants": ["OWNERSHIP"], "reproductions": [{"scenario": "public", "seed": 1}],
            "reason": "enforce current ownership before a protected action"}
