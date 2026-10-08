"""How far a started job is; see dev doc "Fortschritt"."""

from dataclasses import dataclass
from datetime import datetime

from .dispatching import RobotObservation
from .execution import ExecutionAttempt
from .planning import ExecutionPlan
from .queue import Job
from .types import JobState, OperationKind

STARTED_STATES = frozenset({JobState.DISPATCHING, JobState.RUNNING, JobState.CANCELING})


@dataclass(frozen=True, slots=True)
class Progress:
    """The current phase of a started job and, with evidence, its percentages."""

    operation: OperationKind
    phase: int
    phases: int
    started_at: datetime | None
    robot_id: str | None
    phase_percent: int | None
    percent: int | None


def job_progress(
    job: Job,
    plan: ExecutionPlan | None,
    attempt: ExecutionAttempt | None,
    observation: RobotObservation | None,
) -> Progress | None:
    """Describe a started job; a percentage needs a report after the start."""
    if job.state not in STARTED_STATES or plan is None:
        return None
    units = plan.work_units
    completed = set(job.completed_work_unit_ids)
    unit_id = (
        attempt.work_unit_id
        if attempt is not None
        else next(
            (unit.work_unit_id for unit in units if unit.work_unit_id not in completed),
            None,
        )
    )
    index = next(
        (i for i, unit in enumerate(units) if unit.work_unit_id == unit_id), None
    )
    if index is None:
        return None
    phase_percent = (
        observation.clean_percent
        if attempt is not None
        and attempt.observed_start_at is not None
        and observation is not None
        and observation.robot_id == attempt.robot_id
        and observation.clean_percent_at is not None
        and observation.clean_percent_at > attempt.observed_start_at
        else None
    )
    return Progress(
        units[index].operation,
        index + 1,
        len(units),
        attempt.observed_start_at if attempt is not None else None,
        attempt.robot_id if attempt is not None else None,
        phase_percent,
        None
        if phase_percent is None
        else round((index + phase_percent / 100) / len(units) * 100),
    )
