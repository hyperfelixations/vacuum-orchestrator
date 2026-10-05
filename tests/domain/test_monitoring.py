"""Execution monitoring separates factual completion from cautious inference."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.vacuum_orchestrator.domain.completion import CompletionQuality
from custom_components.vacuum_orchestrator.domain.dispatching import RobotObservation
from custom_components.vacuum_orchestrator.domain.execution import (
    ExecutionAttempt,
    ExecutionPolicy,
)
from custom_components.vacuum_orchestrator.domain.intents import CleaningPreferences
from custom_components.vacuum_orchestrator.domain.monitoring import (
    MonitorAction,
    evaluate_observation,
    next_deadline,
)
from custom_components.vacuum_orchestrator.domain.planning import (
    DispatchAssignment,
    PreferenceResolution,
    WorkUnit,
)
from custom_components.vacuum_orchestrator.domain.types import (
    AttemptState,
    OperationKind,
    PassScope,
    RobotAvailabilityState,
    SettingsPolicy,
)

NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)
UNIT = WorkUnit(
    "unit",
    OperationKind.VACUUM,
    ("room",),
    None,
    1,
    PassScope.TARGET_SET,
    CleaningPreferences(),
    SettingsPolicy.BEST_EFFORT,
    None,
    (),
)
ASSIGNMENT = DispatchAssignment(
    "unit", "robot", "source", "fake", ("16",), "caps", PreferenceResolution((), ())
)
ATTEMPT = ExecutionAttempt(
    "attempt",
    "job",
    "unit",
    "robot",
    "source",
    1,
    AttemptState.COMMAND_SENT,
    NOW,
    command_boundary_at=NOW,
)
IDLE = RobotObservation(
    "robot",
    "source",
    RobotAvailabilityState.AVAILABLE,
    observed_at=NOW,
    cleaning_active=False,
    normal_end=True,
)


def evaluate(attempt=ATTEMPT, observation=IDLE, now=NOW):
    return evaluate_observation(attempt, UNIT, ASSIGNMENT, observation, now)


@pytest.mark.parametrize(
    "state", [RobotAvailabilityState.BUSY, RobotAvailabilityState.AVAILABLE]
)
def test_availability_does_not_substitute_for_cleaning_start(state) -> None:
    assert (
        evaluate(observation=replace(IDLE, state=state, cleaning_active=False)).action
        is MonitorAction.WAIT
    )
    assert (
        evaluate(observation=replace(IDLE, state=state, cleaning_active=True)).action
        is MonitorAction.START
    )


def test_confirmed_completion_requires_exact_mode_scope_and_start() -> None:
    complete = replace(
        IDLE,
        completion_confirmed=True,
        observed_operation=OperationKind.VACUUM,
        completed_targets=("16",),
    )
    assert evaluate(observation=complete).action is MonitorAction.WAIT
    started = replace(
        ATTEMPT, state=AttemptState.START_CONFIRMED, observed_start_at=NOW
    )
    result = evaluate(started, complete)
    assert result.action is MonitorAction.COMPLETE
    assert result.quality is CompletionQuality.CONFIRMED
    assert (
        evaluate(started, replace(complete, completed_targets=("other",))).reason
        == "completion_scope_mismatch"
    )
    assert (
        evaluate(
            started, replace(complete, observed_operation=OperationKind.MOP)
        ).reason
        == "observed_mode_mismatch"
    )


def test_derived_completion_waits_for_stability_and_resets_when_cleaning_resumes() -> (
    None
):
    started = replace(
        ATTEMPT, state=AttemptState.START_CONFIRMED, observed_start_at=NOW
    )
    assert evaluate(started).action is MonitorAction.SETTLE
    settling = replace(
        started, state=AttemptState.COMPLETION_PENDING, terminal_observed_at=NOW
    )
    assert evaluate(settling).action is MonitorAction.WAIT
    assert next_deadline(settling) == NOW + timedelta(seconds=30)
    decision = evaluate(
        settling,
        replace(IDLE, observed_at=NOW + timedelta(seconds=30)),
        NOW + timedelta(seconds=30),
    )
    assert decision.quality is CompletionQuality.DERIVED
    assert (
        evaluate(settling, replace(IDLE, cleaning_active=True)).action
        is MonitorAction.RESUME
    )


@pytest.mark.parametrize(
    "state",
    [
        AttemptState.COMMAND_SENT,
        AttemptState.START_CONFIRMED,
        AttemptState.COMPLETION_PENDING,
        AttemptState.CANCEL_PENDING,
    ],
)
def test_errors_and_connection_loss_never_become_success(state) -> None:
    attempt = replace(
        ATTEMPT, state=state, observed_start_at=NOW, terminal_observed_at=NOW
    )
    assert (
        evaluate(attempt, replace(IDLE, error_code="stuck")).reason
        == "robot_reported_error"
    )
    assert (
        evaluate(attempt, replace(IDLE, state=RobotAvailabilityState.UNKNOWN)).reason
        == "robot_connection_lost"
    )


def test_start_run_and_cancel_deadlines_are_persistent_and_distinct() -> None:
    assert next_deadline(ATTEMPT) == NOW + timedelta(seconds=180)
    assert evaluate(now=NOW + timedelta(seconds=180)).reason == "start_timeout"
    running = replace(
        ATTEMPT, state=AttemptState.START_CONFIRMED, observed_start_at=NOW
    )
    assert evaluate(running, now=NOW + timedelta(hours=4)).reason == "run_timeout"
    cancel = replace(
        running,
        state=AttemptState.CANCEL_PENDING,
        cancel_requested_at=NOW + timedelta(hours=1),
    )
    assert next_deadline(cancel) == NOW + timedelta(hours=1, seconds=120)
    assert (
        evaluate(
            cancel, replace(IDLE, normal_end=False), NOW + timedelta(hours=1)
        ).reason
        == "awaiting_stop"
    )
    assert (
        evaluate(
            cancel, replace(IDLE, normal_end=False), NOW + timedelta(hours=2)
        ).reason
        == "cancel_timeout"
    )
    assert (
        evaluate(cancel, IDLE, NOW + timedelta(hours=1)).action is MonitorAction.CANCEL
    )
    assert next_deadline(replace(ATTEMPT, state=AttemptState.SUCCEEDED)) is None


def test_preparation_has_no_observation_deadline() -> None:
    prepared = replace(ATTEMPT, state=AttemptState.PREPARED, command_boundary_at=None)
    assert next_deadline(prepared) is None
    assert evaluate(prepared, now=NOW + timedelta(hours=1)).action is (
        MonitorAction.WAIT
    )


def test_out_of_order_observations_do_not_advance_and_cannot_evade_timeouts() -> None:
    old = replace(IDLE, observed_at=NOW - timedelta(seconds=1))
    assert evaluate(observation=old).reason == "observation_out_of_order"
    assert (
        evaluate(observation=old, now=NOW + timedelta(minutes=5)).reason
        == "observation_timeout"
    )
    assert (
        evaluate(observation=replace(IDLE, source_robot_id="other")).reason
        == "observation_source_mismatch"
    )
    assert (
        evaluate(replace(ATTEMPT, state=AttemptState.RECOVERY_REQUIRED)).reason
        == "attempt_not_observing"
    )
    assert (
        evaluate(
            observation=replace(
                IDLE, cleaning_active=True, observed_operation=OperationKind.MOP
            )
        ).reason
        == "observed_mode_mismatch"
    )


def test_invalid_execution_policy_rejects_nonfinite_timeouts() -> None:
    with pytest.raises(Exception, match="invalid_duration"):
        ExecutionPolicy(run_seconds=float("inf"))
