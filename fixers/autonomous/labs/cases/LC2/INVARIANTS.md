# MoraineSlots invariants

- RECOVERY_RETIRE: reservations present at supervisor crash are eventually
  durably closed and their old workers stopped or already completed.
- CAPACITY_REUSE: a valid request that fits the remaining durable reservation
  pool cannot receive a capacity denial.
- WORKFLOW_RESULT: starts require durable reservations; completions and
  cancellations require matching external outcomes. Configured capacity,
  units, and fencing cannot be bypassed.
- I3: message sends remain within three times the reference traffic.

A denial while an old reservation remains open may be justified; a denial
after retirement needs separate investigation. Observe lease IDs, epochs,
journal closure, external Stop, and the next admission attempt.
