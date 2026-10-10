"""Finished jobs can be corrected; rooms follow. See dev doc "Korrektur"."""

from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.vacuum_orchestrator.domain.completion import CompletionQuality
from custom_components.vacuum_orchestrator.domain.errors import (
    ConflictError,
    ValidationError,
)
from custom_components.vacuum_orchestrator.domain.types import (
    JobState,
    OperationKind,
    WorkUnitState,
)
from tests.application.test_orchestrator import (
    NOW,
    RecordingAdapter,
    RecordingBackend,
    _intent,
    _orchestrator,
)
from tests.application.test_room_execution import finished_run


async def _finished(core, minutes: float) -> str:
    """Complete a kitchen job whose run ends `minutes` after NOW."""
    core._clock = lambda: NOW + timedelta(minutes=minutes - 5)
    job = await core.async_create_job(_intent())
    await core.async_start_job(job)
    attempt_id = core.state.jobs[job].active_attempt_id
    await core.async_confirm_start(attempt_id)
    end = NOW + timedelta(minutes=minutes)
    run = replace(
        finished_run(core.state.attempts[attempt_id], name=f"run-{job}"),
        observed_start=NOW + timedelta(minutes=minutes - 5),
        observed_end=end,
        history_start=None,
        history_end=None,
        completion_quality=CompletionQuality.DERIVED,
    )
    core._clock = lambda: end
    await core.async_record_robot_run(attempt_id, run)
    assert core.state.jobs[job].state is JobState.COMPLETED
    return job


def _kitchen(core):
    return core.rooms.registry.resolve("kitchen").last_cleaning[OperationKind.VACUUM]


async def test_a_completed_job_corrected_to_failed_gives_back_its_rooms() -> None:
    backend = RecordingBackend()
    core = await _orchestrator(backend, RecordingAdapter(backend, "robot"))
    first = await _finished(core, 10)
    second = await _finished(core, 20)
    (first_receipt,) = (
        key
        for key, receipt in core.rooms.registry.receipts.items()
        if receipt.completed_at == NOW + timedelta(minutes=10)
    )
    assert _kitchen(core).completed_at == NOW + timedelta(minutes=20)

    core._clock = lambda: NOW + timedelta(hours=1)
    await core.async_correct_job(second, JobState.FAILED)

    job = core.state.jobs[second]
    assert (job.state, job.failure_code, job.corrected_at) == (
        JobState.FAILED,
        "reported_failed",
        NOW + timedelta(hours=1),
    )
    assert set(core.rooms.registry.receipts) == {first_receipt}
    stamp = _kitchen(core)
    assert (stamp.receipt_id, stamp.occupancy_baseline_known) == (
        first_receipt,
        False,
    )
    assert core.state.completion(second) is None
    assert core.state.jobs[first].state is JobState.COMPLETED

    before = core.state
    await core.async_correct_job(second, JobState.FAILED)
    assert core.state is before


async def test_a_failed_job_corrected_to_completed_counts_as_reported() -> None:
    backend = RecordingBackend()
    core = await _orchestrator(backend, RecordingAdapter(backend, "robot"))
    job = await _finished(core, 20)
    await core.async_correct_job(job, JobState.FAILED)
    await core.async_correct_job(job, JobState.COMPLETED)

    corrected = core.state.jobs[job]
    assert (corrected.state, corrected.failure_code) == (JobState.COMPLETED, None)
    (receipt,) = core.rooms.registry.receipts.values()
    assert receipt.receipt_id.startswith(f"correction:{job}:")
    assert (receipt.quality, receipt.evidence, receipt.room_ids) == (
        CompletionQuality.DERIVED,
        ("reported_by_user",),
        ("kitchen",),
    )
    assert _kitchen(core).receipt_id == receipt.receipt_id
    completion = core.state.completion(job)
    assert completion is not None
    assert (completion.quality, completion.notes) == (
        CompletionQuality.DERIVED,
        ("reported_by_user",),
    )

    # Nothing is missing any more.
    before = core.state
    await core.async_correct_job(job, JobState.COMPLETED)
    assert core.state is before


async def test_a_cancelled_job_corrected_to_completed_completes_every_phase() -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)
    job = await core.async_create_job(_intent())
    await core.async_start_job(job)
    await core.async_confirm_start(core.state.jobs[job].active_attempt_id)
    await core.async_cancel_job(job)
    await core.async_confirm_cancel(job)
    assert core.state.jobs[job].state is JobState.CANCELLED

    await core.async_correct_job(job, JobState.COMPLETED)

    corrected = core.state.jobs[job]
    assert corrected.state is JobState.COMPLETED
    plan = core.state.plans[corrected.plan_id]
    assert corrected.completed_work_unit_ids == tuple(
        unit.work_unit_id for unit in plan.work_units
    )
    assert {
        core.state.work_unit_states[unit.work_unit_id] for unit in plan.work_units
    } == {WorkUnitState.COMPLETED}
    assert core.rooms.registry.resolve("kitchen").last_cleaning


async def test_only_finished_planned_jobs_are_corrected() -> None:
    backend = RecordingBackend()
    core = await _orchestrator(backend, RecordingAdapter(backend, "robot"))
    queued = await core.async_create_job(_intent())
    with pytest.raises(ConflictError, match="job_not_correctable"):
        await core.async_correct_job(queued, JobState.COMPLETED)
    job = await _finished(core, 20)
    with pytest.raises(ValidationError, match="invalid_correction_outcome"):
        await core.async_correct_job(job, JobState.CANCELLED)
