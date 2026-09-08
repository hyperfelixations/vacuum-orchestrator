# Vacuum Orchestrator

Vacuum Orchestrator is a manufacturer-neutral Home Assistant custom integration
for persistent cleaning jobs, one global pending queue, and safe dispatch to one
or more robot vacuums.

## Status

Version `0.1.0` is an early development version. Its orchestration core and
Home Assistant interfaces are executable and extensively tested, but the
integration is not yet ready for production use or HACS distribution.

## Implemented foundation

- one integration-wide job registry and one pending queue;
- create, partially update, delete, move, start, cancel, and retry jobs;
- run, pause, resume, and inspect the queue;
- four canonical cleaning modes: `vacuum`, `mop`, `vacuum_and_mop`, and
  `vacuum_then_mop`;
- accepted shorthand keys `vac`, `vac_and_mop`, and `vac_then_mop`;
- manufacturer-neutral planning from `JobIntent` to `ExecutionPlan` and
  atomic `WorkUnit` objects before a robot is selected;
- optional explicit robot selection or deterministic automatic selection;
- centralized job readiness and robot availability evaluation;
- automatic continuation with the next work unit and the next eligible queued
  job after strongly correlated completion evidence;
- at most one active job per robot and no concurrent work in overlapping areas;
- per-robot command serialization, leases, generation fencing, and conservative
  recovery when physical ownership is uncertain;
- atomic, integrity-protected Home Assistant storage with verified read-back and
  a non-destructive migration path from the earlier development schema;
- compact status entities plus authenticated, paginated WebSocket queries for
  cards and other clients.

Jobs are not stored in entity attributes. Entities expose only bounded summary
state; the job registry and execution ledger live in the integration's
versioned Home Assistant store.

## Basic use

After configuring the integration and at least one robot profile, a job can be
created from an automation or Home Assistant's action developer tool:

```yaml
action: vacuum_orchestrator.create_job
data:
  name: Kitchen after dinner
  areas:
    - kitchen
  mode: vacuum_then_mop
  vacuum_power: high
  mop_intensity: standard
  source: automation
  reason: dinner_finished
  required_on:
    - binary_sensor.kitchen_door_open
```

The response can return the generated `job_id` when the caller requests a
response. That ID is sufficient for later commands:

```yaml
action: vacuum_orchestrator.move_job
data:
  job_id: 3dc8973e-3f88-4a91-b0c7-7cfa47d82a30
  direction: top
```

```yaml
action: vacuum_orchestrator.start_job
data:
  job_id: 3dc8973e-3f88-4a91-b0c7-7cfa47d82a30
```

`robot_id` is optional for `start_job`. When omitted, the orchestrator chooses
an eligible available robot. When supplied, the selected robot must still pass
the same centralized capability, readiness, availability, lease, and area-
overlap checks.

Call `vacuum_orchestrator.run_queue` to enable automatic processing. A blocked
job remains queued; another independently eligible job may be dispatched. Use
`pause_queue` to prevent new automatic dispatches without interrupting active
cleaning.

Internal job and queue revisions are returned for observation and diagnostics,
but callers do not supply expected revision values when editing or moving jobs.

## Read interfaces

Home Assistant actions provide bounded `get_queue` and `get_job` responses. A
card can use these authenticated WebSocket commands:

- `vacuum_orchestrator/queue/get`
- `vacuum_orchestrator/job/get`
- `vacuum_orchestrator/jobs/list`
- `vacuum_orchestrator/subscribe`

The subscription sends lightweight commit notifications. Clients then refresh
only the bounded queue or job pages they need; unbounded job lists are never
placed in entity attributes.

## Current adapter boundary

The included Home Assistant vacuum adapter intentionally exposes only behavior
it can prove through public Home Assistant contracts. At present it dispatches
`vacuum.clean_area`, cancels with `vacuum.stop`, and observes the vacuum entity
plus optional last-clean start and end sensors.

Its currently declared executable capability is one-pass atomic
`vacuum_and_mop` without semantic fan, water, or mop tuning. Other modes and
settings are modeled by the core but require a future adapter that can execute
and observe them without silently changing their meaning. With the default
`best_effort` settings policy, unsupported optional tuning may be omitted;
`strict` requires every requested setting.

## Not yet implemented

- full Roborock-specific capability discovery and vendor extensions;
- vacuum-freshness evidence and automatic vacuum prerequisites before mopping;
- configurable stale/time-window readiness policies;
- job templates, automatic job generation, and queue consolidation;
- manual recovery controls, Repairs, diagnostics exports, and trace replay;
- production installation and release documentation.

## Development

```powershell
python -m pip install -r requirements-test.txt
python -m pytest
python -m ruff format --check custom_components tests
python -m ruff check custom_components tests
python -m mypy custom_components/vacuum_orchestrator
```

The test suite requires no physical robot. Adapter behavior, timing, failures,
storage faults, restarts, and observations are exercised with deterministic
fakes and Home Assistant test infrastructure.

## License

Vacuum Orchestrator is licensed under the MIT License. See [LICENSE](LICENSE).
