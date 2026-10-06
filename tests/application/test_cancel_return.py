"""Cancelling with a return home and sending idle robots back to the dock."""

from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.vacuum_orchestrator.application.robot_session import (
    RobotSession,
)
from custom_components.vacuum_orchestrator.domain.errors import (
    ConflictError,
    StaleCommandError,
)
from custom_components.vacuum_orchestrator.domain.types import (
    JobState,
    RobotAvailabilityState,
)
from tests.application.test_orchestrator import (
    NOW,
    RecordingAdapter,
    RecordingBackend,
    _intent,
    _orchestrator,
)


async def _observe(core, adapter, seconds, **changes):
    adapter.observation = replace(
        adapter.observation,
        observed_at=NOW + timedelta(seconds=seconds),
        **changes,
    )
    await core.async_process_robot_observation(adapter.profile.robot_id)


async def test_return_keeps_the_lease_until_the_robot_rests_at_the_dock() -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)
    job = await core.async_create_job(_intent())
    waiting = await core.async_create_job(_intent("hall"))
    await core.async_start_job(job)
    await core.async_run_queue()
    assert len(adapter.dispatches) == 1

    await core.async_cancel_job(job, return_to_dock=True)

    assert adapter.cancel_returns == [True]
    attempt = core.state.attempts[core.state.jobs[job].active_attempt_id]
    assert attempt.return_to_dock and attempt.policy.return_seconds == 900
    available = RobotAvailabilityState.AVAILABLE
    await _observe(core, adapter, 2, state=available, at_dock=False)
    await _observe(core, adapter, 40, state=available, at_dock=False)
    busy = RobotAvailabilityState.BUSY
    await _observe(core, adapter, 200, state=busy, at_dock=True)
    assert core.state.jobs[job].state is JobState.CANCELING
    assert len(adapter.dispatches) == 1
    await _observe(core, adapter, 400, state=available, at_dock=True)
    await _observe(core, adapter, 430, state=available, at_dock=True)

    assert core.state.jobs[job].state is JobState.CANCELLED
    assert core.state.jobs[waiting].active_attempt_id is not None
    assert len(adapter.dispatches) == 2


async def test_staying_frees_the_robot_where_it_rests() -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)
    job = await core.async_create_job(_intent())
    waiting = await core.async_create_job(_intent("hall"))
    await core.async_start_job(job)
    await core.async_run_queue()

    await core.async_cancel_job(job)

    assert adapter.cancel_returns == [False] and adapter.return_count == 0
    available = RobotAvailabilityState.AVAILABLE
    await _observe(core, adapter, 2, state=available, at_dock=False)
    await _observe(core, adapter, 32, state=available, at_dock=False)
    assert core.state.jobs[job].state is JobState.CANCELLED
    assert core.state.jobs[waiting].active_attempt_id is not None


async def test_return_needs_a_robot_that_can_drive_home() -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    adapter._profile = replace(
        adapter.profile,
        capabilities=replace(adapter.profile.capabilities, returns_to_dock=False),
    )
    core = await _orchestrator(backend, adapter)
    with pytest.raises(ConflictError, match="return_to_dock_unsupported"):
        await core.async_return_robot("robot")
    job = await core.async_create_job(_intent())
    await core.async_start_job(job)
    before = core.state.commit_id

    with pytest.raises(ConflictError, match="return_to_dock_unsupported"):
        await core.async_cancel_job(job, return_to_dock=True)

    assert core.state.commit_id == before and adapter.cancel_count == 0
    queued = await core.async_create_job(_intent("hall"))
    await core.async_cancel_job(queued, return_to_dock=True)
    assert core.state.jobs[queued].state is JobState.CANCELLED


async def test_return_robot_only_without_a_lease() -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)

    await core.async_return_robot("robot")
    assert adapter.return_count == 1
    with pytest.raises(ConflictError, match="unknown_robot"):
        await core.async_return_robot("missing")

    job = await core.async_create_job(_intent())
    await core.async_start_job(job)
    with pytest.raises(ConflictError, match="robot_already_executing"):
        await core.async_return_robot("robot")
    assert adapter.return_count == 1


async def test_return_robot_is_refused_for_robots_needing_attention() -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)
    core._state = replace(
        core.state, blocked_robots={adapter.profile.source_robot_id: "uncertain"}
    )
    with pytest.raises(ConflictError, match="robot_needs_attention"):
        await core.async_return_robot("robot")


async def test_idle_command_yields_to_a_reservation_made_meanwhile() -> None:
    session = RobotSession("source", 3)
    ticket = session.idle_ticket()
    calls = []

    async def command() -> None:
        calls.append("sent")

    session.reserve("attempt")
    with pytest.raises(StaleCommandError, match="robot_reserved"):
        await session.run_idle(ticket, command)
    with pytest.raises(ConflictError, match="robot_already_executing"):
        session.idle_ticket()
    session.release("attempt")
    session.fence(4, needs_attention=True)
    with pytest.raises(ConflictError, match="robot_needs_attention"):
        session.idle_ticket()
    fresh = RobotSession("source", 4)
    await fresh.run_idle(fresh.idle_ticket(), command)
    assert calls == ["sent"]
