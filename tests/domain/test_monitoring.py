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
from custom_components.vacuum_orchestrator.domain.faults import (
    Fault,
    FaultScope,
    FaultSource,
)
from custom_components.vacuum_orchestrator.domain.intents import CleaningPreferences
from custom_components.vacuum_orchestrator.domain.monitoring import (
    MonitorAction,
    evaluate_observation,
    next_deadline,
)
from custom_components.vacuum_orchestrator.domain.planning import (
    DispatchAssignment,
    SettingsResolution,
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
    "unit", "robot", "source", "fake", ("16",), "caps", SettingsResolution()
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


def test_a_confirmed_completion_needs_a_start_and_may_deviate() -> None:
    complete = replace(
        IDLE,
        completion_confirmed=True,
        observed_operation=OperationKind.MOP,
        completed_operation=OperationKind.VACUUM,
        completed_targets=("16",),
    )
    assert evaluate(observation=complete).action is MonitorAction.WAIT
    started = replace(
        ATTEMPT, state=AttemptState.START_CONFIRMED, observed_start_at=NOW
    )
    result = evaluate(started, complete)
    assert result.action is MonitorAction.COMPLETE
    assert result.quality is CompletionQuality.CONFIRMED
    assert result.reason == "scope_mode_and_success_confirmed"
    for changed in (
        replace(complete, completed_targets=("other",)),
        replace(complete, completed_operation=OperationKind.MOP),
    ):
        deviation = evaluate(started, changed)
        assert (deviation.action, deviation.reason, deviation.quality) == (
            MonitorAction.COMPLETE,
            "success_confirmed_with_changes",
            CompletionQuality.CONFIRMED,
        )
    unrecorded = evaluate(started, replace(complete, completed_operation=None))
    assert (unrecorded.action, unrecorded.quality) == (MonitorAction.SETTLE, None)


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
    ("state", "fault_decision"),
    [
        (AttemptState.COMMAND_SENT, (MonitorAction.WAIT, "robot_fault")),
        (AttemptState.START_CONFIRMED, (MonitorAction.FAULT_WAIT, "robot_fault")),
        (AttemptState.COMPLETION_PENDING, (MonitorAction.FAULT_WAIT, "robot_fault")),
        (
            AttemptState.CANCEL_PENDING,
            (MonitorAction.ATTENTION, "robot_reported_error"),
        ),
    ],
)
def test_errors_and_connection_loss_never_become_success(state, fault_decision) -> None:
    attempt = replace(
        ATTEMPT, state=state, observed_start_at=NOW, terminal_observed_at=NOW
    )
    decision = evaluate(
        attempt,
        replace(IDLE, faults=(Fault("stuck", FaultSource.ROBOT, FaultScope.GENERAL),)),
    )
    assert (decision.action, decision.reason) == fault_decision
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
    assert next_deadline(replace(ATTEMPT, state=AttemptState.SUCCEEDED)) is None


CANCELING = replace(
    ATTEMPT,
    state=AttemptState.CANCEL_PENDING,
    cancel_requested_at=NOW,
    last_observation_at=NOW,
)
STOPPED = replace(CANCELING, stop_sent_at=NOW + timedelta(seconds=5))


def _idle_at(seconds: float) -> RobotObservation:
    return replace(IDLE, observed_at=NOW + timedelta(seconds=seconds))


def test_cancel_never_confirms_without_a_sent_stop() -> None:
    at = NOW + timedelta(seconds=60)
    decision = evaluate(CANCELING, _idle_at(60), at)
    assert (decision.action, decision.reason) == (MonitorAction.WAIT, "awaiting_stop")
    timeout = evaluate(CANCELING, _idle_at(120), NOW + timedelta(seconds=120))
    assert (timeout.action, timeout.reason) == (
        MonitorAction.ATTENTION,
        "cancel_timeout",
    )


def test_cancel_ignores_idle_state_sampled_before_the_stop() -> None:
    late = replace(STOPPED, last_observation_at=NOW)
    decision = evaluate(late, _idle_at(4), NOW + timedelta(seconds=6))
    assert decision.action is MonitorAction.WAIT


