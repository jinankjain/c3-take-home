# CitrineCourier

A small courier accepts immutable logical jobs and applies them to an external
ledger. The injected journal and external ledger are durable; courier RAM and
timers vanish on a process restart. Transport replies can be lost after commit.

Repair only src/. Tests, simulator, scenarios, and contracts are fixed inputs.
Current service contracts are in service/CitrineCourier/ in the mounted corpus.

Run from the case directory:

    python -m pytest -q tests
    python -m sim replay --scenario public --seed 13 --out /tmp/citrine-replay
    python -m sim check --trace /tmp/citrine-replay/trace.jsonl

The replay CLI exits zero when the simulation ran, including protocol failures.
The checker exits one for invariant violations. Inspect summary.json,
trace.jsonl, and source-bound controller evidence; exit zero alone proves no repair.
