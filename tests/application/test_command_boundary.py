"""The persisted command boundary separates preparation from the physical start."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.vacuum_orchestrator.domain.errors import (
    DispatchNotStartedError,
    PlanningError,
    StorageIntegrityError,
)
from custom_components.vacuum_orchestrator.domain.execution import ExecutionPolicy
from custom_components.vacuum_orchestrator.domain.planning import (
    DispatchAssignment,
    WorkUnit,
)
from custom_components.vacuum_orchestrator.domain.types import (
    AttemptState,
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


async def test_boundary_is_committed_after_preparation_and_before_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)
    job = await core.async_create_job(_intent())
    prepare = adapter.async_prepare
    settled = NOW + timedelta(seconds=40)

    async def slow_settings(unit: WorkUnit, assignment: DispatchAssignment) -> None:
        await prepare(unit, assignment)
        adapter.observation = replace(adapter.observation, observed_at=settled)

    monkeypatch.setattr(adapter, "async_prepare", slow_settings)
    await core.async_start_job(job)

    attempt = core.state.attempts[core.state.jobs[job].active_attempt_id or ""]
    assert adapter.prepared == [AttemptState.PREPARED]
    assert adapter.dispatches[0][2] is AttemptState.COMMAND_SENT
    assert attempt.prepared_at == NOW
    assert attempt.command_boundary_at == settled


async def test_external_run_during_preparation_is_never_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)
    job = await core.async_create_job(_intent())
    prepare = adapter.async_prepare

    async def external_run(unit: WorkUnit, assignment: DispatchAssignment) -> None:
        await prepare(unit, assignment)
        for state, seconds in (
            (RobotAvailabilityState.BUSY, 1),
            (RobotAvailabilityState.AVAILABLE, 2),
            (RobotAvailabilityState.AVAILABLE, 40),
        ):
            adapter.observation = replace(
                adapter.observation,
                state=state,
                observed_at=NOW + timedelta(seconds=seconds),
            )
            await core.async_process_robot_observation("robot")
        attempt_id = core.state.jobs[job].active_attempt_id or ""
        assert core.state.attempts[attempt_id].state is AttemptState.PREPARED
        raise DispatchNotStartedError("setting_confirmation_timeout")

    monkeypatch.setattr(adapter, "async_prepare", external_run)
    with pytest.raises(DispatchNotStartedError):
        await core.async_start_job(job)

    assert core.state.jobs[job].state is JobState.FAILED
    assert not core.state.robot_leases
    assert not core.state.blocked_robots
    assert not core.rooms.registry.receipts
    assert adapter.dispatches == []


async def test_boundary_commit_failure_never_sends_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)
    job = await core.async_create_job(_intent())
    prepare = adapter.async_prepare

    async def lose_boundary(unit: WorkUnit, assignment: DispatchAssignment) -> None:
        await prepare(unit, assignment)
        backend.swallow_next_save = True

    monkeypatch.setattr(adapter, "async_prepare", lose_boundary)
    with pytest.raises(StorageIntegrityError):
        await core.async_start_job(job)
    assert adapter.dispatches == []


async def test_cancel_during_preparation_never_sends_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)
    job = await core.async_create_job(_intent())
    entered, release = asyncio.Event(), asyncio.Event()

    async def waiting_settings(*_args: object) -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr(adapter, "async_prepare", waiting_settings)
    start = asyncio.create_task(core.async_start_job(job))
    await entered.wait()
    cancel = asyncio.create_task(core.async_cancel_job(job))
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(PlanningError, match="job_not_startable"):
        await start
    await cancel
    assert adapter.dispatches == []


async def test_readiness_loss_at_boundary_fails_without_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)
    job = await core.async_create_job(_intent())

    async def revoke_after_settings(*_args: object) -> None:
        await core.rooms.async_revoke("kitchen")

    monkeypatch.setattr(adapter, "async_prepare", revoke_after_settings)
    with pytest.raises(DispatchNotStartedError, match="readiness_changed"):
        await core.async_start_job(job)

    attempt = next(iter(core.state.attempts.values()))
    assert attempt.command_boundary_at is None
    assert core.state.jobs[job].state is JobState.FAILED
    assert adapter.dispatches == []


async def test_restart_during_preparation_fails_job_without_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)
    job = await core.async_create_job(_intent())
    entered = asyncio.Event()

    async def hang(*_args: object) -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(adapter, "async_prepare", hang)
    start = asyncio.create_task(core.async_start_job(job))
    await entered.wait()
    restarted = await _orchestrator(backend, RecordingAdapter(backend, "robot"))
    start.cancel()

    assert restarted.state.jobs[job].state is JobState.FAILED
    assert restarted.state.jobs[job].failure_code == "interrupted_before_start"
    assert not restarted.state.blocked_robots
    assert not restarted.state.robot_leases


async def test_hanging_preparation_times_out_without_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    adapter._profile = replace(
        adapter.profile, execution_policy=ExecutionPolicy(start_seconds=0.01)
    )
    core = await _orchestrator(backend, adapter)
    job = await core.async_create_job(_intent())

    async def hang(*_args: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(adapter, "async_prepare", hang)
    with pytest.raises(DispatchNotStartedError, match="preparation_timeout"):
        await core.async_start_job(job)
    assert core.state.jobs[job].state is JobState.FAILED
    assert core.state.jobs[job].failure_code == "preparation_timeout"
    assert not core.state.blocked_robots
    assert adapter.dispatches == []
