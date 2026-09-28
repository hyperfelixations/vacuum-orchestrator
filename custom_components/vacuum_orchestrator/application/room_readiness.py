"""Compose canonical room admission and scoped readiness without device I/O."""

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime

from ..domain.queue import Job
from ..domain.readiness import ReadinessReport
from ..domain.requirements import (
    RequirementState,
    StateObservation,
    StateRequirement,
    evaluate_requirements,
)
from ..domain.room_registry import RoomRegistry
from ..domain.types import OperationKind, ReadinessState

RequirementReader = Callable[
    [tuple[StateRequirement, ...]], Mapping[str, StateObservation]
]


def evaluate_room_readiness(
    job: Job,
    registry: RoomRegistry,
    base: ReadinessReport,
    reader: RequirementReader,
    now: datetime,
    *,
    robot_id: str | None = None,
    operation: OperationKind | None = None,
    robot_requirements: tuple[StateRequirement, ...] = (),
) -> ReadinessReport:
    """Return all applicable explanations, including revoked or expired grants."""
    rooms = tuple(registry.resolve(target.area_id) for target in job.intent.areas)
    reasons = list(base.reason_codes)
    blocked_rooms = tuple(
        room.room_id
        for room in rooms
        if not registry.allows_job(job.job_id, room.room_id, now)
    )
    if blocked_rooms:
        reasons.append("room_not_released")
    requirements = (
        *tuple(item for room in rooms for item in room.requirements),
        *robot_requirements,
    )
    observations = reader(requirements)
    results = tuple(
        replace(item, room_id=room_id, robot_id=robot_id, operation=operation)
        for room_id, conditions in (
            *((room.room_id, room.requirements) for room in rooms),
            (None, robot_requirements),
        )
        for item in evaluate_requirements(
            conditions, observations, now, robot_id=robot_id, operation=operation
        )
    )
    reasons.extend(
        item.reason for item in results if item.state is not RequirementState.READY
    )
    unknown = tuple(
        dict.fromkeys(
            (
                *base.unknown,
                *(
                    item.entity_id
                    for item in results
                    if item.state in {RequirementState.UNKNOWN, RequirementState.STALE}
                ),
            )
        )
    )
    state = (
        ReadinessState.UNKNOWN
        if unknown
        else ReadinessState.BLOCKED
        if reasons or base.state is ReadinessState.BLOCKED
        else base.state
    )
    return ReadinessReport(
        state,
        base.failed_on,
        base.failed_off,
        unknown,
        tuple(dict.fromkeys(reasons)),
        results,
        blocked_rooms,
    )
