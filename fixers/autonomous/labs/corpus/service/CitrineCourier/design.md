# CitrineCourier delivery protocol
Status: CURRENT
Service: CitrineCourier

## Admission and durable handoff

A successful accept(job, payload) transfers responsibility from the caller to
the courier. The intake record must be durable before the acknowledgement is
observable. A restart may discard RAM and scheduled work, but recovery must
reconstruct every accepted, unfinished job from the journal.

An intake record contains job, immutable payload, and operation_id. The
operation identity is the namespace and logical job identity, independent of
worker boot, retry count, transmission, or receipt timing. Repeated acceptance
of the same job and payload returns the existing result. A conflicting payload
under the same job identity is rejected.

## External delivery

The external ledger deduplicates Apply by operation_id. It commits the effect
before replying. A dropped reply therefore means UNKNOWN, not that the effect
did not happen. Retransmission uses the same operation_id and immutable payload.
Attempt numbers may identify transport activity in logs but are not effect IDs.

A cached receipt and a newly applied receipt have the same completion meaning.
Journal delivery completion before reporting it to a waiting client. Recovery
replays unfinished intake; it does not repeat delivery for a durable completion.

## Diagnostic distinctions

No Apply after an accepted job survives a restart is a handoff/recovery problem.
Multiple committed Apply effects for one logical job are an operation-identity
problem. An extra transport attempt with one committed effect is normal.
