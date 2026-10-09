"""A queue decision saved while robots are observed wins over the dispatch."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace

import pytest

from custom_components.vacuum_orchestrator.domain.dispatching import RobotObservation
from custom_components.vacuum_orchestrator.domain.types import (
    CleaningMode,
    JobState,
    QueueMode,
    RobotAvailabilityState,
)
from tests.application.test_orchestrator import RecordingAdapter, _intent
from tests.application.test_room_execution import finished_run
from tests.application.test_start_delay import setup_delay


def gate_observation(
    adapter: RecordingAdapter, monkeypatch: pytest.MonkeyPatch
) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold the next observation until the test released it."""
    entered, release = asyncio.Event(), asyncio.Event()
    observe: Callable[[], Awaitable[RobotObservation]] = adapter.async_observe

    async def gated() -> RobotObservation:
        entered.set()
        await release.wait()
        return await observe()

    monkeypatch.setattr(adapter, "async_observe", gated)
    return entered, release


async def test_a_pause_saved_during_observation_stops_an_automatic_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, adapter, _now = await setup_delay(0)
    job = await core.async_create_job(_intent())
    await core.async_set_queue_mode(QueueMode.RUNNING)
    entered, release = gate_observation(adapter, monkeypatch)

    dispatch = asyncio.create_task(core.async_dispatch_available())
    await entered.wait()
    await core.async_set_queue_mode(QueueMode.PAUSED)
    release.set()

    assert await dispatch == ()
    assert core.state.jobs[job].state is JobState.QUEUED
    assert core.state.attempts == {} and not adapter.dispatches


async def test_a_pause_lets_the_next_phase_of_a_started_job_continue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, adapter, _now = await setup_delay(0)
    job = await core.async_create_job(_intent(mode=CleaningMode.VACUUM_THEN_MOP))
    await core.async_start_job(job)
    attempt = core.state.jobs[job].active_attempt_id
    await core.async_confirm_start(attempt)
    available = adapter.observation
    adapter.observation = replace(available, state=RobotAvailabilityState.BUSY)
    await core.async_record_robot_run(
        attempt, finished_run(core.state.attempts[attempt], name=attempt)
    )
    assert core.state.jobs[job].active_attempt_id is None
    adapter.observation = available
    await core.async_set_queue_mode(QueueMode.RUNNING)
    entered, release = gate_observation(adapter, monkeypatch)

    dispatch = asyncio.create_task(core.async_dispatch_available())
    await entered.wait()
    await core.async_set_queue_mode(QueueMode.PAUSED)
    release.set()

    assert len(await dispatch) == 1
    assert core.state.jobs[job].active_attempt_id is not None
