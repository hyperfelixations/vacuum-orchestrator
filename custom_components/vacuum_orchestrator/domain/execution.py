"""Execution intent and observed robot-run models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .completion import CleaningSource, CompletionQuality
from .types import (
    AttemptState,
    CorrelationConfidence,
    CorrelationState,
    OperationKind,
)
from .validation import seconds


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    """Timeouts captured with an attempt rather than read from changing config."""

    start_seconds: float = 180
    run_seconds: float = 14400
    cancel_seconds: float = 120
    settle_seconds: float = 30

    def __post_init__(self) -> None:
        for value in (
            self.start_seconds,
            self.run_seconds,
            self.cancel_seconds,
            self.settle_seconds,
        ):
            seconds(value, positive=True)


@dataclass(frozen=True, slots=True)
class ExecutionAttempt:
    """VOI-owned command attempt; never treated as a physical run token."""

    attempt_id: str
    job_id: str
    work_unit_id: str
    robot_id: str
    source_robot_id: str
    robot_generation: int
    state: AttemptState
    prepared_at: datetime
    command_boundary_at: datetime | None = None
    observed_start_at: datetime | None = None
    prior_history_start: datetime | None = None
    prior_history_end: datetime | None = None
    failure_code: str | None = None
    policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    terminal_observed_at: datetime | None = None
    last_observation_at: datetime | None = None
    completion_quality: CompletionQuality | None = None
    cancel_requested_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RobotRun:
    """Observed physical activity reconstructed without assumed ownership."""

    robot_run_id: str
    source_robot_id: str
    observed_start: datetime | None = None
    observed_end: datetime | None = None
    history_start: datetime | None = None
    history_end: datetime | None = None
    cleaning_activity_seen: bool = False
    causal_token: str | None = None
    completion_quality: CompletionQuality | None = None
    operation: OperationKind | None = None
    canonical_targets: tuple[str, ...] = ()
    source: CleaningSource = CleaningSource.VOI
    failure_code: str | None = None
    capability_revision: str | None = None


@dataclass(frozen=True, slots=True)
class RunCorrelation:
    """Persistable evidence assessment between an attempt and a robot run."""

    attempt_id: str
    robot_run_id: str
    state: CorrelationState
    confidence: CorrelationConfidence
    reason_codes: tuple[str, ...]
    causal_token_available: bool

    @property
    def requires_attention(self) -> bool:
        """Return whether further dispatches must be blocked."""
        return self.state is not CorrelationState.MATCHED


@dataclass(frozen=True, slots=True)
class RobotLease:
    """Persisted exclusive ownership of a source robot by one attempt."""

    source_robot_id: str
    robot_id: str
    attempt_id: str
    work_unit_id: str
    generation: int
