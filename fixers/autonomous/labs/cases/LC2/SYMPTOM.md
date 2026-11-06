# LC2: MoraineSlots queue remains full after supervisor recovery

Holding work occupied the pool when the supervisor restarted. Replacement
requests remain pending with capacity denials. The workers are a separate
durable system, so restarting the supervisor does not kill them. Ordinary
weighted reservations and completion tests pass. Follow recovery through the
later admission attempt; more than one protocol defect may be involved.

Public replay seeds: 17, 37, 59. Budget: 900 seconds and USD 6.
