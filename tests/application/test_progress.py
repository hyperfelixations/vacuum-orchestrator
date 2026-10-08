"""A started job reports its phase and the robot's progress after the start."""

from dataclasses import replace
from datetime import timedelta

from custom_components.vacuum_orchestrator.domain.types import (
    CleaningMode,
    OperationKind,
    RobotAvailabilityState,
)
from tests.application.test_orchestrator import NOW, _intent
from tests.application.test_waiting import setup_waiting


async def test_a_started_job_reports_its_phase_and_the_reported_progress() -> None:
    core, _now = await setup_waiting()
    job = await core.async_create_job(_intent(mode=CleaningMode.VACUUM_THEN_MOP))
    assert core.progress(job) is None

    await core.async_start_job(job)
    progress = core.progress(job)
    assert (
        progress.operation,
        progress.phase,
        progress.phases,
        progress.robot_id,
        progress.started_at,
        progress.percent,
    ) == (OperationKind.VACUUM, 1, 2, "robot", None, None)

    await core.async_confirm_start(core.state.jobs[job].active_attempt_id)
    (adapter,) = core.adapters.values()
    adapter.observation = replace(
        adapter.observation,
        state=RobotAvailabilityState.BUSY,
        cleaning_active=True,
        clean_percent=60,
        clean_percent_at=NOW + timedelta(minutes=1),
    )
    await core.async_process_robot_observation("robot")
    progress = core.progress(job)
    assert (progress.started_at, progress.phase_percent, progress.percent) == (
        NOW,
        60,
        30,
    )
