"""Observations behind a recovery, a failure or a noted completion.

See dev doc "Vorfälle".
"""

from dataclasses import dataclass
from datetime import datetime

from .completion import NOTES
from .execution import ExecutionAttempt, RunCorrelation
from .types import AttemptState
from .validation import identifier, instant

INCIDENT_LIMIT = 20


@dataclass(frozen=True, slots=True)
class ObservationTrace:
    """Monitor inputs and decision of one observation.

    `faults` lists `code:scope:source` entries separated by commas. See dev doc
    "Strukturierte HA-Protokollierung".
    """

    observed_at: str | None = None
    phase: str | None = None
    cleaning_active: bool | None = None
    normal_end: bool | None = None
    at_dock: bool | None = None
    completion_confirmed: bool | None = None
    observed_operation: str | None = None
    completed_operation: str | None = None
    faults: str | None = None
    targets: str | None = None
    previous_state: str | None = None
    monitor_action: str | None = None
    monitor_reason: str | None = None
    deadline_at: str | None = None
    terminal_observed_at: str | None = None


@dataclass(frozen=True, slots=True)
class Incident:
    """One observation that ended or marked an attempt."""

    attempt_id: str
    job_id: str
    robot_id: str
    recorded_at: datetime
    state: str
    reason: str | None
    operation: str | None
    observation: ObservationTrace

    def __post_init__(self) -> None:
        for value in (self.attempt_id, self.job_id, self.robot_id):
            identifier(value)
        instant(self.recorded_at)


def marks_incident(
    previous: ExecutionAttempt,
    current: ExecutionAttempt,
    correlation: RunCorrelation | None,
) -> bool:
    """Return whether a transition needs its observation kept."""
    if current.state is previous.state:
        return False
    if current.state in {AttemptState.RECOVERY_REQUIRED, AttemptState.FAILED}:
        return True
    return (
        current.state is AttemptState.SUCCEEDED
        and correlation is not None
        and any(code in NOTES for code in correlation.reason_codes)
    )
