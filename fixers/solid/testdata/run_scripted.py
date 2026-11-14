#!/usr/bin/env python3
"""Offline test: run the fixer with a scripted LLM on a fresh copy of a practice case, then grade
the resulting src/ with the pack's real evaluator.

    python fixers/solid/testdata/run_scripted.py CASE SCRIPT [--deadline-s N] [--sigterm-after S]
e.g. python fixers/solid/testdata/run_scripted.py A1 A1-fix
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXER = HERE.parent
PACK = FIXER.parent.parent

ap = argparse.ArgumentParser()
ap.add_argument("case")
ap.add_argument("script")
ap.add_argument("--deadline-s", type=float, default=900)
ap.add_argument("--sigterm-after", type=float, default=None)
ap.add_argument("--no-grade", action="store_true")
a = ap.parse_args()

tmp = Path(tempfile.mkdtemp(prefix=f"c3test-{a.case}-"))
ws = tmp / "workspace"
shutil.copytree(PACK / "practice" / a.case, ws)
env = dict(os.environ, C3_WORKSPACE=str(ws), C3_CORPUS=str(PACK / "corpus/build/practice"),
           C3_DEADLINE=str(int(time.time() + a.deadline_s)), C3_BUDGET_USD="3",
           FIXER_LLM=f"scripted:{HERE / 'scripts' / (a.script + '.json')}",
           ANTHROPIC_BASE_URL="http://127.0.0.1:9", ANTHROPIC_API_KEY="none")
t0 = time.time()
p = subprocess.Popen(["sh", str(FIXER / "run.sh")], cwd=ws, env=env, stdout=subprocess.PIPE,
                     stderr=subprocess.STDOUT, text=True)
if a.sigterm_after:
    try:
        p.wait(timeout=a.sigterm_after)
    except subprocess.TimeoutExpired:
        p.send_signal(signal.SIGTERM)
out, _ = p.communicate()
wall = time.time() - t0
(tmp / "fixer.log").write_text(out)
print(out)
print(f"=== fixer exit {p.returncode} after {wall:.0f}s; log at {tmp / 'fixer.log'}")
diff = subprocess.run(["diff", "-ruN", "-x", "__pycache__", str(PACK / "practice" / a.case / "src"), str(ws / "src")],
                      capture_output=True, text=True).stdout
print("=== patch:\n" + (diff or "(no change)"))
stray = [p.name for p in ws.iterdir() if p.name.startswith(".src")]
print(f"=== leftover temp dirs in workspace: {stray or 'none'}")
if not a.no_grade:
    ev = tmp / "eval.json"
    subprocess.run([sys.executable, str(PACK / "grader/evaluate.py"), "--case-dir", str(PACK / "practice" / a.case),
                    "--src", str(ws / "src"), "--out", str(ev)], capture_output=True, text=True)
    d = json.loads(ev.read_text())
    print(f"=== GRADE {a.case}: bugs_fixed={d['bugs_fixed']}/{len(d['bugs'])} tests_ok={d['tests_ok']} "
          f"regression_ok={d['regression_ok']}")
