"""How far a started job is; see dev doc "Fortschritt"."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from .dispatching import RobotObservation
from .execution import ExecutionAttempt
from .faults import blocking
from .planning import DispatchAssignment, ExecutionPlan
from .queue import Job
from .types import JobState, OperationKind

STARTED_STATES = frozenset({JobState.DISPATCHING, JobState.RUNNING, JobState.CANCELING})


@dataclass(frozen=True, slots=True)
class FaultWait:
    """A running job waiting for a device fault to clear; see dev doc "Gerätefehler"."""

    since: datetime
    fails_at: datetime
    codes: tuple[str, ...]


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
    fault: FaultWait | None = None
    # The job's room the robot reports being in.
    room_id: str | None = None


def room_at(target: str | None, rooms: Mapping[str, tuple[str, ...]]) -> str | None:
    """Name the only room whose device targets contain `target`."""
    if target is None:
        return None
    matches = [room for room, targets in rooms.items() if target in targets]
    return matches[0] if len(matches) == 1 else None


def job_progress(
    job: Job,
    plan: ExecutionPlan | None,
    attempt: ExecutionAttempt | None,
    observation: RobotObservation | None,
    assignment: DispatchAssignment | None = None,
) -> Progress | None:
    """Describe a started job.

    A percentage and a room need a report after the start from the robot
    running it.
    """
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
    room_id = (
        room_at(observation.current_target, assignment.room_targets)
        if attempt is not None
        and attempt.observed_start_at is not None
        and observation is not None
        and observation.robot_id == attempt.robot_id
        and assignment is not None
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
        _fault_wait(units[index].operation, attempt, observation),
        room_id,
    )


def _fault_wait(
    operation: OperationKind,
    attempt: ExecutionAttempt | None,
    observation: RobotObservation | None,
) -> FaultWait | None:
    if attempt is None or attempt.fault_since is None:
        return None
    fails_at = attempt.fault_deadline
    assert fails_at is not None
    return FaultWait(
        attempt.fault_since,
        fails_at,
        ()
        if observation is None or observation.robot_id != attempt.robot_id
        else tuple(fault.code for fault in blocking(observation.faults, operation)),
    )
