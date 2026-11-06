# Chained workflow labs

Two invented services exercise repairs that reveal a later protocol failure.
Each case has two bugs, a 900-second deadline and a $6 model budget. Docker
execution uses the grader's two-CPU, 4-GiB limits.

| Case | Initial failure | Failure exposed by the first repair |
| --- | --- | --- |
| LC1 / CitrineCourier | Intake is acknowledged before responsibility reaches the durable journal. A restart loses the job and prevents dispatch. | After durable intake is repaired, a lost reply after an external commit causes retries with new operation IDs and duplicate effects. |
| LC2 / MoraineSlots | Recovery selects the wrong epoch range for retirement, leaving an older lease active. | After recovery retires that lease, StopAck closes its durable reservation but leaves the cached used-unit count unchanged, blocking a later admission. |

These are state-machine defects in service source. The trusted simulator owns
crashes, accepted client responses, durable storage, external effects, the
clock and invariant checks. Removing diagnostic source logs preserves both
failure families.

## Layout and isolation

```text
labs/
  cases/LC1/ and cases/LC2/    src, tests, sim, scenarios and case contracts
  corpus/                    INDEX.md and current service design documents
  private/labs/LC1/ and LC2/  grading seeds, incident, regressions and references
  selftest.py                offline fixture and Controller proof
  selftest-report.json        generated proof summary
```

Each case exposes the usual C3 interface: `src/`, `tests/`, `sim/`,
`scenarios/`, `SYMPTOM.md`, `INVARIANTS.md`, `scenario_schema.md` and `case.json`.
Private grading tests each bug on 20 seeds; the second bug is gated by the
first. Healthy controls cover quiet delivery, restart-free behavior and reused
logical identifiers with changed reservation sizes.

Private files include separate stage-one and full reference source, per-bug
patches, seed lists and regression baselines. The grader mounts the selected
case and public corpus for the fixer; private references remain on the grading
host. The autonomous Docker build copies only `fixer/`, its entrypoint and
pinned dependencies. Its `.dockerignore` excludes this entire lab directory.

## Host setup

Run these commands from the repository root. The host needs Python 3.12 or
newer, pytest and PyYAML, the supplied `grader/` and `proxy/`, and a running
Docker engine for autonomous grading. The frozen image is
`c3-fixer-autonomous:b347883` (`linux/amd64`).

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r fixers/autonomous/requirements.txt
export PATH="$PWD/.venv/bin:$PATH"
export C3_GRADER_PYTHON="$PWD/.venv/bin/python"
```

On macOS, add Docker Desktop's CLI if it is absent from `PATH`:

```sh
export PATH="$HOME/.docker/bin:$PATH"
```

Check that the engine and frozen image are available:

```sh
docker info
docker image inspect c3-fixer-autonomous:b347883 --format '{{.Os}}/{{.Architecture}}'
```

## Offline selftest

```sh
.venv/bin/python fixers/autonomous/labs/selftest.py --workers 2
```

This makes zero model requests. It uses the existing evaluator to verify
unfixed, stage-one and full reference sources, deterministic repeated replays,
20 seeds per bug, public tests, private healthy regressions and public controls.
It also checks that removing every diagnostic source-log call cannot evade the
faults or change the expected failure families.

A real Controller promotes the supplied partial and full repairs in disposable
copies using a zero-cost native review stub and verifies the installed source.
That proves fixture behavior and Controller plumbing. Autonomous solving is
established by the separate Docker run below.

The selftest refreshes `selftest-report.json`, private regression baselines and
reference message counts, and the original public trace logs. Service source
stays unchanged. The recorded offline run completed in 40.41 seconds: both
cases progressed from 0 to 1 to 2 fixed bugs, full repairs passed 20/20 seeds
for each bug, and all eight public tests plus four private regressions passed
at every stage.

## Autonomous Docker grading

Use the funded assessment key in the repository's `.env`. Reuse an existing
healthy proxy on `127.0.0.1:8787`, or start it in another terminal:

```sh
.venv/bin/python proxy/server.py --env .env
```

The proxy reads `.env` at startup and mints per-case tokens through the grader.
The following command makes paid model requests under each case's $6 budget:

```sh
.venv/bin/python grader/grade.py \
  --fixer c3-fixer-autonomous:b347883 --mode docker --proxy http \
  --cases fixers/autonomous/labs/cases \
  --private-root fixers/autonomous/labs/private/labs \
  --corpus fixers/autonomous/labs/corpus \
  --concurrency 2 --eval-workers 2 --keep \
  --out results/autonomous-chained-labs-r1
```

Use `grader/grade.py` directly for these custom fixtures: `grade.sh` appends
`grading/` as its private root, overriding an explicit lab private root.
`--proxy http` requires the live proxy and reports a dead connection as an
infrastructure error.

Inspect each case's `result.json`, `eval.json`, `fixer.log` and `patch.diff` in
the output directory. Grading reports bugs fixed, the 20-seed checks, tests,
regressions, wall time and model spend. The retained source-bound traces and
native reviews show whether the fixer observed the newly exposed failure and
repaired both mechanisms. Reference success alone gives no autonomous score.

## Recorded autonomous results

The frozen `b347883` image solved **4/4 bugs** on the two labs. Both cases passed
all 20 seeds per bug with deterministic repeats, eight public tests and four
private regressions, with no timeout or infrastructure error.

| Case | Bugs fixed | Fixer seconds | Model spend |
| --- | --- | --- | --- |
| LC1 | 2/2 | 81.035 | $0.305925 |
| LC2 | 2/2 | 84.616 | $0.337957 |

Each run repaired both mechanisms in one patch after inspecting source and
contracts. The offline Controller proof separately exercises partial then full
promotion. These paid runs did not exercise the stagnation interventions or
probe composition. Artifacts are under `results/autonomous-chained-labs-r1`,
including image/runtime metadata and unchanged original fixture inputs.
