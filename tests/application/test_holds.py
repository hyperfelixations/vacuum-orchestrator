"""Held jobs never start; a lapsed or released hold restarts the start delay."""

from datetime import timedelta

import pytest

from custom_components.vacuum_orchestrator.domain.errors import ConflictError
from custom_components.vacuum_orchestrator.domain.holds import (
    HOLD_LEASE_SECONDS,
    HoldPurpose,
)
from custom_components.vacuum_orchestrator.domain.intents import JobIntentPatch
from custom_components.vacuum_orchestrator.domain.types import JobState
from tests.application.test_orchestrator import NOW, _intent, _orchestrator
from tests.application.test_start_delay import DELAY, setup_delay

LEASE = timedelta(seconds=HOLD_LEASE_SECONDS)


async def test_a_held_job_starts_on_no_path_until_saved() -> None:
    core, adapter, now = await setup_delay()
    job = await core.async_create_job(_intent())
    await core.async_run_queue()
    hold = await core.async_hold_job(job, HoldPurpose.EDIT)
    assert core.job_hold(job) == hold

    now[0] += DELAY
    assert await core.async_dispatch_available() == ()
    with pytest.raises(ConflictError, match="job_held"):
        await core.async_start_job(job)

    now[0] += timedelta(seconds=30)
    renewed = await core.async_renew_job_hold(hold.hold_id)
    assert renewed.expires_at == now[0] + LEASE
    await core.async_update_job(
        job, JobIntentPatch(note="checked"), hold_id=hold.hold_id
    )
    assert core.job_hold(job) is None
    assert core.state.jobs[job].start_after == now[0] + DELAY
    assert await core.async_dispatch_available() == ()

    now[0] += DELAY
    assert len(await core.async_dispatch_available()) == 1
    assert core.state.jobs[job].state is not JobState.QUEUED
    assert adapter.dispatches


async def test_a_lapsed_hold_ends_with_a_new_delay_before_any_start() -> None:
    core, _adapter, now = await setup_delay()
    job = await core.async_create_job(_intent())
    await core.async_run_queue()
    await core.async_hold_job(job, HoldPurpose.CONFIRM)

    now[0] += LEASE
    assert await core.async_dispatch_available() == ()
    assert core.state.job_holds == {}
    assert core.state.jobs[job].start_after == now[0] + DELAY

    now[0] += DELAY
    assert len(await core.async_dispatch_available()) == 1


async def test_start_now_ignores_a_lapsed_but_unreaped_hold() -> None:
    core, _adapter, now = await setup_delay()
    job = await core.async_create_job(_intent())
    await core.async_hold_job(job, HoldPurpose.EDIT)
    now[0] += LEASE

    await core.async_start_job(job)
    assert core.state.jobs[job].state is not JobState.QUEUED
    assert core.state.job_holds == {}


async def test_cancelling_the_delete_dialog_restarts_the_delay() -> None:
    core, _adapter, now = await setup_delay()
    job = await core.async_create_job(_intent())
    hold = await core.async_hold_job(job, HoldPurpose.CONFIRM)
    with pytest.raises(ConflictError, match="job_held"):
        await core.async_delete_job(job)

    now[0] += timedelta(seconds=20)
    await core.async_release_job_hold(hold.hold_id)
    assert core.state.jobs[job].start_after == now[0] + DELAY
    second = await core.async_hold_job(job, HoldPurpose.CONFIRM)
    await core.async_delete_job(job, hold_id=second.hold_id)
    assert job not in core.state.jobs


async def test_held_work_keeps_the_run_open_and_holds_survive_a_restart() -> None:
    core, adapter, now = await setup_delay()
    await core.runs.async_configure(grace_seconds=0)
    job = await core.async_create_job(_intent())
    await core.async_run_queue()
    hold = await core.async_hold_job(job, HoldPurpose.EDIT)
    now[0] += DELAY
    await core.async_reconcile_queue_run()
    assert core.state.queue_run.active and core.state.queue_run.idle_since is None

    restarted = await _orchestrator(adapter._backend, adapter)
    assert restarted.state.job_holds == {job: hold}
    assert restarted.state.jobs[job].start_after == NOW + DELAY
