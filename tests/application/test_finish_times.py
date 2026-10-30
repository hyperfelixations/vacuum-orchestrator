"""A finished job keeps when it ended; history lists it by that time (JOB-28)."""

from datetime import timedelta

from custom_components.vacuum_orchestrator.domain.types import JobState
from custom_components.vacuum_orchestrator.infrastructure.codec import (
    decode_orchestrator_state,
    encode_orchestrator_state,
)
from tests.application.test_correction import _finished
from tests.application.test_orchestrator import (
    NOW,
    RecordingAdapter,
    RecordingBackend,
    _intent,
    _orchestrator,
)


async def test_a_job_finishes_when_its_last_run_ends_and_a_correction_keeps_it() -> (
    None
):
    backend = RecordingBackend()
    core = await _orchestrator(backend, RecordingAdapter(backend, "robot"))
    job = await _finished(core, 10)
    assert core.state.jobs[job].finished_at == NOW + timedelta(minutes=10)

    core._clock = lambda: NOW + timedelta(hours=1)
    await core.async_correct_job(job, JobState.FAILED)

    corrected = core.state.jobs[job]
    assert corrected.updated_at == NOW + timedelta(hours=1)
    assert corrected.finished_at == NOW + timedelta(minutes=10)


async def test_a_waiting_job_cancelled_finishes_then() -> None:
    backend = RecordingBackend()
    core = await _orchestrator(backend, RecordingAdapter(backend, "robot"))
    job = await core.async_create_job(_intent())
    assert core.state.jobs[job].finished_at is None
    core._clock = lambda: NOW + timedelta(minutes=3)
    await core.async_cancel_job(job)
    assert core.state.jobs[job].finished_at == NOW + timedelta(minutes=3)


async def test_a_job_stored_without_a_finish_time_gets_its_run_end() -> None:
    backend = RecordingBackend()
    core = await _orchestrator(backend, RecordingAdapter(backend, "robot"))
    job = await _finished(core, 10)
    core._clock = lambda: NOW + timedelta(hours=1)
    await core.async_correct_job(job, JobState.FAILED)
    cancelled = await core.async_create_job(_intent("studio"))
    await core.async_cancel_job(cancelled)

    encoded = encode_orchestrator_state(core.state)
    for item in encoded["jobs"].values():
        del item["finished_at"]
    decoded = decode_orchestrator_state(encoded)

    # The run's observed end, not the later correction.
    assert decoded.jobs[job].finished_at == NOW + timedelta(minutes=10)
    # Without a run, the job's last change.
    assert decoded.jobs[cancelled].finished_at == decoded.jobs[cancelled].updated_at
