"""Deterministic execution replay and a controlled twenty-robot simulation."""

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from custom_components.vacuum_orchestrator.domain.intents import JobIntent, TargetRef
from custom_components.vacuum_orchestrator.domain.releases import ReleaseKind
from custom_components.vacuum_orchestrator.domain.types import (
    CleaningMode,
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

CASES = json.loads(
    (Path(__file__).parents[1] / "fixtures/replays/execution.json").read_text(
        encoding="utf-8-sig"
    )
)


@pytest.mark.parametrize("scenario", CASES, ids=[item["name"] for item in CASES])
async def test_replay_with_virtual_time_and_durable_restart(scenario, monkeypatch):
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)
    job = await core.async_create_job(_intent())
    await core.async_start_job(job)
    if scenario.get("restart"):
        core = await _orchestrator(backend, adapter)

    async def observe():
        return adapter.observation

    monkeypatch.setattr(adapter, "async_observe", observe)
    for event in scenario["events"]:
        adapter.observation = replace(
            adapter.observation,
            observed_at=NOW + timedelta(seconds=event["seconds"]),
            state=RobotAvailabilityState(event["state"]),
            cleaning_active=event["cleaning_active"],
            normal_end=event["normal_end"],
            completion_confirmed=False,
        )
        await core.async_process_robot_observation("robot")
    assert core.state.jobs[job].state.value == scenario["job_state"]
    assert len(core.rooms.registry.receipts) == scenario["receipts"]


async def test_twenty_robots_execute_independent_rooms_and_one_fault_is_isolated():
    backend = RecordingBackend()
    adapters = [
        RecordingAdapter(backend, f"robot-{index}", targets=(f"room-{index}",))
        for index in range(20)
    ]
    core = await _orchestrator(backend, *adapters, seed_rooms=False)
    jobs = []
    for index in range(20):
        room_id = await core.rooms.async_create(
            f"Room {index}", area_id=f"room-{index}"
        )
        adapter = adapters[index]
        adapter._profile = replace(
            adapter.profile,
            capabilities=replace(
                adapter.profile.capabilities, target_map={room_id: f"segment-{index}"}
            ),
        )
        await core.rooms.async_grant(room_id, ReleaseKind.PERMANENT)
        jobs.append(
            await core.async_create_job(
                JobIntent((TargetRef(room_id),), CleaningMode.VACUUM)
            )
        )
    adapters[0].fail_dispatch = True
    await core.async_run_queue()
    assert core.state.jobs[jobs[0]].state is JobState.NEEDS_ATTENTION
    assert len(core.state.robot_leases) == 20
    assert sum(len(adapter.dispatches) for adapter in adapters) == 19
    for index, adapter in enumerate(adapters[1:], start=1):
        attempt = core.state.jobs[jobs[index]].active_attempt_id
        await core.async_confirm_start(attempt)
        adapter.observation = replace(
            adapter.observation, observed_at=NOW + timedelta(minutes=10)
        )
        await core.async_process_robot_observation(adapter.profile.robot_id)
    assert (
        sum(job.state is JobState.COMPLETED for job in core.state.jobs.values()) == 19
    )
    assert len(core.state.robot_leases) == 1
    assert len(core.rooms.registry.receipts) == 19