def test_cancel_requires_stable_idle_after_the_stop() -> None:
    first = evaluate(STOPPED, _idle_at(6), NOW + timedelta(seconds=6))
    assert (first.action, first.reason) == (
        MonitorAction.SETTLE,
        "awaiting_stop_stability",
    )
    settling = replace(
        STOPPED,
        terminal_observed_at=NOW + timedelta(seconds=6),
        last_observation_at=NOW + timedelta(seconds=6),
    )
    assert next_deadline(settling) == NOW + timedelta(seconds=36)
    assert (
        evaluate(settling, _idle_at(20), NOW + timedelta(seconds=20)).action
        is MonitorAction.WAIT
    )
    resumed = evaluate(
        settling,
        replace(_idle_at(20), cleaning_active=True, normal_end=False),
        NOW + timedelta(seconds=20),
    )
    assert resumed.action is MonitorAction.RESUME
    done = evaluate(settling, _idle_at(36), NOW + timedelta(seconds=36))
    assert (done.action, done.reason) == (MonitorAction.CANCEL, "stop_observed")


def test_terminal_evidence_from_before_the_stop_does_not_count() -> None:
    earlier = replace(
        STOPPED,
        terminal_observed_at=NOW - timedelta(minutes=5),
        last_observation_at=NOW + timedelta(seconds=5),
    )
    assert next_deadline(earlier) == NOW + timedelta(seconds=120)
    decision = evaluate(earlier, _idle_at(40), NOW + timedelta(seconds=40))
    assert decision.action is MonitorAction.SETTLE


def test_cancel_timeout_wins_over_unfinished_stop_stability() -> None:
    settling = replace(
        STOPPED,
        terminal_observed_at=NOW + timedelta(seconds=110),
        last_observation_at=NOW + timedelta(seconds=110),
    )
    decision = evaluate(
        settling,
        replace(_idle_at(121), cleaning_active=True, normal_end=False),
        NOW + timedelta(seconds=121),
    )
    assert decision.reason == "cancel_timeout"


RETURNING = replace(STOPPED, return_to_dock=True)


def test_return_to_dock_confirms_only_at_rest_in_the_dock() -> None:
    off_dock = replace(_idle_at(6), at_dock=False)
    waiting = evaluate(RETURNING, off_dock, NOW + timedelta(seconds=6))
    assert (waiting.action, waiting.reason) == (MonitorAction.WAIT, "awaiting_stop")
    driving = replace(
        _idle_at(60),
        state=RobotAvailabilityState.BUSY,
        normal_end=False,
        at_dock=False,
    )
    assert evaluate(RETURNING, driving, NOW + timedelta(seconds=60)).action is (
        MonitorAction.WAIT
    )
    washing = replace(driving, at_dock=True)
    assert evaluate(RETURNING, washing, NOW + timedelta(seconds=300)).action is (
        MonitorAction.WAIT
    )
    docked = replace(_idle_at(400), at_dock=True)
    first = evaluate(RETURNING, docked, NOW + timedelta(seconds=400))
    assert first.action is MonitorAction.SETTLE
    settling = replace(
        RETURNING,
        terminal_observed_at=NOW + timedelta(seconds=400),
        last_observation_at=NOW + timedelta(seconds=400),
    )
    rewashing = evaluate(
        settling, replace(washing, observed_at=docked.observed_at), docked.observed_at
    )
    assert rewashing.action is MonitorAction.RESUME
    lost = evaluate(settling, _idle_at(430), NOW + timedelta(seconds=430))
    assert (lost.action, lost.reason) == (MonitorAction.RESUME, "stop_not_observed")
    done = evaluate(
        settling, replace(_idle_at(430), at_dock=True), NOW + timedelta(seconds=430)
    )
    assert (done.action, done.reason) == (MonitorAction.CANCEL, "stop_observed")


def test_return_to_dock_extends_the_cancel_window() -> None:
    policy = ExecutionPolicy(return_seconds=600)
    returning = replace(RETURNING, policy=policy)
    assert next_deadline(returning) == NOW + timedelta(seconds=720)
    driving = replace(_idle_at(500), normal_end=False, at_dock=False)
    assert evaluate(returning, driving, NOW + timedelta(seconds=500)).action is (
        MonitorAction.WAIT
    )
    late = replace(driving, observed_at=NOW + timedelta(seconds=720))
    timeout = evaluate(returning, late, NOW + timedelta(seconds=720))
    assert (timeout.action, timeout.reason) == (
        MonitorAction.ATTENTION,
        "cancel_timeout",
    )
    assert next_deadline(STOPPED) == NOW + timedelta(seconds=120)


def test_activity_after_the_settle_point_restarts_settling_before_the_limit() -> None:
    settling = replace(
        STOPPED,
        terminal_observed_at=NOW + timedelta(seconds=6),
        last_observation_at=NOW + timedelta(seconds=6),
    )
    busy = replace(_idle_at(40), cleaning_active=False, normal_end=False)
    decision = evaluate(settling, busy, NOW + timedelta(seconds=40))
    assert (decision.action, decision.reason) == (
        MonitorAction.RESUME,
        "stop_not_observed",
    )


