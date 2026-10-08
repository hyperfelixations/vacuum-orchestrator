"""Queue-run grants expire only after a complete configurable idle window."""

from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.vacuum_orchestrator.domain.errors import ValidationError
from custom_components.vacuum_orchestrator.domain.queue_runs import QueueRun
from custom_components.vacuum_orchestrator.domain.releases import ReleaseKind
from custom_components.vacuum_orchestrator.domain.types import (
    CleaningMode,
    JobState,
    MoveDirection,
    OperationKind,
    QueueMode,
    RobotAvailabilityState,
)
from custom_components.vacuum_orchestrator.infrastructure.codec import (
    decode_orchestrator_state,
    encode_orchestrator_state,
)
from tests.application.test_orchestrator import (
    NOW,
    RecordingAdapter,
    RecordingBackend,
    _intent,
    _orchestrator,
)
from tests.application.test_room_execution import finished_run


async def setup_run():
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)
    now = [NOW]
    core._clock = lambda: now[0]
    core.runs._clock = lambda: now[0]
    core.rooms._clock = lambda: now[0]
    return core, adapter, now


async def test_blocked_pending_jobs_do_not_keep_run_open_and_only_run_grants_expire():
    core, _, now = await setup_run()
    await core.rooms.async_grant("kitchen", ReleaseKind.QUEUE_RUN)
    await core.rooms.async_revoke("hall")
    job = await core.async_create_job(_intent(area="hall"))
    await core.async_run_queue()
    run = core.state.queue_run
    assert core.rooms.registry.resolve("kitchen").release.queue_run_id == run.run_id
    await core.async_reconcile_queue_run()
    assert core.state.queue_run.deadline == NOW + timedelta(minutes=15)
    assert (
        decode_orchestrator_state(encode_orchestrator_state(core.state)) == core.state
    )
    now[0] += timedelta(minutes=14)
    await core.async_reconcile_queue_run()
    assert core.state.queue_run.active
    now[0] += timedelta(minutes=1)
    await core.async_reconcile_queue_run()
    assert not core.state.queue_run.active
    assert core.state.mode is QueueMode.IDLE and core.state.queue == (job,)
    assert core.rooms.registry.resolve("kitchen").release is None
    await core.async_run_queue()
    assert core.state.queue_run.run_id != run.run_id


async def test_readiness_change_clears_idle_window_and_pause_preserves_run():
    core, _, now = await setup_run()
    await core.rooms.async_revoke("kitchen")
    job = await core.async_create_job(_intent())
    await core.async_run_queue()
    await core.async_reconcile_queue_run()
    run_id = core.state.queue_run.run_id
    now[0] += timedelta(minutes=14)
    await core.rooms.async_grant("kitchen", ReleaseKind.QUEUE_RUN)
    await core.async_reconcile_queue_run()
    assert core.state.queue_run.idle_since is None
    assert core.rooms.registry.resolve("kitchen").release.queue_run_id == run_id
    await core.async_set_queue_mode(QueueMode.PAUSED)
    now[0] += timedelta(hours=1)
    await core.async_reconcile_queue_run()
    assert core.state.queue_run.active and core.state.queue_run.run_id == run_id
    await core.async_set_queue_mode(QueueMode.RUNNING)
    await core.async_move_job(job, MoveDirection.BOTTOM)
    await core.async_reconcile_queue_run()
    assert core.state.queue_run.idle_since is None
    await core.async_cancel_job(job)
    await core.async_reconcile_queue_run()
    assert core.state.queue_run.deadline == now[0] + timedelta(minutes=15)


