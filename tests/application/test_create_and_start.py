"""Create-and-start is atomic: a job that cannot start is never created."""

import asyncio
from dataclasses import replace

import pytest

from custom_components.vacuum_orchestrator.domain.errors import (
    ConflictError,
    PlanningError,
)
from custom_components.vacuum_orchestrator.domain.types import (
    JobState,
    RobotAvailabilityState,
)
from custom_components.vacuum_orchestrator.infrastructure.codec import (
    decode_orchestrator_state,
)
from custom_components.vacuum_orchestrator.infrastructure.integrity import (
    verify_snapshot,
)
from tests.application.test_orchestrator import (
    RecordingAdapter,
    RecordingBackend,
    _intent,
    _orchestrator,
)


async def test_created_job_is_reserved_in_the_same_commit() -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)
    before = core.state.commit_id
    seen = []
    original = core._commit_locked

    async def record(previous, candidate):
        seen.append(candidate)
        await original(previous, candidate)

    core._commit_locked = record

    job_id, assignment = await core.async_create_and_start_job(_intent(), "robot")

    first = seen[0]
    assert first.commit_id == before + 1
    assert first.jobs[job_id].active_attempt_id is not None
    assert job_id not in first.queue
    assert assignment.robot_id == "robot" and len(adapter.dispatches) == 1
    assert core.state.jobs[job_id].state in {JobState.DISPATCHING, JobState.RUNNING}
    stored = decode_orchestrator_state(verify_snapshot(backend.data))
    assert stored.jobs[job_id].intent == core.state.jobs[job_id].intent


@pytest.mark.parametrize(
    ("prepare", "code"),
    [
        ("revoke", "job_blocked"),
        ("busy", "robot_busy"),
        ("unknown_robot", "unknown_robot"),
    ],
)
async def test_job_that_cannot_start_is_not_created(prepare: str, code: str) -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)
    if prepare == "revoke":
        await core.rooms.async_revoke("kitchen")
    if prepare == "busy":
        adapter.observation = replace(
            adapter.observation, state=RobotAvailabilityState.BUSY
        )
    before = core.state

    with pytest.raises(PlanningError, match=code):
        await core.async_create_and_start_job(
            _intent(), "other" if prepare == "unknown_robot" else None
        )

    assert core.state is before and not core.state.jobs
    assert adapter.dispatches == []


async def test_concurrent_queue_dispatch_wins_the_robot_without_leftovers() -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)
    queued = await core.async_create_job(_intent("hall"))

    results = await asyncio.gather(
        core.async_run_queue(),
        core.async_create_and_start_job(_intent()),
        return_exceptions=True,
    )

    assert len(adapter.dispatches) == 1
    started = results[1]
    if isinstance(started, BaseException):
        assert isinstance(started, (PlanningError, ConflictError))
        assert set(core.state.jobs) == {queued}
        assert core.state.jobs[queued].active_attempt_id is not None
    else:
        job_id, _assignment = started
        assert core.state.jobs[job_id].active_attempt_id is not None
        assert core.state.jobs[queued].state is JobState.QUEUED


async def test_duplicate_dedupe_key_creates_nothing() -> None:
    backend = RecordingBackend()
    core = await _orchestrator(backend, RecordingAdapter(backend, "robot"))
    await core.rooms.async_revoke("hall")
    await core.async_create_job(replace(_intent("hall"), dedupe_key="daily"))
    before = core.state

    with pytest.raises(ConflictError, match="dedupe_key_already_queued"):
        await core.async_create_and_start_job(replace(_intent(), dedupe_key="daily"))

    assert core.state is before
