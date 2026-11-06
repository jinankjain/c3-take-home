# MoraineSlots scenario schema

A YAML/JSON mapping with name, actions, and optional world, params, faults, expect.
Top-level faults may be a mapping or a path relative to the scenario;
--faults overrides resolve relative to the case directory. Includes unsupported.

    name: recovery-admission
    world: {capacity: 2}
    params: {horizon: 60}
    actions:
      - {at: 0, op: acquire, task: holding, units: 2, duration: 100}
      - {at: 16, op: acquire, task: replacement, units: 2, duration: 4}
    faults: {restart_at: 5, duplicate_stop_ack: true}
    expect: {completed: 1, cancelled: 1, all_resolved: true}

There are 1-16 actions. capacity is 1-8 units. at lies within horizon (1-1000
simulated milliseconds); units is positive; duration is positive and may exceed
horizon for work that should be retired. Defaults: at=0, units=1, duration=4.

restart_at is a time or null; the supervisor loses RAM at that time while the
journal/executors survive. duplicate_stop_ack optionally repeats valid StopAck
delivery. Start and reply transport delays vary deterministically with seed.

An acquisition retries capacity denials. One logical client action ends after
its work completes or is cancelled by recovery. To isolate retirement, preserve
the holding action and restart but omit the replacement action; update expect.
To exercise capacity reuse, retain the later replacement admission.