async def test_started_phases_and_uncertain_ownership_prevent_run_completion():
    core, adapter, now = await setup_run()
    await core.rooms.async_grant("kitchen", ReleaseKind.QUEUE_RUN)
    job = await core.async_create_job(_intent(mode=CleaningMode.VACUUM_THEN_MOP))
    await core.async_run_queue()
    attempt = core.state.jobs[job].active_attempt_id
    await core.async_confirm_start(attempt)
    await core.async_reconcile_queue_run()
    assert core.state.queue_run.idle_since is None
    adapter._profile = replace(
        adapter.profile, allowed_operations=frozenset({OperationKind.VACUUM})
    )
    await core.async_record_robot_run(
        attempt, finished_run(core.state.attempts[attempt])
    )
    assert core.state.jobs[job].state is JobState.DISPATCHING
    now[0] += timedelta(hours=1)
    await core.async_reconcile_queue_run()
    assert core.state.queue_run.active and core.state.queue_run.idle_since is None
    await core.async_cancel_job(job)
    await core.async_reconcile_queue_run()
    assert core.state.queue_run.idle_since == now[0]


async def test_new_work_joins_same_run_and_superseding_permanent_grant_survives():
    core, _, now = await setup_run()
    await core.rooms.async_grant("kitchen", ReleaseKind.QUEUE_RUN)
    await core.async_run_queue()
    await core.async_reconcile_queue_run()
    run_id = core.state.queue_run.run_id
    now[0] += timedelta(minutes=14)
    await core.async_create_job(_intent())
    await core.async_reconcile_queue_run()
    assert (
        core.state.queue_run.run_id == run_id
        and core.state.queue_run.idle_since is None
    )
    await core.rooms.async_grant("kitchen", ReleaseKind.PERMANENT)
    await core.async_cancel_job(core.state.queue[0])
    await core.async_reconcile_queue_run()
    now[0] += timedelta(minutes=15)
    await core.async_reconcile_queue_run()
    assert core.rooms.registry.resolve("kitchen").release.kind is ReleaseKind.PERMANENT


async def test_immediate_run_end_is_optional_and_grace_changes_apply_to_next_run():
    core, _, _ = await setup_run()
    await core.runs.async_configure(grace_seconds=0)
    await core.runs.async_configure(grace_seconds=0)
    await core.async_run_queue()
    await core.runs.async_configure(grace_seconds=30)
    assert core.state.queue_run.grace_seconds == 0
    await core.async_reconcile_queue_run()
    assert core.state.mode is QueueMode.IDLE
    await core.async_run_queue()
    assert core.state.queue_run.grace_seconds == 30
    with pytest.raises(ValidationError):
        await core.runs.async_configure(grace_seconds=86401)


@pytest.mark.parametrize(
    "changes",
    [
        {"grace_seconds": 86401},
        {"idle_since": NOW - timedelta(seconds=1)},
        {"completed_at": NOW},
    ],
)
def test_invalid_queue_run_time_contracts_are_rejected(changes):
    with pytest.raises(ValidationError):
        QueueRun("run", NOW, **changes)


async def test_idle_grace_and_grants_survive_restart_with_pending_blocked_work():
    core, adapter, _ = await setup_run()
    await core.rooms.async_grant("kitchen", ReleaseKind.QUEUE_RUN)
    await core.rooms.async_revoke("hall")
    job = await core.async_create_job(_intent(area="hall"))
    await core.async_run_queue()
    await core.async_reconcile_queue_run()
    run_id = core.state.queue_run.run_id
    reloaded = await _orchestrator(adapter._backend, adapter)
    reloaded._clock = lambda: NOW + timedelta(minutes=16)
    reloaded.runs._clock = reloaded._clock
    assert reloaded.state.queue_run.run_id == run_id
    await reloaded.async_reconcile_queue_run()
    assert reloaded.state.mode is QueueMode.IDLE
    assert reloaded.state.queue == (job,)
    assert reloaded.rooms.registry.resolve("kitchen").release is None


async def test_unavailable_robot_does_not_keep_unstarted_run_open():
    core, adapter, now = await setup_run()
    adapter.observation = replace(
        adapter.observation, state=RobotAvailabilityState.UNKNOWN
    )
    await core.rooms.async_grant("kitchen", ReleaseKind.QUEUE_RUN)
    job = await core.async_create_job(_intent())
    await core.async_run_queue()
    await core.async_reconcile_queue_run()
    now[0] += timedelta(minutes=15)
    await core.async_reconcile_queue_run()
    assert core.state.jobs[job].state is JobState.QUEUED
    assert core.state.mode is QueueMode.IDLE
