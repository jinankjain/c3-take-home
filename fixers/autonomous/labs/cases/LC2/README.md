# MoraineSlots

A supervisor journals weighted capacity reservations and starts external workers.
Workers survive a supervisor restart. A new supervisor fences and retires work
from previous epochs, then makes its capacity available to new requests.

Repair only src/. Tests, simulator, scenarios, and contracts are fixed inputs.
Current service contracts are in service/MoraineSlots/ in the mounted corpus.

Run from the case directory:

    python -m pytest -q tests
    python -m sim replay --scenario public --seed 17 --out /tmp/moraine-replay
    python -m sim check --trace /tmp/moraine-replay/trace.jsonl

Replay exit zero says the simulation completed, including unsuccessful workflows.
The checker and the recorded client outcomes establish progress and safety.
