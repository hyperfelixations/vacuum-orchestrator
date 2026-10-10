"""Incidents keep the observation behind an ending; see dev doc "Vorfälle"."""

from dataclasses import replace
from datetime import UTC, datetime

from custom_components.vacuum_orchestrator.domain.execution import (
    ExecutionAttempt,
    RunCorrelation,
)
from custom_components.vacuum_orchestrator.domain.incidents import (
    INCIDENT_LIMIT,
    Incident,
    ObservationTrace,
    marks_incident,
)
from custom_components.vacuum_orchestrator.domain.queue import OrchestratorState
from custom_components.vacuum_orchestrator.domain.types import (
    AttemptState,
    CorrelationConfidence,
    CorrelationState,
)

NOW = datetime(2026, 9, 1, tzinfo=UTC)
RUNNING = ExecutionAttempt(
    "attempt", "job", "unit", "robot", "source", 1, AttemptState.START_CONFIRMED, NOW
)


def _correlation(*codes: str) -> RunCorrelation:
    return RunCorrelation(
        "attempt",
        "run",
        CorrelationState.MATCHED,
        CorrelationConfidence.WEAK,
        ("derived_completion", *codes),
        False,
    )


def _incident(attempt_id: str, job_id: str = "job") -> Incident:
    return Incident(
        attempt_id,
        job_id,
        "robot",
        NOW,
        "busy",
        None,
        "vacuum",
        ObservationTrace(phase="cleaning", monitor_action="attention"),
    )


def test_recovery_failure_and_noted_completion_mark_an_incident() -> None:
    def ended(state: AttemptState) -> ExecutionAttempt:
        return replace(RUNNING, state=state)

    assert marks_incident(RUNNING, ended(AttemptState.RECOVERY_REQUIRED), None)
    assert marks_incident(RUNNING, ended(AttemptState.FAILED), None)
    succeeded = ended(AttemptState.SUCCEEDED)
    assert marks_incident(RUNNING, succeeded, _correlation("mode_changed"))
    assert not marks_incident(RUNNING, succeeded, _correlation())
    assert not marks_incident(RUNNING, succeeded, None)
    assert not marks_incident(RUNNING, RUNNING, None)
    settling = ended(AttemptState.COMPLETION_PENDING)
    assert not marks_incident(RUNNING, settling, None)


def test_one_incident_per_attempt_and_only_the_latest_are_kept() -> None:
    state = OrchestratorState.empty("installation")
    for index in range(INCIDENT_LIMIT + 2):
        state = state.record_incident(_incident(f"attempt-{index}"))
    assert len(state.incidents) == INCIDENT_LIMIT
    assert state.incidents[0].attempt_id == "attempt-2"

    again = state.record_incident(replace(_incident("attempt-5"), state="unknown"))
    assert len(again.incidents) == INCIDENT_LIMIT
    assert again.incidents[-1] == replace(_incident("attempt-5"), state="unknown")
    assert [item.attempt_id for item in again.incidents].count("attempt-5") == 1
