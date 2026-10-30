"""A person may start a job although the battery is below the minimum (JOB-27)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from custom_components.vacuum_orchestrator.application.orchestrator import (
    VacuumOrchestrator,
)
from custom_components.vacuum_orchestrator.application.preview import preview_job
from custom_components.vacuum_orchestrator.domain.errors import PlanningError
from custom_components.vacuum_orchestrator.domain.faults import (
    Fault,
    FaultScope,
    FaultSource,
)
from custom_components.vacuum_orchestrator.domain.permissions import job_actions
from custom_components.vacuum_orchestrator.domain.types import (
    CleaningMode,
    JobState,
    OperationKind,
    RobotAvailabilityState,
)
from tests.application.test_orchestrator import (
    RecordingAdapter,
    RecordingBackend,
    _intent,
    _orchestrator,
)

MINIMUM = 50


async def _low(
    battery: int | None = 30, *adapters: RecordingAdapter
) -> tuple[VacuumOrchestrator, RecordingAdapter]:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    adapter._profile = replace(adapter.profile, minimum_battery=MINIMUM)
    adapter.observation = replace(adapter.observation, battery_percentage=battery)
    return await _orchestrator(backend, adapter, *adapters), adapter


def _start(core: VacuumOrchestrator, job_id: str):
    job = core.state.jobs[job_id]
    return job_actions(core.state, job, core.now(), core.waiting(job_id))["start"]


async def test_the_start_is_offered_with_a_warning_and_needs_the_allowance() -> None:
    core, adapter = await _low()
    job_id = await core.async_create_job(_intent())
    await core.async_observed_snapshot()

    start = _start(core, job_id)
    assert (start.available, start.warning, start.robot_ids) == (
        True,
        "battery_low",
        ("robot",),
    )
    with pytest.raises(PlanningError, match="battery_below_minimum"):
        await core.async_start_job(job_id)
    assert adapter.dispatches == []

    assignment = await core.async_start_job(job_id, allow_low_battery=True)

    assert assignment.robot_id == "robot"
    assert core.state.jobs[job_id].allow_low_battery is True
    selected = [
        record for record in core.trace.snapshot() if record.get("stage") == "selected"
    ]
    assert [record.get("reason") for record in selected] == ["battery_minimum_allowed"]


async def test_the_queue_never_starts_a_waiting_job_below_the_minimum() -> None:
    core, adapter = await _low()
    job_id = await core.async_create_job(_intent())

    assert await core.async_run_queue() == ()

    assert adapter.dispatches == []
    assert core.state.jobs[job_id].state is JobState.QUEUED


async def test_an_unknown_battery_is_never_allowed() -> None:
    core, adapter = await _low(None)
    job_id = await core.async_create_job(_intent())
    await core.async_observed_snapshot()

    assert _start(core, job_id).reason == "job_not_startable"
    with pytest.raises(PlanningError, match="battery_below_minimum"):
        await core.async_start_job(job_id, allow_low_battery=True)
    assert adapter.dispatches == []


async def test_a_fault_still_keeps_the_job_from_starting() -> None:
    core, adapter = await _low()
    adapter.observation = replace(
        adapter.observation,
        faults=(Fault("low_battery", FaultSource.ROBOT, FaultScope.GENERAL),),
        state=RobotAvailabilityState.UNAVAILABLE,
    )
    job_id = await core.async_create_job(_intent())
    await core.async_observed_snapshot()

    assert _start(core, job_id).reason == "job_not_startable"
    with pytest.raises(PlanningError, match="robot_fault"):
        await core.async_start_job(job_id, allow_low_battery=True)


async def test_create_and_start_allows_a_low_battery_only_when_asked() -> None:
    core, adapter = await _low()

    with pytest.raises(PlanningError, match="battery_below_minimum"):
        await core.async_create_and_start_job(_intent())
    assert core.state.jobs == {}

    job_id, _ = await core.async_create_and_start_job(_intent(), allow_low_battery=True)
    assert core.state.jobs[job_id].allow_low_battery is True
    assert len(adapter.dispatches) == 1


async def test_a_started_job_finishes_its_next_phase_below_the_minimum() -> None:
    core, adapter = await _low()
    job_id = await core.async_create_job(_intent(mode=CleaningMode.VACUUM_THEN_MOP))
    await core.async_start_job(job_id, allow_low_battery=True)
    adapter.observation = replace(
        adapter.observation,
        state=RobotAvailabilityState.BUSY,
        observed_at=datetime(2026, 9, 7, 12, 1, tzinfo=UTC),
    )
    await core.async_process_robot_observation("robot")
    adapter.observation = replace(
        adapter.observation,
        state=RobotAvailabilityState.AVAILABLE,
        battery_percentage=20,
        observed_at=datetime(2026, 9, 7, 12, 10, tzinfo=UTC),
        history_start=datetime(2026, 9, 7, 12, 1, tzinfo=UTC),
        history_end=datetime(2026, 9, 7, 12, 9, tzinfo=UTC),
    )
    await core.async_process_robot_observation("robot")

    assert [unit.operation for unit, _, _ in adapter.dispatches] == [
        OperationKind.VACUUM,
        OperationKind.MOP,
    ]


async def test_the_preview_offers_a_start_with_a_battery_warning() -> None:
    core, _adapter = await _low()
    intent = _intent()

    preview = await preview_job(core, intent.mode, intent.preferences, intent)

    assert (preview.reason, preview.start_warning) == (
        "battery_below_minimum",
        "battery_low",
    )
    unknown, _ = await _low(None)
    preview = await preview_job(unknown, intent.mode, intent.preferences, intent)
    assert (preview.reason, preview.start_warning) == ("battery_below_minimum", None)
