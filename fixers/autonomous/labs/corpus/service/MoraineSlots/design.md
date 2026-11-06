# MoraineSlots lease protocol
Status: CURRENT
Service: MoraineSlots

## Reservations and generations

A lease reserves positive capacity units and has an immutable lease_id and
epoch. Journal a reservation before sending Start. The supervisor advances its
durable epoch on every boot. A reservation from an older epoch belongs to a
worker that the new supervisor cannot continue as current work.

Recovery retires old reservations through Stop carrying the new fencing epoch.
The executor durably remembers a Stop fence even if the corresponding Start
has not arrived. A delayed old Start is then fenced. Equal or older fences
cannot stop a current worker.

## Retirement and capacity

A matching StopAck means the old worker is stopped or prevented from starting.
Persist closure, remove that exact lease, and return exactly its reserved units
to the available pool. A duplicate acknowledgement or one for a previously
reused task identifier must not release the newer lease or decrement twice.

Normal completion and recovery retirement both close reservations, although
their client outcomes differ: completed work is OK; retired old work is
CANCELLED. Task identifiers may be reused after closure, but lease IDs may not.

The capacity cache represents currently open durable reservations. A request
that fits after retirement must be admitted without another supervisor restart.
External running units may never exceed configured capacity. A capacity denial
while an old reservation is still open differs from a denial after it was closed.
