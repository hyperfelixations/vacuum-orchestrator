"""Cancel releases ownership only after a sent stop and stable idle evidence."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

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


async def _observe(core, adapter, seconds: float, *, idle: bool = True) -> None:
    adapter.observation = replace(
        adapter.observation,
        state=RobotAvailabilityState.AVAILABLE if idle else RobotAvailabilityState.BUSY,
        observed_at=NOW + timedelta(seconds=seconds),
    )
    await core.async_process_robot_observation("robot")


async def test_cancel_before_boundary_finishes_without_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)
    job = await core.async_create_job(_intent())
    entered, release = asyncio.Event(), asyncio.Event()

    async def settings(*_args: object) -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr(adapter, "async_prepare", settings)
    start = asyncio.create_task(core.async_start_job(job))
    await entered.wait()
    await core.async_cancel_job(job)

    assert core.state.jobs[job].state is JobState.CANCELLED
    assert not core.state.robot_leases
    assert adapter.cancel_count == 0
    release.set()
    await asyncio.gather(start, return_exceptions=True)
    assert adapter.dispatches == []
    assert core.state.jobs[job].state is JobState.CANCELLED


async def test_pending_start_transport_keeps_ownership_until_stop_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)
    job = await core.async_create_job(_intent())
    entered, release = asyncio.Event(), asyncio.Event()
    start_call = adapter.async_start

    async def pending_transport(unit: WorkUnit, assignment: DispatchAssignment):
        entered.set()
        await release.wait()
        await start_call(unit, assignment)

    monkeypatch.setattr(adapter, "async_start", pending_transport)
    start = asyncio.create_task(core.async_start_job(job))
    await entered.wait()
    cancel = asyncio.create_task(core.async_cancel_job(job))
    await asyncio.sleep(0)
    attempt_id = core.state.jobs[job].active_attempt_id or ""

    await _observe(core, adapter, 1)
    assert core.state.jobs[job].state is JobState.CANCELING
    assert core.state.robot_leases
    assert adapter.cancel_count == 0

    release.set()
    await asyncio.gather(start, return_exceptions=True)
    await cancel
    assert adapter.cancel_count == 1
    stop_sent_at = core.state.attempts[attempt_id].stop_sent_at
    assert stop_sent_at is not None

    await _observe(core, adapter, 2)
    attempt = core.state.attempts[attempt_id]
    assert attempt.state is AttemptState.CANCEL_PENDING
    assert attempt.terminal_observed_at == NOW + timedelta(seconds=2)
    await _observe(core, adapter, 10, idle=False)
    assert core.state.attempts[attempt_id].terminal_observed_at is None
    await _observe(core, adapter, 11)
    await _observe(core, adapter, 41)
    assert core.state.jobs[job].state is JobState.CANCELLED
    assert not core.state.robot_leases
    assert not core.rooms.registry.receipts


async def test_stop_that_never_settles_requires_attention() -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)
    job = await core.async_create_job(_intent())
    await core.async_start_job(job)
    await core.async_cancel_job(job)

    await _observe(core, adapter, 30, idle=False)
    assert core.state.jobs[job].state is JobState.CANCELING
    await _observe(core, adapter, 120, idle=False)
    assert core.state.jobs[job].state is JobState.NEEDS_ATTENTION
    assert core.state.blocked_robots
    assert core.state.robot_leases
