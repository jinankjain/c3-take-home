# CitrineCourier scenario schema

A YAML/JSON mapping with name, actions, and optional world, params, faults, expect.
File includes are unsupported. Scenario paths may be shipped names or absolute
controller probe paths. Top-level faults may be a mapping or a path relative to
the scenario. A --faults override is relative to the case directory.

    name: focused-delivery
    world: {namespace: citrine}
    params: {horizon: 60}
    actions:
      - {at: 0, op: submit, job: parcel, payload: {amount: 7}}
    faults:
      crash_after_accept: [parcel]
      lose_first_reply: [parcel]
    expect: {completed: 1, effects: 1, all_resolved: true}

There are 1-16 actions. at is nonnegative simulated milliseconds within horizon;
job is a nonempty string; amount is an integer. Defaults: at=0, amount=1,
namespace=citrine, horizon=60. horizon is between 1 and 1000.

Fault selectors name logical jobs. Crash occurs immediately after their first
accept acknowledgement and before batched intake flush. Lost reply drops only
the first external Apply response for the selected job, after the ledger's
commit decision. Empty selector lists disable those faults.

scenarios/faults/restart-only.yaml isolates restart from reply loss. Reuse the
public world/params/actions while selecting that profile, or create a small
immutable probe. Expected metrics do not change simulator behavior.