def test_staying_confirms_at_rest_anywhere() -> None:
    off_dock = replace(_idle_at(6), at_dock=False)
    assert evaluate(STOPPED, off_dock, NOW + timedelta(seconds=6)).action is (
        MonitorAction.SETTLE
    )


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


def test_invalid_execution_policy_rejects_nonfinite_timeouts() -> None:
    with pytest.raises(Exception, match="invalid_duration"):
        ExecutionPolicy(run_seconds=float("inf"))


def test_only_faults_of_the_running_operation_stop_it() -> None:
    started = replace(
        ATTEMPT, state=AttemptState.START_CONFIRMED, observed_start_at=NOW
    )
    cleaning = replace(IDLE, cleaning_active=True, normal_end=False)
    mop = Fault("vibrarise_jammed", FaultSource.ROBOT, FaultScope.MOP)
    tank = Fault("water_empty", FaultSource.DOCK, FaultScope.STATION_VACUUM)

    assert evaluate(started, replace(cleaning, faults=(mop,))).action is (
        MonitorAction.WAIT
    )
    assert evaluate(started, replace(cleaning, faults=(tank,))).action is (
        MonitorAction.FAULT_WAIT
    )
    # A fault first reported after a normal end did not stop the floor run.
    assert evaluate(started, replace(IDLE, faults=(tank,))).action is (
        MonitorAction.SETTLE
    )
    assert evaluate(
        started, replace(IDLE, faults=(replace(mop, scope=FaultScope.STATION_MOP),))
    ).action is (MonitorAction.SETTLE)


def test_a_changed_mode_never_stops_the_run() -> None:
    started = replace(
        ATTEMPT, state=AttemptState.START_CONFIRMED, observed_start_at=NOW
    )
    other = OperationKind.MOP
    cleaning = replace(
        IDLE,
        state=RobotAvailabilityState.BUSY,
        cleaning_active=True,
        normal_end=False,
    )
    returning = replace(cleaning, cleaning_active=False, observed_operation=other)

    assert evaluate(ATTEMPT, replace(cleaning, observed_operation=other)).action is (
        MonitorAction.START
    )
    assert evaluate(started, replace(cleaning, observed_operation=other)).reason == (
        "cleaning_not_finished"
    )
    decision = evaluate(started, returning)
    assert (decision.action, decision.reason) == (
        MonitorAction.WAIT,
        "cleaning_not_finished",
    )
    assert evaluate(started, replace(IDLE, observed_operation=other)).action is (
        MonitorAction.SETTLE
    )
    settling = replace(
        started, state=AttemptState.COMPLETION_PENDING, terminal_observed_at=NOW
    )
    later = NOW + timedelta(seconds=30)
    derived = evaluate(
        settling, replace(IDLE, observed_at=later, observed_operation=other), later
    )
    assert derived.quality is CompletionQuality.DERIVED


def test_station_activity_after_the_return_restarts_settling() -> None:
    started = replace(
        ATTEMPT, state=AttemptState.START_CONFIRMED, observed_start_at=NOW
    )
    docked = replace(IDLE, at_dock=True, observed_operation=OperationKind.MOP)
    emptying = replace(docked, state=RobotAvailabilityState.BUSY, normal_end=False)

    assert evaluate(started, docked).action is MonitorAction.SETTLE
    settling = replace(
        started, state=AttemptState.COMPLETION_PENDING, terminal_observed_at=NOW
    )
    assert evaluate(settling, emptying).action is MonitorAction.RESUME
    assert evaluate(started, docked).action is MonitorAction.SETTLE


MOP_UNIT = replace(UNIT, operation=OperationKind.MOP)
TANK = Fault("water_shortage", FaultSource.ROBOT, FaultScope.MOP, "binary_sensor.tank")
RUNNING = replace(ATTEMPT, state=AttemptState.START_CONFIRMED, observed_start_at=NOW)
WAITING = replace(RUNNING, fault_since=NOW)
MOPPING = replace(
    IDLE, state=RobotAvailabilityState.BUSY, cleaning_active=True, normal_end=False
)
FAILS_AT = NOW + timedelta(seconds=900)


def mopping(attempt, observation, now=NOW):
    return evaluate_observation(attempt, MOP_UNIT, ASSIGNMENT, observation, now)


