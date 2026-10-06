"""Ending a queue run: finish started work or cancel it, then close at once."""

from dataclasses import replace

import pytest

from custom_components.vacuum_orchestrator.domain.errors import ConflictError
from custom_components.vacuum_orchestrator.domain.releases import ReleaseKind
from custom_components.vacuum_orchestrator.domain.types import (
    CleaningMode,
    JobState,
    OperationKind,
    QueueMode,
)
from custom_components.vacuum_orchestrator.infrastructure.codec import (
    decode_orchestrator_state,
    encode_orchestrator_state,
)
from tests.application.test_orchestrator import _intent
from tests.application.test_queue_runs import setup_run
from tests.application.test_room_execution import finished_run


async def _finish_active_attempt(core, job_id):
    attempt = core.state.jobs[job_id].active_attempt_id
    await core.async_confirm_start(attempt)
    await core.async_record_robot_run(
        attempt, finished_run(core.state.attempts[attempt], name=attempt)
    )


async def test_finish_lets_both_phases_complete_then_closes_without_grace():
    core, adapter, _now = await setup_run()
    await core.rooms.async_grant("kitchen", ReleaseKind.QUEUE_RUN)
    job = await core.async_create_job(_intent(mode=CleaningMode.VACUUM_THEN_MOP))
    waiting = await core.async_create_job(_intent("hall"))
    await core.async_run_queue()
    run_id = core.state.queue_run.run_id

    await core.async_end_queue()

    assert core.state.mode is QueueMode.PAUSED and core.state.queue_run.ending
    assert decode_orchestrator_state(encode_orchestrator_state(core.state)) == (
        core.state
    )
    await _finish_active_attempt(core, job)
    assert [unit.operation for unit, *_ in adapter.dispatches] == [
        OperationKind.VACUUM,
        OperationKind.MOP,
    ]
    await core.async_reconcile_queue_run()
    assert core.state.queue_run.ending
    await _finish_active_attempt(core, job)
    await core.async_reconcile_queue_run()

    assert core.state.jobs[job].state is JobState.COMPLETED
    run = core.state.queue_run
    assert (run.run_id, run.active, core.state.mode) == (run_id, False, QueueMode.IDLE)
    assert core.rooms.registry.resolve("kitchen").release is None
    assert core.state.queue == (waiting,) and len(adapter.dispatches) == 2


async def test_finish_without_started_work_closes_immediately_and_is_idempotent():
    core, _adapter, _now = await setup_run()
    await core.rooms.async_grant("kitchen", ReleaseKind.QUEUE_RUN)
    await core.async_run_queue()

    await core.async_end_queue()

    assert core.state.mode is QueueMode.IDLE and not core.state.queue_run.active
    assert core.rooms.registry.resolve("kitchen").release is None
    before = core.state.commit_id
    await core.async_end_queue()
    await core.async_end_queue(cancel_running=True)
    assert core.state.commit_id == before


async def test_unbound_grants_survive_and_paused_queue_without_run_goes_idle():
    core, _adapter, _now = await setup_run()
    await core.async_set_queue_mode(QueueMode.PAUSED)
    await core.rooms.async_grant("kitchen", ReleaseKind.QUEUE_RUN)

    await core.async_end_queue()

    assert core.state.mode is QueueMode.IDLE and core.state.queue_run is None
    release = core.rooms.registry.resolve("kitchen").release
    assert release.kind is ReleaseKind.QUEUE_RUN and release.queue_run_id is None


async def test_resuming_while_ending_withdraws_the_end_request():
    core, _adapter, _now = await setup_run()
    job = await core.async_create_job(_intent())
    await core.async_run_queue()
    run_id = core.state.queue_run.run_id
    await core.async_end_queue()
    assert core.state.queue_run.ending

    await core.async_run_queue()

    run = core.state.queue_run
    assert (run.run_id, run.ending, core.state.mode) == (
        run_id,
        False,
        QueueMode.RUNNING,
    )
    await core.async_set_queue_mode(QueueMode.PAUSED)
    await core.async_end_queue()
    await core.async_set_queue_mode(QueueMode.PAUSED)
    assert core.state.queue_run.ending
    await _finish_active_attempt(core, job)
    await core.async_reconcile_queue_run()
    assert not core.state.queue_run.active


@pytest.mark.parametrize("return_to_dock", [False, True])
async def test_cancel_stops_started_jobs_and_closes_the_run_now(return_to_dock):
    core, adapter, _now = await setup_run()
    await core.rooms.async_grant("kitchen", ReleaseKind.QUEUE_RUN)
    job = await core.async_create_job(_intent())
    waiting = await core.async_create_job(_intent("hall"))
    await core.async_run_queue()

    await core.async_end_queue(cancel_running=True, return_to_dock=return_to_dock)

    assert adapter.cancel_returns == [return_to_dock]
    assert core.state.jobs[job].state is JobState.CANCELING
    assert core.state.mode is QueueMode.IDLE and not core.state.queue_run.active
    assert core.rooms.registry.resolve("kitchen").release is None
    assert core.state.queue == (waiting,)


async def test_cancel_with_return_checks_every_robot_before_stopping_any():
    core, adapter, _now = await setup_run()
    job = await core.async_create_job(_intent())
    await core.async_run_queue()
    adapter._profile = replace(
        adapter.profile,
        capabilities=replace(adapter.profile.capabilities, returns_to_dock=False),
    )

    with pytest.raises(ConflictError, match="return_to_dock_unsupported"):
        await core.async_end_queue(cancel_running=True, return_to_dock=True)

    assert adapter.cancel_count == 0 and core.state.queue_run.active
    assert core.state.jobs[job].state is not JobState.CANCELING


async def test_failed_cancel_still_closes_the_run_and_is_reported():
    core, adapter, _now = await setup_run()
    await core.async_create_job(_intent())
    await core.async_run_queue()
    adapter.fail_cancel = True

    with pytest.raises(RuntimeError, match="cancel uncertainty"):
        await core.async_end_queue(cancel_running=True)

    assert not core.state.queue_run.active and core.state.mode is QueueMode.IDLE
