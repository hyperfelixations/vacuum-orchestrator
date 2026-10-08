"""New and edited jobs start automatically only after a protection window."""

from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.vacuum_orchestrator.domain.errors import ValidationError
from custom_components.vacuum_orchestrator.domain.intents import JobIntentPatch
from custom_components.vacuum_orchestrator.domain.releases import ReleaseKind
from custom_components.vacuum_orchestrator.domain.types import (
    CleaningMode,
    JobState,
    VacuumLevel,
)
from tests.application.test_orchestrator import (
    NOW,
    RecordingAdapter,
    RecordingBackend,
    _intent,
    _orchestrator,
)

DELAY = timedelta(seconds=5)


async def setup_delay(seconds: float = 5):
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter, start_delay=seconds)
    now = [NOW]
    for owner in (core, core.runs, core.rooms, core.templates):
        owner._clock = lambda: now[0]
    return core, adapter, now


async def test_queue_waits_for_the_delay_before_starting_a_new_job() -> None:
    core, adapter, now = await setup_delay()
    job = await core.async_create_job(_intent())
    assert core.state.jobs[job].start_after == NOW + DELAY

    assert await core.async_run_queue() == ()
    now[0] += DELAY - timedelta(microseconds=1)
    assert await core.async_dispatch_available() == ()
    assert not adapter.dispatches

    now[0] += timedelta(microseconds=1)
    assert len(await core.async_dispatch_available()) == 1
    assert core.state.jobs[job].state is not JobState.QUEUED
    assert core.state.jobs[job].start_after is None


async def test_start_now_and_immediate_creation_skip_the_delay() -> None:
    core, adapter, _now = await setup_delay()
    job = await core.async_create_job(_intent())

    await core.async_start_job(job)
    assert core.state.jobs[job].state is not JobState.QUEUED
    assert core.state.jobs[job].start_after is None
    assert adapter.dispatches

    core2, _, _ = await setup_delay()
    created, _assignment = await core2.async_create_and_start_job(_intent())
    assert core2.state.jobs[created].start_after is None


async def test_edits_restart_the_delay_but_releases_and_no_op_saves_do_not() -> None:
    core, _adapter, now = await setup_delay()
    await core.rooms.async_revoke("kitchen")
    job = await core.async_create_job(_intent())
    now[0] += timedelta(minutes=1)

    await core.rooms.async_grant("kitchen", ReleaseKind.PERMANENT)
    assert core.state.jobs[job].start_after == NOW + DELAY

    await core.async_update_job(job, JobIntentPatch(vacuum_power=VacuumLevel.HIGH))
    assert core.state.jobs[job].start_after == now[0] + DELAY

    edited = now[0]
    now[0] += timedelta(seconds=1)
    await core.async_update_job(job, JobIntentPatch(vacuum_power=VacuumLevel.HIGH))
    assert core.state.jobs[job].start_after == edited + DELAY


async def test_retries_and_template_jobs_wait_as_well() -> None:
    core, _adapter, now = await setup_delay()
    job = await core.async_create_job(_intent())
    await core.async_cancel_job(job)
    now[0] += timedelta(minutes=1)
    retry = await core.async_retry_job(job)
    assert core.state.jobs[retry].start_after == now[0] + DELAY

    key = await core.templates.async_save("Routine", _intent(mode=CleaningMode.MOP))
    templated = await core.templates.async_create_job(key)
    assert core.state.jobs[templated].start_after == now[0] + DELAY


async def test_zero_delay_starts_at_once() -> None:
    core, _adapter, _now = await setup_delay(0)
    job = await core.async_create_job(_intent())
    assert core.state.jobs[job].start_after is None
    assert len(await core.async_run_queue()) == 1
    assert core.state.jobs[job].state is not JobState.QUEUED


async def test_delayed_work_keeps_a_run_open_even_without_grace() -> None:
    core, _adapter, now = await setup_delay()
    await core.runs.async_configure(grace_seconds=0)
    await core.async_run_queue()
    await core.async_reconcile_queue_run()
    assert not core.state.queue_run.active

    await core.async_run_queue()
    job = await core.async_create_job(_intent())
    await core.async_reconcile_queue_run()
    assert core.state.queue_run.active and core.state.queue_run.idle_since is None

    now[0] += DELAY
    assert len(await core.async_dispatch_available()) == 1
    assert core.state.jobs[job].state is not JobState.QUEUED
    assert core.state.queue_run.active


async def test_start_delay_is_configured_between_zero_and_ten_minutes() -> None:
    core, _adapter, _now = await setup_delay()
    assert core.state.start_delay_seconds == 5
    await core.runs.async_configure(start_delay_seconds=600)
    assert core.state.start_delay_seconds == 600
    assert core.state.queue_grace_seconds == 900
    commit = core.state.commit_id
    await core.runs.async_configure(start_delay_seconds=600)
    assert core.state.commit_id == commit
    for invalid in (-1, 601):
        with pytest.raises(ValidationError, match="start_delay_out_of_range"):
            await core.runs.async_configure(start_delay_seconds=invalid)
    with pytest.raises(ValidationError, match="start_delay_out_of_range"):
        replace(core.state, start_delay_seconds=601)
