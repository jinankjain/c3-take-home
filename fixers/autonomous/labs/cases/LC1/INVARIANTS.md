# CitrineCourier invariants

- DURABLE_HANDOFF: accepted work survives a courier crash and reaches a terminal
  delivery outcome. At crash, every acknowledged job has durable intake.
- EXACTLY_ONE_EFFECT: a completed logical job has exactly one matching external
  effect and a receipt for that effect. Retries cannot change the effect domain.
- I3: message sends must remain within three times the reference traffic.

The checker reads trusted journal/external-effect records and client outcomes.
A missing dispatch can mask a later effect-identity failure. Probe each causal
obligation and compare accepted/original and candidate source snapshots.
