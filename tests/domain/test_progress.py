"""Progress names the phase and only trusts a percentage reported after start."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from custom_components.vacuum_orchestrator.domain.dispatching import RobotObservation
from custom_components.vacuum_orchestrator.domain.execution import ExecutionAttempt
from custom_components.vacuum_orchestrator.domain.intents import (
    CleaningPreferences,
    JobIntent,
    TargetRef,
)
from custom_components.vacuum_orchestrator.domain.planning import (
    ExecutionPlan,
    WorkUnit,
)
from custom_components.vacuum_orchestrator.domain.progress import (
    Progress,
    job_progress,
)
from custom_components.vacuum_orchestrator.domain.queue import Job
from custom_components.vacuum_orchestrator.domain.types import (
    AttemptState,
    CleaningMode,
    JobState,
    OperationKind,
    PassScope,
    RobotAvailabilityState,
    SettingsPolicy,
)

NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)
MINUTE = timedelta(minutes=1)


def unit(unit_id: str, operation: OperationKind, *depends: str) -> WorkUnit:
    return WorkUnit(
        unit_id,
        operation,
        ("room",),
        None,
        1,
        PassScope.TARGET_SET,
        CleaningPreferences(),
        SettingsPolicy.BEST_EFFORT,
        None,
        depends,
    )


PLAN = ExecutionPlan(
    "plan",
    "job",
    (unit("vacuum", OperationKind.VACUUM), unit("mop", OperationKind.MOP, "vacuum")),
)
JOB = Job(
    "job",
    1,
    JobIntent((TargetRef("room"),), CleaningMode.VACUUM_THEN_MOP),
    JobState.RUNNING,
    NOW,
    NOW,
    plan_id="plan",
    active_attempt_id="attempt",
)
ATTEMPT = ExecutionAttempt(
    "attempt",
    "job",
    "vacuum",
    "robot",
    "source",
    1,
    AttemptState.START_CONFIRMED,
    NOW,
    command_boundary_at=NOW,
    observed_start_at=NOW,
)
OBSERVATION = RobotObservation(
    "robot",
    "source",
    RobotAvailabilityState.BUSY,
    observed_at=NOW + MINUTE,
    clean_percent=40,
    clean_percent_at=NOW + MINUTE,
)


def test_a_running_phase_reports_its_share_of_the_whole_job() -> None:
    assert job_progress(JOB, PLAN, ATTEMPT, OBSERVATION) == Progress(
        OperationKind.VACUUM, 1, 2, NOW, "robot", 40, 20
    )
    second = replace(JOB, completed_work_unit_ids=("vacuum",))
    mopping = replace(ATTEMPT, work_unit_id="mop")
    observed = replace(OBSERVATION, clean_percent=50)
    assert job_progress(second, PLAN, mopping, observed) == Progress(
        OperationKind.MOP, 2, 2, NOW, "robot", 50, 75
    )


def test_a_percentage_without_evidence_from_this_attempt_is_withheld() -> None:
    before = replace(OBSERVATION, clean_percent_at=NOW)
    unobserved = replace(ATTEMPT, observed_start_at=None)
    other = replace(OBSERVATION, robot_id="other")
    for attempt, observation in (
        (ATTEMPT, before),
        (unobserved, OBSERVATION),
        (ATTEMPT, other),
        (ATTEMPT, None),
        (ATTEMPT, replace(OBSERVATION, clean_percent=None)),
    ):
        progress = job_progress(JOB, PLAN, attempt, observation)
        assert progress is not None
        assert (progress.phase_percent, progress.percent) == (None, None)
    assert job_progress(JOB, PLAN, unobserved, OBSERVATION).started_at is None


def test_a_job_between_phases_names_the_next_phase_without_a_robot() -> None:
    between = replace(
        JOB,
        state=JobState.DISPATCHING,
        active_attempt_id=None,
        completed_work_unit_ids=("vacuum",),
    )
    assert job_progress(between, PLAN, None, OBSERVATION) == Progress(
        OperationKind.MOP, 2, 2, None, None, None, None
    )


def test_waiting_and_finished_jobs_have_no_progress() -> None:
    assert job_progress(replace(JOB, state=JobState.QUEUED), PLAN, None, None) is None
    assert (
        job_progress(replace(JOB, state=JobState.COMPLETED), PLAN, None, None) is None
    )
    assert job_progress(JOB, None, ATTEMPT, OBSERVATION) is None
    done = replace(
        JOB, active_attempt_id=None, completed_work_unit_ids=("vacuum", "mop")
    )
    assert job_progress(done, PLAN, None, None) is None
