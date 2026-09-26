"""Typed state requirements shared by room and robot readiness."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .errors import ValidationError
from .types import OperationKind
from .validation import identifier, instant, seconds


class RequirementState(StrEnum):
    """Detailed readiness values, projected compatibly by API v2."""

    READY = "ready"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class StateRequirement:
    """Accepted entity states with optional age and execution scope."""

    entity_id: str
    accepted_states: tuple[str, ...] = ("on",)
    max_age_seconds: float | None = None
    robot_id: str | None = None
    operation: OperationKind | None = None
    entity_registry_id: str | None = None

    def __post_init__(self) -> None:
        identifier(self.entity_id)
        if not self.accepted_states or len(set(self.accepted_states)) != len(
            self.accepted_states
        ):
            raise ValidationError("invalid_accepted_states")
        for state in self.accepted_states:
            identifier(state)
        if set(self.accepted_states) & {"unknown", "unavailable"}:
            raise ValidationError("invalid_accepted_states")
        if self.max_age_seconds is not None:
            seconds(self.max_age_seconds, positive=True)
        for reference in (self.robot_id, self.entity_registry_id):
            if reference is not None:
                identifier(reference)


@dataclass(frozen=True, slots=True)
class StateObservation:
    """An entity value with its actual source update timestamp."""

    value: str | None
    reported_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RequirementResult:
    """One explainable requirement result without short-circuiting peers."""

    entity_id: str
    state: RequirementState
    reason: str


def evaluate_requirements(
    requirements: tuple[StateRequirement, ...],
    observations: Mapping[str, StateObservation],
    now: datetime,
    *,
    robot_id: str | None = None,
    operation: OperationKind | None = None,
) -> tuple[RequirementResult, ...]:
    """Evaluate applicable conditions, keeping every blocking explanation."""
    instant(now)
    results = []
    for requirement in requirements:
        if requirement.robot_id is not None and requirement.robot_id != robot_id:
            continue
        if requirement.operation is not None and requirement.operation != operation:
            continue
        observation = observations.get(requirement.entity_id)
        state = RequirementState.READY
        reason = "requirement_satisfied"
        if observation is None or observation.value in {None, "unknown", "unavailable"}:
            state, reason = RequirementState.UNKNOWN, "requirement_unknown"
        elif requirement.max_age_seconds is not None and (
            observation.reported_at is None
            or observation.reported_at > now
            or (now - observation.reported_at).total_seconds()
            > requirement.max_age_seconds
        ):
            state, reason = RequirementState.STALE, "requirement_stale"
        elif observation.value not in requirement.accepted_states:
            state, reason = RequirementState.BLOCKED, "requirement_not_satisfied"
        results.append(RequirementResult(requirement.entity_id, state, reason))
    return tuple(results)
