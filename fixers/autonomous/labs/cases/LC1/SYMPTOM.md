# LC1: CitrineCourier deliveries disappear after restart

The caller receives an accepted response for parcel, then its delivery remains
unresolved after a courier restart. The external ledger has no corresponding
effect in the attached public trace. Healthy deliveries and repeated acceptance
tests still pass. Investigate the complete delivery workflow: fixing one
observed symptom may reveal a later failure.

Public replay seeds: 13, 31, 47. Budget: 900 seconds and USD 6.