def test_a_fault_during_a_run_opens_a_window_that_waits_for_renewed_cleaning() -> None:
    first = mopping(RUNNING, replace(MOPPING, faults=(TANK,)))
    assert (first.action, first.reason) == (MonitorAction.FAULT_WAIT, "robot_fault")
    later = NOW + timedelta(seconds=60)
    still = mopping(WAITING, replace(MOPPING, observed_at=later, faults=(TANK,)), later)
    assert (still.action, still.reason) == (MonitorAction.WAIT, "robot_fault")
    # Clearing, pausing or docking is no evidence that the run went on.
    for observation in (
        replace(MOPPING, cleaning_active=False),
        replace(IDLE, at_dock=True),
    ):
        decision = mopping(WAITING, replace(observation, observed_at=later), later)
        assert (decision.action, decision.reason) == (
            MonitorAction.WAIT,
            "awaiting_cleaning_after_fault",
        )
    resumed = mopping(WAITING, replace(MOPPING, observed_at=later), later)
    assert (resumed.action, resumed.reason) == (MonitorAction.RESUME, "fault_cleared")


def test_a_fault_that_persists_after_a_normal_end_keeps_the_window_open() -> None:
    decision = mopping(WAITING, replace(IDLE, at_dock=True, faults=(TANK,)))
    assert (decision.action, decision.reason) == (MonitorAction.WAIT, "robot_fault")


def test_a_fault_window_fails_a_resting_robot_and_recovers_an_active_one() -> None:
    resting = replace(IDLE, observed_at=FAILS_AT, at_dock=True, faults=(TANK,))
    decision = mopping(WAITING, resting, FAILS_AT)
    assert (decision.action, decision.reason) == (
        MonitorAction.FAIL,
        "robot_fault_timeout",
    )
    cleared = mopping(WAITING, replace(resting, faults=()), FAILS_AT)
    assert cleared.action is MonitorAction.FAIL
    stuck = replace(
        MOPPING,
        state=RobotAvailabilityState.UNAVAILABLE,
        observed_at=FAILS_AT,
        faults=(replace(TANK, scope=FaultScope.GENERAL),),
    )
    decision = mopping(WAITING, stuck, FAILS_AT)
    assert (decision.action, decision.reason) == (
        MonitorAction.ATTENTION,
        "robot_fault_timeout",
    )
    before = FAILS_AT - timedelta(microseconds=1)
    assert (
        mopping(WAITING, replace(resting, observed_at=before), before).action
        is MonitorAction.WAIT
    )


def test_a_confirmed_completion_wins_over_the_fault_window() -> None:
    confirmed = replace(
        IDLE,
        observed_at=FAILS_AT,
        completion_confirmed=True,
        completed_operation=OperationKind.MOP,
        completed_targets=("16",),
    )
    decision = mopping(WAITING, confirmed, FAILS_AT)
    assert (decision.action, decision.quality) == (
        MonitorAction.COMPLETE,
        CompletionQuality.CONFIRMED,
    )


def test_a_fault_before_the_start_waits_for_the_start_deadline() -> None:
    decision = mopping(ATTEMPT, replace(MOPPING, faults=(TANK,)))
    assert (decision.action, decision.reason) == (MonitorAction.WAIT, "robot_fault")
    timeout = NOW + timedelta(seconds=180)
    decision = mopping(
        ATTEMPT, replace(MOPPING, observed_at=timeout, faults=(TANK,)), timeout
    )
    assert (decision.action, decision.reason) == (
        MonitorAction.ATTENTION,
        "start_timeout",
    )


def test_a_fault_window_is_a_persistent_deadline() -> None:
    assert next_deadline(WAITING) == FAILS_AT
    short = replace(WAITING, policy=ExecutionPolicy(run_seconds=600))
    assert next_deadline(short) == NOW + timedelta(seconds=600)
    assert WAITING.fault_deadline == FAILS_AT
    assert RUNNING.fault_deadline is None
    with pytest.raises(Exception, match="invalid_duration"):
        ExecutionPolicy(fault_seconds=0)


def test_a_resting_robot_cancels_inside_a_fault_window() -> None:
    canceling = replace(
        WAITING,
        state=AttemptState.CANCEL_PENDING,
        cancel_requested_at=NOW,
        stop_sent_at=NOW,
        terminal_observed_at=NOW,
    )
    later = NOW + timedelta(seconds=30)
    resting = replace(IDLE, observed_at=later, faults=(TANK,))
    assert mopping(canceling, resting, later).action is MonitorAction.CANCEL
    moving = replace(MOPPING, observed_at=later, faults=(TANK,))
    assert mopping(canceling, moving, later).reason == "robot_reported_error"
