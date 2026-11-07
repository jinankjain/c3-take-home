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
`c3-fixer-autonomous:24e51ae` (`linux/amd64`). Copy the repository, including
the lab cases, corpus and private grading fixtures, and the frozen image
tarball to the grading machine. The fixtures stay on the host.

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

Load the supplied frozen image, then check the engine and platform:

```sh
docker load -i results/artifacts/c3-fixer-autonomous-24e51ae-linux-amd64.tar
docker info
docker image inspect c3-fixer-autonomous:24e51ae --format '{{.Os}}/{{.Architecture}}'
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
for each bug, and each case's eight public tests plus four private regressions
passed at every stage.

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
  --fixer c3-fixer-autonomous:24e51ae --mode docker --proxy http \
  --cases fixers/autonomous/labs/cases \
  --private-root fixers/autonomous/labs/private/labs \
  --corpus fixers/autonomous/labs/corpus \
  --concurrency 1 --eval-workers 2 --keep \
  --out results/autonomous-chained-labs-r4
```

Use `grader/grade.py` directly for these custom fixtures: `grade.sh` appends
`grading/` as its private root, overriding an explicit lab private root.
`--proxy http` requires the live proxy and reports a dead connection as an
infrastructure error.

Use a fresh `--out` directory when repeating a recorded run, for example
`results/autonomous-chained-labs-repro`.

Inspect each case's `result.json`, `eval.json`, `fixer.log` and `patch.diff` in
the output directory. Grading reports bugs fixed, the 20-seed checks, tests,
regressions, wall time and model spend. The retained source-bound traces and
native reviews show whether the fixer observed the newly exposed failure and
repaired both mechanisms. Reference success alone gives no autonomous score.

## Recorded autonomous results

### Final image `24e51ae`: labs R4

The final image solved **4/4 bugs**. All 80 seed cases have two saved replays
with identical trace bytes, and both cases passed their eight public tests and
four private regressions. No timeout or infrastructure error occurred.

| Case | Bugs fixed | Fixer seconds | Model spend | Repair turns |
| --- | --- | --- | --- | --- |
| LC1 | 2/2 | 98.067 | $0.363976 | 8 |
| LC2 | 2/2 | 89.376 | $0.347121 | 8 |

Total spend was **$0.711097** in 187.443 cumulative fixer seconds. Each case
used one combined patch and one full native-reviewed promotion. Reviewed,
installed, final and evaluated source hashes matched, as did patch digests.
LC1 recorded one fresh ordinary replay and LC2 three; neither returned an
existing cached observation. Neither case exercised compaction, forced
diagnostics, proactive validation or iterative partial promotion. These fast
lab runs therefore do not demonstrate the cache-stall repair behavior.

The bootstrap planner selected zero additional controls for the independent
lab grammar. Accepted/candidate healthy-base replays and private healthy
regressions passed. Artifacts are at `results/autonomous-chained-labs-r4` and
the independent audit is `results/artifacts/image-r5-audit-labs.json`.

### Prior image `44e94ee`: labs R3

This frozen image solved **4/4 bugs**. Each bug passed all 20 grading seeds:
80 seed cases with 160 saved replays, identical trace bytes for every repeated
pair. Each case passed eight public tests and four private regressions, for
16 public tests and eight private regressions in total. There were no timeouts
or infrastructure errors.

| Case | Bugs fixed | Fixer seconds | Model spend | Repair turns |
| --- | --- | --- | --- | --- |
| LC1 | 2/2 | 108.528 | $0.444839 | 9 |
| LC2 | 2/2 | 80.511 | $0.302007 | 7 |

Total model spend was **$0.746846**, with 189.039 cumulative fixer seconds.
Both runs repaired their two mechanisms in one combined patch and one full
promotion. Approved native reviews, installed source, final verification and
the evaluator's source hashes agreed; patch digests also matched.

LC1 compacted once after promotion, from 182,063 to 97,706 characters while
retaining two complete native pairs. LC2 did not compact. Neither run exercised
forced paired or single-source diagnostics, or iterative partial promotion.
The bootstrap planner selected and executed zero additional controls: these
independent action schemas are outside its recognized scenario grammar.
Accepted/candidate healthy-base replays and the private healthy regressions
passed.

Artifacts are preserved under `results/autonomous-chained-labs-r3`; the
independent audit is `results/artifacts/image-r4-audit-labs.json`. The offline
Controller proof separately exercises partial then full promotion. These lab
results do not establish success on unseen services or the official cases.

### Prior image `553364e`: labs R2

This image also solved **4/4 bugs**, with all 20 seeds per bug and identical
repeated traces. Each case passed eight public tests and four private
regressions, with no timeout or infrastructure error.

| Case | Bugs fixed | Fixer seconds | Model spend |
| --- | --- | --- | --- |
| LC1 | 2/2 | 101.558 | $0.444160 |
| LC2 | 2/2 | 97.929 | $0.488013 |

Total model spend was **$0.932173**, with 199.487 cumulative fixer seconds.
Each run used one combined repair; forced diagnostics and partial promotion
were unexercised. Earlier prose incorrectly said these runs did not compact.
The saved logs show one compaction per case: LC1 from 194,778 to 88,202
characters while retaining one complete native pair, and LC2 from 180,574 to
118,358 while retaining three. The bootstrap control planner selected
zero inputs for these independent lab schemas; existing healthy-base replays
and private healthy regressions passed. Artifacts are under
`results/autonomous-chained-labs-r2`; the independent audit is
`results/artifacts/final-audit-labs.json`.

### Initial image `b347883`: labs R1

This image passed **4/4 bugs**, all 20 seeds per bug, and the public/private
gates, with no timeout or infrastructure error.

| Case | Bugs fixed | Fixer seconds | Model spend |
| --- | --- | --- | --- |
| LC1 | 2/2 | 81.035 | $0.305925 |
| LC2 | 2/2 | 84.616 | $0.337957 |

Total model spend was **$0.643882**, with 165.651 cumulative fixer seconds.
Earlier artifacts remain under `results/autonomous-chained-labs-r1`. These
four trials do not establish a cost or latency improvement.
